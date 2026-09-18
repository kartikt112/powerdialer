#!/usr/bin/env python3.12
"""
Dialer server: SQLite-backed lead queue + Twilio glue.

Owns the state the UI needs to be a real calling floor: atomic lead
checkout (two agents never get the same lead), retry scheduling per
config policy, callbacks, notes, lead-local calling-hours enforcement,
per-day dial caps computed from actual history, voicemail drop, and
inbound screen-pop lookups.

With TWILIO_ACCOUNT_SID / TWILIO_API_KEY_SID / TWILIO_API_KEY_SECRET /
TWILIO_TWIML_APP_SID in the environment, /api/token mints Voice access
tokens (outbound + inbound) and the REST helpers drive voicemail drop
and the voicemail inbox. Without them the UI runs as a simulator.

    python3.12 dialer/serve.py            # local, http://localhost:8765
    DIALER_PASSWORD=...                   # enables basic auth (mandatory when public)
    DATA_DIR=/data                        # volume for dialer.db + prepped lists
"""

import argparse
import base64
import glob
import hashlib
import hmac
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import db

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
OUT = os.path.join(ROOT, "out")
DATA_DIR = os.environ.get("DATA_DIR")

CONFIG = {"caller_id": "+1 917 555 0142"}

VM_DROP_TEXT = os.environ.get("VM_DROP_TEXT",
    "Hi, this is Sayim. Sorry I missed you — I was calling about your brand's "
    "TikTok Shop. You can reach me back on this number, or I'll try you again "
    "soon. Thanks, bye!")


def load_dotenv():
    """Pull KEY=value lines from ROOT/.env; real env vars win."""
    path = os.path.join(ROOT, ".env")
    if not os.path.exists(path):
        return
    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip())


# ---------------------------------------------------------------- twilio --

def twilio_token(identity):
    """Voice access token — plain HS256 JWT, no SDK dependency. None when
    the TWILIO_* env is incomplete (the UI's simulator-mode signal)."""
    account = os.environ.get("TWILIO_ACCOUNT_SID")
    key = os.environ.get("TWILIO_API_KEY_SID")
    secret = os.environ.get("TWILIO_API_KEY_SECRET")
    app = os.environ.get("TWILIO_TWIML_APP_SID")
    if not all([account, key, secret, app]):
        return None
    t = int(time.time())
    header = {"typ": "JWT", "alg": "HS256", "cty": "twilio-fpa;v=1"}
    payload = {
        "jti": f"{key}-{t}", "iss": key, "sub": account,
        "iat": t, "nbf": t, "exp": t + 3600,
        "grants": {
            "identity": identity,
            "voice": {
                "outgoing": {"application_sid": app},
                "incoming": {"allow": True},
            },
        },
    }
    b64 = lambda obj: base64.urlsafe_b64encode(
        json.dumps(obj, separators=(",", ":")).encode()).rstrip(b"=")
    signing = b64(header) + b"." + b64(payload)
    sig = base64.urlsafe_b64encode(
        hmac.new(secret.encode(), signing, hashlib.sha256).digest()).rstrip(b"=")
    return (signing + b"." + sig).decode()


def _ssl_context():
    """macOS python.org builds ship without CA certs; certifi (already a
    transitive dep via requests) fills the gap. Linux images are fine."""
    import ssl
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()


def twilio_rest(path, params=None, method="GET"):
    """Minimal REST client against api.twilio.com, API-key auth."""
    account = os.environ.get("TWILIO_ACCOUNT_SID")
    key = os.environ.get("TWILIO_API_KEY_SID")
    secret = os.environ.get("TWILIO_API_KEY_SECRET")
    if not all([account, key, secret]):
        return None
    url = f"https://api.twilio.com/2010-04-01/Accounts/{account}/{path}"
    data = None
    if params and method == "GET":
        url += "?" + urllib.parse.urlencode(params)
    elif params:
        data = urllib.parse.urlencode(params).encode()
    req = urllib.request.Request(url, data=data, method=method)
    auth = base64.b64encode(f"{key}:{secret}".encode()).decode()
    req.add_header("Authorization", f"Basic {auth}")
    with urllib.request.urlopen(req, timeout=15, context=_ssl_context()) as resp:
        body = resp.read()
    return json.loads(body) if body.strip().startswith(b"{") else body


VM_CACHE = {"at": 0, "items": [], "callers": {}}


