"""파이프라인 진입점.

  python -m src.pipeline backfill         # 2010년 이후 연 단위로 전체 최초 수집
  python -m src.pipeline daily            # 마지막 실행 이후 신규만
  python -m src.pipeline export           # DB -> 프론트용 JSON
  python -m src.pipeline verify-journals  # 저널 약어별 PubMed 건수 확인(수집 안 함)

수집은 categories.yaml의 저널 목록 기반이다. 각 하위 분류의 저널들을 OR로 묶고,
topic_filter:true 인 분류에는 공통 주제 필터를 AND로 걸어 재활 관련 논문만 추린다.
품질 필터(IF)는 저널 큐레이션으로 대체되어 사용하지 않는다.
"""
import argparse
import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
import yaml

from . import db
from .pubmed import PubMed

ROOT = Path(__file__).resolve().parent.parent
log = logging.getLogger("pipeline")


def load_config():
    with open(ROOT / "config/settings.yaml", encoding="utf-8") as f:
        settings = yaml.safe_load(f)
    with open(ROOT / "config/categories.yaml", encoding="utf-8") as f:
        cats = yaml.safe_load(f)
    return (settings, cats["groups"], cats.get("topic_filter", ""),
            cats.get("keyword_filter", []))


def _norm(s: str) -> str:
    """YAML 폴딩으로 남은 개행/중복 공백을 한 칸으로."""
    return " ".join((s or "").split())


def subcats_of(groups):
    """(group, subcategory) 쌍을 순서대로."""
    return [(g, sc) for g in groups for sc in g["subcategories"]]


def _journals_clause(sc: dict) -> str:
    return "(" + " OR ".join(f'"{j["ta"]}"[ta]' for j in sc["journals"]) + ")"


def _type_clause(settings: dict):
    pts = settings["pubmed"].get("publication_types") or []
    return "(" + " OR ".join(f'"{p}"[pt]' for p in pts) + ")" if pts else None


def build_query(sc: dict, topic_filter: str, settings: dict, year: int = None) -> str:
    """하위 분류 하나의 PubMed 검색식.
    저널(OR) [AND 주제필터] AND 언어·초록 [AND 연도] [AND 논문타입]."""
    parts = [_journals_clause(sc)]
    if sc.get("topic_filter") and topic_filter:
        parts.append("(" + _norm(topic_filter) + ")")
    parts.append("English[Language]")
    parts.append("hasabstract")
    if year is not None:
        parts.append(f'("{year}/01/01"[pdat] : "{year}/12/31"[pdat])')
    tc = _type_clause(settings)
    if tc:
        parts.append(tc)
    return " AND ".join(parts)


def run(mode: str):
    settings, groups, topic_filter, _ = load_config()
    conn = db.connect(str(ROOT / settings["storage"]["db_path"]))
    pm = PubMed(settings["pubmed"]["tool"], settings["pubmed"]["email"],
                settings["pubmed"]["api_key_env"])
    retmax = settings["pubmed"]["retmax"]
    min_year = settings["pubmed"]["min_year"]
    this_year = datetime.now(timezone.utc).year
    today = datetime.now(timezone.utc).strftime("%Y/%m/%d")
    totals = {"found": 0, "new": 0}

    for _g, sc in subcats_of(groups):
        if mode == "daily":
            # 마지막 실행 이후(edat) 신규만. 첫 실행이면 7일 전부터, 하루 겹침.
            cursor = db.get_cursor(conn, sc["id"]) or \
                (datetime.now(timezone.utc) - timedelta(days=7)).strftime("%Y/%m/%d")
            query = build_query(sc, topic_filter, settings)
            try:
                pmids = pm.search(query, retmax, mindate=cursor, datetype="edat")
            except requests.RequestException as e:
                log.warning("[daily] %s 조회 실패(건너뜀): %s", sc["id"], e)
                continue
        else:
            # backfill: 연 단위로 나눠 esearch 1만 상한을 회피.
            # 한 연도가 실패해도 그 해만 건너뛰고 계속(전체 중단 방지).
            seen = set()
            for yr in range(min_year, this_year + 1):
                try:
                    ids = pm.search(build_query(sc, topic_filter, settings, year=yr), retmax)
                except requests.RequestException as e:
                    log.warning("[backfill] %s %d년 조회 실패(건너뜀): %s", sc["id"], yr, e)
                    continue
                seen.update(ids)
                if len(ids) >= retmax:
                    log.warning("[backfill] %s %d년이 상한(%d) 도달 — 월 단위 분할 필요",
                                sc["id"], yr, retmax)
            pmids = list(seen)

        log.info("[%s] %s → %d PMIDs", mode, sc["id"], len(pmids))
        totals["found"] += len(pmids)

        known = {r[0] for r in conn.execute("SELECT pmid FROM papers")}
        for rec in pm.fetch(pmids):
            if rec["pmid"] not in known:
                totals["new"] += 1
            db.upsert_paper(conn, {
                **rec,
                "authors": json.dumps(rec["authors"], ensure_ascii=False),
                "pub_types": json.dumps(rec["pub_types"], ensure_ascii=False),
                "mesh_terms": json.dumps(rec["mesh_terms"], ensure_ascii=False),
            })
            db.link_category(conn, rec["pmid"], sc["id"])
        conn.commit()
        db.set_cursor(conn, sc["id"], today)
        conn.commit()

    if settings["fable"]["enabled"]:
        from .fable import FableAnalyzer, apply
        analyzer = FableAnalyzer(settings["fable"]["model"], settings["fable"]["api_key_env"])
        done = apply(conn, analyzer, settings["fable"]["max_per_run"])
        log.info("Fable 분석 %d건", done)

    log.info("완료: %s", totals)
    conn.close()


