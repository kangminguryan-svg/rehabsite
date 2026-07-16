"""OpenAlex 저널 조회를 여러 방식으로 직접 찔러보고 원시 응답을 출력한다.
저널명 폴백이 왜 실패하는지 진단하기 위함.

사용법:
  python scripts/probe_openalex.py                       # NEJM 기본
  python scripts/probe_openalex.py "The Lancet"          # 다른 저널
"""
import sys
import time

import requests

OPENALEX = "https://api.openalex.org/sources"
MAILTO = "kangmingu.ryan@gmail.com"


def show(title: str, params: dict) -> None:
    params = {**params, "mailto": MAILTO}
    print(f"\n=== {title} ===")
    print(f"    params: {params}")
    r = None
    for attempt in range(5):  # 429/5xx 백오프
        try:
            r = requests.get(OPENALEX, params=params, timeout=30)
        except requests.RequestException as e:
            print(f"    ⚠ 요청 실패: {e}")
            time.sleep(2 ** attempt)
            continue
        if r.status_code == 429 or r.status_code >= 500:
            ra = r.headers.get("Retry-After", "")
            wait = float(ra) if ra.replace(".", "", 1).isdigit() else 2 ** attempt
            if wait > 120:  # 일일 한도 소진 — 대기 무의미
                print(f"    HTTP {r.status_code}  Retry-After={wait:.0f}s (~{wait/3600:.1f}시간)")
                print("    ⚠ OpenAlex 일일 요청 한도 초과. UTC 자정 리셋 후 다시 시도하세요.")
                return
            print(f"    HTTP {r.status_code} → {wait:.0f}s 대기 후 재시도")
            time.sleep(wait)
            continue
        break
    if r is None:
        return
    print(f"    HTTP {r.status_code}  URL={r.url}")
    try:
        r.raise_for_status()
        results = r.json().get("results", [])
    except requests.RequestException as e:
        print(f"    ⚠ 요청 실패: {e}")
        return
    print(f"    결과 {len(results)}건")
    for i, s in enumerate(results[:5], 1):
        stats = s.get("summary_stats") or {}
        print(f"      [{i}] {s.get('display_name')!r}")
        print(f"          issn_l={s.get('issn_l')}  issn={s.get('issn')}")
        print(f"          2yr_mean_citedness={stats.get('2yr_mean_citedness')}  "
              f"works_count={s.get('works_count')}")


def main() -> None:
    name = sys.argv[1] if len(sys.argv) > 1 else "The New England journal of medicine"
    print(f"저널명: {name!r}")
    # 1) 현재 코드가 쓰는 방식
    show("A) search 파라미터 (현재 폴백 방식)", {"search": name, "per-page": 5})
    # 2) 필드 지정 검색
    show("B) filter=display_name.search", {"filter": f"display_name.search:{name}", "per-page": 5})
    # 3) NEJM ISSN 직접 (OpenAlex에 지표가 있는지 확인용)
    show("C) filter=issn:0028-4793 (NEJM 확인용)", {"filter": "issn:0028-4793", "per-page": 1})


if __name__ == "__main__":
    main()
