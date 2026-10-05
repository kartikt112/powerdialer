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
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import db
import funnel
import policy

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
OUT = os.path.join(ROOT, "out")
DATA_DIR = os.environ.get("DATA_DIR")

CONFIG = {"caller_id": "+1 917 555 0142"}

VM_DROP_TEXT = os.environ.get("VM_DROP_TEXT",
    "Hi, it's Pawan. Sorry I missed you. I was calling about the PPAP paperwork "
    "on your parts. You can reach me back on this number, or I'll try you again "
    "soon. Thanks, bye.")


# ------------------------------------------------------------- ui config --
# Everything the agent screen shows or says lives in config.yaml under
# `dialer:` and reaches the browser through /api/config. These are only the
# scalar fallbacks for keys the file leaves out.

DIALER_DEFAULTS = {
    "daily_goal": 100,
    "autodial_delay_sec": 3,
    "recording": False,          # true only if the carrier leg really records
    "agents": [],
    "pause_reasons": ["Break", "Lunch", "Meeting", "Admin / follow-ups", "Coaching"],
    "outcomes": [],
    "scripts": {},
}
REQUIRED_OUTCOMES = {"CALLBACK", "DNC"}


def load_dialer_config():
    import yaml
    with open(os.path.join(ROOT, "config.yaml")) as fh:
        raw = yaml.safe_load(fh) or {}
    cfg = json.loads(json.dumps(DIALER_DEFAULTS))          # deep copy
    for key, value in (raw.get("dialer") or {}).items():
        if value is not None:
            cfg[key] = value
    disclosure = (raw.get("compliance") or {}).get("recording_disclosure") or \
        "This call is being recorded for quality and training purposes."
    cfg["disclosure"] = " ".join(str(disclosure).split())
    comp = raw.get("compliance") or {}
    cfg["all_party_states"] = [str(s).upper() for s in (comp.get("all_party_consent_states") or [])] \
        if comp.get("hold_recording_in_all_party_states") else []
    cfg["campaign"] = (raw.get("campaign") or {}).get("name", "")
    cfg["_raw"] = raw                                       # retry / numbers / compliance, server side only

    cfg["scripts"] = dict(cfg.get("scripts") or {})
    cfg["_tree_raw"] = cfg["scripts"].get("tree") or {}       # as written in config.yaml, before in-app edits
    cfg["_emails_raw"] = dict(cfg["scripts"].get("emails") or {})
    cfg["_rule_raw"] = cfg["scripts"].get("objection_rule") or ""
    cfg["scripts"]["tree"] = resolve_tree(cfg["_tree_raw"])

    missing = REQUIRED_OUTCOMES - {o.get("key") for o in cfg["outcomes"]}
    if missing:
        raise SystemExit(f"config.yaml dialer.outcomes is missing required keys: {sorted(missing)}")
    return cfg


def resolve_tree(tree):
    """Flatten `extends:` so the browser gets complete versions. A variant
    overrides whole steps' keys, nothing deeper, which keeps A/B edits small
    and obvious."""
    out = {}

    def build(name, seen=()):
        if name in out:
            return out[name]
        node = tree.get(name) or {}
        base = {"order": {}, "steps": {}}
        parent = node.get("extends")
        if parent and parent in tree and parent not in seen:
            resolved = build(parent, seen + (name,))
            base = {"order": dict(resolved["order"]), "steps": {k: dict(v) for k, v in resolved["steps"].items()}}
        base["order"].update(node.get("order") or {})
        for step_id, step in (node.get("steps") or {}).items():
            base["steps"][step_id] = dict(base["steps"].get(step_id, {}), **(step or {}))
        out[name] = base
        return base

    for version in tree:
        build(version)
    return out


# ---- in-app script edits ---------------------------------------------------------
# config.yaml ships the scripts; the cockpit's editor lays changes over them in
# DATA_DIR/scripts.json, so a redeploy never loses wording you tuned on the
# phones, and "reset" is always one click away.

STEP_TEXT = ("title", "cue")
STEP_LISTS = ("say", "prompts", "rules", "contracts")
KNOWN_TOKENS = {"first", "last", "dm_first", "dm_name", "company", "title", "process", "oem", "agent", "city", "state",
                "email", "pain", "callback_number", "calendly", "booked_when"}


def scripts_path():
    return os.path.join(DATA_DIR or ROOT, "scripts.json")


def load_script_edits():
    try:
        with open(scripts_path()) as fh:
            edits = json.load(fh)
    except (OSError, ValueError):
        edits = {}
    edits.setdefault("tree", {})
    edits.setdefault("emails", {})
    return edits


def apply_script_edits():
    """Rebuild DIALER['scripts'] = config.yaml + the editor's changes."""
    edits = load_script_edits()
    raw = json.loads(json.dumps(DIALER.get("_tree_raw") or {}))
    for version, node in edits["tree"].items():
        target = raw.setdefault(version, {"extends": node.get("extends")})
        if node.get("extends") and version not in (DIALER.get("_tree_raw") or {}):
            target["extends"] = node["extends"]
        for step_id, fields in (node.get("steps") or {}).items():
            target.setdefault("steps", {})
            target["steps"][step_id] = dict(target["steps"].get(step_id) or {}, **fields)
    scripts = dict(DIALER.get("scripts") or {})
    scripts["tree"] = resolve_tree(raw)
    emails = {k: dict(v) for k, v in (DIALER.get("_emails_raw") or {}).items()}
    for kind, fields in edits["emails"].items():
        emails[kind] = dict(emails.get(kind) or {}, **fields)
    scripts["emails"] = emails
    scripts["objection_rule"] = edits.get("objection_rule") or DIALER.get("_rule_raw") or ""
    DIALER["scripts"] = scripts
    return edits


