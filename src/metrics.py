"""저널 지표 조회. IF 대신 OpenAlex의 2yr_mean_citedness를 프록시로 사용.

주의: 이 값은 Clarivate Impact Factor와 '같지 않다'. 상관은 높지만 저널마다
0.3~0.8 정도 벌어질 수 있으므로 임계값 2.0은 근사치로 이해할 것.
정식 IF가 필요하면 JCR CSV를 journal_metrics 테이블에 직접 적재하면 된다.
"""
import time

import requests

OPENALEX = "https://api.openalex.org/sources"
STALE_SQL = "julianday('now') - julianday(updated_at) > ?"


class MetricLookup:
    def __init__(self, conn, cache_days: int = 90, mailto: str = None):
        self.conn = conn
        self.cache_days = cache_days
        self.session = requests.Session()
        self.mailto = mailto  # OpenAlex polite pool

    def _cached(self, issn: str):
        row = self.conn.execute(
            f"SELECT metric_value, source FROM journal_metrics "
            f"WHERE issn=? AND NOT ({STALE_SQL})",
            (issn, self.cache_days),
        ).fetchone()
        return (row["metric_value"], row["source"]) if row else None

    def _store(self, issn, name, value, source):
        self.conn.execute(
            "INSERT INTO journal_metrics (issn, journal_name, metric_value, source, updated_at) "
            "VALUES (?,?,?,?, datetime('now')) "
            "ON CONFLICT(issn) DO UPDATE SET journal_name=excluded.journal_name, "
            "metric_value=excluded.metric_value, source=excluded.source, updated_at=datetime('now')",
            (issn, name, value, source),
        )
        self.conn.commit()

    def lookup(self, issn: str, journal_name: str = None) -> tuple[float | None, str]:
        """(지표값, 출처). 못 찾으면 (None, 'unknown')."""
        if not issn:
            return None, "unknown"
        hit = self._cached(issn)
        if hit:
            return hit

        params = {"filter": f"issn:{issn}", "per-page": 1}
        if self.mailto:
            params["mailto"] = self.mailto
        try:
            r = self.session.get(OPENALEX, params=params, timeout=30)
            time.sleep(0.1)
            r.raise_for_status()
            results = r.json().get("results", [])
        except requests.RequestException:
            return None, "unknown"

        if not results:
            self._store(issn, journal_name, None, "openalex:notfound")
            return None, "openalex:notfound"

        src = results[0]
        value = (src.get("summary_stats") or {}).get("2yr_mean_citedness")
        name = src.get("display_name") or journal_name
        self._store(issn, name, value, "openalex")
        return value, "openalex"


def decide(value: float | None, threshold: float, unknown_policy: str) -> int:
    """passed_filter 값 결정: 0=탈락, 1=통과, 2=지표불명(flag)."""
    if value is None:
        return {"keep": 1, "drop": 0, "flag": 2}[unknown_policy]
    return 1 if value >= threshold else 0
