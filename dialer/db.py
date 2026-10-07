"""SQLite state for the dialer.

One file on the volume (DATA_DIR/dialer.db, or repo root locally) owns what
the CSVs used to hold, plus what they couldn't: atomic lead checkout so two
agents never dial the same number, retry scheduling, callbacks, per-day dial
caps computed from actual history, and notes.

Policy constants mirror config.yaml (listprep reads the YAML; the dialer
reads these): change both or wire yaml here if they drift.
"""

import csv
import json
import os
import sqlite3
import threading

import funnel
import policy
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.environ.get("DATA_DIR")
DB_PATH = os.path.join(DATA_DIR or ROOT, "dialer.db")

CHECKOUT_TTL_MIN = 10

# Policy, filled from config.yaml by configure_policy(). See dialer/policy.py.
WINDOWS = dict(policy.DEFAULT_WINDOWS)
RETRY = dict(policy.DEFAULT_RETRY)
NUMBERS = dict(policy.DEFAULT_NUMBERS)
POOL = []                          # [{number, area_code, state, warmup_start?}]
ENFORCE_WINDOWS = True             # off only in simulator mode: no real call is placed
ALLOW_MOBILE = False
MAX_ATTEMPTS = RETRY["max_attempts"]
DAILY_CAP = NUMBERS["max_dials_per_number_per_day"]
_rr_cursor = 0

RETRYABLE = {"NO_ANSWER", "VOICEMAIL", "BUSY", "GATEKEEPER"}
FINAL = {"INTERESTED", "NOT_INT", "WRONG_NUMBER", "DISCONNECTED", "DNC"}
# "Connect" = a human picked up, whoever it was.
CONNECTED = {"INTERESTED", "NOT_INT", "CALLBACK", "DNC", "GATEKEEPER", "WRONG_NUMBER"}
UNDO_WINDOW_SEC = 120


OUTCOMES = {}                      # key -> outcome definition from config.yaml
STATS_TZ = "America/New_York"      # the day that stats, caps and same-day rules run on


OBJECTION_LABELS = {}


def configure(outcomes, stats_tz=None, objection_labels=None):
    """Let serve.py drive retry/connect semantics from config.yaml's
    dialer.outcomes so the UI and the queue can never disagree."""
    global RETRYABLE, FINAL, CONNECTED, OUTCOMES, STATS_TZ, OBJECTION_LABELS
    OBJECTION_LABELS = objection_labels or OBJECTION_LABELS
    if stats_tz:
        STATS_TZ = stats_tz
    if not outcomes:
        return
    OUTCOMES = {o["key"]: o for o in outcomes}
    RETRYABLE = {o["key"] for o in outcomes if o.get("kind") == "retry"}
    FINAL = {o["key"] for o in outcomes if o.get("kind") in ("final", "dnc")}
    CONNECTED = {o["key"] for o in outcomes if o.get("connect")}


SCRUB_FILE = None
_scrub = {"mtime": None, "numbers": set()}


def scrub_numbers():
    """The national DNC scrub set, reloaded whenever the file changes."""
    if not SCRUB_FILE or not os.path.exists(SCRUB_FILE):
        return set()
    mtime = os.path.getmtime(SCRUB_FILE)
    if mtime != _scrub["mtime"]:
        _scrub["numbers"], _scrub["mtime"] = policy.load_number_file(SCRUB_FILE), mtime
    return _scrub["numbers"]


def _scrubbed(con, lead):
    """True when this lead is on the scrub list; it is closed out on the spot."""
    if lead["phone"] not in scrub_numbers():
        return False
    con.execute("INSERT OR REPLACE INTO dnc VALUES (?,?,?,?)", (lead["phone"], "national_dnc", iso(now()), "scrub"))
    con.execute("UPDATE leads SET status='DNC', last_disposition='DNC', checked_out_by=NULL WHERE phone=?", (lead["phone"],))
    return True


def configure_policy(windows=None, retry=None, numbers=None, pool=None, enforce_windows=True, allow_mobile=False,
                     scrub_file=None):
    global WINDOWS, RETRY, NUMBERS, POOL, ENFORCE_WINDOWS, ALLOW_MOBILE, MAX_ATTEMPTS, DAILY_CAP, SCRUB_FILE
    SCRUB_FILE = (scrub_file if os.path.isabs(scrub_file) else os.path.join(ROOT, scrub_file)) if scrub_file else None
    _scrub["mtime"] = None
    WINDOWS = policy.check_windows(dict(policy.DEFAULT_WINDOWS, **(windows or {})))
    RETRY = dict(policy.DEFAULT_RETRY, **{k: v for k, v in (retry or {}).items() if k in policy.DEFAULT_RETRY})
    NUMBERS = dict(policy.DEFAULT_NUMBERS, **{k: v for k, v in (numbers or {}).items() if k in policy.DEFAULT_NUMBERS})
    POOL = []
    for entry in pool or []:
        number = "+" + "".join(ch for ch in str(entry.get("number", "")) if ch.isdigit())
        if len(number) < 11:
            continue
        start = entry.get("warmup_start")
        if isinstance(start, str):
            try:
                start = datetime.fromisoformat(start).date()
            except ValueError:
                start = None
        POOL.append({"number": number, "area_code": str(entry.get("area_code") or policy.area_code(number)),
                     "state": str(entry.get("state") or "").upper(), "warmup_start": start,
                     "label": entry.get("label") or ""})
    ENFORCE_WINDOWS, ALLOW_MOBILE = bool(enforce_windows), bool(allow_mobile)
    MAX_ATTEMPTS = RETRY["max_attempts"]
    DAILY_CAP = NUMBERS["max_dials_per_number_per_day"]


def day_start(at=None):
    """UTC stamp where the current stats day began."""
    return iso(funnel.range_start("today", at or now(), STATS_TZ))

SCHEMA = """
CREATE TABLE IF NOT EXISTS leads (
  id INTEGER PRIMARY KEY,
  phone TEXT UNIQUE NOT NULL,
  first TEXT DEFAULT '', last TEXT DEFAULT '', company TEXT DEFAULT '',
  title TEXT DEFAULT '', city TEXT DEFAULT '', state TEXT DEFAULT '',
  tz_offset REAL DEFAULT -5, tz_name TEXT DEFAULT '',
  rank INTEGER DEFAULT 0,
  employees TEXT DEFAULT '', process TEXT DEFAULT '', oem TEXT DEFAULT '',
  industry TEXT DEFAULT '', website TEXT DEFAULT '', linkedin_url TEXT DEFAULT '',
  li_status TEXT DEFAULT '', email TEXT DEFAULT '', mobile TEXT DEFAULT '',
  is_mobile INTEGER DEFAULT 0, source TEXT DEFAULT '',
  dm_name TEXT DEFAULT '', gatekeeper_name TEXT DEFAULT '', lead_notes TEXT DEFAULT '',
  pain TEXT DEFAULT '', ppap_per_year TEXT DEFAULT '', tags TEXT DEFAULT '', next_half TEXT DEFAULT '',
  list_id TEXT DEFAULT '', source_file TEXT DEFAULT '',
  status TEXT DEFAULT 'NEW',
  checked_out_by TEXT, checked_out_at TEXT,
  attempts INTEGER DEFAULT 0,
  last_disposition TEXT, last_called_at TEXT,
  next_attempt_at TEXT, callback_at TEXT,
  skipped INTEGER DEFAULT 0,
  created_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_leads_status ON leads(status, rank);
CREATE TABLE IF NOT EXISTS dispositions (
  id INTEGER PRIMARY KEY,
  phone TEXT, company TEXT, disposition TEXT, notes TEXT DEFAULT '',
  agent TEXT DEFAULT '', duration INTEGER DEFAULT 0, at TEXT,
  prev_state TEXT,
  -- funnel flags, stamped at save time so later config edits never rewrite history
  pickup INTEGER DEFAULT 0, dm INTEGER DEFAULT 0, pitched INTEGER DEFAULT 0,
  resonated INTEGER DEFAULT 0, offered INTEGER DEFAULT 0, booked INTEGER DEFAULT 0,
  objections TEXT, pain TEXT DEFAULT '', booked_for TEXT,
  show_status TEXT, sale INTEGER DEFAULT 0, sale_amount REAL DEFAULT 0,
  script_version TEXT DEFAULT '', number_used TEXT DEFAULT '',
  talk_seconds INTEGER DEFAULT 0, attempt_no INTEGER DEFAULT 0, session_id INTEGER
);
CREATE INDEX IF NOT EXISTS idx_dispo_at ON dispositions(at);
CREATE INDEX IF NOT EXISTS idx_dispo_phone ON dispositions(phone);
CREATE TABLE IF NOT EXISTS dnc (
  phone TEXT PRIMARY KEY, reason TEXT, added_at TEXT, added_by TEXT
);
CREATE TABLE IF NOT EXISTS sessions (
  id INTEGER PRIMARY KEY,
  agent TEXT, script_version TEXT DEFAULT '', target_dials INTEGER, target_minutes INTEGER,
  started_at TEXT, ended_at TEXT, active_seconds INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS number_state (
  number TEXT PRIMARY KEY, parked INTEGER DEFAULT 0, reason TEXT DEFAULT '', at TEXT
);
CREATE TABLE IF NOT EXISTS webhook_log (
  id INTEGER PRIMARY KEY, at TEXT, url TEXT, body TEXT, status INTEGER, reply TEXT
);
CREATE TABLE IF NOT EXISTS agent_events (
  id INTEGER PRIMARY KEY,
  agent TEXT, event TEXT, reason TEXT DEFAULT '', at TEXT
);
CREATE INDEX IF NOT EXISTS idx_agent_events ON agent_events(agent, at);

-- Campaigns: a set of leads called together (one or more uploaded lists),
-- with its own script and daily target. A number can sit in several
-- campaigns; its tries, callbacks and do-not-call stay shared, so it is
-- never double-called.
CREATE TABLE IF NOT EXISTS campaigns (
  id INTEGER PRIMARY KEY, name TEXT NOT NULL, script_version TEXT DEFAULT '',
  daily_target INTEGER DEFAULT 0, status TEXT DEFAULT 'active',     -- active | archived | deleted
  created_at TEXT, updated_at TEXT
);
CREATE TABLE IF NOT EXISTS campaign_leads (
  campaign_id INTEGER NOT NULL, phone TEXT NOT NULL, added_at TEXT, source_file TEXT DEFAULT '',
  PRIMARY KEY (campaign_id, phone)
);
CREATE INDEX IF NOT EXISTS idx_campaign_leads_phone ON campaign_leads(phone);

-- Pipeline timeline: stage changes, follow-ups, transcripts processed, email
-- drafts and invite links logged by Claude or by hand.
CREATE TABLE IF NOT EXISTS lead_events (
  id INTEGER PRIMARY KEY, phone TEXT NOT NULL, at TEXT, kind TEXT, text TEXT DEFAULT '', by TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_lead_events_phone ON lead_events(phone, at);
"""