def list_voicemails():
    """Recent recordings on the account = inbound voicemails (we don't
    record outbound). Cached for 60s; caller numbers resolved per call."""
    if time.time() - VM_CACHE["at"] < 60:
        return VM_CACHE["items"]
    out = []
    data = twilio_rest("Recordings.json", {"PageSize": 12})
    for rec in (data or {}).get("recordings", []):
        call_sid = rec["call_sid"]
        caller = VM_CACHE["callers"].get(call_sid)
        if caller is None:
            try:
                call = twilio_rest(f"Calls/{call_sid}.json")
                caller = call.get("from_formatted") or call.get("from") or "?"
                if call.get("direction") != "inbound":
                    caller = ""
            except Exception:
                caller = "?"
            VM_CACHE["callers"][call_sid] = caller
        if not caller:
            continue
        lead = db.lookup(re.sub(r"[^+\d]", "", caller)) if caller != "?" else None
        out.append({
            "sid": rec["sid"],
            "from": caller,
            "company": (lead or {}).get("company", ""),
            "duration": int(rec.get("duration") or 0),
            "at": rec.get("date_created", ""),
        })
    VM_CACHE["items"], VM_CACHE["at"] = out, time.time()
    return out


def vm_drop(parent_call_sid):
    """Redirect the callee leg into a spoken message, freeing the agent."""
    kids = twilio_rest("Calls.json", {"ParentCallSid": parent_call_sid,
                                      "PageSize": 1})
    calls = (kids or {}).get("calls", [])
    if not calls:
        return {"error": "no child call found for this call"}
    child = calls[0]["sid"]
    twiml = ("<Response><Pause length='1'/><Say voice='Polly.Matthew'>"
             + VM_DROP_TEXT.replace("&", "and").replace("<", "")
             + "</Say></Response>")
    twilio_rest(f"Calls/{child}.json", {"Twiml": twiml}, method="POST")
    return {"ok": True}


# ------------------------------------------------------------- lists/prep --

def out_dirs():
    dirs = [OUT]
    if DATA_DIR:
        dirs.insert(0, os.path.join(DATA_DIR, "out"))
    return dirs


def newest_list_file(list_id=None):
    if list_id:
        patterns = [f"vicidial_*_list{list_id}_*.csv"]
    else:
        patterns = ["vicidial_*_agent1_list*_*.csv", "vicidial_*_direct_*.csv"]
    matches = []
    for d in out_dirs():
        for p in patterns:
            matches += glob.glob(os.path.join(d, p))
    return max(matches, key=os.path.getmtime) if matches else None


def migrate():
    """First boot: fold the CSV era (newest list + call log + DNC) into SQLite."""
    db.init()
    called = os.path.join(ROOT, "cache", "called_log.csv")
    dnc = os.path.join(ROOT, "dnc.csv")
    if DATA_DIR:                       # live CSVs from the volume era win
        for vol, repo in ((os.path.join(DATA_DIR, "called_log.csv"), called),
                          (os.path.join(DATA_DIR, "dnc.csv"), dnc)):
            if os.path.exists(vol):
                if repo == called:
                    called = vol
                else:
                    dnc = vol
    added = db.import_legacy(newest_list_file(os.environ.get("LIST_ID")), called, dnc)
    if added is not None:
        print(f"  migrated  {added} leads from CSV era into dialer.db")


def run_listprep(src, outdir):
    """Prep an uploaded file and import the result. Returns (result, err)."""
    db.export_suppression(os.path.join(ROOT, "cache", "called_log.csv"),
                          os.path.join(ROOT, "dnc.csv"))
    cmd = [sys.executable, os.path.join(ROOT, "listprep.py"),
           "--input", src, "--outdir", outdir]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    except subprocess.TimeoutExpired:
        return None, ("listprep timed out after 5 min", "")
    tail = "\n".join((proc.stdout + "\n" + proc.stderr).strip().splitlines()[-12:])
    if proc.returncode != 0:
        return None, ("listprep failed", tail)
    produced = glob.glob(os.path.join(outdir, "vicidial_*_direct_*.csv"))
    newest = max(produced, key=os.path.getmtime) if produced else None
    if not newest:
        return None, ("prep produced no direct list", tail)
    added, refreshed = db.import_list_csv(newest)
    return {"ok": True, "added": added, "refreshed": refreshed,
            "source": os.path.basename(newest), "log": tail}, None


# ---------------------------------------------------------------- server --

