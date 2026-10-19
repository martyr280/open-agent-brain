#!/usr/bin/env python3
"""One-time helper: get a Google refresh token for the Gmail + Calendar ingestors.

Run on your own computer (it opens a browser):

    python tools/google_oauth.py --client-id XXX --client-secret YYY

Use an OAuth client of type "Desktop app" from Google Cloud Console, with the
Gmail API and Google Calendar API enabled. Scopes are read-only. The script
prints the refresh token; store it as GOOGLE_<ACCOUNT>_REFRESH_TOKEN in your
GitHub repo secrets. It does not write the token anywhere.
"""

from __future__ import annotations

import argparse
import http.server
import secrets
import threading
import urllib.parse
import webbrowser

import httpx

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly",
          "https://www.googleapis.com/auth/calendar.readonly"]
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--client-id", required=True)
    ap.add_argument("--client-secret", required=True)
    ap.add_argument("--port", type=int, default=8765)
    args = ap.parse_args()

    state = secrets.token_urlsafe(16)
    redirect = f"http://127.0.0.1:{args.port}/callback"
    result: dict[str, str] = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            if q.get("state", [""])[0] != state:
                self.send_response(400); self.end_headers(); self.wfile.write(b"state mismatch"); return
            result["code"] = q.get("code", [""])[0]
            result["error"] = q.get("error", [""])[0]
            self.send_response(200); self.end_headers()
            self.wfile.write(b"Done. You can close this tab and return to the terminal.")

        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", args.port), Handler)
    t = threading.Thread(target=srv.handle_request, daemon=True)
    t.start()
    url = AUTH_URL + "?" + urllib.parse.urlencode({
        "client_id": args.client_id, "redirect_uri": redirect, "response_type": "code",
        "scope": " ".join(SCOPES), "access_type": "offline", "prompt": "consent", "state": state})
    print("Opening browser. If it does not open, visit:\n" + url)
    webbrowser.open(url)
    t.join(timeout=300)
    srv.server_close()
    if not result.get("code"):
        raise SystemExit(f"no authorization code received ({result.get('error') or 'timeout'})")
    r = httpx.post(TOKEN_URL, data={"code": result["code"], "client_id": args.client_id,
                                    "client_secret": args.client_secret, "redirect_uri": redirect,
                                    "grant_type": "authorization_code"}, timeout=30)
    r.raise_for_status()
    tok = r.json().get("refresh_token")
    if not tok:
        raise SystemExit("Google returned no refresh_token. Remove the app's access at "
                         "https://myaccount.google.com/permissions and run again.")
    print("\nREFRESH TOKEN (store as a secret, do not commit):\n" + tok)


if __name__ == "__main__":
    main()
