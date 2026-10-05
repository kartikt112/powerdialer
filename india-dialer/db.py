"""SQLite state for the India dialer.

One file (india-dialer/data/india.db, or $INDIA_DATA_DIR) holds the leads,
their numbers, every call, the sample / demo pipeline, follow-ups, DNC,
sessions and pause events. Times are stored in UTC as 'YYYY-MM-DD HH:MM:SS';
everything the caller sees is IST. Python 3.9 compatible.
"""

import json
import os
import sqlite3
from datetime import datetime, timedelta, timezone

import common
import intake

DB_PATH = os.path.join(common.DATA_DIR, "india.db")
CHECKOUT_TTL_MIN = 15
UNDO_WINDOW_SEC = 180

SCHEMA = """
CREATE TABLE IF NOT EXISTS leads (
  id INTEGER PRIMARY KEY,
  company TEXT NOT NULL, company_key TEXT UNIQUE NOT NULL,
  tier TEXT DEFAULT 'B', city TEXT DEFAULT '', state TEXT DEFAULT '',
  type_raw TEXT DEFAULT '', segment TEXT DEFAULT '',
  ask_for TEXT DEFAULT '', why TEXT DEFAULT '',
  est_drawings_month TEXT DEFAULT '', est_volume INTEGER DEFAULT 0,
  website TEXT DEFAULT '', flags TEXT DEFAULT '', source TEXT DEFAULT '', phone_source TEXT DEFAULT '',
  hold INTEGER DEFAULT 0, hold_reason TEXT DEFAULT '', cleared_at TEXT, cleared_by TEXT,
  dm_name TEXT DEFAULT '', dm_title TEXT DEFAULT '', dm_mobile TEXT DEFAULT '',
  email TEXT DEFAULT '', whatsapp TEXT DEFAULT '', language_pref TEXT DEFAULT '',
  actual_drawings_month TEXT DEFAULT '', current_method TEXT DEFAULT '', software_used TEXT DEFAULT '',
  pain TEXT DEFAULT '', notes TEXT DEFAULT '',
  status TEXT DEFAULT 'NEW',          -- NEW OUT PIPELINE DONE DNC EXHAUSTED INTL NO_PHONE
  list_tag TEXT DEFAULT '',           -- whatsapp_linkedin once exhausted
  attempts INTEGER DEFAULT 0, last_outcome TEXT, last_called_at TEXT,
  next_action_at TEXT, next_action_type TEXT, next_half TEXT DEFAULT '', skip_until TEXT,
  dm_ever INTEGER DEFAULT 0, interested_before INTEGER DEFAULT 0,
  raw_score INTEGER DEFAULT 0, rank INTEGER DEFAULT 0,
  checked_out_by TEXT, checked_out_at TEXT,
  referred_by INTEGER, international TEXT DEFAULT '',
  source_file TEXT DEFAULT '', created_at TEXT, updated_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_leads_status ON leads(status);

CREATE TABLE IF NOT EXISTS phones (
  id INTEGER PRIMARY KEY,
  lead_id INTEGER NOT NULL, e164 TEXT NOT NULL, dial TEXT NOT NULL,
  kind TEXT NOT NULL, raw TEXT DEFAULT '', position INTEGER DEFAULT 0,
  origin TEXT DEFAULT 'list',          -- list | dm | added | referral
  bad INTEGER DEFAULT 0, last_dialed_at TEXT, last_result TEXT,
  UNIQUE(lead_id, e164)
);
CREATE INDEX IF NOT EXISTS idx_phones_e164 ON phones(e164);

CREATE TABLE IF NOT EXISTS calls (
  id INTEGER PRIMARY KEY,
  lead_id INTEGER NOT NULL, phone_id INTEGER, number TEXT, number_kind TEXT,
  outcome TEXT NOT NULL, attempt_no INTEGER, continued INTEGER DEFAULT 0,
  dialed_at TEXT NOT NULL, connected_at TEXT, ended_at TEXT,
  ring_seconds INTEGER DEFAULT 0, talk_seconds INTEGER DEFAULT 0,
  pickup INTEGER DEFAULT 0, dm INTEGER DEFAULT 0, pitched INTEGER DEFAULT 0,
  interested INTEGER DEFAULT 0, sample_asked INTEGER DEFAULT 0, demo_booked INTEGER DEFAULT 0,
  objections TEXT, pain TEXT DEFAULT '', notes TEXT DEFAULT '',
  actual_drawings_month TEXT DEFAULT '', current_method TEXT DEFAULT '', software_used TEXT DEFAULT '',
  demo_at TEXT, callback_at TEXT,
  script_version TEXT DEFAULT '', session_id INTEGER, agent TEXT DEFAULT '',
  tier TEXT DEFAULT '', segment TEXT DEFAULT '', state TEXT DEFAULT '', city TEXT DEFAULT '',
  hour_ist INTEGER, prev_state TEXT, created_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_calls_dialed ON calls(dialed_at);
CREATE INDEX IF NOT EXISTS idx_calls_lead ON calls(lead_id);

-- The sample / demo pipeline. Everything here is credited to the dial that
-- created it (origin_call_id), not to the day the stage moved.
CREATE TABLE IF NOT EXISTS samples (
  id INTEGER PRIMARY KEY,
  lead_id INTEGER NOT NULL, origin_call_id INTEGER, kind TEXT DEFAULT 'sample',   -- sample | demo
  stage TEXT DEFAULT 'asked',          -- demo asked received delivered feedback quote won lost
  channel TEXT DEFAULT '',             -- whatsapp | email
  asked_at TEXT, demo_at TEXT, received_at TEXT, delivered_at TEXT, turnaround_min INTEGER,
  feedback TEXT, feedback_note TEXT DEFAULT '', feedback_at TEXT, quote_sent_at TEXT,
  won_at TEXT, lost_at TEXT, lost_reason TEXT DEFAULT '',
  deal_value_inr INTEGER DEFAULT 0, drawings_committed INTEGER DEFAULT 0, updated_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_samples_lead ON samples(lead_id);

CREATE TABLE IF NOT EXISTS followups (
  id INTEGER PRIMARY KEY, lead_id INTEGER NOT NULL, sample_id INTEGER,
  channel TEXT, template TEXT, to_addr TEXT DEFAULT '', at TEXT, agent TEXT DEFAULT ''
);

-- Permanent. `key` is an E.164 number or 'company:<company_key>'.
CREATE TABLE IF NOT EXISTS dnc (
  key TEXT PRIMARY KEY, reason TEXT DEFAULT '', added_at TEXT, added_by TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS sessions (
  id INTEGER PRIMARY KEY, agent TEXT, script_version TEXT DEFAULT '',
  target_dials INTEGER, target_minutes INTEGER, started_at TEXT, ended_at TEXT, active_seconds INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS agent_events (
  id INTEGER PRIMARY KEY, agent TEXT, event TEXT, reason TEXT DEFAULT '', at TEXT
);
"""