# Columns added after the first deploy; applied idempotently by init().
MIGRATIONS = [
    ("dispositions", "prev_state", "TEXT"),      # JSON lead snapshot, powers undo
    # PPAP campaign lead fields
    ("leads", "tz_name", "TEXT DEFAULT ''"),
    ("leads", "employees", "TEXT DEFAULT ''"),
    ("leads", "process", "TEXT DEFAULT ''"),
    ("leads", "oem", "TEXT DEFAULT ''"),
    ("leads", "industry", "TEXT DEFAULT ''"),
    ("leads", "website", "TEXT DEFAULT ''"),
    ("leads", "linkedin_url", "TEXT DEFAULT ''"),
    ("leads", "li_status", "TEXT DEFAULT ''"),
    ("leads", "email", "TEXT DEFAULT ''"),
    ("leads", "mobile", "TEXT DEFAULT ''"),
    ("leads", "is_mobile", "INTEGER DEFAULT 0"),
    ("leads", "source", "TEXT DEFAULT ''"),
    ("leads", "dm_name", "TEXT DEFAULT ''"),
    ("leads", "gatekeeper_name", "TEXT DEFAULT ''"),
    ("leads", "lead_notes", "TEXT DEFAULT ''"),
    ("leads", "pain", "TEXT DEFAULT ''"),
    ("leads", "ppap_per_year", "TEXT DEFAULT ''"),
    ("leads", "tags", "TEXT DEFAULT ''"),
    ("leads", "next_half", "TEXT DEFAULT ''"),       # 'am' | 'pm': the half of the day the next try must land in
    # Imperium funnel
    ("dispositions", "pickup", "INTEGER DEFAULT 0"),
    ("dispositions", "dm", "INTEGER DEFAULT 0"),
    ("dispositions", "pitched", "INTEGER DEFAULT 0"),
    ("dispositions", "resonated", "INTEGER DEFAULT 0"),
    ("dispositions", "offered", "INTEGER DEFAULT 0"),
    ("dispositions", "booked", "INTEGER DEFAULT 0"),
    ("dispositions", "objections", "TEXT"),
    ("dispositions", "pain", "TEXT DEFAULT ''"),
    ("dispositions", "booked_for", "TEXT"),
    ("dispositions", "show_status", "TEXT"),
    ("dispositions", "sale", "INTEGER DEFAULT 0"),
    ("dispositions", "sale_amount", "REAL DEFAULT 0"),
    ("dispositions", "script_version", "TEXT DEFAULT ''"),
    ("dispositions", "number_used", "TEXT DEFAULT ''"),
    ("dispositions", "talk_seconds", "INTEGER DEFAULT 0"),
    ("dispositions", "attempt_no", "INTEGER DEFAULT 0"),
    ("dispositions", "session_id", "INTEGER"),
    # Campaigns and lead delete
    ("dispositions", "campaign_id", "INTEGER"),
    # Call recording
    ("dispositions", "call_sid", "TEXT DEFAULT ''"),
    ("dispositions", "recording_sid", "TEXT DEFAULT ''"),
    ("leads", "deleted_at", "TEXT"),
    ("leads", "prev_status", "TEXT"),
    # Pipeline and the call agent
    ("leads", "stage", "TEXT DEFAULT ''"),
    ("leads", "stage_at", "TEXT"),
    ("leads", "follow_up_at", "TEXT"),
    ("leads", "follow_up_note", "TEXT DEFAULT ''"),
    ("dispositions", "transcript", "TEXT"),
    ("dispositions", "transcript_status", "TEXT DEFAULT ''"),   # queued | working | done | failed | ''
    ("dispositions", "transcript_error", "TEXT DEFAULT ''"),
    ("dispositions", "ai_summary", "TEXT DEFAULT ''"),
    ("dispositions", "processed_at", "TEXT"),
]


def now():
    return datetime.now(timezone.utc).replace(microsecond=0, tzinfo=None)


def iso(dt):
    return dt.isoformat(sep=" ")


def connect():
    con = sqlite3.connect(DB_PATH, timeout=10)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    return con


def init():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    with connect() as con:
        con.executescript(SCHEMA)
        added = set()
        for table, column, decl in MIGRATIONS:
            have = {r["name"] for r in con.execute(f"PRAGMA table_info({table})")}
            if column not in have:
                con.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
                added.add(column)
        _campaigns_from_lists(con)
        if "pickup" in added:            # first boot after the funnel landed: flag old history
            for key, f in funnel.LEGACY_FLAGS.items():
                con.execute(
                    "UPDATE dispositions SET pickup=?, dm=?, pitched=?, resonated=?, talk_seconds=duration "
                    "WHERE disposition=?",
                    (f.get("pickup", 0), f.get("dm", 0), f.get("pitched", 0), f.get("resonated", 0), key))


def lead_count(con=None):
    with connect() as con:
        return con.execute("SELECT COUNT(*) FROM leads WHERE status != 'DELETED'").fetchone()[0]


# --------------------------------------------------------------- campaigns --
# The cockpit says which campaign it is calling on every request; serve.py
# sets it here per request thread, and every queue, list and stats query
# below narrows to it. None means every campaign.

_ctx = threading.local()


def use_campaign(campaign_id):
    try:
        _ctx.cid = int(campaign_id) or None
    except (TypeError, ValueError):
        _ctx.cid = None


def current_campaign():
    return getattr(_ctx, "cid", None)


def _cc(col="phone"):
    """' AND <lead phone column> in the current campaign', or ''."""
    cid = current_campaign()
    return f" AND {col} IN (SELECT phone FROM campaign_leads WHERE campaign_id = {int(cid)})" if cid else ""


def _cd(col="campaign_id"):
    """' AND <dispositions.campaign_id> is the current campaign', or ''."""
    cid = current_campaign()
    return f" AND {col} = {int(cid)}" if cid else ""


def campaign_name_for(source_file):
    base = os.path.splitext(os.path.basename(source_file or ""))[0]
    if not base or base == "manual dial":
        return "Dialed by hand"
    if base.startswith("demo_leads"):
        return "Demo leads"
    return base.replace("_", " ").replace("-", " ").strip().capitalize()


def _campaigns_from_lists(con):
    """Every lead belongs to a campaign. On the first boot with campaigns, and
    for anything loaded outside the cockpit since (demo seed, CLI import),
    leads with no campaign join one named after their list, so nothing on
    file goes missing and old calls keep their numbers."""
    t = iso(now())
    orphans = """FROM leads WHERE status != 'DELETED' AND phone NOT IN (SELECT phone FROM campaign_leads)"""
    for (source,) in con.execute(f"SELECT COALESCE(source_file, '') s {orphans} GROUP BY s ORDER BY MIN(created_at)").fetchall():
        name = campaign_name_for(source)
        row = con.execute("SELECT id FROM campaigns WHERE name=? AND status='active' ORDER BY id LIMIT 1", (name,)).fetchone()
        cid = row["id"] if row else con.execute("INSERT INTO campaigns (name, created_at, updated_at) VALUES (?,?,?)",
                                                 (name, t, t)).lastrowid
        con.execute(f"INSERT OR IGNORE INTO campaign_leads (campaign_id, phone, added_at, source_file) "
                    f"SELECT ?, phone, created_at, source_file {orphans} AND COALESCE(source_file, '') = ?", (cid, source))
    con.execute("UPDATE dispositions SET campaign_id = (SELECT MIN(campaign_id) FROM campaign_leads cl "
                "WHERE cl.phone = dispositions.phone) WHERE campaign_id IS NULL")


