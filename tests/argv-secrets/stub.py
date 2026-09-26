import http.server, json, sys, os
S = json.load(open("secrets.json")); P = json.load(open("pki.json")); seen = []
class H(http.server.BaseHTTPRequestHandler):
    def _r(self, code, obj):
        b = json.dumps(obj).encode(); self.send_response(code); self.send_header("Content-Type","application/json"); self.end_headers(); self.wfile.write(b)
    def do_GET(self): self.do_POST()
    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0) or 0); body = self.rfile.read(n).decode() if n else ""
        blob = str(dict(self.headers)) + body
        seen.append({"path": self.path, "got": sorted(k for k, v in S.items() if v in blob)})
        json.dump(seen, open("seen.json", "w"))
        p = self.path
        if p.startswith("/crl/"):
            b = open("x.crl","rb").read(); self.send_response(200); self.end_headers(); self.wfile.write(b); return
        if p.startswith("/oidc"): return self._r(200, {"value": S["OIDC_JWT"]})
        if p.endswith("/auth/jwt-github/login"): return self._r(200, {"auth": {"client_token": S["BAO_TOKEN"], "token_policies": ["sign-by-repo-id-stg"]}})
        if p.endswith("/auth/approle/login"): return self._r(200, {"auth": {"client_token": S["RUNNER_TOKEN"]}})
        if "/issue/" in p: return self._r(200, {"data": {"certificate": P["leaf"], "private_key": P["key"], "ca_chain": [P["root"]], "serial_number": "01:02"}})
        return self._r(200, {})
    def log_message(self, *a): pass
http.server.ThreadingHTTPServer(("127.0.0.1", 18790), H).serve_forever()
