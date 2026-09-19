"""SQLite state for the dialer.

One file on the volume (DATA_DIR/dialer.db, or repo root locally) owns what
the CSVs used to hold, plus what they couldn't: atomic lead checkout so two
agents never dial the same number, retry scheduling, callbacks, per-day dial
caps computed from actual history, and notes.

Policy constants mirror config.yaml (listprep reads the YAML; the dialer
reads these) — change both or wire yaml here if they drift.
"""

import csv
import json
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

RETRYABLE = {"NO_ANSWER", "VOICEMAIL", "BUSY", "GATEKEEPER"}
FINAL = {"INTERESTED", "NOT_INT", "WRONG_NUMBER", "DISCONNECTED", "DNC"}
# "Connect" = a human picked up, whoever it was.
CONNECTED = {"INTERESTED", "NOT_INT", "CALLBACK", "DNC", "GATEKEEPER", "WRONG_NUMBER"}
UNDO_WINDOW_SEC = 120


def configure(outcomes):
    """Let serve.py drive retry/connect semantics from config.yaml's
    dialer.outcomes so the UI and the queue can never disagree."""
    global RETRYABLE, FINAL, CONNECTED
    if not outcomes:
        return
    RETRYABLE = {o["key"] for o in outcomes if o.get("kind") == "retry"}
    FINAL = {o["key"] for o in outcomes if o.get("kind") in ("final", "dnc")}
    CONNECTED = {o["key"] for o in outcomes if o.get("connect")}

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
CREATE TABLE IF NOT EXISTS agent_events (
  id INTEGER PRIMARY KEY,
  agent TEXT, event TEXT, reason TEXT DEFAULT '', at TEXT
);
CREATE INDEX IF NOT EXISTS idx_agent_events ON agent_events(agent, at);
"""

# Columns added after the first deploy; applied idempotently by init().
MIGRATIONS = [
    ("dispositions", "prev_state", "TEXT"),      # JSON lead snapshot, powers undo
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
        for table, column, decl in MIGRATIONS:
            have = {r["name"] for r in con.execute(f"PRAGMA table_info({table})")}
            if column not in have:
                con.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")


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

        # A reload mid-lead must not strand it for the TTL: if this agent
        # already holds one, hand the same lead back.
        held = con.execute(
            "SELECT * FROM leads WHERE status='OUT' AND checked_out_by=? "
            "ORDER BY checked_out_at DESC LIMIT 1", (agent,)).fetchone()
        if held is not None:
            con.execute("UPDATE leads SET checked_out_at=? WHERE id=?", (iso(t), held["id"]))
            return dict(held), None

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


def history(phone, limit=20):
    with connect() as con:
        return [dict(r) for r in con.execute(
            "SELECT id, disposition, notes, agent, duration, at FROM dispositions "
            "WHERE phone=? ORDER BY at DESC, id DESC LIMIT ?", (phone, limit))]


def queue_preview(limit=5):
    with connect() as con:
        return [dict(r) for r in con.execute(
            """SELECT company, state, rank FROM leads WHERE status='NEW'
               AND callback_at IS NULL AND (next_attempt_at IS NULL OR next_attempt_at <= ?)
               ORDER BY skipped, rank DESC LIMIT ?""", (iso(now()), limit))]


_LIST_COLS = ("phone, first, last, company, title, city, state, tz_offset, rank, "
              "attempts, status, last_disposition, last_called_at, next_attempt_at, "
              "callback_at, checked_out_by")


def _list_row(r, t):
    d = dict(r)
    d["in_window"] = in_window(d["tz_offset"], t, callback=bool(d["callback_at"]))
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
                    WHERE company LIKE ? OR first LIKE ? OR last LIKE ?
                       OR (first || ' ' || last) LIKE ? OR state LIKE ?
                       OR (? != '' AND phone LIKE ?)
                    ORDER BY (status='NEW') DESC, rank DESC LIMIT ?""",
                (like, like, like, like, q, digits, f"%{digits}%", limit)).fetchall()
        else:
            rows = con.execute(
                f"""SELECT {_LIST_COLS} FROM leads WHERE status='NEW'
                    AND callback_at IS NULL
                    AND (next_attempt_at IS NULL OR next_attempt_at <= ?)
                    AND attempts < ?
                    ORDER BY skipped, rank DESC, attempts LIMIT ?""",
                (iso(t), MAX_ATTEMPTS, limit)).fetchall()
        return [_list_row(r, t) for r in rows]