def create_campaign(name, script_version="", daily_target=0):
    t = iso(now())
    with connect() as con:
        return con.execute(
            "INSERT INTO campaigns (name, script_version, daily_target, created_at, updated_at) VALUES (?,?,?,?,?)",
            ((str(name or "").strip() or "New campaign")[:80], str(script_version or "")[:24],
             max(0, int(daily_target or 0)), t, t)).lastrowid


def update_campaign(campaign_id, fields):
    sets = {}
    if str(fields.get("name") or "").strip():
        sets["name"] = str(fields["name"]).strip()[:80]
    if "script_version" in fields:
        sets["script_version"] = str(fields.get("script_version") or "")[:24]
    if "daily_target" in fields:
        try:
            sets["daily_target"] = max(0, min(2000, int(fields.get("daily_target") or 0)))
        except (TypeError, ValueError):
            pass
    if fields.get("status") in ("active", "archived"):
        sets["status"] = fields["status"]
    if not sets:
        return
    sets["updated_at"] = iso(now())
    with connect() as con:
        con.execute(f"UPDATE campaigns SET {', '.join(k + '=?' for k in sets)} WHERE id=?",
                    list(sets.values()) + [int(campaign_id)])


def campaigns_list():
    """Every campaign with its numbers."""
    t = now()
    start = day_start(t)
    with connect() as con:
        rows = con.execute(
            """SELECT c.*,
                 (SELECT COUNT(*) FROM campaign_leads cl JOIN leads l ON l.phone=cl.phone
                    WHERE cl.campaign_id=c.id AND l.status != 'DELETED') leads,
                 (SELECT COUNT(*) FROM campaign_leads cl JOIN leads l ON l.phone=cl.phone
                    WHERE cl.campaign_id=c.id AND l.status IN ('NEW','OUT')) open,
                 (SELECT COUNT(*) FROM campaign_leads cl JOIN leads l ON l.phone=cl.phone
                    WHERE cl.campaign_id=c.id AND l.status IN ('NEW','OUT') AND l.attempts = 0) untouched,
                 (SELECT COUNT(*) FROM dispositions d WHERE d.campaign_id=c.id AND d.disposition != 'SKIP' AND d.at >= ?) dials_today,
                 (SELECT COUNT(*) FROM dispositions d WHERE d.campaign_id=c.id AND d.disposition != 'SKIP') dials_all,
                 (SELECT COUNT(*) FROM dispositions d WHERE d.campaign_id=c.id AND d.booked = 1) booked,
                 (SELECT GROUP_CONCAT(DISTINCT cl.source_file) FROM campaign_leads cl WHERE cl.campaign_id=c.id) files
               FROM campaigns c WHERE c.status != 'deleted' ORDER BY c.status = 'archived', c.id DESC""", (start,)).fetchall()
    return [dict(r, files=[f for f in (r["files"] or "").split(",") if f]) for r in rows]


def delete_leads(phones, agent=""):
    """Take leads off every queue and list. They stay on file as DELETED so
    calls already made still count and an undo can bring them back; a later
    upload won't add them again."""
    phones = [str(p) for p in phones or [] if p]
    if not phones:
        return 0
    marks = ",".join("?" * len(phones))
    with connect() as con:
        return con.execute(
            f"UPDATE leads SET prev_status=status, status='DELETED', deleted_at=?, checked_out_by=NULL "
            f"WHERE phone IN ({marks}) AND status != 'DELETED'", [iso(now())] + phones).rowcount


def restore_leads(phones):
    phones = [str(p) for p in phones or [] if p]
    if not phones:
        return 0
    marks = ",".join("?" * len(phones))
    with connect() as con:
        return con.execute(
            f"UPDATE leads SET status=COALESCE(prev_status, 'NEW'), prev_status=NULL, deleted_at=NULL "
            f"WHERE phone IN ({marks}) AND status='DELETED'", phones).rowcount


def remove_from_campaign(phones, campaign_id):
    """Drop numbers from one campaign. A number left in no campaign is deleted."""
    phones = [str(p) for p in phones or [] if p]
    if not phones or not campaign_id:
        return 0
    marks = ",".join("?" * len(phones))
    with connect() as con:
        con.execute(f"DELETE FROM campaign_leads WHERE campaign_id=? AND phone IN ({marks})", [int(campaign_id)] + phones)
        orphans = [r[0] for r in con.execute(
            f"SELECT phone FROM leads WHERE phone IN ({marks}) AND phone NOT IN (SELECT phone FROM campaign_leads)", phones)]
        con.execute(f"UPDATE leads SET status='NEW', checked_out_by=NULL WHERE status='OUT' AND phone IN ({marks})", phones)
    delete_leads(orphans)
    return len(phones)


def delete_campaign(campaign_id):
    """Remove a campaign. Leads in no other campaign go with it (as DELETED,
    so their call history still counts); shared leads stay in the others."""
    cid = int(campaign_id)
    with connect() as con:
        only_here = [r[0] for r in con.execute(
            """SELECT cl.phone FROM campaign_leads cl WHERE cl.campaign_id=? AND NOT EXISTS (
                 SELECT 1 FROM campaign_leads o JOIN campaigns c ON c.id=o.campaign_id
                 WHERE o.phone=cl.phone AND o.campaign_id != ? AND c.status != 'deleted')""", (cid, cid))]
        con.execute("UPDATE campaigns SET status='deleted', updated_at=? WHERE id=?", (iso(now()), cid))
    return delete_leads(only_here)


def link_to_campaign(phone, campaign_id, source_file=""):
    if campaign_id:
        with connect() as con:
            con.execute("INSERT OR IGNORE INTO campaign_leads (campaign_id, phone, added_at, source_file) VALUES (?,?,?,?)",
                        (int(campaign_id), phone, iso(now()), source_file))


# ---------------------------------------------------------------- import --

LEAD_FIELDS = ("first", "last", "company", "title", "city", "state", "tz_offset", "tz_name",
               "rank", "employees", "process", "oem", "industry", "website", "linkedin_url",
               "li_status", "email", "mobile", "is_mobile", "source", "dm_name",
               "gatekeeper_name", "lead_notes", "list_id")


def _row_to_lead(row):
    """Accepts both list shapes listprep writes: the dialer CSV (phone_e164,
    company, ...) and the VICIdial load file (phone_code + phone_number,
    company in address3)."""
    get = lambda *names: next((str(row[n]).strip() for n in names if row.get(n) not in (None, "")), "")
    phone = get("phone_e164") or ("+" + (get("phone_code") or "1") + get("phone_number"))
    if len(phone) != 12 or not phone[1:].isdigit():
        return None, None
    offset = get("gmt_offset_now", "tz_offset")
    return phone, {
        "first": get("first_name", "first"), "last": get("last_name", "last"),
        "company": get("company", "address3"), "title": get("title", "job_title"),
        "city": get("city"), "state": get("state"),
        "tz_offset": float(offset) if offset else -5.0, "tz_name": get("timezone", "tz_name"),
        "rank": int(float(get("rank") or 0)),
        "employees": get("employees", "company_size"), "process": get("process"), "oem": get("oem"),
        "industry": get("industry"), "website": get("website", "domain"),
        "linkedin_url": get("linkedin_url", "linkedin"), "li_status": get("li_status").lower(),
        "email": get("email"), "mobile": get("mobile"),
        "is_mobile": 1 if get("is_mobile") in ("1", "true", "True", "yes") else 0,
        "source": get("source"), "dm_name": get("dm_name"),
        "gatekeeper_name": get("gatekeeper_name"), "lead_notes": get("notes", "lead_notes"),
        "list_id": get("list_id"),
    }


def import_list_csv(path, campaign_id=None, label=None):
    """Load a listprep-generated list into a campaign. Returns (added,
    refreshed). Known leads that have not been worked yet pick up the new
    rank and research fields; anything already dialed keeps its history
    untouched; leads you deleted stay deleted."""
    stamp = iso(now())
    source = os.path.basename(path)
    cols = ", ".join(LEAD_FIELDS)
    marks = ", ".join(":" + f for f in LEAD_FIELDS)
    refresh = ", ".join(f"{f}=:{f}" for f in LEAD_FIELDS)
    processed = 0
    with connect() as con, open(path, newline="", encoding="utf-8-sig") as fh:
        before = con.execute("SELECT COUNT(*) FROM leads").fetchone()[0]
        for row in csv.DictReader(fh):
            phone, fields = _row_to_lead(row)
            if not phone:
                continue
            if con.execute("SELECT 1 FROM leads WHERE phone=? AND status='DELETED'", (phone,)).fetchone():
                continue
            con.execute(
                f"""INSERT INTO leads (phone, {cols}, source_file, created_at)
                    VALUES (:phone, {marks}, :source_file, :created_at)
                    ON CONFLICT(phone) DO UPDATE SET {refresh}, source_file=:source_file
                    WHERE leads.status='NEW' AND leads.attempts=0""",
                dict(fields, phone=phone, source_file=source, created_at=stamp))
            if campaign_id:
                con.execute("INSERT OR IGNORE INTO campaign_leads (campaign_id, phone, added_at, source_file) VALUES (?,?,?,?)",
                            (int(campaign_id), phone, stamp, label or source))
            processed += 1
        added = con.execute("SELECT COUNT(*) FROM leads").fetchone()[0] - before
        if not campaign_id:
            _campaigns_from_lists(con)                  # no campaign given: one named after the list
    return added, processed - added


