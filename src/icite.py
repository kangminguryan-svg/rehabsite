"""NIH iCite 피인용수 조회. PMID 기반, 무키. 배치로 한 번에 수백 개씩.

https://icite.od.nih.gov/api  — citation_count(총 피인용수)를 준다.
OpenAlex와 달리 PMID로 바로 조회되고 배치가 커서 요청 수가 적다.
"""
import logging
import time

import requests

ICITE = "https://icite.od.nih.gov/api/pubs"
log = logging.getLogger("icite")


def fetch_citations(pmids, chunk: int = 500, progress=None) -> dict:
    """PMID(str) → citation_count(int|None) 딕셔너리.
    429/5xx·네트워크 오류는 지수 백오프로 재시도, 실패한 청크는 건너뛴다."""
    session = requests.Session()
    out = {}
    pmids = [str(p) for p in pmids]
    for i in range(0, len(pmids), chunk):
        part = pmids[i:i + chunk]
        params = {"pmids": ",".join(part), "format": "json"}
        for attempt in range(5):
            try:
                r = session.get(ICITE, params=params, timeout=60)
            except requests.RequestException as e:
                if attempt == 4:
                    log.warning("iCite 청크 실패(건너뜀) %d건: %s", len(part), e)
                time.sleep(min(2 ** attempt, 30))
                continue
            if r.status_code == 429 or r.status_code >= 500:
                time.sleep(min(2 ** attempt, 30))
                continue
            if not r.ok:
                log.warning("iCite 청크 실패(건너뜀) HTTP %d", r.status_code)
                break
            for rec in r.json().get("data", []) or []:
                out[str(rec.get("pmid"))] = rec.get("citation_count")
            time.sleep(0.2)
            break
        if progress:
            progress(min(i + chunk, len(pmids)), len(pmids))
    return out
