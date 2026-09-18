"""SQLite state for the dialer.

One file on the volume (DATA_DIR/dialer.db, or repo root locally) owns what
the CSVs used to hold, plus what they couldn't: atomic lead checkout so two
agents never dial the same number, retry scheduling, callbacks, per-day dial
caps computed from actual history, and notes.

Policy constants mirror config.yaml (listprep reads the YAML; the dialer
reads these) — change both or wire yaml here if they drift.
"""

import csv
import os
import sqlite3
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.environ.get("DATA_DIR")
DB_PATH = os.path.join(DATA_DIR or ROOT, "dialer.db")

MAX_ATTEMPTS = int(os.environ.get("MAX_ATTEMPTS", 5))
RETRY_HOURS = int(os.environ.get("RETRY_HOURS", 24))
DAILY_CAP = int(os.environ.get("DAILY_CAP", 150))
CHECKOUT_TTL_MIN = 10

# Lead-local calling windows (hours). Weekday satisfies both the US TCPA
# floor (8-21) and CRTC (9-21:30); weekend uses the tighter CRTC rule.
# Override with e.g. WINDOW_WEEKDAY="9-20.5" — do not widen past 8-21.
def _window(env, default):
    raw = os.environ.get(env)
    if raw and "-" in raw:
        lo, _, hi = raw.partition("-")
        return (float(lo), float(hi))
    return default


WINDOW_WEEKDAY = _window("WINDOW_WEEKDAY", (9.0, 20.5))
WINDOW_WEEKEND = _window("WINDOW_WEEKEND", (10.0, 18.0))
CALLBACK_WINDOW = _window("WINDOW_CALLBACK", (8.0, 21.0))