LEAD_UPDATABLE = ("tier", "city", "state", "type_raw", "segment", "ask_for", "why", "est_drawings_month",
                  "est_volume", "website", "flags", "source", "phone_source", "dm_name", "dm_title", "email",
                  "whatsapp", "language_pref")


# ------------------------------------------------------------------ basics --

# Rehearsal only: INDIA_CLOCK_SHIFT_MIN moves the server clock so the calling
# windows can be tried outside office hours. serve.py warns when it is set.
CLOCK_SHIFT = timedelta(minutes=float(os.environ.get("INDIA_CLOCK_SHIFT_MIN") or 0))


def now():
    return (datetime.now(timezone.utc) + CLOCK_SHIFT).replace(microsecond=0, tzinfo=None)


def iso(dt):
    return dt.isoformat(sep=" ") if dt else None


def parse(stamp):
    return datetime.fromisoformat(stamp) if stamp else None


def connect():
    con = sqlite3.connect(DB_PATH, timeout=10)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA foreign_keys=ON")
    return con


def init():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    with connect() as con:
        con.executescript(SCHEMA)


def lead_count():
    with connect() as con:
        return con.execute("SELECT COUNT(*) FROM leads").fetchone()[0]


# --------------------------------------------------------------------- DNC --

def dnc_keys(con):
    return {r["key"] for r in con.execute("SELECT key FROM dnc")}


def add_dnc(con, lead_id, reason, agent):
    """Block every number on the lead and the company itself, for good."""
    t = iso(now())
    lead = con.execute("SELECT company_key FROM leads WHERE id=?", (lead_id,)).fetchone()
    keys = [r["e164"] for r in con.execute("SELECT e164 FROM phones WHERE lead_id=?", (lead_id,))]
    if lead is not None:
        keys.append("company:" + lead["company_key"])
    for key in keys:
        con.execute("INSERT OR IGNORE INTO dnc (key, reason, added_at, added_by) VALUES (?,?,?,?)",
                    (key, reason or "asked not to call", t, agent))
    con.execute("UPDATE leads SET status='DNC', checked_out_by=NULL, next_action_at=NULL, next_action_type=NULL "
                "WHERE id=?", (lead_id,))


# ------------------------------------------------------------------ import --

def import_leads(leads, source_file, cfg):
    """Insert new leads, refresh untouched ones, never disturb worked ones.
    Returns a report dict for the CLI."""
    t = iso(now())
    report = {"added": [], "refreshed": [], "kept_worked": [], "dnc": [], "held": [], "intl": [], "no_phone": [],
              "numbers_added": 0}
    with connect() as con:
        blocked = dnc_keys(con)
        for lead in leads:
            row = con.execute("SELECT * FROM leads WHERE company_key=?", (lead["company_key"],)).fetchone()
            numbers = [n for n in lead["numbers"]]
            is_dnc = ("company:" + lead["company_key"]) in blocked or any(n["e164"] in blocked for n in numbers)
            if row is None:
                status = "DNC" if is_dnc else "INTL" if (not numbers and lead["international"]) else \
                    "NO_PHONE" if not numbers else "NEW"
                fields = {k: lead.get(k, "") for k in LEAD_UPDATABLE}
                fields.update(company=lead["company"], company_key=lead["company_key"], status=status,
                              hold=1 if lead["hold_reason"] else 0, hold_reason=lead["hold_reason"],
                              international=" / ".join(lead["international"]), source_file=source_file,
                              created_at=t, updated_at=t)
                cols = ", ".join(fields)
                cur = con.execute(f"INSERT INTO leads ({cols}) VALUES ({', '.join('?' * len(fields))})",
                                  list(fields.values()))
                lead_id = cur.lastrowid
                report["added"].append(lead["company"])
            else:
                lead_id = row["id"]
                if row["attempts"] == 0 and row["status"] in ("NEW", "NO_PHONE", "INTL"):
                    sets = {k: lead[k] for k in LEAD_UPDATABLE if lead.get(k) not in (None, "", 0)}
                    if lead["hold_reason"] and not row["cleared_at"]:
                        sets.update(hold=1, hold_reason=lead["hold_reason"])
                    sets.update(updated_at=t, source_file=source_file)
                    con.execute(f"UPDATE leads SET {', '.join(k + '=?' for k in sets)} WHERE id=?",
                                list(sets.values()) + [lead_id])
                    report["refreshed"].append(lead["company"])
                else:
                    report["kept_worked"].append(lead["company"])
                if is_dnc and row["status"] != "DNC":
                    con.execute("UPDATE leads SET status='DNC' WHERE id=?", (lead_id,))
            pos = con.execute("SELECT COALESCE(MAX(position), -1) FROM phones WHERE lead_id=?", (lead_id,)).fetchone()[0]
            for n in numbers:
                pos += 1
                cur = con.execute(
                    "INSERT OR IGNORE INTO phones (lead_id, e164, dial, kind, raw, position, origin) VALUES (?,?,?,?,?,?,?)",
                    (lead_id, n["e164"], n["dial"], n["kind"], n["raw"], pos, "list"))
                report["numbers_added"] += cur.rowcount
            has_numbers = con.execute("SELECT COUNT(*) FROM phones WHERE lead_id=?", (lead_id,)).fetchone()[0]
            if has_numbers:
                con.execute("UPDATE leads SET status='NEW' WHERE id=? AND status IN ('NO_PHONE','INTL')", (lead_id,))
            if is_dnc:
                report["dnc"].append(lead["company"])
            if lead["hold_reason"]:
                report["held"].append(lead["company"])
            if lead["international"]:
                report["intl"].append((lead["company"], lead["international"], bool(numbers)))
            if not numbers and not lead["international"]:
                report["no_phone"].append(lead["company"])
    rescore(cfg)
    return report


# ----------------------------------------------------------------- scoring --