def callbacks_list(limit=100):
    """Every scheduled callback, soonest first, with the note that set it."""
    t = now()
    with connect() as con:
        rows = con.execute(
            f"""SELECT {_LIST_COLS} FROM leads
                WHERE status IN ('NEW','OUT') AND callback_at IS NOT NULL
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
    day = iso(now())[:10]
    sql = ("""SELECT d.id, d.phone, d.company, d.disposition, d.notes, d.agent,
                     d.duration, d.at, l.first, l.last, l.tz_offset, l.status
              FROM dispositions d LEFT JOIN leads l ON l.phone = d.phone
              WHERE d.at >= ? AND d.disposition != 'SKIP'""")
    args = [day]
    if agent:
        sql += " AND d.agent = ?"
        args.append(agent)
    sql += " ORDER BY d.at DESC, d.id DESC LIMIT ?"
    args.append(limit)
    with connect() as con:
        return [dict(r) for r in con.execute(sql, args)]


def _drop_untouched_manual(con, phone):
    """A hand-typed number that was opened but never dialed is not a lead;
    leaving the bare row behind would feed it to the auto-queue later."""
    return con.execute(
        "DELETE FROM leads WHERE phone=? AND list_id='manual' AND attempts=0 "
        "AND company='' AND callback_at IS NULL", (phone,)).rowcount > 0


def skip(phone, agent):
    """Back of the queue. Clears any callback timer — a skipped due-callback
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


def checkout_specific(phone, agent, tz_offset=None):
    """Agent picked a lead by hand (queue click, callback, typed number).
    Same guard rails as the automatic path: DNC, daily cap, the lead-local
    calling window, and no stealing a lead another agent has open. Unknown
    numbers get a bare lead row so the call is logged like any other.
    Returns (lead_dict, None) or (None, reason_string)."""
    t = now()
    with connect() as con:
        if con.execute("SELECT 1 FROM dnc WHERE phone=?", (phone,)).fetchone():
            return None, "That number is on the do-not-call list."
        done = dials_today(con)
        if done >= DAILY_CAP:
            return None, f"Daily cap reached ({done}/{DAILY_CAP} dials on this number)."

        row = con.execute("SELECT * FROM leads WHERE phone=?", (phone,)).fetchone()
        if row is None:
            con.execute(
                "INSERT INTO leads (phone, company, tz_offset, list_id, source_file, created_at) "
                "VALUES (?,?,?,?,?,?)",
                (phone, "", tz_offset if tz_offset is not None else -5,
                 "manual", "manual dial", iso(t)))
            row = con.execute("SELECT * FROM leads WHERE phone=?", (phone,)).fetchone()

        if row["status"] == "DNC":
            return None, "That lead is marked do-not-call."
        stale = iso(t - timedelta(minutes=CHECKOUT_TTL_MIN))
        if (row["status"] == "OUT" and row["checked_out_by"] not in (None, agent)
                and (row["checked_out_at"] or "") >= stale):
            return None, f"{row['checked_out_by']} has that lead open right now."
        if not in_window(row["tz_offset"], t, callback=True):
            return None, "Outside that lead's local calling hours (8am to 9pm their time)."

        con.execute(
            "UPDATE leads SET status='OUT', checked_out_by=?, checked_out_at=? WHERE id=?",
            (agent, iso(t), row["id"]))
        return dict(row), None


def reschedule(phone, callback_at):
    """Move a callback, or drop it (callback_at=None) back into the normal queue."""
    with connect() as con:
        cur = con.execute(
            "UPDATE leads SET callback_at=?, status='NEW', checked_out_by=NULL "
            "WHERE phone=? AND status IN ('NEW','OUT','DONE')", (callback_at, phone))
        return cur.rowcount > 0


_SNAP = ("status", "attempts", "last_disposition", "last_called_at",
         "next_attempt_at", "callback_at", "skipped")


def disposition(phone, company, dispo, notes, agent, duration, callback_at=None):
    """Record an outcome and advance the lead. Returns the disposition id,
    which /api/undo takes while the agent's undo toast is still up."""
    t = iso(now())
    with connect() as con:
        lead = con.execute("SELECT * FROM leads WHERE phone=?", (phone,)).fetchone()
        snap = {k: lead[k] for k in _SNAP} if lead else None
        if snap is not None:
            snap["had_dnc"] = con.execute(
                "SELECT 1 FROM dnc WHERE phone=?", (phone,)).fetchone() is not None

        cur = con.execute(
            "INSERT INTO dispositions (phone, company, disposition, notes, agent, duration, at, prev_state) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (phone, company, dispo, notes, agent, duration, t,
             json.dumps(snap) if snap is not None else None))
        dispo_id = cur.lastrowid

        if dispo == "DNC":
            con.execute("INSERT OR REPLACE INTO dnc VALUES (?,?,?,?)",
                        (phone, notes or "agent_request", t, agent))

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
        return dispo_id


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
            return None, None, "Too late to undo — fix it from the lead's history instead."
        newest = con.execute(
            "SELECT id FROM dispositions WHERE phone=? ORDER BY at DESC, id DESC LIMIT 1",
            (d["phone"],)).fetchone()
        if newest["id"] != d["id"] or not d["prev_state"]:
            return None, None, "That lead has moved on; it can no longer be undone."

        snap = json.loads(d["prev_state"])
        con.execute(
            """UPDATE leads SET status='OUT', checked_out_by=?, checked_out_at=?,
               attempts=?, last_disposition=?, last_called_at=?, next_attempt_at=?,
               callback_at=?, skipped=? WHERE phone=?""",
            (agent, iso(t), snap["attempts"], snap["last_disposition"],
             snap["last_called_at"], snap["next_attempt_at"], snap["callback_at"],
             snap["skipped"], d["phone"]))
        if d["disposition"] == "DNC" and not snap.get("had_dnc"):
            con.execute("DELETE FROM dnc WHERE phone=?", (d["phone"],))
        con.execute("DELETE FROM dispositions WHERE id=?", (dispo_id,))
        lead = con.execute("SELECT * FROM leads WHERE phone=?", (d["phone"],)).fetchone()
        return dict(lead), d["notes"], None


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


