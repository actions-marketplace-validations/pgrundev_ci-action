#!/usr/bin/env python3
"""Stub PGRun API: serves $STUB_DIR/context.json (status from context.status)."""
import http.server, json, os, sys
d = os.environ["STUB_DIR"]
class H(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def do_GET(self):
        with open(os.path.join(d, "api_calls"), "a") as f:
            f.write(self.path + " auth=" + ("yes" if self.headers.get("Authorization", "").startswith("Bearer ") else "no") + "\n")
        spec = json.load(open(os.path.join(d, "context.json")))
        if self.headers.get("Authorization") != "Bearer " + spec.get("token", "good"):
            code, body = 401, {"error": "unauthorized"}
        elif self.path.startswith("/api/v1/ci/context"):
            code, body = spec.get("status", 200), spec.get("body", {})
        else:
            code, body = 404, {"error": "not_found"}
        out = json.dumps(body).encode()
        self.send_response(code); self.send_header("Content-Type", "application/json"); self.send_header("Content-Length", str(len(out))); self.end_headers(); self.wfile.write(out)
s = http.server.HTTPServer(("127.0.0.1", 0), H)
open(os.path.join(d, "api_port"), "w").write(str(s.server_port))
s.serve_forever()