def rescore(cfg):
    """Recompute raw points and the percentile rank for every lead that can
    still be dialled. Cheap at this size; called after imports and saves."""
    scoring = (cfg or {}).get("scoring") or {}
    with connect() as con:
        rows = con.execute(
            "SELECT l.*, EXISTS(SELECT 1 FROM phones p WHERE p.lead_id=l.id AND p.kind='mobile' AND p.bad=0) AS has_mobile "
            "FROM leads l").fetchall()
        raw = {}
        for r in rows:
            lead = dict(r)
            points = intake.raw_score(lead, scoring, bool(r["has_mobile"] or r["dm_mobile"]), bool(r["interested_before"]))
            con.execute("UPDATE leads SET raw_score=? WHERE id=?", (points, r["id"]))
            if r["status"] in ("NEW", "OUT", "PIPELINE"):
                raw[r["id"]] = points
        for lead_id, rank in intake.percentile_ranks(raw).items():
            con.execute("UPDATE leads SET rank=? WHERE id=?", (rank, lead_id))


# ------------------------------------------------------------------- reads --

def lead(lead_id, con=None):
    own = con is None
    con = con or connect()
    try:
        row = con.execute("SELECT * FROM leads WHERE id=?", (lead_id,)).fetchone()
        if row is None:
            return None
        out = dict(row)
        out["phones"] = [dict(p) for p in con.execute(
            "SELECT * FROM phones WHERE lead_id=? ORDER BY position, id", (lead_id,))]
        return out
    finally:
        if own:
            con.close()


def summary_counts():
    with connect() as con:
        by = lambda q: {r[0]: r[1] for r in con.execute(q)}
        return {
            "status": by("SELECT status, COUNT(*) FROM leads GROUP BY status"),
            "tier": by("SELECT tier, COUNT(*) FROM leads GROUP BY tier"),
            "kind": by("SELECT kind, COUNT(*) FROM phones GROUP BY kind"),
            "primary_kind": by("SELECT p.kind, COUNT(*) FROM phones p WHERE p.position = "
                               "(SELECT MIN(position) FROM phones q WHERE q.lead_id=p.lead_id) GROUP BY p.kind"),
            "held": con.execute("SELECT COUNT(*) FROM leads WHERE hold=1 AND cleared_at IS NULL").fetchone()[0],
            "multi": [(r[0], r[1]) for r in con.execute(
                "SELECT l.company, COUNT(p.id) n FROM leads l JOIN phones p ON p.lead_id=l.id "
                "GROUP BY l.id HAVING n > 1 ORDER BY n DESC, l.company")],
        }


# =================================================================== queue ==
import policy  # noqa: E402  (after the schema so policy stays importable on its own)

DIALABLE = ("NEW", "PIPELINE")


def _dt(value):
    return datetime.fromisoformat(value) if isinstance(value, str) and value else value


def outcomes(cfg):
    return {o["key"]: o for o in (cfg or {}).get("outcomes") or []}


def _followup_due_ids(con, now_utc, cfg):
    """Pipeline leads owed a follow-up: drawings asked for but not in after
    24 h, or delivered with no feedback after 48 h."""
    fu = (cfg or {}).get("followups") or {}
    asked_h = int(fu.get("sample_not_received_hours", 24))
    fb_h = int(fu.get("delivered_no_feedback_hours", 48))
    rows = con.execute(
        "SELECT lead_id FROM samples WHERE (stage='asked' AND asked_at <= ?) OR (stage='delivered' AND delivered_at <= ?)",
        (iso(now_utc - timedelta(hours=asked_h)), iso(now_utc - timedelta(hours=fb_h)))).fetchall()
    return {r["lead_id"] for r in rows}


def _dialable_phones(con, lead_id, blocked):
    return [dict(p) for p in con.execute("SELECT * FROM phones WHERE lead_id=? AND bad=0 ORDER BY position, id", (lead_id,))
            if p["e164"] not in blocked]


def queue(now_utc, cfg, agent=None, limit=None):
    """Every lead that could be handed out right now, in queue order:
    (group, reason, lead row)."""
    cal = (cfg or {}).get("calling")
    stale = iso(now_utc - timedelta(minutes=CHECKOUT_TTL_MIN))
    out = []
    with connect() as con:
        blocked = dnc_keys(con)
        due = _followup_due_ids(con, now_utc, cfg)
        rows = con.execute(f"SELECT * FROM leads WHERE status IN {DIALABLE}").fetchall()
        for r in rows:
            lead = dict(r)
            if lead["checked_out_by"] and lead["checked_out_by"] != agent and (lead["checked_out_at"] or "") >= stale:
                continue
            if ("company:" + lead["company_key"]) in blocked or not _dialable_phones(con, lead["id"], blocked):
                continue
            group, reason = policy.group_for(lead, now_utc, cal, lead["id"] in due)
            if group is not None:
                out.append((group, reason, lead))
    out.sort(key=lambda g: policy.order_key(g[0], g[2]))
    return out[:limit] if limit else out


def waiting(now_utc, cfg, agent=None, limit=150):
    """Leads on the list that can't be handed out right now, with why and
    when, best first. The list never looks empty when it isn't."""
    cal = (cfg or {}).get("calling")
    out = []
    with connect() as con:
        blocked = dnc_keys(con)
        due = _followup_due_ids(con, now_utc, cfg)
        for r in con.execute(f"SELECT * FROM leads WHERE status IN {DIALABLE} ORDER BY rank DESC, id").fetchall():
            lead = dict(r)
            if ("company:" + lead["company_key"]) in blocked or not _dialable_phones(con, lead["id"], blocked):
                continue
            group, reason = policy.group_for(lead, now_utc, cal, lead["id"] in due)
            if group is not None:
                continue
            nxt = _dt(lead["next_action_at"])
            if reason == "outside window" and lead["attempts"] and nxt and nxt > now_utc:
                reason = "retry later"
            when = policy.to_ist(nxt).strftime("%a %-d %b, %-I:%M %p").replace("AM", "am").replace("PM", "pm") if nxt else ""
            lead["wait"] = {
                "outside window": "when cold calls open",
                "outside hours": "after 09:00 IST",
                "held": "held for the defence check",
                "skipped for now": "skipped for a couple of hours",
                "callback later": "callback " + when,
                "retry later": "next try " + when,
                "retry in the other half of the day": "next try in the other half of the day",
                "in pipeline": "in the sample pipeline",
            }.get(reason, reason)
            out.append(lead)
    return out[:limit], len(out)


