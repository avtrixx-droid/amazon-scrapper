"""
Vercel serverless proxy for the AmazonScraper license-admin dashboard.

Why a proxy?
  - Keeps the license server's ADMIN token server-side (a Vercel env var). It is
    NEVER sent to the browser, unlike a static page that would embed it.
  - Same-origin: the dashboard calls /api/proxy on its own Vercel domain, so
    there is no CORS to configure on the Render license server.

Auth: the dashboard sends a `password` with every request; it is compared
(constant-time) against the DASHBOARD_PASSWORD env var. Only on a match does the
proxy forward the call to the Render license server with the Bearer admin token.

Required Vercel environment variables:
  LICENSE_SERVER_URL    e.g. https://amazon-scraper-license.onrender.com
  LICENSE_ADMIN_TOKEN   the same value as the server's LICENSE_ADMIN_TOKEN
  DASHBOARD_PASSWORD    a password you choose to gate this dashboard
"""

from http.server import BaseHTTPRequestHandler
import hmac
import json
import os
import urllib.error
import urllib.parse
import urllib.request

LICENSE_SERVER_URL = os.environ.get("LICENSE_SERVER_URL", "").rstrip("/")
ADMIN_TOKEN = os.environ.get("LICENSE_ADMIN_TOKEN", "")
DASHBOARD_PASSWORD = os.environ.get("DASHBOARD_PASSWORD", "")

# action -> (HTTP method, server path, accepted parameter names)
ROUTES = {
    "list":     ("GET",  "/admin/list",            []),
    "runs":     ("GET",  "/admin/runs",            ["key"]),
    "info":     ("GET",  "/admin/info",            ["key"]),
    "issue":    ("POST", "/admin/issue",           ["customer", "days", "max_machines", "notes"]),
    "revoke":   ("POST", "/admin/revoke",          ["key"]),
    "unrevoke": ("POST", "/admin/unrevoke",        ["key"]),
    "extend":   ("POST", "/admin/extend",          ["key", "days"]),
    "release":  ("POST", "/admin/release-machine", ["key", "machine_id"]),
}


class handler(BaseHTTPRequestHandler):
    def _send(self, code, obj):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(length) if length else b"{}"
            data = json.loads(raw or b"{}")
            if not isinstance(data, dict):
                raise ValueError
        except Exception:
            return self._send(400, {"ok": False, "error": "Bad request."})

        if not (LICENSE_SERVER_URL and ADMIN_TOKEN and DASHBOARD_PASSWORD):
            return self._send(500, {
                "ok": False,
                "error": "Dashboard not configured. Set LICENSE_SERVER_URL, "
                         "LICENSE_ADMIN_TOKEN and DASHBOARD_PASSWORD in Vercel.",
            })

        if not hmac.compare_digest(str(data.get("password", "")), DASHBOARD_PASSWORD):
            return self._send(401, {"ok": False, "error": "Wrong dashboard password."})

        route = ROUTES.get(data.get("action", ""))
        if not route:
            return self._send(400, {"ok": False, "error": "Unknown action."})

        method, path, params = route
        url = LICENSE_SERVER_URL + path
        headers = {"Authorization": "Bearer " + ADMIN_TOKEN}

        if method == "GET":
            query = {k: data[k] for k in params if data.get(k) not in (None, "")}
            if query:
                url += "?" + urllib.parse.urlencode(query)
            req = urllib.request.Request(url, headers=headers, method="GET")
        else:
            payload = {k: data.get(k) for k in params if data.get(k) is not None}
            headers["Content-Type"] = "application/json"
            req = urllib.request.Request(
                url, data=json.dumps(payload).encode("utf-8"),
                headers=headers, method="POST",
            )

        try:
            with urllib.request.urlopen(req, timeout=40) as resp:
                out = resp.read()
                return self._send(resp.status, json.loads(out or b"{}"))
        except urllib.error.HTTPError as e:
            try:
                out = json.loads(e.read() or b"{}")
            except Exception:
                out = {"ok": False, "error": "HTTP %d" % e.code}
            return self._send(e.code, out)
        except Exception as e:
            return self._send(502, {"ok": False, "error": "Could not reach the license server: %s" % e})
