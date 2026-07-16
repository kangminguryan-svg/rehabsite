"""파이프라인 진입점.

  python -m src.pipeline backfill   # 2010년 이후 전체 최초 수집
  python -m src.pipeline daily      # 마지막 실행 이후 신규만
  python -m src.pipeline relookup   # 지표가 비어있는(unknown) 논문만 재조회
  python -m src.pipeline export     # DB -> 프론트용 JSON
"""
import argparse
import json
import logging
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

from . import db, metrics as m
from .pubmed import PubMed

ROOT = Path(__file__).resolve().parent.parent
log = logging.getLogger("pipeline")


def load_config():
    with open(ROOT / "config/settings.yaml", encoding="utf-8") as f:
        settings = yaml.safe_load(f)
    with open(ROOT / "config/categories.yaml", encoding="utf-8") as f:
        cats = yaml.safe_load(f)["categories"]
    return settings, cats


def _norm(s: str) -> str:
    """YAML 폴딩으로 남은 개행/중복 공백을 한 칸으로."""
    return " ".join(s.split())


def build_query(cat: dict, settings: dict) -> str:
    parts = [f"({_norm(cat['query'])})", f"({_norm(settings['pubmed']['common_filter'])})"]
    pts = settings["pubmed"].get("publication_types") or []
    if pts:
        parts.append("(" + " OR ".join(f'"{p}"[pt]' for p in pts) + ")")
    return " AND ".join(parts)


def run(mode: str):
    settings, cats = load_config()
    conn = db.connect(str(ROOT / settings["storage"]["db_path"]))
    pm = PubMed(settings["pubmed"]["tool"], settings["pubmed"]["email"],
                settings["pubmed"]["api_key_env"])
    lookup = m.MetricLookup(conn, settings["metrics"]["cache_days"], settings["pubmed"]["email"])
    threshold = settings["metrics"]["threshold"]
    policy = settings["metrics"]["unknown_journal_policy"]

    today = datetime.now(timezone.utc).strftime("%Y/%m/%d")
    totals = {"found": 0, "new": 0, "passed": 0}

    for cat in cats:
        query = build_query(cat, settings)
        cursor = None
        if mode == "daily":
            # 하루 겹치게 잡아 경계 누락 방지. 첫 실행이면 7일 전부터.
            saved = db.get_cursor(conn, cat["id"])
            cursor = saved or (datetime.now(timezone.utc) - timedelta(days=7)).strftime("%Y/%m/%d")

        pmids = pm.search(query, settings["pubmed"]["retmax"], mindate=cursor)
        log.info("[%s] %s → %d PMIDs", mode, cat["id"], len(pmids))
        totals["found"] += len(pmids)

        known = {r[0] for r in conn.execute("SELECT pmid FROM papers")}
        for rec in pm.fetch(pmids):
            is_new = rec["pmid"] not in known
            db.upsert_paper(conn, {
                **rec,
                "authors": json.dumps(rec["authors"], ensure_ascii=False),
                "pub_types": json.dumps(rec["pub_types"], ensure_ascii=False),
                "mesh_terms": json.dumps(rec["mesh_terms"], ensure_ascii=False),
            })
            db.link_category(conn, rec["pmid"], cat["id"])
            if is_new:
                totals["new"] += 1
                value, source = lookup.lookup(rec["journal_issn"], rec["journal"])
                passed = m.decide(value, threshold, policy)
                db.set_filter_result(conn, rec["pmid"], value, source, passed)
                if passed == 1:
                    totals["passed"] += 1
        conn.commit()
        db.set_cursor(conn, cat["id"], today)
        conn.commit()

    if settings["fable"]["enabled"]:
        from .fable import FableAnalyzer, apply
        analyzer = FableAnalyzer(settings["fable"]["model"], settings["fable"]["api_key_env"])
        done = apply(conn, analyzer, settings["fable"]["max_per_run"])
        log.info("Fable 분석 %d건", done)

    log.info("완료: %s", totals)
    conn.close()