def import_legacy(newest_list, called_log, dnc_csv):
    """One-time migration when the leads table is empty: newest prepped list
    becomes the queue; called_log/dnc.csv history is folded in on top."""
    if lead_count():
        return None
    added = 0
    if newest_list and os.path.exists(newest_list):
        added, _ = import_list_csv(newest_list)
    stamp = iso(now())
    with connect() as con:
        if called_log and os.path.exists(called_log):
            with open(called_log, newline="") as fh:
                for row in csv.DictReader(fh):
                    phone = (row.get("phone_e164") or "").strip()
                    if not phone:
                        continue
                    con.execute(
                        """UPDATE leads SET status='DONE', attempts=?,
                             last_disposition=?, last_called_at=? WHERE phone=?""",
                        (int(row.get("attempts") or 1),
                         row.get("last_disposition") or "",
                         row.get("last_called_at") or "", phone))
        if dnc_csv and os.path.exists(dnc_csv):
            with open(dnc_csv, newline="") as fh:
                for row in csv.DictReader(fh):
                    phone = (row.get("phone_e164") or "").strip()
                    if not phone:
                        continue
                    con.execute(
                        "INSERT OR IGNORE INTO dnc VALUES (?,?,?,?)",
                        (phone, row.get("reason") or "legacy",
                         row.get("added_at") or stamp, row.get("added_by") or "import"))
                    con.execute("UPDATE leads SET status='DNC' WHERE phone=?", (phone,))
    return added


# ----------------------------------------------------------------- queue --

def lead_offset(lead, at=None):
    """Hours from UTC for this lead right now. The zone name survives the
    clock change in March and November; the stored offset (taken at prep
    time) is only the fallback."""
    name = (lead.get("tz_name") if isinstance(lead, dict) else lead["tz_name"]) or ""
    if name:
        try:
            from zoneinfo import ZoneInfo
            moment = (at or now()).replace(tzinfo=timezone.utc).astimezone(ZoneInfo(name))
            return moment.utcoffset().total_seconds() / 3600.0
        except Exception:
            pass
    stored = lead.get("tz_offset") if isinstance(lead, dict) else lead["tz_offset"]
    return stored if stored is not None else -5.0


def to_local(lead, at=None):
    """The prospect's wall clock (naive) at UTC moment `at`."""
    return (at or now()) + timedelta(hours=lead_offset(lead, at))


def to_utc(lead, local):
    """A naive prospect-local datetime -> naive UTC, honouring their zone's
    clock changes when the zone name is known."""
    name = (lead.get("tz_name") if isinstance(lead, dict) else lead["tz_name"]) or ""
    if name:
        try:
            from zoneinfo import ZoneInfo
            return local.replace(tzinfo=ZoneInfo(name)).astimezone(timezone.utc).replace(tzinfo=None)
        except Exception:
            pass
    return local - timedelta(hours=lead_offset(lead))


def lead_tier(lead, at=None):
    """'power' | 'secondary' | None for a cold dial right now."""
    return policy.tier(to_local(lead, at), WINDOWS) if ENFORCE_WINDOWS else "power"


def in_hard(lead, at=None):
    """May this lead be rung at all right now (requested callbacks, hand-picked leads)?"""
    return policy.in_hard(to_local(lead, at), WINDOWS) if ENFORCE_WINDOWS else True


def in_window(tz_offset, at=None, callback=False):
    """Kept for list rows: is a lead at this UTC offset callable now?"""
    if not ENFORCE_WINDOWS:
        return True
    local = (at or now()) + timedelta(hours=tz_offset if tz_offset is not None else -5)
    return policy.in_hard(local, WINDOWS) if callback else policy.tier(local, WINDOWS) is not None


def dials_today(con, number=None):
    sql, args = "SELECT COUNT(*) FROM dispositions WHERE at>=? AND disposition!='SKIP'", [day_start()]
    if number:
        sql += " AND number_used=?"
        args.append(number)
    return con.execute(sql, args).fetchone()[0]


# ---- caller-ID pool ----------------------------------------------------------

def _pool():
    return POOL or [{"number": FALLBACK_CALLER_ID, "area_code": policy.area_code(FALLBACK_CALLER_ID),
                     "state": "", "warmup_start": None, "label": ""}]


FALLBACK_CALLER_ID = "+19175550142"


def _today_local():
    return funnel.local_date(iso(now()), STATS_TZ)


def _effective_pool(con):
    """Pool entries with a warm-up start: the configured date, else the day
    the number was first used here, else today (a brand-new number)."""
    out = []
    for e in _pool():
        start = e.get("warmup_start")
        if start is None:
            first = con.execute("SELECT MIN(at) FROM dispositions WHERE number_used=?", (e["number"],)).fetchone()[0]
            start = funnel.local_date(first, STATS_TZ) if first else _today_local()
        out.append(dict(e, warmup_start=start))
    return out


def _usage(con):
    return {r["number_used"]: r["n"] for r in con.execute(
        "SELECT number_used, COUNT(*) n FROM dispositions WHERE at>=? AND disposition!='SKIP' "
        "AND number_used!='' GROUP BY number_used", (day_start(),))}


def _parked(con):
    return {r["number"] for r in con.execute("SELECT number FROM number_state WHERE parked=1")}


def assign_caller_id(con, lead, over_cap=False):
    """(number, reason) for this lead, or (None, why-not). Advances the
    round-robin cursor only when round robin actually chose."""
    global _rr_cursor
    entry, reason, _rr_cursor = policy.pick_caller_id(
        lead["phone"], lead["state"], _effective_pool(con), _usage(con), _today_local(),
        _parked(con), _rr_cursor, NUMBERS, over_cap=over_cap)
    return (entry["number"] if entry else None), reason


def numbers_health():
    """Per caller ID: today's dials against its cap, warm-up day, 7-day pickup
    rate, and whether it is flagged or parked."""
    week = iso(now() - timedelta(days=7))
    today = _today_local()
    with connect() as con:
        usage, parked = _usage(con), {r["number"]: dict(r) for r in con.execute("SELECT * FROM number_state WHERE parked=1")}
        out = []
        for e in _effective_pool(con):
            r = con.execute(
                "SELECT COUNT(*) dials, COALESCE(SUM(pickup),0) pickups FROM dispositions "
                "WHERE number_used=? AND at>=? AND disposition!='SKIP'", (e["number"], week)).fetchone()
            dials, pickups = r["dials"], r["pickups"]
            warm_day = (today - e["warmup_start"]).days + 1
            out.append({
                "number": e["number"], "area_code": e["area_code"], "state": e["state"], "label": e.get("label") or "",
                "used_today": usage.get(e["number"], 0), "cap": policy.daily_cap(e, today, NUMBERS),
                "warming": warm_day <= NUMBERS["warmup_days"], "warmup_day": warm_day, "warmup_days": NUMBERS["warmup_days"],
                "dials_7d": dials, "pickups_7d": pickups, "pickup_rate_7d": (pickups / dials) if dials else None,
                "spam_suspect": policy.spam_suspect(dials, pickups, NUMBERS),
                "parked": e["number"] in parked, "park_reason": (parked.get(e["number"]) or {}).get("reason", ""),
            })
        return out


def set_parked(number, parked, reason="manual"):
    with connect() as con:
        con.execute("INSERT INTO number_state (number, parked, reason, at) VALUES (?,?,?,?) "
                    "ON CONFLICT(number) DO UPDATE SET parked=excluded.parked, reason=excluded.reason, at=excluded.at",
                    (number, 1 if parked else 0, reason if parked else "", iso(now())))


def _auto_park(con, number):
    if not number:
        return False
    r = con.execute("SELECT COUNT(*) dials, COALESCE(SUM(pickup),0) pickups FROM dispositions "
                    "WHERE number_used=? AND at>=? AND disposition!='SKIP'",
                    (number, iso(now() - timedelta(days=7)))).fetchone()
    if not policy.should_park(r["dials"], r["pickups"], NUMBERS):
        return False
    already = con.execute("SELECT parked FROM number_state WHERE number=?", (number,)).fetchone()
    if already and already["parked"]:
        return False
    rate = r["pickups"] / r["dials"]
    con.execute("INSERT INTO number_state (number, parked, reason, at) VALUES (?,?,?,?) "
                "ON CONFLICT(number) DO UPDATE SET parked=1, reason=excluded.reason, at=excluded.at",
                (number, 1, f"auto: {rate:.0%} pickup over {r['dials']} dials in 7 days", iso(now())))
    return True


