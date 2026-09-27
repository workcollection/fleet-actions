import http.server, json, sys, os
S = json.load(open("secrets.json")); P = json.load(open("pki.json")); seen = []
import subprocess
crl_misses = {"left": int(os.environ.get("STUB_CRL_MISSES", "0"))}   # 404 the leaf CRL N times (retry test)
class H(http.server.BaseHTTPRequestHandler):
    def _r(self, code, obj):
        b = json.dumps(obj).encode(); self.send_response(code); self.send_header("Content-Type","application/json"); self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)
    def do_GET(self): self.do_POST()
    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0) or 0); self._raw = self.rfile.read(n) if n else b""; body = self._raw.decode("latin-1")
        blob = str(dict(self.headers)) + body
        rec = {"path": self.path, "got": sorted(k for k, v in S.items() if v in blob), "ua": self.headers.get("User-Agent", "")}
        if "unsigned-release" in self.path: rec["repository_id"] = json.loads(body).get("repository_id")
        seen.append(rec)
        json.dump(seen, open("seen.json", "w"))
        p = self.path
        if p.startswith("/crl/") or p.startswith("/certs/"):
            if p == "/crl/x.crl" and crl_misses["left"] > 0:
                crl_misses["left"] -= 1; self.send_response(404); self.send_header("Content-Length", "0"); self.end_headers(); return
            f = {"/certs/t0.crt": "t0.der", "/crl/t0.crl": "t0.crl"}.get(p, "x.crl")   # DER, exactly like pki-web
            b = open(f,"rb").read(); self.send_response(200); self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b); return
        if p.startswith("/api/v1/timestamp"):   # RFC 3161 via openssl ts -reply under the throwaway T0
            open("q.tsq","wb").write(self._raw)
            subprocess.run(["openssl","ts","-reply","-config","ts.cnf","-queryfile","q.tsq","-signer","tsa.pem","-inkey","tsa.key","-out","r.tsr"], check=True, capture_output=True)   # signer cert only, like the real TSA: T0 must come from -untrusted
            b = open("r.tsr","rb").read(); self.send_response(200); self.send_header("Content-Type","application/timestamp-reply"); self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b); return
        if p.startswith("/oidc"): return self._r(200, {"value": S["OIDC_JWT"]})
        if p.endswith("/auth/jwt-github/login"): return self._r(200, {"auth": {"client_token": S["BAO_TOKEN"], "token_policies": ["sign-by-repo-id-stg"]}})
        if p.endswith("/auth/approle/login"): return self._r(200, {"auth": {"client_token": S["RUNNER_TOKEN"]}})
        if p.endswith("/pki/ensure-repo-issuer"): return self._r(int(os.environ.get("STUB_ERI_CODE", "200")), {})
        if "/issue/" in p: return self._r(200, {"data": {"certificate": P["leaf"], "private_key": P["key"], "ca_chain": P["chain"], "serial_number": "01:02"}})
        return self._r(200, {})
    def log_message(self, *a): pass
# plain HTTP for OIDC/CatCMDB/CRL, HTTPS (throwaway TLS CA, like the real runners' CACERT_B64) for OpenBao
import ssl, threading
tls = http.server.ThreadingHTTPServer(("127.0.0.1", 18791), H)
ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER); ctx.load_cert_chain("tls.pem", "tls.key")
tls.socket = ctx.wrap_socket(tls.socket, server_side=True)
threading.Thread(target=tls.serve_forever, daemon=True).start()
http.server.ThreadingHTTPServer(("127.0.0.1", 18790), H).serve_forever()
