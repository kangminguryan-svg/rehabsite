# 재활의학 논문 인덱스

2010년 이후 PubMed 논문을 5개 재활 분야로 나눠 수집하고, 저널 인용지표로 걸러 정적 사이트로 보여줍니다.

## 구조

```
config/categories.yaml   5개 분야 PubMed 검색식  ← 가장 먼저 손볼 파일
config/settings.yaml     연도·지표 임계값·Fable 스위치
src/pubmed.py            E-utilities (esearch → efetch)
src/metrics.py           OpenAlex 저널 지표 조회 + 캐시
src/db.py                SQLite 스키마
src/fable.py             Fable 분석 훅 (기본 off)
src/pipeline.py          backfill / daily / export
web/index.html           프론트엔드 (web/data/*.json 소비)
.github/workflows/daily.yml   매일 05:00 KST 증분 수집
```

## 시작하기

```bash
pip install -r requirements.txt
# config/settings.yaml 의 pubmed.email 을 본인 주소로 교체 (NCBI 요구사항)
export NCBI_API_KEY=...        # https://account.ncbi.nlm.nih.gov 에서 발급, 없어도 동작하나 3배 느림

python -m src.pipeline backfill   # 최초 전체 수집 (수 시간 소요 가능)
python -m src.pipeline daily      # 이후 증분
python -m src.pipeline export     # DB → web/data/*.json

cd web && python -m http.server 8000   # http://localhost:8000
```

## Impact Factor에 대해

PubMed도 OpenAlex도 Clarivate의 Impact Factor를 제공하지 않습니다. 유료 라이선스 지표이기 때문입니다.
이 프로젝트는 대신 **OpenAlex의 `2yr_mean_citedness`**(최근 2년 평균 피인용수)를 프록시로 씁니다.
정의상 IF와 매우 유사하지만 집계 모수가 달라 **같은 값이 아닙니다**. 저널에 따라 0.3~0.8 정도 차이날 수 있으므로
임계값 2.0은 "대략 IF 2점 언저리"로 이해해야 합니다.

정식 IF가 필요하면 JCR CSV를 구해 `journal_metrics` 테이블에 직접 적재하세요.
스키마가 이미 `(issn, journal_name, metric_value, source)` 라서 소스만 바꿔 끼우면 됩니다.

지표를 못 구한 저널은 `unknown_journal_policy` 설정에 따라 처리됩니다. 기본값 `flag`는
버리지 않고 화면에 "지표 확인 필요" 배지를 달아 보여줍니다. 신생 저널을 놓치지 않기 위함입니다.

## Fable 켜기

`config/settings.yaml` 에서 `fable.enabled: true` 로 바꾸고 `ANTHROPIC_API_KEY` 를 설정하면,
통과한 논문의 초록에 구조화 요약(연구설계·대상·중재·주요결과)과 태그가 붙습니다.
`max_per_run` 으로 실행당 건수를 제한해 비용을 통제합니다.

## 앞으로 손볼 것

- **검색식 튜닝**: `categories.yaml` 의 각 query를 PubMed에 직접 붙여넣고 결과 수와 정밀도 확인
- **논문 타입 제한**: RCT·메타분석만 볼 거면 `settings.yaml` 의 `publication_types` 에 추가
- **중복 카테고리**: 한 논문이 여러 분야에 걸릴 수 있고, 스키마는 이미 N:M로 허용