def verify_journals():
    """저널 약어([ta])별 PubMed 건수를 출력. 약어 오타(건수 0)를 잡기 위함."""
    settings, groups, _, _ = load_config()
    pm = PubMed(settings["pubmed"]["tool"], settings["pubmed"]["email"],
                settings["pubmed"]["api_key_env"])
    min_year = settings["pubmed"]["min_year"]
    print(f"[verify] {min_year}년 이후 · 영어 · 초록 있음 기준\n")
    for _g, sc in subcats_of(groups):
        print(f"[{sc['id']}]")
        for j in sc["journals"]:
            q = (f'"{j["ta"]}"[ta] AND English[Language] AND hasabstract '
                 f'AND ("{min_year}/01/01"[pdat] : "3000"[pdat])')
            n = pm.count(q)
            flag = "  ⚠ 0건 — 약어 확인" if n == 0 else ""
            print(f"  {n:7d}  {j['ta']:34s} {j['name']}{flag}")


def _keyword_match(title: str, mesh_json: str, keywords: list) -> bool:
    """제목 또는 MeSH 용어에 키워드가 하나라도 포함되면 True(대소문자 무시, 부분일치)."""
    if not keywords:
        return True
    hay = (title or "").lower() + " " + " ".join(json.loads(mesh_json or "[]")).lower()
    return any(k in hay for k in keywords)


def export():
    settings, groups, _, keyword_filter = load_config()
    conn = db.connect(str(ROOT / settings["storage"]["db_path"]))
    out_dir = ROOT / settings["storage"]["export_dir"]
    out_dir.mkdir(parents=True, exist_ok=True)
    keywords = [k.lower() for k in (keyword_filter or [])]

    index_groups = []
    total = 0
    dropped = 0
    for g in groups:
        subs = []
        for sc in g["subcategories"]:
            rows = conn.execute(
                "SELECT p.* FROM papers p JOIN paper_categories c ON p.pmid=c.pmid "
                "WHERE c.category_id=? ORDER BY p.pub_year DESC, p.pmid DESC",
                (sc["id"],),
            ).fetchall()
            kept = [r for r in rows if _keyword_match(r["title"], r["mesh_terms"], keywords)]
            dropped += len(rows) - len(kept)
            rows = kept
            papers = [{
                "pmid": r["pmid"], "doi": r["doi"], "title": r["title"],
                "abstract": r["abstract"], "journal": r["journal"], "year": r["pub_year"],
                "authors": json.loads(r["authors"] or "[]"),
                "pubTypes": json.loads(r["pub_types"] or "[]"),
                "summary": json.loads(r["summary"]) if r["summary"] else None,
            } for r in rows]
            (out_dir / f"{sc['id']}.json").write_text(
                json.dumps(papers, ensure_ascii=False), encoding="utf-8")
            subs.append({"id": sc["id"], "nameKo": sc["name_ko"],
                         "nameEn": sc.get("name_en", ""), "count": len(papers)})
            total += len(papers)
        index_groups.append({"id": g["id"], "nameKo": g["name_ko"], "subcategories": subs})

    (out_dir / "index.json").write_text(json.dumps({
        "groups": index_groups,
        "total": total,
        "updatedAt": datetime.now(timezone.utc).isoformat(),
    }, ensure_ascii=False), encoding="utf-8")
    if keywords:
        log.info("키워드 필터로 %d편 제외(제목·MeSH 미포함)", dropped)
    log.info("export 완료 → %s (총 %d편)", out_dir, total)
    conn.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["backfill", "daily", "export", "verify-journals"])
    args = ap.parse_args()
    if args.mode == "export":
        export()
    elif args.mode == "verify-journals":
        verify_journals()
    else:
        run(args.mode)
        export()
