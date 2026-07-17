"""NIH iCite 피인용수 조회. PMID 기반, 무키.

https://icite.od.nih.gov/api  — citation_count(총 피인용수)를 준다.
GET 방식이라 URL 길이 제한이 있어(PMID를 너무 많이 넣으면 413) 청크를 작게 잡고,
413/414가 나면 청크를 반씩 쪼개 자동 재시도한다.
"""
import logging
import time

import requests

ICITE = "https://icite.od.nih.gov/api/pubs"
log = logging.getLogger("icite")


def fetch_citations(pmids, chunk: int = 100, progress=None) -> dict:
    """PMID(str) → citation_count(int|None) 딕셔너리."""
    session = requests.Session()
    out = {}
    pmids = [str(p) for p in pmids]
    total = len(pmids)
    for i in range(0, total, chunk):
        _fetch_into(session, pmids[i:i + chunk], out)
        if progress:
            progress(min(i + chunk, total), total)
    return out


def _fetch_into(session, part, out) -> None:
    """한 청크를 조회해 out에 채운다. 413/414면 반으로 쪼개 재시도."""
    if not part:
        return
    params = {"pmids": ",".join(part), "format": "json"}
    for attempt in range(5):
        try:
            r = session.get(ICITE, params=params, timeout=60)
        except requests.RequestException as e:
            if attempt == 4:
                log.warning("iCite 청크 실패(건너뜀) %d건: %s", len(part), e)
                return
            time.sleep(min(2 ** attempt, 30))
            continue
        if r.status_code in (413, 414):  # URL/payload 너무 큼 → 분할
            if len(part) == 1:
                log.warning("iCite 단일 PMID도 413 — 건너뜀: %s", part[0])
                return
            mid = len(part) // 2
            _fetch_into(session, part[:mid], out)
            _fetch_into(session, part[mid:], out)
            return
        if r.status_code == 429 or r.status_code >= 500:
            time.sleep(min(2 ** attempt, 30))
            continue
        if not r.ok:
            log.warning("iCite 청크 실패(건너뜀) HTTP %d", r.status_code)
            return
        for rec in r.json().get("data", []) or []:
            out[str(rec.get("pmid"))] = rec.get("citation_count")
        time.sleep(0.2)
        return
