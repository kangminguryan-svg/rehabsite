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
        # 지표값이 실제로 있는 캐시만 히트로 취급. None 캐시는 매번 재시도한다
        # (과거 조회 실패가 영구히 굳는 것을 막기 위함).
        row = self.conn.execute(
            f"SELECT metric_value, source FROM journal_metrics "
            f"WHERE issn=? AND metric_value IS NOT NULL AND NOT ({STALE_SQL})",
            (issn, self.cache_days),
        ).fetchone()
        return (row["metric_value"], row["source"]) if row else None

    def _query_openalex(self, params: dict):
        """OpenAlex sources 조회 → (metric_value, display_name, issn_l) 또는 None."""
        if self.mailto:
            params["mailto"] = self.mailto
        try:
            r = self.session.get(OPENALEX, params=params, timeout=30)
            time.sleep(0.1)
            r.raise_for_status()
            results = r.json().get("results", [])
        except requests.RequestException:
            return None
        if not results:
            return None
        src = results[0]
        value = (src.get("summary_stats") or {}).get("2yr_mean_citedness")
        return value, src.get("display_name"), src.get("issn_l")

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
        """(지표값, 출처). ISSN 조회 실패 시 저널명으로 폴백. 최종 실패는 (None, 'unknown')."""
        # 1) ISSN 캐시
        if issn:
            hit = self._cached(issn)
            if hit:
                return hit

        # 2) ISSN 직접 조회
        if issn:
            res = self._query_openalex({"filter": f"issn:{issn}", "per-page": 1})
            if res and res[0] is not None:
                value, name, _ = res
                self._store(issn, name or journal_name, value, "openalex")
                return value, "openalex"

        # 3) 저널명 폴백 (ISSN이 없거나 ISSN으로 못 찾은 경우).
        #    NEJM 처럼 레코드에 ISSN이 누락돼도 이름으로 지표를 확보한다.
        if journal_name:
            res = self._query_openalex({"search": journal_name, "per-page": 1})
            if res and res[0] is not None:
                value, name, issn_l = res
                key = issn or issn_l or f"name:{journal_name}"
                self._store(key, name or journal_name, value, "openalex:byname")
                return value, "openalex:byname"

        # 4) 전부 실패
        if issn:
            self._store(issn, journal_name, None, "openalex:notfound")
        return None, "unknown"


def decide(value: float | None, threshold: float, unknown_policy: str) -> int:
    """passed_filter 값 결정: 0=탈락, 1=통과, 2=지표불명(flag)."""
    if value is None:
        return {"keep": 1, "drop": 0, "flag": 2}[unknown_policy]
    return 1 if value >= threshold else 0