def list_totals():
    """How many leads came from each imported list, and how many are on file in all."""
    with connect() as con:
        lists = [dict(r) for r in con.execute(
            "SELECT COALESCE(NULLIF(source_file, ''), 'added by hand') file, COUNT(*) leads, MAX(created_at) at "
            "FROM leads GROUP BY 1 ORDER BY at DESC")]
        total = con.execute("SELECT COUNT(*) FROM leads").fetchone()[0]
        callable_ = con.execute(f"SELECT COUNT(*) FROM leads WHERE status IN {DIALABLE}").fetchone()[0]
    return {"total": total, "open": callable_, "lists": lists}


def checkout(agent, cfg, now_utc=None):
    """(lead_id, None) for the next lead, or (None, reason). The lead this
    agent already has open comes back first, so a reload never loses it."""
    t = now_utc or now()
    with connect() as con:
        held = con.execute("SELECT id FROM leads WHERE checked_out_by=? ORDER BY checked_out_at DESC LIMIT 1", (agent,)).fetchone()
        if held is not None:
            con.execute("UPDATE leads SET checked_out_at=? WHERE id=?", (iso(t), held["id"]))
            return held["id"], None
    q = queue(t, cfg, agent)
    if not q:
        return None, _why_empty(t, cfg)
    group, _reason, lead = q[0]
    with connect() as con:
        con.execute("UPDATE leads SET checked_out_by=?, checked_out_at=? WHERE id=?", (agent, iso(t), lead["id"]))
    return lead["id"], None


def _why_empty(t, cfg):
    cal = (cfg or {}).get("calling")
    ist = policy.to_ist(t)
    status = policy.window_status(ist, cal)
    totals = list_totals()
    if not totals["total"]:
        return "No leads yet. Import a list to start."
    safe = f"Your {totals['total']} leads are all here, {totals['open']} still to work. "
    opens = f" Cold calls open {status['next_open']}." if status.get("next_open") else ""
    if not status["legal"]:
        return safe + "Nothing can be dialled outside 09:00 to 21:00 IST." + opens
    if status["tier"] == "closed":
        return safe + f"{status['day_note'] or 'Closed'}: no cold calls today. Callbacks still come through." + opens
    if status["tier"] in ("lunch", "off"):
        return safe + f"{status['label']}, so the queue waits. Callbacks and follow-ups still come through," \
                      " and you can open any lead from the list on the left to call it by hand." + opens
    if totals["open"]:
        return safe + "Each one is waiting for its next try, a callback time, or a hold to be cleared. The list on the left shows when."
    return safe + "Every lead has been worked to the end. Import a new list to keep going."


def release(lead_id, agent=None):
    with connect() as con:
        con.execute("UPDATE leads SET checked_out_by=NULL WHERE id=?" + (" AND checked_out_by=?" if agent else ""),
                    (lead_id, agent) if agent else (lead_id,))


def checkout_specific(lead_id, agent, now_utc=None):
    """Hand-pick a lead (queue click, callback, follow-up). Opening is always
    allowed; dialling may still be blocked (hold, DNC, hours), which the card
    shows. Another agent's open lead cannot be taken."""
    t = now_utc or now()
    stale = iso(t - timedelta(minutes=CHECKOUT_TTL_MIN))
    with connect() as con:
        row = con.execute("SELECT * FROM leads WHERE id=?", (lead_id,)).fetchone()
        if row is None:
            return "That lead does not exist."
        if row["checked_out_by"] not in (None, agent) and (row["checked_out_at"] or "") >= stale:
            return f"{row['checked_out_by']} has that lead open right now."
        con.execute("UPDATE leads SET checked_out_by=NULL WHERE checked_out_by=? AND id!=?", (agent, lead_id))
        con.execute("UPDATE leads SET checked_out_by=?, checked_out_at=? WHERE id=?", (agent, iso(t), lead_id))
    return None


# ------------------------------------------------------------ lead payload --

def _phone_view(p, blocked, now_utc):
    view = dict(p)
    tried_today = p["last_result"] == "NO_ANSWER" and policy.same_ist_day(_dt(p["last_dialed_at"]), now_utc)
    view["tried_today"] = tried_today
    view["blocked"] = ("On the do-not-call list" if p["e164"] in blocked else
                       "Marked wrong or not in service" if p["bad"] else
                       "No answer on this number today. Try another number, or tomorrow" if tried_today else "")
    return view


def lead_payload(lead_id, cfg, now_utc=None):
    t = now_utc or now()
    cal = (cfg or {}).get("calling")
    retry = (cfg or {}).get("retry") or {}
    with connect() as con:
        row = con.execute("SELECT * FROM leads WHERE id=?", (lead_id,)).fetchone()
        if row is None:
            return None
        lead = dict(row)
        blocked = dnc_keys(con)
        lead["phones"] = [_phone_view(p, blocked, t) for p in con.execute(
            "SELECT * FROM phones WHERE lead_id=? ORDER BY position, id", (lead_id,))]
        lead["history"] = [dict(c) for c in con.execute(
            "SELECT id, outcome, number, number_kind, attempt_no, continued, dialed_at, talk_seconds, ring_seconds, notes, "
            "objections, pain, demo_at, callback_at, script_version FROM calls WHERE lead_id=? ORDER BY dialed_at DESC, id DESC LIMIT 30",
            (lead_id,))]
        lead["samples"] = [dict(s) for s in con.execute("SELECT * FROM samples WHERE lead_id=? ORDER BY id DESC", (lead_id,))]
        lead["followups"] = [dict(f) for f in con.execute(
            "SELECT channel, template, at FROM followups WHERE lead_id=? ORDER BY at DESC LIMIT 10", (lead_id,))]
        lead["referred_by_company"] = ""
        if lead["referred_by"]:
            ref = con.execute("SELECT company FROM leads WHERE id=?", (lead["referred_by"],)).fetchone()
            lead["referred_by_company"] = ref["company"] if ref else ""
        due = lead_id in _followup_due_ids(con, t, cfg)
    ist = policy.to_ist(t)
    group, reason = policy.group_for(dict(lead, checked_out_by=None), t, cal, due)
    lead["group"] = policy.GROUP_LABEL.get(group, "")
    lead["attempt_no"] = int(lead["attempts"] or 0) + 1
    lead["max_attempts"] = int(retry.get("max_attempts", 5))
    lead["held"] = bool(lead["hold"] and not lead["cleared_at"])
    lead["is_dnc"] = lead["status"] == "DNC" or ("company:" + lead["company_key"]) in blocked
    usable = [p for p in lead["phones"] if not p["blocked"]]
    lead["dial_block"] = (
        "Held for a defence or aerospace check. Tick cleared first." if lead["held"] else
        "On the do-not-call list." if lead["is_dnc"] else
        "Outside calling hours (09:00 to 21:00 IST)." if not policy.legal(ist, cal) else
        "No number to dial. Add one below." if not lead["phones"] else
        "No number can be dialled right now." if not usable else "")
    lead["window"] = policy.window_status(ist, cal)
    return lead