# ---- picking the next lead ---------------------------------------------------

_ELIGIBLE = ("status='NEW' AND callback_at IS NULL AND (next_attempt_at IS NULL OR next_attempt_at <= :now) "
             "AND attempts < :max AND (is_mobile=0 OR :mobile=1)")


def _zones(con, t):
    """Every (zone, offset) combination among cold-eligible leads, with what
    the clock says there right now."""
    args = {"now": iso(t), "max": MAX_ATTEMPTS, "mobile": int(ALLOW_MOBILE)}
    out = []
    for r in con.execute(f"SELECT tz_name, tz_offset, COUNT(*) n FROM leads WHERE {_ELIGIBLE}{_cc()} GROUP BY tz_name, tz_offset", args):
        zone = {"tz_name": r["tz_name"] or "", "tz_offset": r["tz_offset"]}
        local = to_local(zone, t)
        midnight_utc = to_utc(zone, local.replace(hour=0, minute=0, second=0, microsecond=0))
        where = "(last_called_at IS NULL OR last_called_at < :midnight)"          # never twice in one local day
        if ENFORCE_WINDOWS:
            where += " AND (next_half IS NULL OR next_half='' OR next_half=:half)"
        zargs = dict(args, tzn=zone["tz_name"], tzo=zone["tz_offset"], midnight=iso(midnight_utc), half=policy.half(local))
        n = con.execute(f"SELECT COUNT(*) FROM leads WHERE {_ELIGIBLE}{_cc()} AND tz_name=:tzn AND tz_offset IS :tzo AND {where}", zargs).fetchone()[0]
        out.append(dict(zone, local=local, tier=lead_tier(zone, t), label=policy.zone_label(zone["tz_name"], lead_offset(zone, t)),
                        eligible=n, where=where, args=zargs))
    return out


def window_status(at=None):
    """For the cockpit's one-liner: 'now dialing: ET power window, 41 leads eligible'."""
    t = at or now()
    with connect() as con:
        zones = _zones(con, t)
    for tier in ("power", "secondary"):
        live = [z for z in zones if z["tier"] == tier and z["eligible"]]
        if live:
            by_label = {}
            for z in live:
                by_label[z["label"]] = by_label.get(z["label"], 0) + z["eligible"]
            order = ["ET", "CT", "MT", "PT", "AKT", "HT"]
            labels = sorted(by_label, key=lambda l: order.index(l) if l in order else 99)
            return {"open": True, "enforced": ENFORCE_WINDOWS, "tier": tier, "zones": [{"label": l, "eligible": by_label[l]} for l in labels],
                    "eligible": sum(by_label.values())}
    nxt = None
    for z in zones:
        if not z["eligible"] and not ENFORCE_WINDOWS:
            continue
        opening = policy.next_open(z["local"], WINDOWS)
        if opening:
            when = to_utc(z, opening)
            if nxt is None or when < nxt[0]:
                nxt = (when, z["label"], policy.tier(opening, WINDOWS), opening)
    return {"open": False, "enforced": ENFORCE_WINDOWS, "tier": None, "zones": [], "eligible": 0,
            "next_open_at": iso(nxt[0]) if nxt else None, "next_zone": nxt[1] if nxt else None,
            "next_tier": nxt[2] if nxt else None,
            "next_local": nxt[3].strftime("%a %-I:%M%p").replace("AM", "am").replace("PM", "pm") if nxt else None}


def _with_caller_id(con, lead, over_cap=False):
    number, reason = assign_caller_id(con, lead, over_cap)
    if number is None:
        return None, reason
    lead = dict(lead)
    lead["_caller_id"], lead["_caller_reason"] = number, reason
    return lead, None


def checkout(agent):
    """Atomically hand the next dialable lead to `agent`.

    Order: the lead they already hold, then due callbacks (any time inside the
    hard limits, the prospect asked for it), then cold leads from zones in a
    POWER window, then zones in a SECONDARY window, best rank first. Because
    the windows follow each prospect's clock, the queue walks ET, CT, MT, PT
    on its own as the day moves.
    Returns (lead_dict, None) or (None, reason_string).
    """
    t = now()
    with connect() as con:
        con.execute(
            "UPDATE leads SET status='NEW', checked_out_by=NULL WHERE status='OUT' AND checked_out_at < ?",
            (iso(t - timedelta(minutes=CHECKOUT_TTL_MIN)),))

        held = con.execute(
            "SELECT * FROM leads WHERE status='OUT' AND checked_out_by=?" + _cc() +
            " ORDER BY checked_out_at DESC LIMIT 1", (agent,)).fetchone()
        if held is not None:
            con.execute("UPDATE leads SET checked_out_at=? WHERE id=?", (iso(t), held["id"]))
            return _with_caller_id(con, held)
        # A lead held from another campaign goes back to its own queue.
        con.execute("UPDATE leads SET status='NEW', checked_out_by=NULL WHERE status='OUT' AND checked_out_by=?", (agent,))

        pick = None
        due = con.execute(
            """SELECT * FROM leads WHERE status='NEW' AND callback_at IS NOT NULL
               AND callback_at <= ? AND (is_mobile=0 OR ?=1)""" + _cc() + " ORDER BY callback_at LIMIT 50",
            (iso(t), int(ALLOW_MOBILE))).fetchall()
        pick = next((r for r in due if in_hard(r, t)), None)

        zones = None
        if pick is None:
            zones = _zones(con, t)
            for tier in ("power", "secondary"):
                best = None
                for z in (z for z in zones if z["tier"] == tier and z["eligible"]):
                    r = con.execute(
                        f"SELECT * FROM leads WHERE {_ELIGIBLE}{_cc()} AND tz_name=:tzn AND tz_offset IS :tzo AND {z['where']} "
                        "ORDER BY skipped, rank DESC, attempts LIMIT 1", z["args"]).fetchone()
                    if r is not None and (best is None or (r["skipped"], -r["rank"], r["attempts"]) < (best["skipped"], -best["rank"], best["attempts"])):
                        best = r
                if best is not None:
                    pick = best
                    break

        if pick is None:
            waiting = sum(z["eligible"] for z in zones or [])
            if due or waiting:
                return None, "Leads remain, but nobody is inside a calling window on their own clock right now."
            if zones:
                return None, "Every lead left was already tried today, or is waiting for its next morning or afternoon slot."
            return None, ("This campaign's queue is empty. Load another list into it, or wait for retries to come due."
                          if current_campaign() else "Queue is empty. Load a new list or wait for retries to come due.")

        if _scrubbed(con, pick):
            con.commit()
            return checkout(agent)       # closed out as national DNC; take the next one
        lead, why_not = _with_caller_id(con, pick)
        if lead is None:
            return None, why_not
        cur = con.execute(
            "UPDATE leads SET status='OUT', checked_out_by=?, checked_out_at=? "
            "WHERE id=? AND status='NEW'", (agent, iso(t), pick["id"]))
        if cur.rowcount == 0:            # lost the race; caller retries
            return checkout(agent)
        return lead, None


def history(phone, limit=20):
    with connect() as con:
        return [dict(r) for r in con.execute(
            "SELECT id, disposition, notes, agent, duration, at, objections, pain, booked_for, "
            "show_status, sale, attempt_no, script_version, recording_sid, transcript, transcript_status, "
            "transcript_error, ai_summary FROM dispositions "
            "WHERE phone=? ORDER BY at DESC, id DESC LIMIT ?", (phone, limit))]


def queue_preview(limit=5):
    with connect() as con:
        return [dict(r) for r in con.execute(
            """SELECT company, state, rank FROM leads WHERE status='NEW'
               AND callback_at IS NULL AND (next_attempt_at IS NULL OR next_attempt_at <= ?)""" + _cc() +
            " ORDER BY skipped, rank DESC LIMIT ?", (iso(now()), limit))]


_LIST_COLS = ("phone, first, last, company, title, city, state, tz_offset, tz_name, rank, process, "
              "attempts, status, last_disposition, last_called_at, next_attempt_at, "
              "callback_at, checked_out_by")


def _list_row(r, t):
    d = dict(r)
    d["tz_offset"] = lead_offset(d, t)               # live, DST-aware
    d["in_window"] = in_hard(d, t) if d["callback_at"] else lead_tier(d, t) is not None
    d["tier"] = lead_tier(d, t)
    return d


def lead_list(q="", limit=60):
    """Left-rail queue. No query: what the dialer would hand out next, in
    order. With a query: every lead that matches, whatever its status, so an
    agent can find the person who just emailed back."""
    t = now()
    q = (q or "").strip()
    with connect() as con:
        if q:
            like = f"%{q}%"
            digits = "".join(ch for ch in q if ch.isdigit())
            rows = con.execute(
                f"""SELECT {_LIST_COLS} FROM leads
                    WHERE status != 'DELETED' AND (company LIKE ? OR first LIKE ? OR last LIKE ?
                       OR (first || ' ' || last) LIKE ? OR state LIKE ?
                       OR (? != '' AND phone LIKE ?)){_cc()}
                    ORDER BY (status='NEW') DESC, rank DESC LIMIT ?""",
                (like, like, like, like, q, digits, f"%{digits}%", limit)).fetchall()
        else:
            rows = con.execute(
                f"""SELECT {_LIST_COLS} FROM leads WHERE status='NEW'
                    AND callback_at IS NULL
                    AND (next_attempt_at IS NULL OR next_attempt_at <= ?)
                    AND attempts < ?{_cc()}
                    ORDER BY skipped, rank DESC, attempts LIMIT ?""",
                (iso(t), MAX_ATTEMPTS, limit)).fetchall()
        return [_list_row(r, t) for r in rows]


