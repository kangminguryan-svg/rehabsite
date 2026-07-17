"""PubMed E-utilities 클라이언트. esearch로 PMID를 모으고 efetch로 상세를 가져온다."""
import os
import time
import xml.etree.ElementTree as ET
from typing import Iterator

import requests

BASE = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"


class PubMed:
    def __init__(self, tool: str, email: str, api_key_env: str = "NCBI_API_KEY"):
        self.api_key = os.environ.get(api_key_env)
        self.common = {"tool": tool, "email": email, "db": "pubmed"}
        if self.api_key:
            self.common["api_key"] = self.api_key
        # API key 있으면 10 req/s, 없으면 3 req/s
        self.delay = 0.11 if self.api_key else 0.35
        self.session = requests.Session()

    def _get(self, endpoint: str, params: dict) -> requests.Response:
        for attempt in range(4):
            r = self.session.get(f"{BASE}/{endpoint}", params={**self.common, **params}, timeout=60)
            if r.status_code == 429 or r.status_code >= 500:
                time.sleep(2 ** attempt)
                continue
            r.raise_for_status()
            time.sleep(self.delay)
            return r
        r.raise_for_status()
        return r

    def search(self, query: str, retmax: int = 9999, mindate: str = None,
               maxdate: str = None, datetype: str = "edat") -> list[str]:
        """검색식에 맞는 PMID 목록.
        mindate/maxdate 지정 시 datetype(edat=Entrez date, pdat=발행일) 기준으로 제한.
        연 단위 backfill은 datetype='pdat' 로 연도 범위를 넘겨 1만 상한을 회피한다."""
        params = {"term": query, "retmax": retmax, "retmode": "xml", "sort": "date"}
        if mindate:
            params.update({"datetype": datetype, "mindate": mindate,
                           "maxdate": maxdate or "3000/12/31"})
        root = ET.fromstring(self._get("esearch.fcgi", params).content)
        return [e.text for e in root.findall(".//IdList/Id")]

    def count(self, query: str) -> int:
        """검색식의 결과 건수만 반환(retmax=0). 저널 약어 검증용."""
        root = ET.fromstring(self._get(
            "esearch.fcgi", {"term": query, "retmax": 0, "retmode": "xml"}).content)
        el = root.find(".//Count")
        return int(el.text) if el is not None and el.text else 0

    def fetch(self, pmids: list[str], batch: int = 200) -> Iterator[dict]:
        """PMID 목록의 상세 레코드를 배치로 yield."""
        for i in range(0, len(pmids), batch):
            chunk = pmids[i:i + batch]
            r = self._get("efetch.fcgi", {"id": ",".join(chunk), "retmode": "xml"})
            root = ET.fromstring(r.content)
            for art in root.findall(".//PubmedArticle"):
                rec = _parse_article(art)
                if rec:
                    yield rec


def _text(node, path, default=None):
    el = node.find(path)
    return el.text if el is not None and el.text else default


def _parse_article(art) -> dict | None:
    pmid = _text(art, ".//PMID")
    title = _text(art, ".//ArticleTitle")
    if not pmid or not title:
        return None

    # 초록은 여러 <AbstractText Label="..."> 로 쪼개져 있을 수 있음
    parts = []
    for a in art.findall(".//Abstract/AbstractText"):
        label = a.get("Label")
        body = "".join(a.itertext()).strip()
        parts.append(f"{label}: {body}" if label else body)
    abstract = "\n".join(p for p in parts if p) or None

    doi = None
    for aid in art.findall(".//ArticleId"):
        if aid.get("IdType") == "doi":
            doi = aid.text

    authors = []
    for a in art.findall(".//AuthorList/Author"):
        last, fore = _text(a, "LastName"), _text(a, "ForeName")
        if last:
            authors.append(f"{fore} {last}".strip())

    year = _text(art, ".//JournalIssue/PubDate/Year")
    if not year:
        medline = _text(art, ".//JournalIssue/PubDate/MedlineDate", "")
        year = medline[:4] if medline[:4].isdigit() else None

    # ISSN: <Journal><ISSN> 이 없는 레코드가 있어(예: 일부 NEJM 레코드)
    # MedlineJournalInfo/ISSNLinking 으로 폴백한다.
    issn = _text(art, ".//Journal/ISSN") or _text(art, ".//MedlineJournalInfo/ISSNLinking")

    return {
        "pmid": pmid,
        "doi": doi,
        "title": "".join(art.find(".//ArticleTitle").itertext()).strip(),
        "abstract": abstract,
        "journal": _text(art, ".//Journal/Title"),
        "journal_issn": issn,
        "pub_year": int(year) if year and year.isdigit() else None,
        "pub_date": year,
        "authors": authors,
        "pub_types": [e.text for e in art.findall(".//PublicationTypeList/PublicationType") if e.text],
        "mesh_terms": [e.text for e in art.findall(".//MeshHeadingList/MeshHeading/DescriptorName") if e.text],
    }
