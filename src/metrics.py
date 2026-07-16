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
        # OpenAlex 폴라이트 풀은 초당 10건까지지만 여유를 둔다(버스트 429 회피).
        self.delay = 0.15

    def _cached(self, issn: str):
        # 지표값이 실제로 있는 캐시만 히트로 취급. None 캐시는 매번 재시도한다
        # (과거 조회 실패가 영구히 굳는 것을 막기 위함).
        row = self.conn.execute(
            f"SELECT metric_value, source FROM journal_metrics "
            f"WHERE issn=? AND metric_value IS NOT NULL AND NOT ({STALE_SQL})",
            (issn, self.cache_days),
        ).fetchone()
        return (row["metric_value"], row["source"]) if row else None

    def _cached_by_name(self, name: str):
        # 이름 폴백으로 확보한 지표를 이름으로 재히트(같은 저널 반복 조회 방지).
        row = self.conn.execute(
            f"SELECT metric_value, source FROM journal_metrics "
            f"WHERE journal_name=? AND metric_value IS NOT NULL AND NOT ({STALE_SQL}) "
            f"LIMIT 1",
            (name, self.cache_days),
        ).fetchone()
        return (row["metric_value"], row["source"]) if row else None

    @staticmethod
    def _metric_of(src: dict):
        return (src.get("summary_stats") or {}).get("2yr_mean_citedness")

    def _fetch_sources(self, params: dict):
        """OpenAlex sources 조회 → results 리스트 또는 None(실패).
        429/5xx는 지수 백오프로 재시도하고 Retry-After 헤더를 존중한다."""
        if self.mailto:
            params["mailto"] = self.mailto
        for attempt in range(5):
            try:
                r = self.session.get(OPENALEX, params=params, timeout=30)
            except requests.RequestException:
                time.sleep(2 ** attempt)
                continue
            if r.status_code == 429 or r.status_code >= 500:
                ra = r.headers.get("Retry-After", "")
                time.sleep(float(ra) if ra.replace(".", "", 1).isdigit() else 2 ** attempt)
                continue
            if not r.ok:
                return None
            time.sleep(self.delay)
            return r.json().get("results", [])
        return None  # 재시도 소진

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
        # 1) 캐시 (ISSN → 이름 순)
        if issn:
            hit = self._cached(issn)
            if hit:
                return hit
        if journal_name:
            hit = self._cached_by_name(journal_name)
            if hit:
                return hit

        # 2) ISSN 직접 조회
        if issn:
            results = self._fetch_sources({"filter": f"issn:{issn}", "per-page": 1})
            if results and self._metric_of(results[0]) is not None:
                src = results[0]
                self._store(issn, src.get("display_name") or journal_name,
                            self._metric_of(src), "openalex")
                return self._metric_of(src), "openalex"

        # 3) 저널명 폴백 (ISSN이 없거나 ISSN으로 못 찾은 경우).
        #    NEJM 처럼 레코드에 ISSN이 누락돼도 이름으로 지표를 확보한다.
        #    여러 후보 중 지표가 있으면서 논문 수가 가장 많은(= 대표) 저널을 택한다.
        if journal_name:
            results = self._fetch_sources(
                {"filter": f"display_name.search:{journal_name}", "per-page": 5})
            if results:
                cand = [s for s in results if self._metric_of(s) is not None]
                if cand:
                    best = max(cand, key=lambda s: s.get("works_count") or 0)
                    value = self._metric_of(best)
                    key = issn or best.get("issn_l") or f"name:{journal_name}"
                    # journal_name(=PubMed 표기)으로 저장해야 다음번 이름 캐시가 히트함
                    self._store(key, journal_name, value, "openalex:byname")
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