def callbacks_list(limit=100):
    """Every scheduled callback, soonest first, with the note that set it."""
    t = now()
    with connect() as con:
        rows = con.execute(
            f"""SELECT {_LIST_COLS} FROM leads
                WHERE status IN ('NEW','OUT') AND callback_at IS NOT NULL{_cc()}
                ORDER BY callback_at LIMIT ?""", (limit,)).fetchall()
        out = []
        for r in rows:
            d = _list_row(r, t)
            note = con.execute(
                "SELECT notes, agent FROM dispositions WHERE phone=? AND notes!='' "
                "ORDER BY at DESC, id DESC LIMIT 1", (d["phone"],)).fetchone()
            d["note"] = note["notes"] if note else ""
            d["overdue"] = d["callback_at"] <= iso(t)
            out.append(d)
        return out


def calls_today(agent=None, limit=300):
    sql = ("""SELECT d.id, d.phone, d.company, d.disposition, d.notes, d.agent,
                     d.duration, d.at, d.objections, d.pain, d.booked, d.booked_for,
                     d.show_status, d.sale, d.sale_amount, d.script_version, d.recording_sid,
                     l.first, l.last, l.tz_offset, l.tz_name, l.status, l.email
              FROM dispositions d LEFT JOIN leads l ON l.phone = d.phone
              WHERE d.at >= ? AND d.disposition != 'SKIP'""") + _cd("d.campaign_id")
    args = [day_start()]
    if agent:
        sql += " AND d.agent = ?"
        args.append(agent)
    sql += " ORDER BY d.at DESC, d.id DESC LIMIT ?"
    args.append(limit)
    with connect() as con:
        out = []
        for r in con.execute(sql, args):
            d = dict(r)
            d["tz_offset"] = lead_offset(d)
            out.append(d)
        return out


def bookings(limit=200):
    """Every booked call, soonest upcoming first, then the past ones that
    still need a show / no-show / sale marked."""
    t = iso(now())
    with connect() as con:
        rows = con.execute(
            """SELECT d.id, d.phone, d.company, d.at, d.booked_for, d.show_status, d.sale,
                      d.sale_amount, d.pain, d.notes, d.script_version, d.agent,
                      l.first, l.last, l.dm_name, l.email, l.tz_offset, l.tz_name, l.process
               FROM dispositions d LEFT JOIN leads l ON l.phone = d.phone
               WHERE d.booked = 1""" + _cd("d.campaign_id") + " ORDER BY d.booked_for DESC LIMIT ?", (limit,)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["tz_offset"] = lead_offset(d)
        d["upcoming"] = bool(d["booked_for"] and d["booked_for"] > t)
        d["needs_status"] = not d["upcoming"] and not d["show_status"]
        out.append(d)
    out.sort(key=lambda d: (0, d["booked_for"] or "") if d["upcoming"] else (1, "~" if d["needs_status"] else "", d["booked_for"] or ""))
    return out


def follow_through(dispo_id, show_status=None, sale=None, sale_amount=None):
    """Mark what happened to a booked call. Stored on the dial that booked it,
    which is what attributes the sales call and the sale to that dial's date."""
    status = (show_status or "").upper() or None
    if status not in (None, "SHOWED", "NO_SHOW", "RESCHEDULED"):
        return "show_status must be SHOWED, NO_SHOW or RESCHEDULED"
    with connect() as con:
        row = con.execute("SELECT booked FROM dispositions WHERE id=?", (dispo_id,)).fetchone()
        if row is None or not row["booked"]:
            return "That call was not a booking."
        sale_flag = 1 if sale else 0
        if sale_flag:
            status = "SHOWED"                       # a sale implies the call happened
        con.execute("UPDATE dispositions SET show_status=?, sale=?, sale_amount=? WHERE id=?",
                    (status, sale_flag, float(sale_amount or 0) if sale_flag else 0, dispo_id))
    return None


def _drop_untouched_manual(con, phone):
    """A hand-typed number that was opened but never dialed is not a lead;
    leaving the bare row behind would feed it to the auto-queue later."""
    return con.execute(
        "DELETE FROM leads WHERE phone=? AND list_id='manual' AND attempts=0 "
        "AND company='' AND callback_at IS NULL", (phone,)).rowcount > 0


def skip(phone, agent):
    """Back of the queue. Clears any callback timer: a skipped due-callback
    must not boomerang straight back to the top."""
    with connect() as con:
        if _drop_untouched_manual(con, phone):
            return
        con.execute(
            "UPDATE leads SET status='NEW', skipped=1, callback_at=NULL, "
            "checked_out_by=NULL WHERE phone=?", (phone,))


def release(phone):
    """Lead handed back untouched (e.g. tab closed cleanly)."""
    with connect() as con:
        if _drop_untouched_manual(con, phone):
            return
        con.execute(
            "UPDATE leads SET status='NEW', checked_out_by=NULL WHERE phone=? AND status='OUT'",
            (phone,))


def is_dnc(phone):
    with connect() as con:
        return con.execute("SELECT 1 FROM dnc WHERE phone=?", (phone,)).fetchone() is not None


# Refusals the agent may override with "Dial anyway" on a lead they picked.
# Do-not-call, the mobile block and calling hours are never overridable.
OVERRIDABLE = ("Already tried today", "daily dial cap")


def checkout_specific(phone, agent, tz_offset=None, tz_name="", returning=False, force=False):
    """Agent picked a lead by hand (queue click, callback, inbox, typed number).
    Same guard rails as the automatic path: DNC, mobile block, a caller ID with
    room under its cap, the 08:00 to 18:00 weekday limit on the prospect's
    clock, no second dial of a no-answer on the same day, and no stealing a
    lead another agent has open. `returning` is set when they rang us first
    (inbox call back), which lifts the same-day rule. Unknown numbers get a
    bare lead row so the call is logged like any other.
    Returns (lead_dict, None) or (None, reason_string)."""
    t = now()
    with connect() as con:
        if con.execute("SELECT 1 FROM dnc WHERE phone=?", (phone,)).fetchone():
            return None, "That number is on the do-not-call list."
        if phone in scrub_numbers():
            existing = con.execute("SELECT * FROM leads WHERE phone=?", (phone,)).fetchone()
            if existing is not None:
                _scrubbed(con, existing)
            return None, "That number is on the national do-not-call scrub list."

        row = con.execute("SELECT * FROM leads WHERE phone=?", (phone,)).fetchone()
        if row is None:
            con.execute(
                "INSERT INTO leads (phone, company, tz_offset, tz_name, list_id, source, source_file, created_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (phone, "", tz_offset if tz_offset is not None else -5, tz_name or "",
                 "manual", "manual dial", "manual dial", iso(t)))
            row = con.execute("SELECT * FROM leads WHERE phone=?", (phone,)).fetchone()
        if current_campaign():
            con.execute("INSERT OR IGNORE INTO campaign_leads (campaign_id, phone, added_at, source_file) VALUES (?,?,?,?)",
                        (current_campaign(), phone, iso(t), "dialed by hand"))
        if row["status"] == "DELETED":
            con.execute("UPDATE leads SET status=COALESCE(prev_status, 'NEW'), prev_status=NULL, deleted_at=NULL WHERE id=?", (row["id"],))
            row = con.execute("SELECT * FROM leads WHERE phone=?", (phone,)).fetchone()

        if row["status"] == "DNC":
            return None, "That lead is marked do-not-call."
        if row["is_mobile"] and not ALLOW_MOBILE:
            return None, "That number is a mobile. Mobiles are blocked (compliance.allow_mobile is off)."
        stale = iso(t - timedelta(minutes=CHECKOUT_TTL_MIN))
        if (row["status"] == "OUT" and row["checked_out_by"] not in (None, agent)
                and (row["checked_out_at"] or "") >= stale):
            return None, f"{row['checked_out_by']} has that lead open right now."
        if not in_hard(row, t):
            return None, "Outside calling hours on their clock (weekdays, 8am to 6pm their time)."
        if (not returning and not force and row["last_called_at"] and not row["callback_at"]
                and row["last_disposition"] not in CONNECTED
                and policy.same_local_day(to_local(row, datetime.fromisoformat(row["last_called_at"])), to_local(row, t))):
            return None, "Already tried today with no answer. Same-day redials get numbers labelled as spam."

        lead, why_not = _with_caller_id(con, row, over_cap=force)
        if lead is None:
            return None, why_not
        con.execute(
            "UPDATE leads SET status='OUT', checked_out_by=?, checked_out_at=? WHERE id=?",
            (agent, iso(t), row["id"]))
        return lead, None


