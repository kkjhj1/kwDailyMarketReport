"""Lightweight built-in HTTP server to serve generated HTML reports with optional HMAC signature check."""
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from market_brief.web_report import verify_signed_report_token


def create_report_handler(report_dir: Path, secret_key: str, require_signature: bool = False):
    class ReportHTTPRequestHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            parsed = urlparse(self.path)
            if not parsed.path.startswith("/reports/") or not parsed.path.endswith(".html"):
                self.send_response(404)
                self.end_headers()
                self.wfile.write(b"Not Found")
                return

            filename = Path(parsed.path).name
            report_id = filename[:-5]
            if require_signature:
                qs = parse_qs(parsed.query)
                sig = (qs.get("sig") or [""])[0]
                if not verify_signed_report_token(report_id, sig, secret_key):
                    self.send_response(403)
                    self.end_headers()
                    self.wfile.write(b"Forbidden: Invalid or expired report signature")
                    return

            target = report_dir / filename
            if not target.exists():
                self.send_response(404)
                self.end_headers()
                self.wfile.write(b"Report Not Found")
                return

            content = target.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)

    return ReportHTTPRequestHandler


def run_report_server(report_dir: str | Path = "reports", port: int = 8080,
                      secret_key: str = "dev-secret-key", require_signature: bool = False):
    handler = create_report_handler(Path(report_dir), secret_key, require_signature)
    server = HTTPServer(("0.0.0.0", port), handler)
    print(f"Serving detailed HTML market reports at http://localhost:{port}/reports/ ...")
    server.serve_forever()
