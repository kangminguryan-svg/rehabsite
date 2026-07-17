#!/usr/bin/env python3
"""로컬 개발 서버 + 논문 수집 트리거.

  python serve.py           # http://localhost:8000
  python serve.py 9000      # 포트 지정

web/ 를 정적 서빙하면서, 사이트의 '↻ 갱신' 버튼이 호출하는 API를 제공한다:
  POST /api/refresh  → 백그라운드로 `python -m src.pipeline daily` 실행(신규 논문 수집)
  GET  /api/status   → 실행 상태/최근 로그 반환

수집이 끝나면 web/data 가 갱신되고, 프론트가 데이터를 다시 불러온다.
NCBI_API_KEY 를 설정한 셸에서 실행하면 그 값이 수집 프로세스로 전달된다.
"""
import json
import subprocess
import sys
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent
WEB = ROOT / "web"


class Job:
    """수집 프로세스 한 번에 하나만 실행. 상태와 최근 로그를 보관."""

    def __init__(self):
        self.lock = threading.Lock()
        self.running = False
        self.done = False
        self.ok = False
        self.tail = []

    def start(self) -> bool:
        with self.lock:
            if self.running:
                return False
            self.running, self.done, self.ok, self.tail = True, False, False, []
        threading.Thread(target=self._run, daemon=True).start()
        return True

    def _run(self):
        try:
            proc = subprocess.Popen(
                [sys.executable, "-m", "src.pipeline", "daily"],
                cwd=str(ROOT), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, bufsize=1)
            for line in proc.stdout:
                line = line.rstrip()
                if line:
                    with self.lock:
                        self.tail = (self.tail + [line])[-40:]
            ok = proc.wait() == 0
        except Exception as e:  # noqa: BLE001
            with self.lock:
                self.tail = (self.tail + [f"오류: {e}"])[-40:]
            ok = False
        with self.lock:
            self.running, self.done, self.ok = False, True, ok

    def status(self) -> dict:
        with self.lock:
            return {"running": self.running, "done": self.done,
                    "ok": self.ok, "tail": self.tail[-12:]}


job = Job()


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *a, **k):
        super().__init__(*a, directory=str(WEB), **k)

    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        if self.path.rstrip("/") == "/api/refresh":
            started = job.start()
            self._json({"started": started, **job.status()})
        else:
            self.send_error(404)

    def do_GET(self):
        if self.path.rstrip("/") == "/api/status":
            self._json(job.status())
        else:
            super().do_GET()

    def end_headers(self):
        # 갱신이 즉시 보이도록 정적 파일도 캐시하지 않는다.
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8000
    print(f"http://localhost:{port}   ('↻ 갱신' 버튼으로 신규 논문 수집)")
    try:
        ThreadingHTTPServer(("", port), Handler).serve_forever()
    except KeyboardInterrupt:
        print("\n종료")