def lead_payload(lead):
    if not lead:
        return None
    local = db.now() + timedelta(hours=lead.get("tz_offset") or -5)
    return {
        "phone": lead["phone"],
        "first": lead["first"], "last": lead["last"], "co": lead["company"],
        "title": lead["title"], "city": lead["city"], "state": lead["state"],
        "tt": lead["tiktok_followers"], "size": lead["company_size"],
        "rank": lead["rank"], "attempts": lead["attempts"],
        "tz_offset": lead.get("tz_offset", -5),
        "last_disposition": lead["last_disposition"],
        "callback_at": lead["callback_at"],
        "local_time": local.strftime("%-I:%M%p").lower(),
        "history": db.history(lead["phone"]),
    }


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body, content_type="application/json"):
        payload = body if isinstance(body, bytes) else body.encode()
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _json(self, obj, code=200):
        return self._send(code, json.dumps(obj))

    def _authed(self):
        password = os.environ.get("DIALER_PASSWORD")
        if not password:
            return True
        user = os.environ.get("DIALER_USER", "agent")
        expected = base64.b64encode(f"{user}:{password}".encode()).decode()

        supplied = (self.headers.get("Authorization") or "").removeprefix("Basic ")
        if hmac.compare_digest(supplied, expected):
            return True

        cookies = dict(p.strip().split("=", 1) for p in
                       (self.headers.get("Cookie") or "").split(";") if "=" in p)
        if hmac.compare_digest(cookies.get("dialer_auth", ""), expected):
            return True

        # Login link: /?key=<password> sets the cookie — friendlier than the
        # browser's native basic-auth prompt for agents on shared machines.
        q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        if hmac.compare_digest((q.get("key") or [""])[0], password):
            self.send_response(302)
            self.send_header("Set-Cookie",
                             f"dialer_auth={expected}; Path=/; HttpOnly; SameSite=Lax; Max-Age=2592000")
            self.send_header("Location", "/")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return False

        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="powerdialer"')
        self.send_header("Content-Length", "0")
        self.end_headers()
        return False

    def _body(self):
        length = int(self.headers.get("Content-Length") or 0)
        try:
            return json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            return None

    def _agent(self, data=None):
        raw = (data or {}).get("agent") or \
            urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query
                                  ).get("agent", ["agent1"])[0]
        return re.sub(r"[^A-Za-z0-9_-]", "", raw)[:24] or "agent1"

    # ------------------------------------------------------------- GET --

    def do_GET(self):
        if not self._authed():
            return
        route = urllib.parse.urlparse(self.path).path

        if route in ("/", "/index.html"):
            with open(os.path.join(HERE, "index.html"), "rb") as fh:
                page = fh.read()
            if not page.lstrip().lower().startswith(b"<!doctype"):
                page = (b"<!doctype html><html><head><meta charset='utf-8'>"
                        b"<meta name='viewport' content='width=device-width,initial-scale=1'>"
                        b"</head><body>" + page + b"</body></html>")
            return self._send(200, page, "text/html; charset=utf-8")

        if route == "/twilio.min.js":
            with open(os.path.join(HERE, "twilio.min.js"), "rb") as fh:
                return self._send(200, fh.read(), "application/javascript")

        if route == "/api/token":
            token = twilio_token(self._agent())
            return self._json({"token": token} if token else {"sim": True})

        if route == "/api/next":
            lead, reason = db.checkout(self._agent())
            return self._json({
                "lead": lead_payload(lead),
                "reason": reason,
                "queue": db.queue_preview(),
                "stats": db.stats(),
                "caller_id": CONFIG["caller_id"],
            })

        if route == "/api/stats":
            return self._json(db.stats())

        if route == "/api/lookup":
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            phone = re.sub(r"[^+\d]", "", (q.get("phone") or [""])[0])
            return self._json({"lead": lead_payload(db.lookup(phone))})

        if route == "/api/voicemails":
            try:
                return self._json({"voicemails": list_voicemails()})
            except Exception as e:
                return self._json({"voicemails": [], "error": str(e)})

        if route.startswith("/api/voicemail/"):
            sid = re.sub(r"[^A-Za-z0-9]", "", route.rsplit("/", 1)[-1].removesuffix(".mp3"))
            try:
                audio = twilio_rest(f"Recordings/{sid}.mp3")
                return self._send(200, audio, "audio/mpeg")
            except Exception:
                return self._json({"error": "recording unavailable"}, 404)

        if route == "/api/dnc.csv":
            db.export_suppression(os.path.join(ROOT, "cache", "called_log.csv"),
                                  os.path.join(ROOT, "dnc.csv"))
            with open(os.path.join(ROOT, "dnc.csv"), "rb") as fh:
                return self._send(200, fh.read(), "text/csv")

        return self._json({"error": "not found"}, 404)

    # ------------------------------------------------------------ POST --

    def do_POST(self):
        if not self._authed():
            return
        route = urllib.parse.urlparse(self.path).path

        if route == "/api/upload":
            return self._upload()

        data = self._body()
        if data is None:
            return self._json({"error": "bad json"}, 400)

        if route == "/api/disposition":
            phone = re.sub(r"[^+\d]", "", data.get("phone", ""))
            code = data.get("disposition", "")
            if not phone or not code:
                return self._json({"error": "phone and disposition required"}, 400)
            db.disposition(phone, data.get("company", ""), code,
                           (data.get("notes") or "")[:2000], self._agent(data),
                           int(data.get("duration") or 0),
                           callback_at=data.get("callback_at"))
            print(f"  {code:11s} {phone}  {data.get('company', '')}"
                  + (f"  [{(data.get('notes') or '')[:60]}]" if data.get("notes") else ""))
            return self._json({"ok": True, "stats": db.stats()})

        if route == "/api/skip":
            db.skip(re.sub(r"[^+\d]", "", data.get("phone", "")), self._agent(data))
            return self._json({"ok": True})

        if route == "/api/release":
            db.release(re.sub(r"[^+\d]", "", data.get("phone", "")))
            return self._json({"ok": True})

        if route == "/api/vmdrop":
            sid = re.sub(r"[^A-Za-z0-9]", "", data.get("call_sid", ""))
            if not sid:
                return self._json({"error": "call_sid required"}, 400)
            if not os.environ.get("TWILIO_ACCOUNT_SID"):
                return self._json({"sim": True})
            try:
                return self._json(vm_drop(sid))
            except Exception as e:
                return self._json({"error": str(e)}, 502)

        return self._json({"error": "not found"}, 404)

    def _upload(self):
        length = int(self.headers.get("Content-Length") or 0)
        if not length or length > 30_000_000:
            return self._json({"error": "missing or oversized file (30 MB cap)"}, 400)
        raw = self.rfile.read(length)
        name = os.path.basename(self.headers.get("X-Filename") or "upload.xlsx")
        name = re.sub(r"[^A-Za-z0-9._-]", "_", name)
        if not name.lower().endswith((".xlsx", ".xls", ".csv")):
            return self._json({"error": "need a .xlsx or .csv file"}, 400)

        base = DATA_DIR or ROOT
        updir = os.path.join(base, "uploads")
        outdir = out_dirs()[0]
        os.makedirs(updir, exist_ok=True)
        os.makedirs(outdir, exist_ok=True)
        src = os.path.join(updir, datetime.now().strftime("%Y%m%d_%H%M%S_") + name)
        with open(src, "wb") as fh:
            fh.write(raw)

        result, err = run_listprep(src, outdir)
        if err:
            return self._json({"error": err[0], "log": err[1]}, 422)
        result["stats"] = db.stats()
        print(f"  UPLOAD    {name}: +{result['added']} new, "
              f"{result['refreshed']} refreshed -> {result['source']}")
        return self._json(result)

    def log_message(self, *args):
        pass