def reschedule(phone, callback_at):
    """Move a callback, or drop it (callback_at=None) back into the normal queue."""
    with connect() as con:
        cur = con.execute(
            "UPDATE leads SET callback_at=?, status='NEW', checked_out_by=NULL "
            "WHERE phone=? AND status IN ('NEW','OUT','DONE')", (callback_at, phone))
        return cur.rowcount > 0


_NO_SNAP = {"id", "phone", "created_at", "checked_out_by", "checked_out_at"}


def _snapshot(lead):
    return {k: lead[k] for k in lead.keys() if k not in _NO_SNAP}


LEAD_CAPTURE = ("dm_name", "email", "mobile", "pain", "ppap_per_year", "oem", "gatekeeper_name")


def disposition(phone, company, dispo, notes, agent, duration, callback_at=None, extra=None):
    """Record an outcome and advance the lead. `extra` carries the level-2
    wrap-up (objections, offered, pain, booked_for, script_version,
    number_used, session_id) and captured lead fields. Returns the
    disposition id, which /api/undo takes while the undo toast is still up."""
    extra = extra or {}
    t = iso(now())
    flags = funnel.flags_for(OUTCOMES.get(dispo), offered_ticked=bool(extra.get("offered")))
    with connect() as con:
        lead = con.execute("SELECT * FROM leads WHERE phone=?", (phone,)).fetchone()
        snap = _snapshot(lead) if lead else None
        if snap is not None:
            snap["had_dnc"] = con.execute(
                "SELECT 1 FROM dnc WHERE phone=?", (phone,)).fetchone() is not None
        attempts = (lead["attempts"] if lead else 0) + 1
        objections = [str(o)[:80] for o in (extra.get("objections") or [])][:12]
        member = [r[0] for r in con.execute("SELECT campaign_id FROM campaign_leads WHERE phone=? ORDER BY campaign_id", (phone,))]
        campaign_id = current_campaign() if current_campaign() in member else (member[0] if member else current_campaign())

        cur = con.execute(
            """INSERT INTO dispositions (phone, company, disposition, notes, agent, duration, at, prev_state,
                 pickup, dm, pitched, resonated, offered, booked, objections, pain, booked_for,
                 script_version, number_used, talk_seconds, attempt_no, session_id, campaign_id, call_sid, recording_sid)
               VALUES (?,?,?,?,?,?,?,?, ?,?,?,?,?,?, ?,?,?, ?,?,?,?,?,?,?,?)""",
            (phone, company, dispo, notes, agent, duration, t,
             json.dumps(snap) if snap is not None else None,
             flags["pickup"], flags["dm"], flags["pitched"], flags["resonated"], flags["offered"], flags["booked"],
             json.dumps(objections) if objections else None, (extra.get("pain") or "")[:500],
             extra.get("booked_for"), (extra.get("script_version") or "")[:24],
             (extra.get("number_used") or "")[:20], duration if flags["pickup"] else 0,
             attempts, extra.get("session_id"), campaign_id,
             str(extra.get("call_sid") or "")[:40], str(extra.get("recording_sid") or "")[:40]))
        dispo_id = cur.lastrowid

        if dispo == "DNC":
            con.execute("INSERT OR REPLACE INTO dnc VALUES (?,?,?,?)",
                        (phone, notes or "agent_request", t, agent))

        final_dispo, next_half, exhausted_now = dispo, "", False
        if dispo == "CALLBACK" and callback_at:
            status, next_at, cb = "NEW", None, callback_at
        elif dispo in RETRYABLE:
            zone = dict(lead) if lead is not None else {"tz_name": "", "tz_offset": -5}
            again, next_half = policy.next_attempt(to_local(zone), attempts, RETRY, WINDOWS)
            if again is None:            # six tries, never reached the DM: email only from here
                status, next_at, cb, final_dispo, next_half, exhausted_now = "EXHAUSTED", None, None, "EXHAUSTED", "", True
            else:
                status, next_at, cb = "NEW", iso(to_utc(zone, again)), None
        elif dispo == "DNC":
            status, next_at, cb = "DNC", None, None
        else:
            status, next_at, cb = "DONE", None, None

        con.execute(
            """UPDATE leads SET status=?, attempts=?, last_disposition=?, last_called_at=?,
               next_attempt_at=?, next_half=?, callback_at=?, checked_out_by=NULL WHERE phone=?""",
            (status, attempts, final_dispo, t, next_at, next_half or "", cb, phone))
        if exhausted_now and lead is not None:
            tags = [x for x in (lead["tags"] or "").split(",") if x]
            if "email_only" not in tags:
                tags.append("email_only")
            con.execute("UPDATE leads SET tags=? WHERE phone=?", (",".join(tags), phone))
        _auto_park(con, (extra.get("number_used") or ""))

        # What the caller learned on the call sticks to the lead.
        learned = {k: str(extra[k]).strip()[:500] for k in LEAD_CAPTURE if str(extra.get(k) or "").strip()}
        if learned and lead is not None:
            con.execute(f"UPDATE leads SET {', '.join(k + '=?' for k in learned)} WHERE phone=?",
                        list(learned.values()) + [phone])
    if exhausted_now:
        export_exhausted()
    return dispo_id