# ------------------------------------------------------------------- dials --

def log_dial(lead_id, phone_id):
    """Stamp the moment a number was handed to the phone. Returns the stamp."""
    t = iso(now())
    with connect() as con:
        con.execute("UPDATE phones SET last_dialed_at=? WHERE id=? AND lead_id=?", (t, phone_id, lead_id))
    return t


LEAD_CAPTURE = ("dm_name", "dm_title", "dm_mobile", "whatsapp", "email", "language_pref",
                "actual_drawings_month", "current_method", "software_used", "pain")


def _snapshot(con, lead_id):
    lead = dict(con.execute("SELECT * FROM leads WHERE id=?", (lead_id,)).fetchone())
    ph = [dict(p) for p in con.execute("SELECT * FROM phones WHERE lead_id=?", (lead_id,))]
    return {"lead": lead, "phones": ph}


def _add_phone(con, lead_id, raw, origin, first=False):
    import phones as ph
    n = ph.parse_one(raw)
    if n is None:
        return None
    existing = con.execute("SELECT id FROM phones WHERE lead_id=? AND e164=?", (lead_id, n["e164"])).fetchone()
    if existing:
        return existing["id"]
    pos = con.execute("SELECT COALESCE(MIN(position),0) - 1, COALESCE(MAX(position),0) + 1 FROM phones WHERE lead_id=?",
                      (lead_id,)).fetchone()
    cur = con.execute("INSERT INTO phones (lead_id, e164, dial, kind, raw, position, origin) VALUES (?,?,?,?,?,?,?)",
                      (lead_id, n["e164"], n["dial"], n["kind"], n["raw"], pos[0] if first else pos[1], origin))
    return cur.lastrowid


def _ist_local_to_utc(value):
    """'2026-10-05T14:30' typed on an IST clock -> UTC datetime (None if unreadable)."""
    try:
        return policy.to_utc(datetime.fromisoformat(str(value).replace(" ", "T")[:16]))
    except (TypeError, ValueError):
        return None


class SaveError(Exception):
    pass