RETRYABLE = {"NO_ANSWER", "VOICEMAIL", "BUSY"}
FINAL = {"INTERESTED", "NOT_INT", "DNC"}
CONNECTED = {"INTERESTED", "NOT_INT", "CALLBACK", "DNC"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS leads (
  id INTEGER PRIMARY KEY,
  phone TEXT UNIQUE NOT NULL,
  first TEXT DEFAULT '', last TEXT DEFAULT '', company TEXT DEFAULT '',
  title TEXT DEFAULT '', city TEXT DEFAULT '', state TEXT DEFAULT '',
  tz_offset REAL DEFAULT -5,
  rank INTEGER DEFAULT 0,
  tiktok_followers TEXT DEFAULT '', company_size TEXT DEFAULT '',
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
  agent TEXT DEFAULT '', duration INTEGER DEFAULT 0, at TEXT
);
CREATE INDEX IF NOT EXISTS idx_dispo_at ON dispositions(at);
CREATE INDEX IF NOT EXISTS idx_dispo_phone ON dispositions(phone);
CREATE TABLE IF NOT EXISTS dnc (
  phone TEXT PRIMARY KEY, reason TEXT, added_at TEXT, added_by TEXT
);
"""


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


def lead_count(con=None):
    with connect() as con:
        return con.execute("SELECT COUNT(*) FROM leads").fetchone()[0]


# ---------------------------------------------------------------- import --

def import_list_csv(path):
    """Load a listprep-generated VICIdial CSV. Returns (added, refreshed)."""
    stamp = iso(now())
    source = os.path.basename(path)
    processed = 0
    with connect() as con, open(path, newline="") as fh:
        before = con.execute("SELECT COUNT(*) FROM leads").fetchone()[0]
        for row in csv.DictReader(fh):
            phone = "+" + (row.get("phone_code") or "1") + (row.get("phone_number") or "")
            if len(phone) != 12:
                continue
            fields = {
                "first": row.get("first_name") or "",
                "last": row.get("last_name") or "",
                "company": row.get("address3") or "",
                "title": row.get("job_title") or "",
                "city": row.get("city") or "",
                "state": row.get("state") or "",
                "tz_offset": float(row.get("gmt_offset_now") or -5),
                "rank": int(row.get("rank") or 0),
                "tiktok_followers": row.get("tiktok_followers") or "",
                "company_size": row.get("company_size") or "",
                "list_id": row.get("list_id") or "",
            }
            con.execute(
                """INSERT INTO leads (phone, first, last, company, title, city, state,
                     tz_offset, rank, tiktok_followers, company_size, list_id,
                     source_file, created_at)
                   VALUES (:phone,:first,:last,:company,:title,:city,:state,
                     :tz_offset,:rank,:tiktok_followers,:company_size,:list_id,
                     :source_file,:created_at)
                   ON CONFLICT(phone) DO UPDATE SET
                     rank=:rank, list_id=:list_id, source_file=:source_file
                   WHERE leads.status='NEW'""",
                dict(fields, phone=phone, source_file=source, created_at=stamp))
            processed += 1
        added = con.execute("SELECT COUNT(*) FROM leads").fetchone()[0] - before
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

def in_window(tz_offset, at=None, callback=False):
    """Is it callable now at the lead's local (UTC+offset) time?"""
    local = (at or now()) + timedelta(hours=tz_offset or -5)
    if callback:
        lo, hi = CALLBACK_WINDOW
    elif local.weekday() >= 5:
        lo, hi = WINDOW_WEEKEND
    else:
        lo, hi = WINDOW_WEEKDAY
    h = local.hour + local.minute / 60.0
    return lo <= h < hi


def dials_today(con):
    day = iso(now())[:10]
    return con.execute(
        "SELECT COUNT(*) FROM dispositions WHERE at>=? AND disposition!='SKIP'",
        (day,)).fetchone()[0]


def checkout(agent):
    """Atomically hand the next dialable lead to `agent`.

    Priority: due callbacks, then fresh leads by rank, then skipped ones.
    Returns (lead_dict, None) or (None, reason_string).
    """
    t = now()
    with connect() as con:
        con.execute(
            "UPDATE leads SET status='NEW', checked_out_by=NULL WHERE status='OUT' AND checked_out_at < ?",
            (iso(t - timedelta(minutes=CHECKOUT_TTL_MIN)),))

        done = dials_today(con)
        if done >= DAILY_CAP:
            return None, f"Daily cap reached ({done}/{DAILY_CAP} dials on this number)."

        # Due callbacks first — they may stretch the normal window a little.
        rows = con.execute(
            """SELECT * FROM leads WHERE status='NEW' AND callback_at IS NOT NULL
               AND callback_at <= ? ORDER BY callback_at LIMIT 50""", (iso(t),)).fetchall()
        pick = next((r for r in rows if in_window(r["tz_offset"], t, callback=True)), None)

        if pick is None:
            rows = con.execute(
                """SELECT * FROM leads WHERE status='NEW' AND callback_at IS NULL
                   AND (next_attempt_at IS NULL OR next_attempt_at <= ?)
                   AND attempts < ?
                   ORDER BY skipped, rank DESC, attempts LIMIT 400""",
                (iso(t), MAX_ATTEMPTS)).fetchall()
            pick = next((r for r in rows if in_window(r["tz_offset"], t)), None)
            if pick is None and rows:
                return None, ("Leads remain, but none are inside their local "
                              "calling window right now.")

        if pick is None:
            return None, "Queue is empty — load a new list or wait for retries to come due."

        cur = con.execute(
            "UPDATE leads SET status='OUT', checked_out_by=?, checked_out_at=? "
            "WHERE id=? AND status='NEW'", (agent, iso(t), pick["id"]))
        if cur.rowcount == 0:            # lost the race; caller retries
            return checkout(agent)
        return dict(pick), None


def history(phone, limit=3):
    with connect() as con:
        return [dict(r) for r in con.execute(
            "SELECT disposition, notes, agent, duration, at FROM dispositions "
            "WHERE phone=? ORDER BY at DESC LIMIT ?", (phone, limit))]


def queue_preview(limit=5):
    with connect() as con:
        return [dict(r) for r in con.execute(
            """SELECT company, state, rank FROM leads WHERE status='NEW'
               AND callback_at IS NULL AND (next_attempt_at IS NULL OR next_attempt_at <= ?)
               ORDER BY skipped, rank DESC LIMIT ?""", (iso(now()), limit))]


def skip(phone, agent):
    """Back of the queue. Clears any callback timer — a skipped due-callback
    must not boomerang straight back to the top."""
    with connect() as con:
        con.execute(
            "UPDATE leads SET status='NEW', skipped=1, callback_at=NULL, "
            "checked_out_by=NULL WHERE phone=?", (phone,))


def release(phone):
    """Lead handed back untouched (e.g. tab closed cleanly)."""
    with connect() as con:
        con.execute(
            "UPDATE leads SET status='NEW', checked_out_by=NULL WHERE phone=? AND status='OUT'",
            (phone,))


def disposition(phone, company, dispo, notes, agent, duration, callback_at=None):
    t = iso(now())
    with connect() as con:
        con.execute(
            "INSERT INTO dispositions (phone, company, disposition, notes, agent, duration, at) "
            "VALUES (?,?,?,?,?,?,?)", (phone, company, dispo, notes, agent, duration, t))

        if dispo == "DNC":
            con.execute("INSERT OR REPLACE INTO dnc VALUES (?,?,?,?)",
                        (phone, notes or "agent_request", t, agent))

        lead = con.execute("SELECT attempts FROM leads WHERE phone=?", (phone,)).fetchone()
        attempts = (lead["attempts"] if lead else 0) + 1

        if dispo == "CALLBACK" and callback_at:
            status, next_at, cb = "NEW", None, callback_at
        elif dispo in RETRYABLE and attempts < MAX_ATTEMPTS:
            status, next_at, cb = "NEW", iso(now() + timedelta(hours=RETRY_HOURS)), None
        elif dispo == "DNC":
            status, next_at, cb = "DNC", None, None
        else:
            status, next_at, cb = "DONE", None, None

        con.execute(
            """UPDATE leads SET status=?, attempts=?, last_disposition=?, last_called_at=?,
               next_attempt_at=?, callback_at=?, checked_out_by=NULL WHERE phone=?""",
            (status, attempts, dispo, t, next_at, cb, phone))


def lookup(phone):
    """Screen-pop for an inbound caller."""
    with connect() as con:
        row = con.execute("SELECT * FROM leads WHERE phone=?", (phone,)).fetchone()
    return dict(row) if row else None


def stats():
    t = now()
    day = iso(t)[:10]
    with connect() as con:
        dials = dials_today(con)
        row = lambda q, *a: con.execute(q, a).fetchone()[0]
        return {
            "dials_today": dials,
            "cap": DAILY_CAP,
            "connects_today": row(
                "SELECT COUNT(*) FROM dispositions WHERE at>=? AND disposition IN "
                "('INTERESTED','NOT_INT','CALLBACK','DNC')", day),
            "interested_today": row(
                "SELECT COUNT(*) FROM dispositions WHERE at>=? AND disposition='INTERESTED'", day),
            "callbacks_due": row(
                "SELECT COUNT(*) FROM leads WHERE status='NEW' AND callback_at IS NOT NULL "
                "AND callback_at <= ?", iso(t + timedelta(hours=24))),
            "queue": row(
                "SELECT COUNT(*) FROM leads WHERE status='NEW' AND callback_at IS NULL "
                "AND (next_attempt_at IS NULL OR next_attempt_at <= ?) AND attempts < ?",
                iso(t), MAX_ATTEMPTS),
            "retry_pool": row(
                "SELECT COUNT(*) FROM leads WHERE status='NEW' AND next_attempt_at > ?", iso(t)),
        }


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