def script_text_problems(values):
    """(error, warnings) for text headed into a script."""
    warnings = []
    for text in values:
        if "\u2014" in text:
            return "No em dashes in scripts. Use a comma or a full stop.", []
        opens = re.findall(r"\{[?!](\w+)\}", text)
        closes = re.findall(r"\{/(\w+)\}", text)
        if sorted(opens) != sorted(closes):
            return "A {?token}...{/token} block is not closed.", []
        for token in re.findall(r"\{[?!/]?(\w+)\}", text):
            if token not in KNOWN_TOKENS and f"Unknown token {{{token}}}" not in warnings:
                warnings.append(f"Unknown token {{{token}}}")
    return None, warnings


def save_script_edit(data):
    """One editor action. Returns (error, warnings)."""
    op = data.get("op")
    edits = load_script_edits()
    tree = (DIALER.get("scripts") or {}).get("tree") or {}
    warnings = []

    if op == "step":
        version, step_id = str(data.get("version") or ""), str(data.get("id") or "")
        if version not in tree or step_id not in tree[version]["steps"]:
            return "That step does not exist.", []
        fields, incoming = {}, data.get("fields") or {}
        for key in STEP_TEXT:
            if key in incoming:
                fields[key] = str(incoming[key] or "").strip()[:600]
        for key in STEP_LISTS:
            if key in incoming:
                fields[key] = [str(x).strip()[:1500] for x in (incoming[key] or []) if str(x).strip()][:12]
        if "chips" in incoming:
            fields["chips"] = [{"q": str(c.get("q") or "").strip()[:200], "a": str(c.get("a") or "").strip()[:600]}
                               for c in (incoming["chips"] or []) if str(c.get("q") or "").strip()][:12]
        if "say" in fields and not fields["say"]:
            return "A step needs at least one line to say.", []
        texts = [v for v in fields.values() if isinstance(v, str)] + \
                [x for v in fields.values() if isinstance(v, list) for x in (v if v and isinstance(v[0], str) else [])] + \
                [c[k] for c in fields.get("chips", []) for k in ("q", "a")]
        error, warnings = script_text_problems(texts)
        if error:
            return error, []
        node = edits["tree"].setdefault(version, {})
        node.setdefault("steps", {})
        node["steps"][step_id] = dict(node["steps"].get(step_id) or {}, **fields)

    elif op == "reset_step":
        version, step_id = str(data.get("version") or ""), str(data.get("id") or "")
        ((edits["tree"].get(version) or {}).get("steps") or {}).pop(step_id, None)

    elif op == "email":
        kind = str(data.get("kind") or "")
        if kind not in (DIALER.get("_emails_raw") or {}):
            return "That email template does not exist.", []
        subject, body = str(data.get("subject") or "").strip()[:200], str(data.get("body") or "").strip()[:4000]
        if not subject or not body:
            return "An email needs a subject and a body.", []
        error, warnings = script_text_problems([subject, body])
        if error:
            return error, []
        edits["emails"][kind] = {"subject": subject, "body": body + "\n"}

    elif op == "reset_email":
        edits["emails"].pop(str(data.get("kind") or ""), None)

    elif op == "rule":
        text = str(data.get("text") or "").strip()[:600]
        error, warnings = script_text_problems([text])
        if error:
            return error, []
        edits["objection_rule"] = text

    elif op == "version":
        name = re.sub(r"[^a-z0-9_]", "", str(data.get("name") or "").lower())[:12]
        base = str(data.get("extends") or "")
        if not name or name in tree:
            return "Give the new version a short name that is not taken, like v3.", []
        if base not in tree:
            return "Pick an existing version to copy.", []
        edits["tree"][name] = {"extends": base, "steps": {}}

    elif op == "delete_version":
        name = str(data.get("name") or "")
        if name in (DIALER.get("_tree_raw") or {}):
            return "That version comes from config.yaml. Remove it there.", []
        edits["tree"].pop(name, None)

    else:
        return "Unknown editor action.", []

    os.makedirs(os.path.dirname(scripts_path()), exist_ok=True)
    with open(scripts_path(), "w") as fh:
        json.dump(edits, fh, indent=2)
    apply_script_edits()
    return None, warnings


def edited_map():
    """Which steps / emails carry in-app edits, so the editor can offer a reset."""
    edits = load_script_edits()
    return {"steps": {v: sorted((n.get("steps") or {})) for v, n in edits["tree"].items()},
            "emails": sorted(edits["emails"]), "rule": bool(edits.get("objection_rule")),
            "custom_versions": [v for v in edits["tree"] if v not in (DIALER.get("_tree_raw") or {})]}


def objections_path():
    return os.path.join(DATA_DIR or ROOT, "objections.json")


def merged_objections():
    """config.yaml objections with the in-app edits laid over them by key."""
    base = [dict(o) for o in (DIALER.get("scripts") or {}).get("objections") or []]
    try:
        with open(objections_path()) as fh:
            edits = json.load(fh)
    except (OSError, ValueError):
        edits = {}
    by_key = {o.get("key"): o for o in base}
    for key, edit in edits.items():
        if key in by_key:
            by_key[key].update(edit, edited=True)
        else:
            base.append(dict(edit, key=key, edited=True, custom=True))
    return base


