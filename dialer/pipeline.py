"""The pipeline after the call: where each lead stands, when to follow up,
and the queue of transcribed calls waiting for Claude.

Stages follow the trial-first sale: interested on the call, a trial invite
sent, they sign up, they use it, then a call is booked, shows (or not), and
it is sold or lost. Call outcomes move a lead forward on their own; anything
else is set by hand in the cockpit or by Claude through /api/agent.
"""

import db

STAGES = [
    ("interested", "Interested"),
    ("invite_sent", "Invite sent"),
    ("signed_up", "Signed up"),
    ("using", "Using it"),
    ("call_booked", "Call booked"),
    ("showed", "Showed"),
    ("no_show", "No-show"),
    ("sold", "Sold"),
    ("lost", "Lost"),
]
LABEL = dict(STAGES)
ORDER = {k: i for i, (k, _) in enumerate(STAGES)}
TERMINAL = {"sold", "lost"}

# Outcome -> the stage it moves a lead to, if that is further along.
AUTO_FROM_OUTCOME = {"RESONATED_NO": "interested", "BOOKED": "call_booked", "DNC": "lost"}
AUTO_FROM_SHOW = {"SHOWED": "showed", "NO_SHOW": "no_show"}


def _event(con, phone, kind, text="", by=""):
    con.execute("INSERT INTO lead_events (phone, at, kind, text, by) VALUES (?,?,?,?,?)",
                (phone, db.iso(db.now()), kind, str(text or "")[:4000], str(by or "")[:40]))


def add_event(phone, kind, text="", by=""):
    with db.connect() as con:
        _event(con, phone, kind, text, by)


def set_stage(phone, stage, by="", note="", force=True):
    """Move a lead to `stage`. With force=False it only moves forward and never
    out of sold/lost (what call outcomes do). Returns True when it moved."""
    if stage not in LABEL and stage != "":
        raise ValueError(f"unknown stage {stage!r}")
    with db.connect() as con:
        row = con.execute("SELECT stage FROM leads WHERE phone=?", (phone,)).fetchone()
        if row is None:
            return False
        cur = row["stage"] or ""
        if cur == stage:
            return False
        if not force and (cur in TERMINAL or (cur and stage and ORDER.get(stage, -1) <= ORDER.get(cur, -1))):
            return False
        con.execute("UPDATE leads SET stage=?, stage_at=? WHERE phone=?", (stage, db.iso(db.now()), phone))
        _event(con, phone, "stage", (LABEL.get(cur, "None") + " -> " + LABEL.get(stage, "None")) + (": " + note if note else ""), by)
        return True


def set_follow_up(phone, at_utc, note="", by=""):
    """at_utc: 'YYYY-MM-DD HH:MM:SS' UTC, or None to clear."""
    with db.connect() as con:
        con.execute("UPDATE leads SET follow_up_at=?, follow_up_note=? WHERE phone=?", (at_utc, str(note or "")[:300], phone))
        _event(con, phone, "follow_up", ("Follow up " + at_utc + (": " + note if note else "")) if at_utc else "Follow-up cleared", by)


def update_lead(phone, fields, by=""):
    """Contact details Claude or the agent learned: email, dm_name, title, notes."""
    allowed = {k: str(v).strip()[:500] for k, v in (fields or {}).items()
               if k in ("email", "dm_name", "title", "first", "last", "lead_notes", "mobile") and v is not None}
    if not allowed:
        return
    with db.connect() as con:
        con.execute(f"UPDATE leads SET {', '.join(k + '=?' for k in allowed)} WHERE phone=?", list(allowed.values()) + [phone])
        _event(con, phone, "details", ", ".join(f"{k}: {v}" for k, v in allowed.items()), by)


def after_call(phone, outcome, callback_at=None, by=""):
    """Called after every saved outcome."""
    stage = AUTO_FROM_OUTCOME.get(outcome)
    if stage:
        set_stage(phone, stage, by=by, note="from the call outcome", force=False)
    if outcome == "CALLBACK" and callback_at:
        set_follow_up(phone, callback_at, "callback they asked for", by)


def after_followthrough(phone, show_status=None, sale=None):
    if sale:
        set_stage(phone, "sold", note="sale logged", force=False)
    elif show_status in AUTO_FROM_SHOW:
        set_stage(phone, AUTO_FROM_SHOW[show_status], note="booked call " + show_status.lower().replace("_", "-"), force=True)


_LEAD_COLS = ("phone, first, last, company, title, city, state, tz_offset, tz_name, email, dm_name, website, "
              "status, stage, stage_at, follow_up_at, follow_up_note, last_disposition, last_called_at, attempts, lead_notes")