def export_exhausted(path=None):
    """Leads that used all their attempts without reaching the DM, for the
    cold-email sequence. Rewritten whole each time so it never drifts."""
    path = path or os.path.join(DATA_DIR or ROOT, "out", "exhausted_for_email.csv")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    cols = ["company", "first", "last", "dm_name", "title", "email", "phone", "website", "linkedin_url",
            "process", "oem", "city", "state", "attempts", "last_called_at", "tags"]
    with connect() as con, open(path, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(cols)
        n = 0
        for r in con.execute(f"SELECT {', '.join(cols)} FROM leads WHERE status='EXHAUSTED' ORDER BY last_called_at"):
            w.writerow([r[c] for c in cols])
            n += 1
    return path, n


def undo(dispo_id, agent):
    """Take back a mis-click. Only the agent who made it, only while it is
    still the newest outcome on that lead, only for UNDO_WINDOW_SEC. The lead
    comes back checked out to them, exactly as it was before the outcome.
    Returns (lead_dict, notes, None) or (None, None, reason)."""
    t = now()
    with connect() as con:
        d = con.execute("SELECT * FROM dispositions WHERE id=?", (dispo_id,)).fetchone()
        if d is None:
            return None, None, "Nothing to undo."
        if d["agent"] != agent:
            return None, None, "Only the agent who saved it can undo it."
        if d["at"] < iso(t - timedelta(seconds=UNDO_WINDOW_SEC)):
            return None, None, "Too late to undo: fix it from the lead's history instead."
        newest = con.execute(
            "SELECT id FROM dispositions WHERE phone=? ORDER BY at DESC, id DESC LIMIT 1",
            (d["phone"],)).fetchone()
        if newest["id"] != d["id"] or not d["prev_state"]:
            return None, None, "That lead has moved on; it can no longer be undone."

        snap = json.loads(d["prev_state"])
        had_dnc = snap.pop("had_dnc", False)
        was_exhausted = con.execute("SELECT status FROM leads WHERE phone=?", (d["phone"],)).fetchone()["status"] == "EXHAUSTED"
        live_cols = {r["name"] for r in con.execute("PRAGMA table_info(leads)")}
        fields = [k for k in snap if k in live_cols and k != "status"]
        con.execute(
            f"UPDATE leads SET status='OUT', checked_out_by=?, checked_out_at=?, "
            f"{', '.join(f + '=?' for f in fields)} WHERE phone=?",
            [agent, iso(t)] + [snap[f] for f in fields] + [d["phone"]])
        if d["disposition"] == "DNC" and not had_dnc:
            con.execute("DELETE FROM dnc WHERE phone=?", (d["phone"],))
        con.execute("DELETE FROM dispositions WHERE id=?", (dispo_id,))
        lead = con.execute("SELECT * FROM leads WHERE phone=?", (d["phone"],)).fetchone()
        lead, _ = _with_caller_id(con, lead) if lead is not None else (None, None)
    if was_exhausted:
        export_exhausted()
    return lead, d["notes"], None


def session_start(agent, script_version, target_dials, target_minutes):
    with connect() as con:
        con.execute("UPDATE sessions SET ended_at=? WHERE agent=? AND ended_at IS NULL", (iso(now()), agent))
        return con.execute(
            "INSERT INTO sessions (agent, script_version, target_dials, target_minutes, started_at) VALUES (?,?,?,?,?)",
            (agent, script_version[:24], int(target_dials or 0), int(target_minutes or 0), iso(now()))).lastrowid


def session_end(session_id, active_seconds):
    """Close the session and hand back its own funnel and top objection."""
    with connect() as con:
        con.execute("UPDATE sessions SET ended_at=?, active_seconds=? WHERE id=?",
                    (iso(now()), int(active_seconds or 0), session_id))
        row = con.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
    rows = funnel_rows(session_id=session_id)
    return {"session": dict(row) if row else None, "funnel": funnel.summarise(rows, now()),
            "objections": funnel.top_objections(rows, OBJECTION_LABELS, limit=3)}


def log_webhook(url, body, status, reply):
    with connect() as con:
        con.execute("INSERT INTO webhook_log (at, url, body, status, reply) VALUES (?,?,?,?,?)",
                    (iso(now()), url, body, status, reply))


def agent_event(agent, event, reason=""):
    """Session start / pause / resume / end. Feeds a future manager view."""
    with connect() as con:
        con.execute("INSERT INTO agent_events (agent, event, reason, at) VALUES (?,?,?,?)",
                    (agent, event[:24], reason[:80], iso(now())))


def last_contact_at(phone):
    with connect() as con:
        return con.execute("SELECT MAX(at) FROM dispositions WHERE phone=?",
                           (phone,)).fetchone()[0]


def lookup(phone):
    """Screen-pop for an inbound caller."""
    with connect() as con:
        row = con.execute("SELECT * FROM leads WHERE phone=?", (phone,)).fetchone()
    return dict(row) if row else None


_FUNNEL_COLS = ("phone, disposition, at, agent, duration, pickup, dm, pitched, resonated, offered, "
                "booked, objections, booked_for, show_status, sale, sale_amount, script_version, "
                "number_used, talk_seconds, session_id")


def funnel_rows(start=None, agent=None, script=None, since=None, session_id=None):
    sql, args = f"SELECT {_FUNNEL_COLS} FROM dispositions WHERE disposition != 'SKIP'" + _cd(), []
    for clause, value in (("at >= ?", start), ("agent = ?", agent), ("script_version = ?", script),
                          ("at >= ?", since), ("session_id = ?", session_id)):
        if value:
            sql += " AND " + clause
            args.append(value)
    with connect() as con:
        return [dict(r) for r in con.execute(sql + " ORDER BY at", args)]


def stats(agent=None, range_kind="today", script=None, since=None, labels=None):
    """Queue health plus the Imperium funnel for the asked range."""
    t = now()
    if range_kind not in ("today", "week", "month", "all"):
        range_kind = "today"
    start = funnel.range_start(range_kind, t, STATS_TZ)
    rows = funnel_rows(iso(start) if start else None, agent, script)
    today_rows = rows if range_kind == "today" and not script else funnel_rows(day_start(t), agent)
    with connect() as con:
        row = lambda q, *a: con.execute(q, a).fetchone()[0]
        out = {
            "dials_today": dials_today(con),
            "cap": DAILY_CAP,
            "caps": [{"number": e["number"], "used": _usage(con).get(e["number"], 0),
                      "cap": policy.daily_cap(e, _today_local(), NUMBERS)} for e in _effective_pool(con)],
            "parked": sorted(_parked(con)),
            "callbacks_due": row(
                "SELECT COUNT(*) FROM leads WHERE status='NEW' AND callback_at IS NOT NULL "
                "AND callback_at <= ?" + _cc(), iso(t + timedelta(hours=24))),
            "callbacks_overdue": row(
                "SELECT COUNT(*) FROM leads WHERE status='NEW' AND callback_at IS NOT NULL "
                "AND callback_at <= ?" + _cc(), iso(t)),
            "queue": row(
                "SELECT COUNT(*) FROM leads WHERE status='NEW' AND callback_at IS NULL "
                "AND (next_attempt_at IS NULL OR next_attempt_at <= ?) AND attempts < ?" + _cc(),
                iso(t), MAX_ATTEMPTS),
            "exhausted": row("SELECT COUNT(*) FROM leads WHERE status='EXHAUSTED'" + _cc()),
            "retry_pool": row(
                "SELECT COUNT(*) FROM leads WHERE status='NEW' AND next_attempt_at > ?" + _cc(), iso(t)),
            "bookings_open": row(
                "SELECT COUNT(*) FROM dispositions WHERE booked=1 AND show_status IS NULL AND booked_for <= ?" + _cd(), iso(t)),
        }
    out.update({
        "range": range_kind, "script": script or "",
        "funnel": funnel.summarise(rows, t),
        "by_script": funnel.by_script(rows, t),
        "today": funnel.summarise(today_rows, t),
        "objections": funnel.top_objections(today_rows, labels or OBJECTION_LABELS),
        "server_now": iso(t),
        "window": window_status(t),
    })
    if since:
        out["session"] = funnel.summarise(funnel_rows(agent=agent, since=since), t)
    return out


DAILY_TARGET = 150                 # dials a day when a campaign sets none; config dialer.daily_target


def _calling_ends(con, t):
    """UTC moment today's cold-calling windows close for the last zone that
    still has leads in the current campaign, or None when none open again today."""
    zones = _zones(con, t)
    last = None
    for z in zones:
        local = z["local"].replace(second=0, microsecond=0)
        probe, end = local, None
        for _ in range(24 * 4):
            if policy.tier(probe, WINDOWS) is not None:
                end = probe + timedelta(minutes=15)
            probe += timedelta(minutes=15)
            if probe.date() != local.date():
                break
        if end is not None:
            when = to_utc(z, end)
            if last is None or when > last:
                last = when
    return last


def target_status(at=None):
    """Today's dials against the daily target, the pace so far, and whether
    that pace reaches the target before calling hours end. Also how many
    calling days the campaign still needs to reach every lead once."""
    t = at or now()
    cid = current_campaign()
    start = day_start(t)
    with connect() as con:
        target = DAILY_TARGET
        name = "All campaigns"
        if cid:
            row = con.execute("SELECT name, daily_target FROM campaigns WHERE id=?", (cid,)).fetchone()
            if row is not None:
                name = row["name"]
                target = row["daily_target"] or DAILY_TARGET
        stamps = [r[0] for r in con.execute(
            "SELECT at FROM dispositions WHERE disposition != 'SKIP' AND at >= ?" + _cd() + " ORDER BY at", (start,))]
        untouched = con.execute(
            "SELECT COUNT(*) FROM leads WHERE status IN ('NEW','OUT') AND attempts = 0" + _cc()).fetchone()[0]
        open_leads = con.execute("SELECT COUNT(*) FROM leads WHERE status IN ('NEW','OUT')" + _cc()).fetchone()[0]
        ends = _calling_ends(con, t)
    done = len(stamps)
    per_hour = None
    if done >= 3:
        span = (t - datetime.fromisoformat(stamps[0])).total_seconds() / 3600.0
        if span >= 0.25:
            per_hour = done / span
    left = max(0, target - done)
    eta = None
    if left == 0:
        verdict = "hit"
    elif per_hour:
        eta = t + timedelta(hours=left / per_hour)
        verdict = "on_track" if (ends is None or eta <= ends) else "short"
    else:
        verdict = "no_pace"
    by_end = None
    if per_hour and ends is not None and ends > t:
        by_end = done + int(per_hour * (ends - t).total_seconds() / 3600.0)
    return {
        "campaign": name, "target": target, "done": done, "left": left,
        "per_hour": round(per_hour, 1) if per_hour else None,
        "eta": iso(eta) if eta else None, "calling_ends": iso(ends) if ends else None,
        "projected_by_end": by_end, "verdict": verdict,
        "untouched": untouched, "open": open_leads,
        "days_to_first_touch": (-(-untouched // target)) if target else None,
    }


def analytics(period="day", count=None, agent=None):
    """Day, week or month series for the current campaign."""
    period = period if period in ("day", "week", "month") else "day"
    count = int(count or {"day": 30, "week": 12, "month": 12}[period])
    t = now()
    first = {"day": t - timedelta(days=count + 2), "week": t - timedelta(weeks=count + 1),
             "month": t - timedelta(days=31 * (count + 1))}[period]
    rows = funnel_rows(iso(first), agent)
    return {"period": period, "series": funnel.series(rows, STATS_TZ, period, count, t),
            "totals": funnel.summarise(rows, t), "tz": STATS_TZ}


def funnel_sheet(range_kind="all", agent=None, labels=None):
    start = funnel.range_start(range_kind, now(), STATS_TZ)
    return funnel.daily_sheet(funnel_rows(iso(start) if start else None, agent), STATS_TZ,
                              labels or OBJECTION_LABELS, now())


# --------------------------------------------------------------- exports --

def export_suppression(called_log_path, dnc_path):
    """Dump DB state into the CSV shapes listprep.py reads for suppression."""
    with connect() as con:
        os.makedirs(os.path.dirname(called_log_path), exist_ok=True)
        with open(called_log_path, "w", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["phone_e164", "last_called_at", "attempts", "last_disposition"])
            for r in con.execute(
                    "SELECT phone, last_called_at, attempts, last_disposition "
                    "FROM leads WHERE last_called_at IS NOT NULL"):
                w.writerow([r["phone"], r["last_called_at"], r["attempts"],
                            r["last_disposition"] or ""])
        with open(dnc_path, "w", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["phone_e164", "reason", "added_at", "added_by"])
            for r in con.execute("SELECT * FROM dnc"):
                w.writerow([r["phone"], r["reason"], r["added_at"], r["added_by"]])
