"""파이프라인 진입점.

  python -m src.pipeline backfill   # 2010년 이후 전체 최초 수집
  python -m src.pipeline daily      # 마지막 실행 이후 신규만
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


def export():
    settings, cats = load_config()
    conn = db.connect(str(ROOT / settings["storage"]["db_path"]))
    out_dir = ROOT / settings["storage"]["export_dir"]
    out_dir.mkdir(parents=True, exist_ok=True)
    keep = (1, 2) if settings["metrics"]["unknown_journal_policy"] == "flag" else (1,)

    index = []
    for cat in cats:
        rows = conn.execute(
            f"SELECT p.* FROM papers p JOIN paper_categories c ON p.pmid=c.pmid "
            f"WHERE c.category_id=? AND p.passed_filter IN ({','.join('?'*len(keep))}) "
            f"ORDER BY p.pub_year DESC, p.pmid DESC",
            (cat["id"], *keep),
        ).fetchall()
        papers = [{
            "pmid": r["pmid"], "doi": r["doi"], "title": r["title"],
            "abstract": r["abstract"], "journal": r["journal"], "year": r["pub_year"],
            "authors": json.loads(r["authors"] or "[]"),
            "pubTypes": json.loads(r["pub_types"] or "[]"),
            "metric": r["metric_value"], "flagged": r["passed_filter"] == 2,
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
    ap.add_argument("mode", choices=["backfill", "daily", "export"])
    args = ap.parse_args()
    if args.mode == "export":
        export()
    else:
        run(args.mode)
        export()