def save_call(p, cfg, agent):
    """Record one dial and move the lead on. `p` is the cockpit's payload.
    Returns the call id. Raises SaveError with a sentence the cockpit shows."""
    oc = outcomes(cfg).get(p.get("outcome"))
    if oc is None:
        raise SaveError("Unknown outcome.")
    lead_id, phone_id = int(p.get("lead_id") or 0), int(p.get("phone_id") or 0)
    continued = bool(p.get("continued"))
    t = now()
    cal, retry = cfg.get("calling"), cfg.get("retry") or {}
    with connect() as con:
        lead = con.execute("SELECT * FROM leads WHERE id=?", (lead_id,)).fetchone()
        if lead is None:
            raise SaveError("That lead no longer exists.")
        phone = con.execute("SELECT * FROM phones WHERE id=? AND lead_id=?", (phone_id, lead_id)).fetchone()
        if phone is None and p.get("outcome") != "DNC":
            raise SaveError("That number is not on this lead.")
        if continued and oc["key"] != "NO_ANSWER":
            raise SaveError("Only a no answer can move on to the next number.")
        email = str(p.get("email") or "").strip()
        whatsapp_raw = str(p.get("whatsapp") or "").strip()
        wa = None
        if whatsapp_raw:
            import phones as ph
            wa = ph.parse_one(whatsapp_raw)
            if wa is None:
                raise SaveError("That WhatsApp number does not read as an Indian number.")
        if email and ("@" not in email or "." not in email.split("@")[-1]):
            raise SaveError("That email does not look right.")
        demo_at = callback_at = None
        if oc.get("sample") and not (wa or email or lead["whatsapp"] or lead["email"]):
            raise SaveError("Agreed to send drawings needs a WhatsApp number or an email, so you can send the details.")
        if oc.get("demo"):
            demo_at = _ist_local_to_utc(p.get("demo_at"))
            if demo_at is None or demo_at <= t:
                raise SaveError("Demo booked needs a date and time in the future.")
        if oc["kind"] == "callback":
            callback_at = _ist_local_to_utc(p.get("callback_at"))
            if callback_at is None or callback_at <= t:
                raise SaveError("A callback needs a date and time in the future.")

        snap = _snapshot(con, lead_id)
        dialed = _dt(p.get("dialed_at")) or t
        connected = _dt(p.get("connected_at"))
        ended = _dt(p.get("ended_at")) or t
        talk = max(0, int((ended - connected).total_seconds())) if (connected and oc.get("connect")) else 0
        ring = max(0, int(((connected or ended) - dialed).total_seconds()))
        attempt_no = int(lead["attempts"] or 0) + 1           # "try next number" stays inside this attempt
        objections = [str(o)[:80] for o in (p.get("objections") or [])][:12]
        cur = con.execute(
            """INSERT INTO calls (lead_id, phone_id, number, number_kind, outcome, attempt_no, continued, dialed_at,
                 connected_at, ended_at, ring_seconds, talk_seconds, pickup, dm, pitched, interested, sample_asked,
                 demo_booked, objections, pain, notes, actual_drawings_month, current_method, software_used, demo_at,
                 callback_at, script_version, session_id, agent, tier, segment, state, city, hour_ist, prev_state, created_at)
               VALUES (?,?,?,?,?,?,?,?, ?,?,?,?,?,?,?,?,?, ?,?,?,?,?,?,?,?, ?,?,?,?,?,?,?,?,?,?,?)""",
            (lead_id, phone["id"] if phone else None, phone["e164"] if phone else "", phone["kind"] if phone else "",
             oc["key"], attempt_no, 1 if continued else 0, iso(dialed),
             iso(connected) if connected else None, iso(ended), ring, talk,
             int(bool(oc.get("connect"))), int(bool(oc.get("dm"))), int(bool(oc.get("pitched"))),
             int(bool(oc.get("interested"))), int(bool(oc.get("sample"))), int(bool(oc.get("demo"))),
             json.dumps(objections) if objections else None, str(p.get("pain") or "")[:500], str(p.get("notes") or "")[:2000],
             str(p.get("actual_drawings_month") or "")[:40], str(p.get("current_method") or "")[:120],
             str(p.get("software_used") or "")[:120], iso(demo_at), iso(callback_at),
             str(p.get("script_version") or "")[:24], p.get("session_id"), agent,
             lead["tier"], lead["segment"], lead["state"], lead["city"], policy.to_ist(dialed).hour,
             json.dumps(snap, default=str), iso(t)))
        call_id = cur.lastrowid
        if phone is not None:
            con.execute("UPDATE phones SET last_result=?, last_dialed_at=?, bad=CASE WHEN ?='BAD_NUMBER' THEN 1 ELSE bad END WHERE id=?",
                        (oc["key"], iso(dialed), oc["key"], phone["id"]))
        if continued:
            # Same attempt, next number: the lead stays open and the attempt is not counted yet.
            con.execute("UPDATE leads SET last_called_at=?, updated_at=? WHERE id=?", (iso(dialed), iso(t), lead_id))
            return call_id

        # What the call taught us sticks to the lead.
        learned = {}
        for key in LEAD_CAPTURE:
            value = str(p.get(key) or "").strip()
            if key == "whatsapp":
                value = wa["e164"] if wa else ""
            if value:
                learned[key] = value[:500]
        if learned:
            con.execute(f"UPDATE leads SET {', '.join(k + '=?' for k in learned)} WHERE id=?", list(learned.values()) + [lead_id])
        if learned.get("dm_mobile"):
            _add_phone(con, lead_id, learned["dm_mobile"], "dm", first=True)

        status, next_at, next_type, next_half, list_tag = lead["status"], None, None, "", lead["list_tag"]
        dm_ever = int(lead["dm_ever"] or 0) | int(bool(oc.get("dm")))
        in_pipeline = lead["status"] == "PIPELINE"
        if oc["kind"] == "dnc":
            add_dnc(con, lead_id, str(p.get("notes") or "") or "asked not to call", agent)
            status = "DNC"
        elif oc.get("sample"):
            status = "PIPELINE"
            con.execute("INSERT INTO samples (lead_id, origin_call_id, kind, stage, channel, asked_at, updated_at) VALUES (?,?,?,?,?,?,?)",
                        (lead_id, call_id, "sample", "asked", "whatsapp" if (wa or lead["whatsapp"]) else "email", iso(t), iso(t)))
        elif oc.get("demo"):
            status = "PIPELINE"
            con.execute("INSERT INTO samples (lead_id, origin_call_id, kind, stage, demo_at, updated_at) VALUES (?,?,?,?,?,?)",
                        (lead_id, call_id, "demo", "demo", iso(demo_at), iso(t)))
        elif oc["kind"] == "callback":
            next_at, next_type = callback_at, "callback"
            status = "PIPELINE" if in_pipeline else "NEW"
        elif in_pipeline:
            pass                               # a follow-up call; the sample card drives what happens next
        elif oc["key"] == "BAD_NUMBER" and not _dialable_phones(con, lead_id, dnc_keys(con)):
            status = "DONE"
        elif oc["kind"] in ("retry",) or oc["key"] == "BAD_NUMBER":
            again, want = policy.next_attempt(policy.to_ist(dialed), attempt_no, retry, cal)
            if again is None:
                status = "EXHAUSTED" if not dm_ever else "DONE"
                list_tag = "whatsapp_linkedin" if not dm_ever else list_tag
            else:
                status, next_at, next_type, next_half = "NEW", policy.to_utc(again), "retry", want
        else:
            status = "DONE"
        con.execute(
            """UPDATE leads SET status=?, attempts=?, last_outcome=?, last_called_at=?, next_action_at=?, next_action_type=?,
                 next_half=?, list_tag=?, dm_ever=?, interested_before=MAX(interested_before, ?), checked_out_by=NULL, updated_at=?
               WHERE id=?""",
            (status, attempt_no, oc["key"], iso(dialed), iso(next_at), next_type, next_half or "", list_tag, dm_ever,
             int(bool(oc.get("interested"))), iso(t), lead_id))
    rescore(cfg)
    return call_id


def undo(call_id, agent, cfg):
    """Take back the newest outcome on a lead within a few minutes. The lead
    comes back open, exactly as it was, and anything the call created goes."""
    t = now()
    with connect() as con:
        c = con.execute("SELECT * FROM calls WHERE id=?", (call_id,)).fetchone()
        if c is None:
            return None, "Nothing to undo."
        if c["agent"] != agent:
            return None, "Only the person who saved it can undo it."
        if _dt(c["created_at"]) < t - timedelta(seconds=UNDO_WINDOW_SEC):
            return None, "Too late to undo. Fix it from the lead instead."
        newest = con.execute("SELECT id FROM calls WHERE lead_id=? ORDER BY id DESC LIMIT 1", (c["lead_id"],)).fetchone()
        if newest["id"] != call_id:
            return None, "That lead has moved on; it can no longer be undone."
        snap = json.loads(c["prev_state"])
        lead = snap["lead"]
        cols = [k for k in lead if k not in ("id",)]
        con.execute(f"UPDATE leads SET {', '.join(k + '=?' for k in cols)} WHERE id=?", [lead[k] for k in cols] + [lead["id"]])
        con.execute("UPDATE leads SET checked_out_by=?, checked_out_at=? WHERE id=?", (agent, iso(t), lead["id"]))
        keep = {p["id"] for p in snap["phones"]}
        for p in con.execute("SELECT id FROM phones WHERE lead_id=?", (lead["id"],)).fetchall():
            if p["id"] not in keep:
                con.execute("DELETE FROM phones WHERE id=?", (p["id"],))
        for p in snap["phones"]:
            con.execute("UPDATE phones SET bad=?, last_result=?, last_dialed_at=?, position=? WHERE id=?",
                        (p["bad"], p["last_result"], p["last_dialed_at"], p["position"], p["id"]))
        con.execute("DELETE FROM samples WHERE origin_call_id=?", (call_id,))
        if c["outcome"] == "DNC":
            con.execute("DELETE FROM dnc WHERE added_at >= ? AND added_by=?", (c["created_at"], agent))
        con.execute("DELETE FROM calls WHERE id=?", (call_id,))
        notes = c["notes"]
    rescore(cfg)
    return lead["id"], notes


