"""Fable 분석 훅. settings.yaml에서 fable.enabled=true 로 켜면 동작.

지금 단계에서는 자리만 잡아둔 것. 켜면 초록을 받아 구조화 요약 + 태그를 붙인다.
비용이 논문 수에 비례하므로 max_per_run 으로 한 실행당 상한을 둔다.
"""
import json
import os

PROMPT = """다음은 재활의학 논문의 초록이다. 아래 JSON만 출력하라. 다른 말은 쓰지 마라.

{{
  "summary_ko": "핵심 결과 2~3문장, 한국어",
  "study_design": "RCT | cohort | case-control | systematic review | meta-analysis | other",
  "population": "대상자 한 줄",
  "intervention": "중재 한 줄",
  "key_outcome": "주요 결과 지표와 방향성 한 줄",
  "tags": ["키워드", "3~6개"]
}}

제목: {title}
저널: {journal}
초록: {abstract}
"""


class FableAnalyzer:
    def __init__(self, model: str, api_key_env: str = "ANTHROPIC_API_KEY"):
        from anthropic import Anthropic  # 지연 임포트: off일 땐 의존성 불필요
        self.client = Anthropic(api_key=os.environ[api_key_env])
        self.model = model

    def analyze(self, rec: dict) -> dict | None:
        if not rec.get("abstract"):
            return None
        msg = self.client.messages.create(
            model=self.model,
            max_tokens=1000,
            messages=[{"role": "user", "content": PROMPT.format(
                title=rec["title"], journal=rec.get("journal", ""), abstract=rec["abstract"]
            )}],
        )
        text = "".join(b.text for b in msg.content if b.type == "text")
        try:
            return json.loads(text.replace("```json", "").replace("```", "").strip())
        except json.JSONDecodeError:
            return None


def apply(conn, analyzer: "FableAnalyzer", limit: int) -> int:
    """요약이 아직 없는 통과 논문에 대해 분석을 채운다. 처리 건수 반환."""
    rows = conn.execute(
        "SELECT pmid, title, journal, abstract FROM papers "
        "WHERE passed_filter=1 AND summary IS NULL AND abstract IS NOT NULL LIMIT ?",
        (limit,),
    ).fetchall()
    done = 0
    for row in rows:
        out = analyzer.analyze(dict(row))
        if not out:
            continue
        conn.execute(
            "UPDATE papers SET summary=?, tags=? WHERE pmid=?",
            (json.dumps(out, ensure_ascii=False), json.dumps(out.get("tags", []), ensure_ascii=False), row["pmid"]),
        )
        done += 1
    conn.commit()
    return done