def relookup():
    """지표가 비어있는(metric_value IS NULL) 논문만 골라 지표를 다시 조회한다.
    전체 재수집(backfill) 없이 개선된 조회 로직을 기존 데이터에 적용하기 위함."""
    settings, cats = load_config()
    conn = db.connect(str(ROOT / settings["storage"]["db_path"]))
    lookup = m.MetricLookup(conn, settings["metrics"]["cache_days"], settings["pubmed"]["email"])

    rows = conn.execute(
        "SELECT pmid, journal, journal_issn FROM papers WHERE metric_value IS NULL"
    ).fetchall()
    log.info("[relookup] 지표 미확보 %d편 재조회 시작", len(rows))

    fixed = 0
    for i, r in enumerate(rows, 1):
        value, source = lookup.lookup(r["journal_issn"], r["journal"])
        conn.execute(
            "UPDATE papers SET metric_value=?, metric_source=? WHERE pmid=?",
            (value, source, r["pmid"]),
        )
        if value is not None:
            fixed += 1
        if i % 200 == 0:
            conn.commit()
            log.info("[relookup] %d/%d 진행 (지표 확보 %d)", i, len(rows), fixed)
    conn.commit()
    log.info("[relookup] 완료: %d편 중 %d편 지표 확보", len(rows), fixed)
    conn.close()


def export():
    settings, cats = load_config()
    conn = db.connect(str(ROOT / settings["storage"]["db_path"]))
    out_dir = ROOT / settings["storage"]["export_dir"]
    out_dir.mkdir(parents=True, exist_ok=True)
    # 임계값은 export 시점에 metric_value로 직접 판정한다.
    # (수집 시점의 passed_filter에 의존하면 임계값을 바꿀 때마다 재수집이 필요해짐)
    threshold = settings["metrics"]["threshold"]
    flag_unknown = settings["metrics"]["unknown_journal_policy"] == "flag"
    cond = "p.metric_value >= ?" + (" OR p.metric_value IS NULL" if flag_unknown else "")

    index = []
    for cat in cats:
        rows = conn.execute(
            f"SELECT p.* FROM papers p JOIN paper_categories c ON p.pmid=c.pmid "
            f"WHERE c.category_id=? AND ({cond}) "
            f"ORDER BY p.pub_year DESC, p.pmid DESC",
            (cat["id"], threshold),
        ).fetchall()
        papers = [{
            "pmid": r["pmid"], "doi": r["doi"], "title": r["title"],
            "abstract": r["abstract"], "journal": r["journal"], "year": r["pub_year"],
            "authors": json.loads(r["authors"] or "[]"),
            "pubTypes": json.loads(r["pub_types"] or "[]"),
            "metric": r["metric_value"], "flagged": r["metric_value"] is None,
            "summary": json.loads(r["summary"]) if r["summary"] else None,
        } for r in rows]
        (out_dir / f"{cat['id']}.json").write_text(
            json.dumps(papers, ensure_ascii=False), encoding="utf-8")
        index.append({"id": cat["id"], "nameKo": cat["name_ko"],
                      "nameEn": cat["name_en"], "count": len(papers)})

    (out_dir / "index.json").write_text(json.dumps({
        "categories": index,
        "updatedAt": datetime.now(timezone.utc).isoformat(),
        "metric": {"label": "OpenAlex 2yr mean citedness (IF 프록시)",
                   "threshold": settings["metrics"]["threshold"]},
    }, ensure_ascii=False), encoding="utf-8")
    log.info("export 완료 → %s", out_dir)
    conn.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["backfill", "daily", "relookup", "export"])
    args = ap.parse_args()
    if args.mode == "export":
        export()
    elif args.mode == "relookup":
        relookup()
        export()
    else:
        run(args.mode)
        export()
