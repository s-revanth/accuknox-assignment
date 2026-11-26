"""A tiny local REST API so every script here runs without the internet.

    GET /books      -> list of books  {title, author, publication_year}
    GET /students   -> list of students {id, name, scores: {subject: score}}

Run:  python mock_api.py            (serves on http://127.0.0.1:8765)
"""
import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

DATA = json.loads((Path(__file__).parent / "data" / "api_fixture.json").read_text())


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        key = self.path.strip("/").split("?")[0]
        if key not in DATA:
            self._send(404, {"error": f"unknown resource '{key}'"})
            return
        self._send(200, DATA[key])

    def _send(self, status, body):
        raw = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *args):  # keep test output quiet
        pass


def make_server(port=8765):
    return ThreadingHTTPServer(("127.0.0.1", port), Handler)


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8765
    print(f"mock API on http://127.0.0.1:{port}  (/books, /students)")
    make_server(port).serve_forever()
