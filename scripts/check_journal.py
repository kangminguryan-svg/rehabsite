"""저널 진단 도구. 특정 저널이 DB에 수집됐는지, 지표값이 얼마인지 확인한다.

사용법:
  python scripts/check_journal.py                 # 기본: "New England" 검색
  python scripts/check_journal.py "Lancet"        # 다른 저널 검색
  python scripts/check_journal.py --top           # 통과 논문이 많은 저널 순위
"""
import sqlite3
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "papers.db"


def threshold() -> float:
    with open(ROOT / "config" / "settings.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f)["metrics"]["threshold"]


def main() -> None:
    if not DB.exists():
        print(f"DB가 없습니다: {DB}\n먼저 `python -m src.pipeline backfill` 을 실행하세요.")
        return

    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    thr = threshold()
    args = sys.argv[1:]

    if args and args[0] == "--top":
        print(f"[지표 ≥ {thr} 통과 논문이 많은 저널 상위 25개]\n")
        rows = conn.execute(
            "SELECT journal, COUNT(*) n, ROUND(AVG(metric_value), 2) metric "
            "FROM papers WHERE metric_value >= ? "
            "GROUP BY journal ORDER BY n DESC LIMIT 25",
            (thr,),
        ).fetchall()
        for r in rows:
            print(f"  {r['n']:5d}편  지표 {r['metric']:>6}  {r['journal']}")
        return

    needle = args[0] if args else "New England"
    rows = conn.execute(
        "SELECT pmid, journal, journal_issn, pub_year, metric_value, "
        "metric_source, passed_filter FROM papers WHERE journal LIKE ? "
        "ORDER BY pub_year DESC",
        (f"%{needle}%",),
    ).fetchall()

    print(f"[\"{needle}\" 매칭: {len(rows)}편]  (임계값 ≥ {thr})\n")
    if not rows:
        print("  → DB에 없습니다. 검색식에 이 저널의 논문이 걸리지 않았다는 뜻입니다")
        print("    (= 해당 분야에서 재활 논문을 내지 않아서일 가능성이 큼).")
        return

    shown = rows[:15]
    for r in shown:
        mv = "None(지표없음)" if r["metric_value"] is None else f"{r['metric_value']:.2f}"
        verdict = "통과" if (r["metric_value"] is not None and r["metric_value"] >= thr) else "제외"
        print(f"  PMID {r['pmid']}  {r['pub_year']}  지표 {mv:>14}  [{verdict}]  ({r['metric_source']})")
    if len(rows) > len(shown):
        print(f"  … 외 {len(rows) - len(shown)}편")

    n_pass = sum(1 for r in rows if r["metric_value"] is not None and r["metric_value"] >= thr)
    n_none = sum(1 for r in rows if r["metric_value"] is None)
    print(f"\n요약: 총 {len(rows)}편 중 통과 {n_pass}편 · 지표없음 {n_none}편 · "
          f"지표미달 {len(rows) - n_pass - n_none}편")
    if n_none:
        print("  ※ 지표없음이 있으면 ISSN/OpenAlex 조회 실패로 빠진 것 — 고칠 수 있습니다.")


if __name__ == "__main__":
    main()