def save_objection(data):
    key = re.sub(r"[^a-z0-9_]", "", str(data.get("key") or "").lower())[:40]
    title = str(data.get("title") or "").strip()[:80]
    if not key:
        key = re.sub(r"[^a-z0-9]+", "_", title.lower()).strip("_")[:40]
    if not key or not title:
        return "An objection needs a title."
    fields = {k: str(data.get(k) or "").strip()[:600] for k in ("title", "anchor", "disrupt", "question", "note", "tag")}
    if any("\u2014" in v for v in fields.values()):
        return "No em dashes in scripts. Use a comma or a full stop."
    try:
        with open(objections_path()) as fh:
            edits = json.load(fh)
    except (OSError, ValueError):
        edits = {}
    edits[key] = fields
    os.makedirs(os.path.dirname(objections_path()), exist_ok=True)
    with open(objections_path(), "w") as fh:
        json.dump(edits, fh, indent=2)
    return None


def fire_booking_webhook(dispo_id, lead, booked_for_utc, booked_for_local, extra, agent):
    """Tell the outside world a call was booked, without holding up the save."""
    url = DIALER.get("booking_webhook_url")
    if not url:
        return "off"
    payload = {
        "event": "call_booked", "disposition_id": dispo_id, "agent": agent,
        "booked_for_utc": booked_for_utc, "booked_for_local": booked_for_local,
        "timezone": (lead or {}).get("tz_name") or "", "calendly_url": DIALER.get("calendly_url") or "",
        "lead": {k: (lead or {}).get(k) or "" for k in ("phone", "company", "first", "last", "title", "city", "state",
                                                         "website", "linkedin_url", "process", "oem", "employees")},
        "dm_name": extra.get("dm_name") or "", "email": extra.get("email") or "", "mobile": extra.get("mobile") or "",
        "pain": extra.get("pain") or "", "script_version": extra.get("script_version") or "",
    }

    def post():
        body = json.dumps(payload).encode()
        status, reply = 0, ""
        try:
            req = urllib.request.Request(url, data=body, method="POST", headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=10, context=_ssl_context()) as resp:
                status, reply = resp.status, resp.read(300).decode("utf-8", "replace")
        except Exception as e:                                   # logged, never raised: the booking is already saved
            reply = str(e)[:300]
        db.log_webhook(url, body.decode(), status, reply)
        print(f"  WEBHOOK      booking {dispo_id} -> {status or 'failed'} {reply[:60]}")

    threading.Thread(target=post, daemon=True).start()
    return "queued"


def public_config():
    """What the browser may see: everything except the raw file."""
    out = {k: v for k, v in DIALER.items() if not k.startswith("_") and k != "booking_webhook_url"}
    out["scripts"] = dict(out.get("scripts") or {}, objections=merged_objections())
    out["script_versions"] = script_versions()
    out["scripts_edited"] = edited_map()
    out["booking_webhook"] = bool(DIALER.get("booking_webhook_url"))
    return out


DIALER = dict(DIALER_DEFAULTS)


def number_tz(phone):
    """Best-effort (utc_offset_hours, iana_zone) for a hand-typed number, from
    its area code. (None, "") when phonenumbers is missing or it is ambiguous."""
    try:
        import phonenumbers
        from phonenumbers import timezone as pntz
        from zoneinfo import ZoneInfo
        zones = pntz.time_zones_for_number(phonenumbers.parse(phone, None))
        if not zones or zones[0] == "Etc/Unknown":
            return None, ""
        off = datetime.now(ZoneInfo(zones[0])).utcoffset()
        return (off.total_seconds() / 3600.0 if off is not None else None), zones[0]
    except Exception:
        return None, ""


def clean_phone(raw):
    """Normalise whatever the agent typed to NANP E.164, or ''."""
    digits = re.sub(r"\D", "", raw or "")
    if len(digits) == 10:
        digits = "1" + digits
    return "+" + digits if re.fullmatch(r"1[2-9]\d{9}", digits) else ""


def script_versions():
    tree = (DIALER.get("scripts") or {}).get("tree") or {}
    return list(tree) or [DIALER.get("default_script_version") or "v1"]


def objection_labels():
    return {o["key"]: o.get("label", o["key"]) for o in DIALER.get("objection_tags") or []}


def local_to_utc(local_stamp, lead):
    """'YYYY-MM-DDTHH:MM' on the prospect's wall clock -> UTC stamp, using the
    lead's zone so a booking across the clock change still lands right."""
    try:
        naive = datetime.fromisoformat(str(local_stamp).replace(" ", "T")[:16])
    except (TypeError, ValueError):
        return None
    name = (lead or {}).get("tz_name") or ""
    if name:
        try:
            from zoneinfo import ZoneInfo
            return naive.replace(tzinfo=ZoneInfo(name)).astimezone(timezone.utc).replace(tzinfo=None).isoformat(sep=" ")
        except Exception:
            pass
    return (naive - timedelta(hours=db.lead_offset(lead or {"tz_name": "", "tz_offset": -5}))).isoformat(sep=" ")


