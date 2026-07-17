# 재활의학 · 신경학 논문 인덱스

엄선한 신경학·재활의학 저널의 2022년 이후 PubMed 논문을 두 대분류(신경학·재활의학)와
하위 분야로 나눠 수집하고 정적 사이트로 보여줍니다. 초록은 화면에서 한글로 번역해 봅니다.

## 구조

```
config/categories.yaml   대분류·하위분류·저널 목록 + 주제 필터  ← 가장 먼저 손볼 파일
config/settings.yaml     연도·논문타입·Fable 스위치
src/pubmed.py            E-utilities (esearch → efetch), 연 단위/건수 조회
src/db.py                SQLite 스키마
src/fable.py             Fable 분석 훅 (기본 off)
src/pipeline.py          backfill / daily / export / verify-journals
web/index.html           프론트엔드 (web/data/*.json 소비, 2단계 네비 + 초록 번역)
scripts/                 진단 도구 (저널 건수 확인 등)
.github/workflows/daily.yml   매일 05:00 KST 증분 수집
```

## 수집 방식

품질 필터를 IF(인용지표) 대신 **저널 큐레이션**으로 한다. `categories.yaml`에 나열한
저널의 게재분만 모은다. 각 하위 분류는 저널 약어(NLM title abbreviation) 목록으로 정의되고,
`topic_filter: true` 인 분류(대형 종합지·광범위 정형/물리치료지)는 공통 주제 필터를 AND로
걸어 재활 관련 주제만 추린다. 전문지(`topic_filter: false`)는 게재분 전체를 담는다.

한 논문이 여러 분류에 걸릴 수 있다(예: Stroke 지는 신경학·재활의학 양쪽). 스키마가 N:M을
허용하므로 각 분류에 중복 노출된다.

## 시작하기

```bash
pip install -r requirements.txt
export NCBI_API_KEY=...        # https://account.ncbi.nlm.nih.gov 에서 발급, 없어도 동작하나 느림

python -m src.pipeline verify-journals   # (선택) 저널 약어별 건수 확인 — 오타 점검
python -m src.pipeline backfill          # 2022년 이후 연 단위 전체 수집
python -m src.pipeline daily             # 이후 증분
python -m src.pipeline export            # DB → web/data/*.json

cd web && python -m http.server 8000     # http://localhost:8000
```

윈도우 PowerShell에서는 `export NCBI_API_KEY=...` 대신 `$env:NCBI_API_KEY="..."`.

## 저널 목록 손보기

`categories.yaml`의 각 하위 분류 `journals` 항목에 `{name: 표시용 이름, ta: PubMed 약어}` 로
추가·삭제한다. 약어가 맞는지는 `verify-journals`로 건수를 확인한다(0건이면 약어 오타 가능).
대형 종합지에서 잡음이 많으면 `topic_filter: true` 로, 전문지를 통째로 담으려면 `false` 로.

## 논문 타입 제한

RCT·메타분석만 보려면 `settings.yaml` 의 `publication_types` 에 추가하면 모든 분류에
AND로 적용된다. 예: `[Randomized Controlled Trial, Meta-Analysis, Systematic Review]`.

## 키워드 필터 (export 단계)

`categories.yaml` 의 `keyword_filter` 로 제목·MeSH 기준으로 한 번 더 거른다.
`require_all`(전부 포함) + `require_any`(하나 이상 포함)를 모두 만족해야 노출된다.
기본값은 `rehabilitation` 필수 + (stroke·traumatic brain injury·spinal cord injury·
parkinson·dementia·dysphagia·arthroplasty 중 하나). 수집과 무관하게 `export` 만 다시
돌리면 즉시 반영되므로 조건을 바꿔가며 규모를 조절하기 좋다.

## 초록 한글 번역 · 데이터 갱신

프론트엔드에서 "초록 보기"를 열면 Google 번역(무키)으로 한글 번역을 표시하고 원문 탭으로
전환할 수 있다. 번역 결과는 브라우저에 캐시된다. 실패 시 원문과 외부 번역 링크로 폴백한다.

툴바의 "↻ 갱신" 버튼은 저장소의 최신 `web/data` 를 캐시 무시하고 다시 불러온다
(페이지 새로고침 없이). 논문 수집 자체는 파이프라인/일일 워크플로가 수행한다.

## Fable 켜기

`config/settings.yaml` 에서 `fable.enabled: true` 로 바꾸고 `ANTHROPIC_API_KEY` 를 설정하면,
수집된 논문의 초록에 구조화 요약(연구설계·대상·중재·주요결과)과 태그가 붙는다.
`max_per_run` 으로 실행당 건수를 제한해 비용을 통제한다.

## 참고

- `metrics.py`(OpenAlex 지표 조회)는 현재 수집·필터에 쓰지 않는다. 향후 저널 지표를
  화면에 표기하고 싶을 때를 위해 남겨둔 것이다.
- backfill은 연 단위로 나눠 수집해 esearch의 1회 1만 건 상한을 회피한다.