def board(stage=None, limit=500):
    """Leads in the pipeline (any stage set), by stage order, most recent first,
    in the current campaign, with counts per stage and the follow-ups due."""
    t = db.iso(db.now())
    sql = f"SELECT {_LEAD_COLS} FROM leads WHERE stage != '' AND status != 'DELETED'" + db._cc()
    args = []
    if stage == "due":
        sql += " AND follow_up_at IS NOT NULL AND follow_up_at <= ?"
        args.append(t)
    elif stage:
        sql += " AND stage = ?"
        args.append(stage)
    with db.connect() as con:
        rows = [dict(r) for r in con.execute(sql + " ORDER BY stage_at DESC LIMIT ?", args + [limit])]
        counts = {r[0]: r[1] for r in con.execute(
            "SELECT stage, COUNT(*) FROM leads WHERE stage != '' AND status != 'DELETED'" + db._cc() + " GROUP BY stage")}
        due = con.execute("SELECT COUNT(*) FROM leads WHERE stage != '' AND status != 'DELETED' AND follow_up_at IS NOT NULL "
                          "AND follow_up_at <= ?" + db._cc(), (t,)).fetchone()[0]
    rows.sort(key=lambda r: ORDER.get(r["stage"], 99))      # stable: newest first inside each stage
    return {"stages": [{"key": k, "label": v, "count": counts.get(k, 0)} for k, v in STAGES],
            "due": due, "leads": rows}


def followups_due(limit=100):
    t = db.iso(db.now())
    with db.connect() as con:
        rows = [dict(r) for r in con.execute(
            f"SELECT {_LEAD_COLS} FROM leads WHERE status != 'DELETED' AND follow_up_at IS NOT NULL AND follow_up_at <= ?"
            + db._cc() + " ORDER BY follow_up_at LIMIT ?", (t, limit))]
    for r in rows:
        r["recent"] = lead_detail(r["phone"], calls=3, n_events=10)
    return rows


def events(phone, limit=50):
    with db.connect() as con:
        return [dict(r) for r in con.execute(
            "SELECT id, at, kind, text, by FROM lead_events WHERE phone=? ORDER BY at DESC, id DESC LIMIT ?", (phone, limit))]


def lead_detail(phone, calls=10, n_events=30):
    """Everything about one lead for Claude: the lead, its calls with
    transcripts, and its timeline."""
    with db.connect() as con:
        lead = con.execute("SELECT * FROM leads WHERE phone=?", (phone,)).fetchone()
        if lead is None:
            return None
        history = [dict(r) for r in con.execute(
            "SELECT id, at, disposition, notes, duration, objections, pain, booked_for, show_status, sale, "
            "transcript, transcript_status, ai_summary, processed_at, recording_sid FROM dispositions "
            "WHERE phone=? AND disposition != 'SKIP' ORDER BY at DESC, id DESC LIMIT ?", (phone, calls))]
        timeline = [dict(r) for r in con.execute(
            "SELECT at, kind, text, by FROM lead_events WHERE phone=? ORDER BY at DESC, id DESC LIMIT ?", (phone, n_events))]
        campaigns = [dict(r) for r in con.execute(
            "SELECT c.id, c.name FROM campaign_leads cl JOIN campaigns c ON c.id=cl.campaign_id WHERE cl.phone=?", (phone,))]
    keep = ("phone", "first", "last", "company", "title", "city", "state", "tz_name", "email", "mobile", "dm_name",
            "website", "industry", "process", "employees", "status", "stage", "stage_at", "follow_up_at",
            "follow_up_note", "attempts", "last_disposition", "last_called_at", "lead_notes", "pain", "ppap_per_year", "oem")
    out = {k: lead[k] for k in keep if k in lead.keys()}
    out["stage_label"] = LABEL.get(lead["stage"] or "", "")
    out["calls"], out["timeline"], out["campaigns"] = history, timeline, campaigns
    return out


def inbox(limit=50):
    """Calls with a transcript that Claude has not processed yet, oldest first."""
    with db.connect() as con:
        rows = [dict(r) for r in con.execute(
            "SELECT d.id, d.phone, d.at, d.disposition, d.notes, d.duration, d.transcript, d.campaign_id "
            "FROM dispositions d WHERE d.transcript_status = 'done' AND d.processed_at IS NULL" + db._cd("d.campaign_id") +
            " ORDER BY d.at LIMIT ?", (limit,))]
        pending = con.execute("SELECT COUNT(*) FROM dispositions WHERE transcript_status IN ('queued','working')"
                              + db._cd()).fetchone()[0]
    for r in rows:
        r["lead"] = lead_detail(r["phone"], calls=4, n_events=10)
    return {"calls": rows, "still_transcribing": pending}


def mark_processed(dispo_id, summary="", by="claude"):
    with db.connect() as con:
        row = con.execute("SELECT phone FROM dispositions WHERE id=?", (int(dispo_id),)).fetchone()
        if row is None:
            return False
        con.execute("UPDATE dispositions SET processed_at=?, ai_summary=? WHERE id=?",
                    (db.iso(db.now()), str(summary or "")[:2000], int(dispo_id)))
        if summary:
            _event(con, row["phone"], "summary", summary, by)
    return True