def stats(agent=None):
    t = now()
    day = iso(t)[:10]
    with connect() as con:
        dials = dials_today(con)
        row = lambda q, *a: con.execute(q, a).fetchone()[0]
        marks = ",".join("?" * len(CONNECTED)) or "''"
        connected = sorted(CONNECTED)
        out = {
            "dials_today": dials,
            "cap": DAILY_CAP,
            "connects_today": row(
                f"SELECT COUNT(*) FROM dispositions WHERE at>=? AND disposition IN ({marks})",
                day, *connected),
            "interested_today": row(
                "SELECT COUNT(*) FROM dispositions WHERE at>=? AND disposition='INTERESTED'", day),
            "callbacks_due": row(
                "SELECT COUNT(*) FROM leads WHERE status='NEW' AND callback_at IS NOT NULL "
                "AND callback_at <= ?", iso(t + timedelta(hours=24))),
            "callbacks_overdue": row(
                "SELECT COUNT(*) FROM leads WHERE status='NEW' AND callback_at IS NOT NULL "
                "AND callback_at <= ?", iso(t)),
            "queue": row(
                "SELECT COUNT(*) FROM leads WHERE status='NEW' AND callback_at IS NULL "
                "AND (next_attempt_at IS NULL OR next_attempt_at <= ?) AND attempts < ?",
                iso(t), MAX_ATTEMPTS),
            "retry_pool": row(
                "SELECT COUNT(*) FROM leads WHERE status='NEW' AND next_attempt_at > ?", iso(t)),
        }
        # Per-agent productivity. The cap above is per caller ID, so it stays
        # floor-wide; everything below is "how is *my* day going".
        who, args = ("AND agent=?", [agent]) if agent else ("", [])
        mine = con.execute(
            f"SELECT disposition, COUNT(*) n, COALESCE(SUM(duration),0) secs, MIN(at) first_at "
            f"FROM dispositions WHERE at>=? AND disposition!='SKIP' {who} GROUP BY disposition",
            [day] + args).fetchall()
        out["outcomes"] = {r["disposition"]: r["n"] for r in mine}
        out["my_dials"] = sum(r["n"] for r in mine)
        out["my_connects"] = sum(r["n"] for r in mine if r["disposition"] in CONNECTED)
        out["talk_seconds"] = sum(r["secs"] for r in mine if r["disposition"] in CONNECTED)
        firsts = [r["first_at"] for r in mine if r["first_at"]]
        out["first_dial_at"] = min(firsts) if firsts else None
        out["server_now"] = iso(t)
        return out


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