def main():
    load_dotenv()
    global VM_DROP_TEXT
    VM_DROP_TEXT = os.environ.get("VM_DROP_TEXT", VM_DROP_TEXT)
    migrate()

    parser = argparse.ArgumentParser(description="Run the dialer.")
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", 8765)))
    parser.add_argument("--host", default=os.environ.get("HOST", "127.0.0.1"))
    parser.add_argument("--caller-id",
                        default=os.environ.get("TWILIO_CALLER_ID", CONFIG["caller_id"]))
    parser.add_argument("--no-open", action="store_true")
    args = parser.parse_args()
    CONFIG["caller_id"] = args.caller_id

    s = db.stats()
    print(f"  db        {db.DB_PATH}")
    print(f"  queue     {s['queue']} dialable now, {s['retry_pool']} waiting on retry timers, "
          f"{s['callbacks_due']} callbacks due in 24h")
    print(f"  today     {s['dials_today']}/{s['cap']} dials")
    print(f"  caller ID {args.caller_id}")
    print(f"  twilio    {'credentials found — real calls' if twilio_token('probe') else 'not configured — simulator mode (see TWILIO.md)'}")
    print(f"  auth      {'basic auth on' if os.environ.get('DIALER_PASSWORD') else 'OFF — local use only'}")
    print(f"  serving   {args.host}:{args.port}\n")

    if not args.no_open and args.host in ("127.0.0.1", "localhost"):
        webbrowser.open(f"http://localhost:{args.port}/")
    try:
        ThreadingHTTPServer((args.host, args.port), Handler).serve_forever()
    except KeyboardInterrupt:
        print("\n  stopped")


if __name__ == "__main__":
    main()