# ------------------------------------------------------------ lead edits --

def clear_hold(lead_id, agent):
    with connect() as con:
        con.execute("UPDATE leads SET cleared_at=?, cleared_by=? WHERE id=? AND hold=1", (iso(now()), agent, lead_id))


def update_lead(lead_id, fields):
    allowed = {k: str(v or "").strip()[:500] for k, v in (fields or {}).items()
               if k in LEAD_CAPTURE + ("notes",)}
    if not allowed:
        return
    with connect() as con:
        con.execute(f"UPDATE leads SET {', '.join(k + '=?' for k in allowed)}, updated_at=? WHERE id=?",
                    list(allowed.values()) + [iso(now()), lead_id])


def add_phone(lead_id, raw):
    """Returns (phone_id, error)."""
    import phones as ph
    n = ph.parse_one(raw)
    if n is None:
        return None, "That does not read as an Indian number."
    with connect() as con:
        if n["e164"] in dnc_keys(con):
            return None, "That number is on the do-not-call list."
        pid = _add_phone(con, lead_id, raw, "added")
        con.execute("UPDATE leads SET status='NEW' WHERE id=? AND status='NO_PHONE'", (lead_id,))
    return pid, None


def add_referral(from_lead_id, fields, cfg, agent):
    """A new lead from 'who else does a lot of these?', or added by hand when
    from_lead_id is None. Returns (lead_id, error)."""
    import phones as ph
    company = intake.clean(fields.get("company"))
    if not company:
        return None, "A referral needs at least a company name."
    key = intake.company_key(company)
    number = ph.parse_one(fields.get("phone")) if fields.get("phone") else None
    if fields.get("phone") and number is None:
        return None, "That number does not read as an Indian number."
    t = iso(now())
    with connect() as con:
        if con.execute("SELECT 1 FROM leads WHERE company_key=?", (key,)).fetchone():
            return None, f"{company} is already on the list."
        blocked = dnc_keys(con)
        if ("company:" + key) in blocked or (number and number["e164"] in blocked):
            return None, f"{company} is on the do-not-call list."
        src = con.execute("SELECT company FROM leads WHERE id=?", (from_lead_id,)).fetchone() if from_lead_id else None
        dm_name, dm_title = intake.parse_ask_for(fields.get("contact"))
        cur = con.execute(
            """INSERT INTO leads (company, company_key, tier, city, state, segment, ask_for, why, dm_name, dm_title, status,
                 referred_by, source, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (company, key, (fields.get("tier") or "B")[:1].upper(), intake.clean(fields.get("city")), intake.clean(fields.get("state")),
             (cfg.get("import") or {}).get("default_segment", "Other"), intake.clean(fields.get("contact")),
             intake.clean(fields.get("why")) or ("Referred by " + src["company"] if src else ""),
             dm_name or intake.clean(fields.get("contact")), dm_title, "NEW" if number else "NO_PHONE",
             from_lead_id or None, "referral from " + src["company"] if src else "added by hand", t, t))
        new_id = cur.lastrowid
        if number:
            _add_phone(con, new_id, fields.get("phone"), "referral" if src else "added")
    rescore(cfg)
    return new_id, None


# ---------------------------------------------------------------- samples --

STAGES = ["demo", "asked", "received", "delivered", "feedback", "quote", "won", "lost"]


def sample_stage(sample_id, stage, fields):
    """Move a pipeline card. Returns an error sentence or None."""
    if stage not in STAGES:
        return "Unknown stage."
    t = now()
    fields = fields or {}
    with connect() as con:
        s = con.execute("SELECT * FROM samples WHERE id=?", (sample_id,)).fetchone()
        if s is None:
            return "That card does not exist."
        sets = {"stage": stage, "updated_at": iso(t)}
        if stage == "asked" and not s["asked_at"]:
            sets["asked_at"] = iso(t)
        if stage in ("received", "delivered", "feedback", "quote", "won") and not s["received_at"] and s["kind"] == "sample":
            sets["received_at"] = iso(t)
        if stage == "delivered":
            sets["delivered_at"] = iso(t)
            minutes = fields.get("turnaround_min")
            if minutes in (None, "") and (s["received_at"] or sets.get("received_at")):
                start = _dt(s["received_at"] or sets.get("received_at"))
                minutes = int((t - start).total_seconds() // 60)
            try:
                sets["turnaround_min"] = max(0, int(minutes))
            except (TypeError, ValueError):
                return "Turnaround should be a number of minutes."
        if stage == "feedback":
            if fields.get("feedback") not in ("good", "issues"):
                return "Pick how the accuracy landed: good, or issues."
            sets.update(feedback=fields["feedback"], feedback_note=str(fields.get("feedback_note") or "")[:500], feedback_at=iso(t))
        if stage == "quote":
            sets["quote_sent_at"] = iso(t)
        if stage == "won":
            try:
                sets["deal_value_inr"] = max(0, int(str(fields.get("deal_value_inr") or "0").replace(",", "")))
                sets["drawings_committed"] = max(0, int(str(fields.get("drawings_committed") or "0").replace(",", "")))
            except ValueError:
                return "Deal value and drawings should be numbers."
            if not sets["deal_value_inr"]:
                return "A win needs the deal value in rupees."
            sets["won_at"] = iso(t)
        if stage == "lost":
            sets.update(lost_at=iso(t), lost_reason=str(fields.get("lost_reason") or "")[:300])
        con.execute(f"UPDATE samples SET {', '.join(k + '=?' for k in sets)} WHERE id=?", list(sets.values()) + [sample_id])
        if stage in ("won", "lost"):
            open_cards = con.execute("SELECT COUNT(*) FROM samples WHERE lead_id=? AND stage NOT IN ('won','lost')",
                                     (s["lead_id"],)).fetchone()[0]
            if not open_cards:
                con.execute("UPDATE leads SET status='DONE', last_outcome=? WHERE id=?", (stage.upper(), s["lead_id"]))
    return None


def samples_board():
    with connect() as con:
        rows = con.execute(
            """SELECT s.*, l.company, l.dm_name, l.whatsapp, l.email, l.city, c.dialed_at AS origin_dialed_at
               FROM samples s JOIN leads l ON l.id=s.lead_id LEFT JOIN calls c ON c.id=s.origin_call_id
               ORDER BY s.updated_at DESC""").fetchall()
    return [dict(r) for r in rows]


# -------------------------------------------------------------- follow-ups --

def log_followup(lead_id, channel, template, to_addr, agent, sample_id=None):
    with connect() as con:
        con.execute("INSERT INTO followups (lead_id, sample_id, channel, template, to_addr, at, agent) VALUES (?,?,?,?,?,?,?)",
                    (lead_id, sample_id, channel, template, to_addr, iso(now()), agent))


def followups_due(cfg, now_utc=None):
    """Nudges owed, oldest first. A nudge sent after the trigger clears it."""
    t = now_utc or now()
    fu = (cfg or {}).get("followups") or {}
    rules = [
        ("sample_asked", "Drawings not in yet", "sample_request",
         "SELECT s.lead_id, s.id sample_id, s.asked_at since FROM samples s WHERE s.stage='asked' AND s.asked_at <= ?",
         t - timedelta(hours=int(fu.get("sample_not_received_hours", 24)))),
        ("delivered", "Delivered, no feedback yet", "sample_delivered",
         "SELECT s.lead_id, s.id sample_id, s.delivered_at since FROM samples s WHERE s.stage='delivered' AND s.delivered_at <= ?",
         t - timedelta(hours=int(fu.get("delivered_no_feedback_hours", 48)))),
        ("interested", "Interested, no next step", "after_call_intro",
         "SELECT l.id lead_id, NULL sample_id, l.last_called_at since FROM leads l WHERE l.last_outcome='INTERESTED_NO' "
         "AND l.status='NEW' AND l.last_called_at <= ?",
         t - timedelta(days=int(fu.get("interested_no_step_days", 3)))),
    ]
    out = []
    with connect() as con:
        for kind, label, template, sql, cutoff in rules:
            for r in con.execute(sql, (iso(cutoff),)).fetchall():
                sent = con.execute("SELECT MAX(at) FROM followups WHERE lead_id=? AND at >= ?", (r["lead_id"], r["since"])).fetchone()[0]
                if sent and _dt(sent) > cutoff:
                    continue
                lead = con.execute("SELECT id, company, dm_name, whatsapp, email, dm_mobile, city FROM leads WHERE id=?",
                                   (r["lead_id"],)).fetchone()
                out.append(dict(dict(lead), kind=kind, label=label, template=template, sample_id=r["sample_id"],
                                since=r["since"], last_nudge=sent))
    out.sort(key=lambda x: x["since"] or "")
    return out


def whatsapp_linkedin_list(limit=100):
    """Leads that used up their tries without reaching the DM: reach them in writing."""
    with connect() as con:
        rows = con.execute(
            """SELECT l.id, l.company, l.city, l.dm_name, l.whatsapp, l.email, l.dm_mobile, l.website, l.attempts, l.last_called_at,
                      (SELECT MAX(at) FROM followups f WHERE f.lead_id=l.id) last_nudge,
                      (SELECT e164 FROM phones p WHERE p.lead_id=l.id AND p.kind='mobile' AND p.bad=0 ORDER BY position LIMIT 1) mobile
               FROM leads l WHERE l.list_tag='whatsapp_linkedin' AND l.status='EXHAUSTED'
               ORDER BY l.rank DESC LIMIT ?""", (limit,)).fetchall()
    return [dict(r) for r in rows]


# ------------------------------------------------------------------ lists --

def callbacks_list():
    with connect() as con:
        return [dict(r) for r in con.execute(
            "SELECT id, company, dm_name, city, next_action_at, last_outcome, notes FROM leads "
            "WHERE next_action_type='callback' AND status IN ('NEW','PIPELINE') ORDER BY next_action_at")]


def calls_between(start_utc=None, end_utc=None, agent=None, limit=None):
    sql = ("SELECT c.*, l.company, l.dm_name FROM calls c JOIN leads l ON l.id=c.lead_id WHERE 1=1")
    args = []
    if start_utc:
        sql += " AND c.dialed_at >= ?"
        args.append(iso(start_utc))
    if end_utc:
        sql += " AND c.dialed_at < ?"
        args.append(iso(end_utc))
    if agent:
        sql += " AND c.agent = ?"
        args.append(agent)
    sql += " ORDER BY c.dialed_at DESC, c.id DESC"
    if limit:
        sql += f" LIMIT {int(limit)}"
    with connect() as con:
        return [dict(r) for r in con.execute(sql, args)]


def search(q, limit=60):
    like = f"%{q.strip()}%"
    digits = "".join(ch for ch in q if ch.isdigit())
    with connect() as con:
        return [dict(r) for r in con.execute(
            """SELECT DISTINCT l.id, l.company, l.tier, l.city, l.status, l.attempts, l.rank, l.dm_name, l.last_outcome
               FROM leads l LEFT JOIN phones p ON p.lead_id=l.id
               WHERE l.company LIKE ? OR l.city LIKE ? OR l.dm_name LIKE ? OR l.ask_for LIKE ? OR (? != '' AND p.e164 LIKE ?)
               ORDER BY l.rank DESC LIMIT ?""", (like, like, like, like, digits, f"%{digits}%", limit))]


# --------------------------------------------------------------- sessions --

def session_start(agent, script_version, target_dials, target_minutes):
    with connect() as con:
        con.execute("UPDATE sessions SET ended_at=? WHERE agent=? AND ended_at IS NULL", (iso(now()), agent))
        return con.execute(
            "INSERT INTO sessions (agent, script_version, target_dials, target_minutes, started_at) VALUES (?,?,?,?,?)",
            (agent, str(script_version or "")[:24], int(target_dials or 0), int(target_minutes or 0), iso(now()))).lastrowid


def session_end(session_id, active_seconds):
    with connect() as con:
        con.execute("UPDATE sessions SET ended_at=?, active_seconds=? WHERE id=?", (iso(now()), int(active_seconds or 0), session_id))
        row = con.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
    return dict(row) if row else None


def agent_event(agent, event, reason=""):
    with connect() as con:
        con.execute("INSERT INTO agent_events (agent, event, reason, at) VALUES (?,?,?,?)",
                    (agent, str(event)[:24], str(reason)[:80], iso(now())))
