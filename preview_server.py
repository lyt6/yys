"""Dependency-free local preview server for the SQLite listing database."""

from __future__ import annotations

import argparse
import json
import mimetypes
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from storage import SQLiteStore

STATIC_FILES = {
    "/": "index.html",
    "/index.html": "index.html",
    "/app.js": "app.js",
    "/styles.css": "styles.css",
    "/favicon.svg": "favicon.svg",
}


def _first(query: dict[str, list[str]], name: str, default: str = "") -> str:
    values = query.get(name)
    return values[0] if values else default


def create_handler(store: SQLiteStore, static_dir: Path):
    class PreviewHandler(BaseHTTPRequestHandler):
        server_version = "YYSPreview/1.0"

        def log_message(self, fmt: str, *args: Any) -> None:
            print(f"[预览] {self.address_string()} - {fmt % args}", flush=True)

        def _security_headers(self) -> None:
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Cache-Control", "no-store")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; script-src 'self'; style-src 'self'; "
                "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'",
            )

        def _send_json(self, value: Any, status: int = HTTPStatus.OK) -> None:
            payload = json.dumps(value, ensure_ascii=False, default=str).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self._security_headers()
            self.end_headers()
            self.wfile.write(payload)

        def _send_static(self, path: str) -> None:
            filename = STATIC_FILES[path]
            file_path = (static_dir / filename).resolve()
            if file_path.parent != static_dir.resolve() or not file_path.is_file():
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            payload = file_path.read_bytes()
            content_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
            if filename.endswith((".html", ".js", ".css", ".svg")):
                content_type += "; charset=utf-8"
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(payload)))
            self._security_headers()
            self.end_headers()
            self.wfile.write(payload)

        def do_GET(self) -> None:  # noqa: N802 - stdlib callback name
            parsed = urlparse(self.path)
            query = parse_qs(parsed.query)
            try:
                if parsed.path in STATIC_FILES:
                    self._send_static(parsed.path)
                    return
                if parsed.path == "/healthz":
                    self._send_json({"ok": True})
                    return
                if parsed.path == "/api/options":
                    self._send_json(store.get_options())
                    return
                if parsed.path == "/api/summary":
                    self._send_json(
                        store.get_summary(_first(query, "account"), _first(query, "target"))
                    )
                    return
                if parsed.path == "/api/items":
                    self._send_json(
                        store.list_items(
                            account_key=_first(query, "account"),
                            target_key=_first(query, "target"),
                            query=_first(query, "q"),
                            limit=int(_first(query, "limit", "100")),
                            offset=int(_first(query, "offset", "0")),
                            sort=_first(query, "sort", "last_changed_at"),
                            order=_first(query, "order", "desc"),
                        )
                    )
                    return
                if parsed.path == "/api/runs":
                    self._send_json(
                        {
                            "runs": store.list_runs(
                                account_key=_first(query, "account"),
                                target_key=_first(query, "target"),
                                limit=int(_first(query, "limit", "50")),
                            )
                        }
                    )
                    return
                self._send_json({"error": "not_found"}, HTTPStatus.NOT_FOUND)
            except (TypeError, ValueError) as exc:
                self._send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
            except Exception as exc:
                self._send_json(
                    {"error": "internal_error", "message": str(exc)},
                    HTTPStatus.INTERNAL_SERVER_ERROR,
                )

    return PreviewHandler


def build_server(
    database_path: str = "data/cbg.sqlite3",
    host: str = "127.0.0.1",
    port: int = 8765,
    *,
    allow_remote: bool = False,
) -> ThreadingHTTPServer:
    if host.lower() not in {"127.0.0.1", "localhost", "::1"} and not allow_remote:
        raise ValueError("预览页没有身份认证；如确需远程监听，必须显式传入 allow_remote")
    static_dir = Path(__file__).resolve().parent / "web"
    store = SQLiteStore(database_path)
    server = ThreadingHTTPServer((host, int(port)), create_handler(store, static_dir))
    server.daemon_threads = True
    return server


def main() -> int:
    parser = argparse.ArgumentParser(description="藏宝阁 SQLite 数据预览页")
    parser.add_argument("--database", default="data/cbg.sqlite3")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument(
        "--allow-remote",
        action="store_true",
        help="明确允许监听非本机地址（服务本身没有身份认证）",
    )
    args = parser.parse_args()
    try:
        server = build_server(
            args.database,
            args.host,
            args.port,
            allow_remote=args.allow_remote,
        )
    except ValueError as exc:
        parser.error(str(exc))
    host, port = server.server_address[:2]
    print(f"预览页已启动：http://{host}:{port}", flush=True)
    print(f"数据库：{Path(args.database).resolve()}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n预览页已停止。", flush=True)
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