def clean_when(raw):
    """Accept 'YYYY-MM-DD HH:MM[:SS]' (UTC) only; anything else is None."""
    try:
        return datetime.fromisoformat(str(raw).replace("T", " ")[:19]).isoformat(sep=" ")
    except (TypeError, ValueError):
        return None

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
    """Voice access token: plain HS256 JWT, no SDK dependency. None when
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
    """Recent inbound voicemails. Outbound call recordings (started by
    /api/record/start) are skipped. Cached for 60s; caller numbers resolved per call."""
    if time.time() - VM_CACHE["at"] < 60:
        return VM_CACHE["items"]
    out = []
    data = twilio_rest("Recordings.json", {"PageSize": 12})
    for rec in (data or {}).get("recordings", []):
        if rec.get("source") == "StartCallRecordingAPI":
            continue                              # our own outbound call recordings, not voicemail
        call_sid = rec["call_sid"]
        caller = VM_CACHE["callers"].get(call_sid)
        if caller is None:
            try:
                call = twilio_rest(f"Calls/{call_sid}.json")
                caller = call.get("from_formatted") or call.get("from") or "?"
                if call.get("direction") != "inbound" or str(call.get("from") or "").startswith("client:"):
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
            "phone": re.sub(r"[^+\d]", "", caller) if caller != "?" else "",
            "company": (lead or {}).get("company", ""),
            "duration": int(rec.get("duration") or 0),
            "at": rec.get("date_created", ""),
        })
    VM_CACHE["items"], VM_CACHE["at"] = out, time.time()
    return out


MISSED_CACHE = {"at": 0, "items": []}


def list_missed():
    """Inbound callers from the last 48 h that nobody has spoken to since:
    no disposition logged on that number after the call came in. Survives
    reloads and covers calls that rang while the tab was closed."""
    if time.time() - MISSED_CACHE["at"] < 60:
        return MISSED_CACHE["items"]
    from email.utils import parsedate_to_datetime
    number = re.sub(r"[^+\d]", "", CONFIG["caller_id"])
    data = twilio_rest("Calls.json", {"To": number, "PageSize": 40})
    cutoff = db.now() - timedelta(hours=48)
    seen, out = set(), []
    for call in (data or {}).get("calls", []):
        if call.get("direction") != "inbound":
            continue
        caller = re.sub(r"[^+\d]", "", call.get("from") or "")
        if not caller or caller in seen:
            continue
        try:
            at = parsedate_to_datetime(call.get("start_time") or call.get("date_created"))
            at = at.astimezone(timezone.utc).replace(tzinfo=None)
        except (TypeError, ValueError):
            continue
        if at < cutoff:
            continue
        seen.add(caller)                       # newest call per caller wins
        last = db.last_contact_at(caller)
        if last and last >= db.iso(at):
            continue                           # already handled
        lead = db.lookup(caller)
        out.append({
            "phone": caller,
            "company": (lead or {}).get("company", ""),
            "name": (((lead or {}).get("first", "") + " " + (lead or {}).get("last", "")).strip()),
            "at": db.iso(at),
            "dnc": bool(lead and lead.get("status") == "DNC"),
        })
    MISSED_CACHE["items"], MISSED_CACHE["at"] = out, time.time()
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


def _list_id_of(path):
    try:
        with open(path, newline="", encoding="utf-8-sig") as fh:
            import csv as _csv
            row = next(_csv.DictReader(fh), None)
            return str((row or {}).get("list_id") or "")
    except OSError:
        return ""


def newest_list_file(list_id=None):
    """Newest prepped list. Prefers the dialer CSV (<campaign>_list_<date>.csv)
    and falls back to VICIdial load files from before it existed."""
    tag = (DIALER.get("campaign") or "ppap").lower()
    found = []
    for d in out_dirs():
        found += glob.glob(os.path.join(d, f"{tag}_list_*.csv"))
    found = [p for p in found if not list_id or _list_id_of(p) == str(list_id)]
    if not found:
        patterns = [f"vicidial_*_list{list_id}_*.csv"] if list_id else \
            ["vicidial_*_agent1_list*_*.csv", "vicidial_*_direct_*.csv"]
        for d in out_dirs():
            for p in patterns:
                found += glob.glob(os.path.join(d, p))
        if list_id:
            found += [p for d in out_dirs() for p in glob.glob(os.path.join(d, "vicidial_*_direct_*.csv"))
                      if _list_id_of(p) == str(list_id)]
    return max(found, key=os.path.getmtime) if found else None


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


def run_listprep(src, outdir, campaign_id=None, campaign_name=None, original_name=None):
    """Prep an uploaded file and import the result into a campaign (a new
    one named campaign_name when campaign_id is None). Returns (result, err)."""
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
    tag = (DIALER.get("campaign") or "ppap").lower()
    produced = glob.glob(os.path.join(outdir, f"{tag}_list_*.csv")) or glob.glob(os.path.join(outdir, "vicidial_*_direct_*.csv"))
    newest = max(produced, key=os.path.getmtime) if produced else None
    if not newest:
        return None, ("prep produced no dialable list", tail)
    if not campaign_id:
        campaign_id = db.create_campaign(campaign_name or db.campaign_name_for(original_name or newest),
                                         DIALER.get("default_script_version") or "")
    added, refreshed = db.import_list_csv(newest, campaign_id, original_name or os.path.basename(newest))
    db.use_campaign(campaign_id)
    return {"ok": True, "added": added, "refreshed": refreshed, "campaign_id": campaign_id,
            "in_campaign": db.target_status()["open"],
            "source": os.path.basename(newest), "log": tail}, None


# ---------------------------------------------------------------- server --

def lead_payload(lead):
    if not lead:
        return None
    offset = db.lead_offset(lead)
    local = db.now() + timedelta(hours=offset)
    return {
        "phone": lead["phone"],
        "first": lead["first"], "last": lead["last"], "co": lead["company"],
        "title": lead["title"], "city": lead["city"], "state": lead["state"],
        "size": lead.get("employees") or "", "process": lead.get("process") or "",
        "oem": lead.get("oem") or "", "industry": lead.get("industry") or "",
        "website": lead.get("website") or "", "linkedin_url": lead.get("linkedin_url") or "",
        "li_status": lead.get("li_status") or "", "email": lead.get("email") or "",
        "mobile": lead.get("mobile") or "", "is_mobile": bool(lead.get("is_mobile")),
        "source": lead.get("source") or "", "dm_name": lead.get("dm_name") or "",
        "gatekeeper_name": lead.get("gatekeeper_name") or "",
        "lead_notes": lead.get("lead_notes") or "",
        "rank": lead["rank"], "attempts": lead["attempts"],
        "tz_offset": offset, "tz_name": lead.get("tz_name") or "",
        "last_disposition": lead["last_disposition"],
        "callback_at": lead["callback_at"],
        "local_time": local.strftime("%-I:%M%p").lower(),
        "in_window": db.in_hard(lead),
        "tier": db.lead_tier(lead),
        "zone": policy.zone_label(lead.get("tz_name") or "", offset),
        "attempt_no": (lead["attempts"] or 0) + 1,
        "vm_allowed": policy.voicemail_allowed((lead["attempts"] or 0) + 1, db.RETRY),
        "caller_id": lead.get("_caller_id") or "",
        "caller_id_reason": lead.get("_caller_reason") or "",
        "caller_id_spoken": policy.spoken(lead.get("_caller_id") or CONFIG["caller_id"]),
        "pain": lead.get("pain") or "", "ppap_per_year": lead.get("ppap_per_year") or "",
        "tags": [x for x in (lead.get("tags") or "").split(",") if x],
        "list_id": lead.get("list_id") or "",
        "status": lead.get("status") or "",
        "history": db.history(lead["phone"]),
    }


STATIC = {
    "/twilio.min.js": "application/javascript",
    "/app.css": "text/css; charset=utf-8",
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

        # Login link: /?key=<password> sets the cookie: friendlier than the
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

    def _use_campaign(self, data=None):
        """Which campaign this request is about: the X-Campaign header the
        cockpit sends on every call, ?campaign= on download links, or none (all)."""
        raw = self.headers.get("X-Campaign") or \
            (urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query).get("campaign") or [""])[0] or \
            (data or {}).get("campaign")
        db.use_campaign(raw)

    def do_GET(self):
        if not self._authed():
            return
        route = urllib.parse.urlparse(self.path).path
        self._use_campaign()

        if route in ("/", "/index.html"):
            with open(os.path.join(HERE, "index.html"), "rb") as fh:
                page = fh.read()
            if not page.lstrip().lower().startswith(b"<!doctype"):
                page = (b"<!doctype html><html><head><meta charset='utf-8'>"
                        b"<meta name='viewport' content='width=device-width,initial-scale=1'>"
                        b"</head><body>" + page + b"</body></html>")
            return self._send(200, page, "text/html; charset=utf-8")

        if route in STATIC:
            with open(os.path.join(HERE, route.lstrip("/")), "rb") as fh:
                return self._send(200, fh.read(), STATIC[route])

        if re.fullmatch(r"/js/[a-z_]+\.js", route):       # cockpit ES modules
            path = os.path.join(HERE, route.lstrip("/"))
            if os.path.exists(path):
                with open(path, "rb") as fh:
                    return self._send(200, fh.read(), "application/javascript; charset=utf-8")
            return self._json({"error": "not found"}, 404)

        query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)

        if route == "/api/config":
            return self._json(dict(public_config(), caller_id=CONFIG["caller_id"],
                                   live=bool(twilio_token("probe")),
                                   windows={"hard": [policy.hhmm(x) for x in db.WINDOWS["hard"]],
                                            "power": db.WINDOWS["power"], "secondary": db.WINDOWS["secondary"],
                                            "enforced": db.ENFORCE_WINDOWS,
                                            "weekdays_only": not db.WINDOWS.get("weekends")},
                                   max_attempts=db.MAX_ATTEMPTS,
                                   voicemail_attempts=db.RETRY.get("voicemail_attempts"),
                                   script_versions=script_versions(),
                                   session_defaults=DIALER.get("session") or {},
                                   daily_target=db.DAILY_TARGET))

        if route == "/api/leads":
            return self._json({"leads": db.lead_list((query.get("q") or [""])[0][:80])})

        if route == "/api/campaigns":
            return self._json({"campaigns": db.campaigns_list(), "current": db.current_campaign()})

        if route == "/api/target":
            return self._json(db.target_status())

        if route == "/api/analytics":
            return self._json(db.analytics((query.get("period") or ["day"])[0],
                                           (query.get("count") or [""])[0] or None))

        if route == "/api/callbacks":
            return self._json({"callbacks": db.callbacks_list()})

        if route == "/api/calls":
            mine = (query.get("mine") or ["1"])[0] != "0"
            return self._json({"calls": db.calls_today(self._agent() if mine else None)})

        if route == "/api/history":
            phone = clean_phone((query.get("phone") or [""])[0])
            return self._json({"history": db.history(phone, 100) if phone else []})

        if route == "/api/token":
            token = twilio_token(self._agent())
            return self._json({"token": token} if token else {"sim": True})

        if route == "/api/next":
            lead, reason = db.checkout(self._agent())
            return self._json({
                "lead": lead_payload(lead),
                "reason": reason,
                "stats": db.stats(self._agent()),
                "caller_id": CONFIG["caller_id"],
            })

        if route == "/api/stats":
            since = clean_when((query.get("since") or [""])[0]) if query.get("since") else None
            return self._json(db.stats(self._agent(), (query.get("range") or ["today"])[0],
                                       (query.get("script") or [""])[0][:24] or None, since,
                                       objection_labels()))

        if route == "/api/funnel.csv":
            import csv as _csv
            import io
            buf = io.StringIO()
            writer = _csv.writer(buf)
            writer.writerow(funnel.SHEET_COLUMNS)
            writer.writerows(db.funnel_sheet((query.get("range") or ["all"])[0], None, objection_labels()))
            self.send_response(200)
            payload = buf.getvalue().encode("utf-8")
            self.send_header("Content-Type", "text/csv; charset=utf-8")
            self.send_header("Content-Disposition", 'attachment; filename="imperium_tracker.csv"')
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return

        if route == "/api/numbers":
            return self._json({"numbers": db.numbers_health(),
                               "spam_check_urls": (DIALER["_raw"].get("numbers") or {}).get("spam_check_urls") or []})

        if route == "/api/exhausted.csv":
            path, _ = db.export_exhausted()
            with open(path, "rb") as fh:
                return self._send(200, fh.read(), "text/csv")

        if route == "/api/bookings":
            return self._json({"bookings": db.bookings()})

        if route == "/api/lookup":
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            phone = re.sub(r"[^+\d]", "", (q.get("phone") or [""])[0])
            return self._json({"lead": lead_payload(db.lookup(phone))})

        if route == "/api/voicemails":
            try:
                return self._json({"voicemails": list_voicemails()})
            except Exception as e:
                return self._json({"voicemails": [], "error": str(e)})

        if route == "/api/missed":
            try:
                return self._json({"missed": list_missed()})
            except Exception as e:
                return self._json({"missed": [], "error": str(e)})

        if route.startswith("/api/recording/"):
            if not DIALER.get("recording"):
                return self._json({"error": "Recording is off on this server."}, 403)
            sid = route.rsplit("/", 1)[-1].removesuffix(".mp3")
            if not re.fullmatch(r"RE[0-9a-f]{32}", sid):
                return self._json({"error": "not found"}, 404)
            try:
                audio = twilio_rest(f"Recordings/{sid}.mp3")
                return self._send(200, audio, "audio/mpeg")
            except Exception:
                return self._json({"error": "recording unavailable (still processing, or deleted)"}, 404)

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
        self._use_campaign()

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
            if code not in {o["key"] for o in DIALER["outcomes"]}:
                return self._json({"error": f"unknown disposition {code}"}, 400)
            when = clean_when(data.get("callback_at")) if data.get("callback_at") else None
            if code == "CALLBACK" and not when:
                return self._json({"error": "callback needs a valid time"}, 400)
            extra = {k: data.get(k) for k in ("objections", "offered", "pain", "script_version",
                                              "number_used", "session_id", "dm_name", "email",
                                              "mobile", "ppap_per_year", "oem", "gatekeeper_name")}
            extra["call_sid"] = data.get("call_sid") if re.fullmatch(r"CA[0-9a-f]{32}", str(data.get("call_sid") or "")) else ""
            extra["recording_sid"] = data.get("recording_sid") if re.fullmatch(r"RE[0-9a-f]{32}", str(data.get("recording_sid") or "")) else ""
            if not isinstance(extra["objections"], list):
                extra["objections"] = []
            if (db.OUTCOMES.get(code) or {}).get("booked"):
                lead = db.lookup(phone)
                email = str(data.get("email") or "").strip()
                if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
                    return self._json({"error": "A booked call needs their email for the invite."}, 400)
                extra["booked_for"] = local_to_utc(data.get("booked_for_local"), lead)
                if not extra["booked_for"]:
                    return self._json({"error": "A booked call needs a date and time."}, 400)
                if extra["booked_for"] <= db.iso(db.now()):
                    return self._json({"error": "The booked time is in the past."}, 400)
            dispo_id = db.disposition(phone, data.get("company", ""), code,
                                      (data.get("notes") or "")[:2000], self._agent(data),
                                      int(data.get("duration") or 0), callback_at=when, extra=extra)
            webhook = None
            if extra.get("booked_for"):
                webhook = fire_booking_webhook(dispo_id, lead, extra["booked_for"], data.get("booked_for_local"),
                                               extra, self._agent(data))
            print(f"  {code:12s} {phone}  {data.get('company', '')}"
                  + (f"  [{(data.get('notes') or '')[:60]}]" if data.get("notes") else ""))
            return self._json({"ok": True, "id": dispo_id, "webhook": webhook,
                               "stats": db.stats(self._agent(data))})

        if route == "/api/undo":
            lead, notes, reason = db.undo(int(data.get("id") or 0), self._agent(data))
            if reason:
                return self._json({"error": reason}, 409)
            print(f"  UNDO         {lead['phone']}  {lead['company']}")
            return self._json({"ok": True, "lead": lead_payload(lead), "notes": notes,
                               "stats": db.stats(self._agent(data))})

        if route in ("/api/checkout", "/api/manual"):
            phone = clean_phone(data.get("phone", ""))
            if not phone:
                return self._json({"error": "Enter a 10-digit US or Canadian number."}, 400)
            tz, tz_name = number_tz(phone) if route == "/api/manual" else (None, "")
            lead, reason = db.checkout_specific(phone, self._agent(data), tz_offset=tz, tz_name=tz_name,
                                                returning=bool(data.get("returning")))
            if reason:
                return self._json({"error": reason}, 409)
            return self._json({"ok": True, "lead": lead_payload(lead),
                               "tz_known": route != "/api/manual" or tz is not None,
                               "stats": db.stats(self._agent(data))})

        if route == "/api/reschedule":
            phone = clean_phone(data.get("phone", ""))
            when = clean_when(data.get("callback_at")) if data.get("callback_at") else None
            if not phone or (data.get("callback_at") and not when):
                return self._json({"error": "phone and a valid time required"}, 400)
            if not db.reschedule(phone, when):
                return self._json({"error": "lead not found"}, 404)
            return self._json({"ok": True, "callbacks": db.callbacks_list(),
                               "stats": db.stats(self._agent(data))})

        if route == "/api/scripts":
            error, warnings = save_script_edit(data)
            if error:
                return self._json({"error": error}, 400)
            cfg = public_config()
            return self._json({"ok": True, "warnings": warnings, "scripts": cfg["scripts"],
                               "script_versions": cfg["script_versions"], "scripts_edited": cfg["scripts_edited"]})

        if route == "/api/objections":
            error = save_objection(data)
            if error:
                return self._json({"error": error}, 400)
            return self._json({"ok": True, "objections": merged_objections()})

        if route == "/api/session/start":
            versions = script_versions()
            version = data.get("script_version") if data.get("script_version") in versions else versions[0]
            sid = db.session_start(self._agent(data), version, data.get("target_dials"), data.get("target_minutes"))
            return self._json({"ok": True, "id": sid, "script_version": version})

        if route == "/api/session/end":
            return self._json(dict(db.session_end(int(data.get("id") or 0), data.get("active_seconds")), ok=True))

        if route == "/api/record/start":
            sid = str(data.get("call_sid") or "")
            if not re.fullmatch(r"CA[0-9a-f]{32}", sid):
                return self._json({"error": "No live call to record."}, 400)
            if not DIALER.get("recording"):
                return self._json({"error": "Recording is off (dialer.recording in config.yaml)."}, 400)
            try:
                rec = twilio_rest(f"Calls/{sid}/Recordings.json",
                                  {"RecordingChannels": "dual", "RecordingTrack": "both"}, "POST")
            except Exception as e:
                return self._json({"error": f"Twilio would not start the recording: {e}"}, 502)
            if not isinstance(rec, dict) or not rec.get("sid"):
                return self._json({"error": "Twilio is not configured on this server."}, 502)
            print(f"  RECORD    {sid} -> {rec['sid']}")
            return self._json({"ok": True, "recording_sid": rec["sid"]})

        if route == "/api/campaigns/new":
            versions = script_versions()
            script = data.get("script_version") if data.get("script_version") in versions else ""
            cid = db.create_campaign(data.get("name"), script, data.get("daily_target") or 0)
            return self._json({"ok": True, "id": cid, "campaigns": db.campaigns_list()})

        if route == "/api/campaigns/update":
            if data.get("script_version") and data["script_version"] not in script_versions():
                return self._json({"error": "Unknown script version."}, 400)
            db.update_campaign(int(data.get("id") or 0), data)
            return self._json({"ok": True, "campaigns": db.campaigns_list()})

        if route == "/api/campaigns/delete":
            gone = db.delete_campaign(int(data.get("id") or 0))
            return self._json({"ok": True, "deleted_leads": gone, "campaigns": db.campaigns_list()})

        if route in ("/api/leads/delete", "/api/leads/restore", "/api/leads/remove"):
            phones = [clean_phone(p) for p in (data.get("phones") or []) if clean_phone(p)][:2000]
            if route == "/api/leads/delete":
                n = db.delete_leads(phones, self._agent(data))
            elif route == "/api/leads/restore":
                n = db.restore_leads(phones)
            else:
                n = db.remove_from_campaign(phones, db.current_campaign())
            return self._json({"ok": True, "count": n, "phones": phones})

        if route == "/api/numbers/park":
            number = "+" + re.sub(r"\D", "", str(data.get("number") or ""))
            db.set_parked(number, bool(data.get("parked")), "manual")
            return self._json({"ok": True, "numbers": db.numbers_health()})

        if route == "/api/followthrough":
            error = db.follow_through(int(data.get("id") or 0), data.get("show_status"),
                                      data.get("sale"), data.get("sale_amount"))
            if error:
                return self._json({"error": error}, 400)
            return self._json({"ok": True, "bookings": db.bookings(),
                               "stats": db.stats(self._agent(data), labels=objection_labels())})

        if route == "/api/agent-event":
            event = re.sub(r"[^A-Z_]", "", str(data.get("event", "")).upper())
            if event:
                db.agent_event(self._agent(data), event, str(data.get("reason") or ""))
            return self._json({"ok": True})

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

        # Into an existing campaign (X-Campaign), or a new one named by
        # X-Campaign-Name, or after the file.
        new_name = urllib.parse.unquote(self.headers.get("X-Campaign-Name") or "").strip()
        campaign_id = None if new_name else db.current_campaign()
        result, err = run_listprep(src, outdir, campaign_id, new_name or db.campaign_name_for(name), name)
        if err:
            return self._json({"error": err[0], "log": err[1]}, 422)
        result["stats"] = db.stats()
        print(f"  UPLOAD    {name}: +{result['added']} new, "
              f"{result['refreshed']} refreshed -> {result['source']}")
        return self._json(result)

    def log_message(self, *args):
        pass


def ensure_deps():
    """config.yaml holds the whole script tree, so PyYAML is required. A system
    Python without it hops into ROOT/.venv when one exists, which keeps
    `python3.12 dialer/serve.py` working on a Mac with a bare Homebrew Python."""
    try:
        import yaml  # noqa: F401
        return
    except ImportError:
        pass
    venv_python = os.path.join(ROOT, ".venv", "bin", "python")
    inside = os.path.realpath(sys.prefix) == os.path.realpath(os.path.join(ROOT, ".venv"))
    if os.path.exists(venv_python) and not inside and not os.environ.get("PD_NO_VENV_HOP"):
        print("  deps      PyYAML missing here, re-running inside .venv")
        os.environ["PD_NO_VENV_HOP"] = "1"
        os.execv(venv_python, [venv_python] + sys.argv)
    sys.exit("PyYAML is required. Run:  uv venv .venv && uv pip install --python .venv/bin/python -r requirements.txt\n"
             "(or: python3.12 -m pip install -r requirements.txt)")


def configure_policy_from(raw, enforce):
    db.configure_policy(windows=DIALER.get("windows"), retry=raw.get("retry"), numbers=raw.get("numbers"),
                        pool=(raw.get("numbers") or {}).get("pool"), enforce_windows=enforce,
                        allow_mobile=bool((raw.get("compliance") or {}).get("allow_mobile")),
                        scrub_file=(raw.get("compliance") or {}).get("dnc_scrub_file"))


def main():
    ensure_deps()
    load_dotenv()
    global VM_DROP_TEXT
    VM_DROP_TEXT = os.environ.get("VM_DROP_TEXT", VM_DROP_TEXT)
    DIALER.clear()
    DIALER.update(load_dialer_config())
    apply_script_edits()
    db.configure(DIALER["outcomes"], DIALER.get("stats_timezone"), objection_labels())
    db.DAILY_TARGET = int(DIALER.get("daily_target") or 150)
    migrate()

    parser = argparse.ArgumentParser(description="Run the dialer.")
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", 8765)))
    parser.add_argument("--host", default=os.environ.get("HOST", "127.0.0.1"))
    parser.add_argument("--caller-id",
                        default=os.environ.get("TWILIO_CALLER_ID", CONFIG["caller_id"]))
    parser.add_argument("--no-open", action="store_true")
    parser.add_argument("--list", dest="list_id", metavar="ID",
                        help="import the newest prepped list with this list id before serving")
    parser.add_argument("--strict-windows", action="store_true",
                        help="simulator only: enforce calling windows anyway, to watch the ET to PT rotation")
    args = parser.parse_args()
    CONFIG["caller_id"] = args.caller_id

    # Policy. With no carrier credentials nothing real is dialed, so the
    # simulator leaves the calling windows open unless asked not to. With
    # credentials the windows are ALWAYS enforced; there is no switch for that.
    raw = DIALER["_raw"]
    simulator = not twilio_token("probe")
    enforce = (not simulator) or args.strict_windows or os.environ.get("STRICT_WINDOWS") == "1"
    db.FALLBACK_CALLER_ID = "+" + re.sub(r"\D", "", args.caller_id)
    try:
        configure_policy_from(raw, enforce)
    except ValueError as e:
        sys.exit(f"config.yaml dialer.windows refused: {e}")

    if args.list_id:
        path = newest_list_file(args.list_id)
        if path:
            added, refreshed = db.import_list_csv(path)
            print(f"  list      {os.path.basename(path)}: +{added} new, {refreshed} refreshed")
        else:
            print(f"  list      no prepped file for list id {args.list_id} under out/ (run listprep.py first)")

    if simulator and db.lead_count() == 0:
        added, _ = db.import_list_csv(os.path.join(HERE, "demo_leads.csv"))
        print(f"  demo      empty database in simulator mode: seeded {added} sample shops")

    s = db.stats()
    w = s["window"]
    print(f"  db        {db.DB_PATH}")
    print(f"  queue     {s['queue']} due, {s['retry_pool']} waiting on the retry cadence, "
          f"{s['callbacks_due']} callbacks due in 24h, {s['exhausted']} exhausted (email only)")
    print(f"  windows   {'enforced' if enforce else 'NOT enforced (simulator)'}: "
          + (f"{w['tier']} window open, {w['eligible']} leads eligible" if w["open"]
             else f"closed, next {w.get('next_zone') or '-'} {w.get('next_local') or ''}"))
    for n in db.numbers_health():
        print(f"  caller ID {n['number']}  {n['used_today']}/{n['cap']} today"
              + (f"  warm-up day {n['warmup_day']}/{n['warmup_days']}" if n["warming"] else "")
              + ("  PARKED" if n["parked"] else ""))
    scrub = (raw.get("compliance") or {}).get("dnc_scrub_file")
    print(f"  dnc       internal list + " + (f"scrub file {scrub} ({len(db.scrub_numbers())} numbers)" if scrub else "no national scrub file")
          + f"; mobiles {'ALLOWED' if db.ALLOW_MOBILE else 'blocked'}")
    print(f"  carrier   {'twilio credentials found: real calls' if not simulator else 'not configured: simulator mode (see TWILIO.md)'}")
    print(f"  auth      {'basic auth on' if os.environ.get('DIALER_PASSWORD') else 'OFF: local use only'}")
    if DIALER.get("recording") and args.host not in ("127.0.0.1", "localhost", "::1") and not os.environ.get("DIALER_PASSWORD"):
        DIALER["recording"] = False      # never record (or serve recordings) on a public server anyone can open
        print("  recording OFF: this server is public and DIALER_PASSWORD is not set. Set it to turn recording on.")
    print(f"  serving   {args.host}:{args.port}\n")

    if not args.no_open and args.host in ("127.0.0.1", "localhost"):
        webbrowser.open(f"http://localhost:{args.port}/")
    try:
        ThreadingHTTPServer((args.host, args.port), Handler).serve_forever()
    except KeyboardInterrupt:
        print("\n  stopped")


if __name__ == "__main__":
    main()
