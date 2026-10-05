"""Metrics: counts, rates against targets, breakdowns, the Imperium tracker
sheet and the every-call export.

Attribution (same rule as the Imperium tracker): samples, demos, wins and
rupees are credited to the IST date of the dial that created them, so a
sample asked on Monday and won on Friday counts on Monday.

Definitions
  dials        every number handed to the phone, including "try next number"
  connects     somebody picked up (outcome with connect: true)
  dms_reached  the decision maker was on the line (dm flag)
  pitched      the pitch was made (pitched flag; today the same outcomes as dm)
  interested   SAMPLE_REQ, DEMO_BOOKED, INTERESTED_NO
  positive     unique leads pitched whose call ended interested, in the
               sample pipeline, with a demo or with a callback they asked for
"""
import csv
import io
import json
from collections import Counter, OrderedDict
from datetime import datetime, timedelta

import db
import policy

POSITIVE = ("SAMPLE_REQ", "DEMO_BOOKED", "CALLBACK", "INTERESTED_NO")
BREAKDOWNS = (("tier", "Tier"), ("segment", "Type"), ("state", "State"), ("city", "City"),
              ("number_kind", "Landline or mobile"), ("hour_ist", "Hour (IST)"), ("script_version", "Script"))


# ------------------------------------------------------------------ ranges --

def range_bounds(rng, now_utc=None):
    """(start_utc, end_utc) for today / week / all. Week starts Monday, IST."""
    t = now_utc or db.now()
    day = policy.ist_day_start_utc(t)
    if rng == "today":
        return day, None
    if rng == "week":
        return day - timedelta(days=policy.to_ist(t).weekday()), None
    if rng == "yesterday":
        return day - timedelta(days=1), day
    return None, None


def _where(start, end, script=None, session=None, alias="c"):
    sql, args = [], []
    if start:
        sql.append(f"{alias}.dialed_at >= ?")
        args.append(db.iso(start))
    if end:
        sql.append(f"{alias}.dialed_at < ?")
        args.append(db.iso(end))
    if script:
        sql.append(f"{alias}.script_version = ?")
        args.append(script)
    if session:
        sql.append(f"{alias}.session_id = ?")
        args.append(int(session))
    return (" WHERE " + " AND ".join(sql)) if sql else "", args


# ------------------------------------------------------------------ counts --

COUNT_SQL = """SELECT COUNT(*) dials, COALESCE(SUM(pickup),0) connects, COALESCE(SUM(dm),0) dms_reached,
  COALESCE(SUM(pitched),0) pitched, COALESCE(SUM(interested),0) interested, COALESCE(SUM(sample_asked),0) samples_asked,
  COALESCE(SUM(demo_booked),0) demos_booked, COALESCE(SUM(talk_seconds),0) talk_total,
  COALESCE(SUM(CASE WHEN talk_seconds > 0 THEN 1 ELSE 0 END),0) talked FROM calls c"""

PIPE_SQL = """SELECT
  COALESCE(SUM(CASE WHEN s.received_at IS NOT NULL THEN 1 ELSE 0 END),0) samples_received,
  COALESCE(SUM(CASE WHEN s.delivered_at IS NOT NULL THEN 1 ELSE 0 END),0) samples_delivered,
  COALESCE(SUM(CASE WHEN s.stage='won' THEN 1 ELSE 0 END),0) won,
  COALESCE(SUM(CASE WHEN s.stage='lost' THEN 1 ELSE 0 END),0) lost,
  COALESCE(SUM(CASE WHEN s.stage='won' THEN s.deal_value_inr ELSE 0 END),0) inr_won,
  COALESCE(SUM(CASE WHEN s.stage='won' THEN s.drawings_committed ELSE 0 END),0) drawings_committed,
  AVG(s.turnaround_min) avg_turnaround_min
  FROM samples s JOIN calls c ON c.id = s.origin_call_id"""


def counts(con, start, end, script=None, session=None):
    where, args = _where(start, end, script, session)
    out = dict(con.execute(COUNT_SQL + where, args).fetchone())
    out.update(dict(con.execute(PIPE_SQL + where, args).fetchone()))
    out["avg_talk"] = int(out["talk_total"] / out["talked"]) if out["talked"] else 0
    if out["avg_turnaround_min"] is not None:
        out["avg_turnaround_min"] = int(round(out["avg_turnaround_min"]))
    pos_where = where + (" AND " if where else " WHERE ") + f"c.pitched=1 AND c.outcome IN ({','.join('?' * len(POSITIVE))})"
    out["positive"] = con.execute("SELECT COUNT(DISTINCT c.lead_id) FROM calls c" + pos_where, args + list(POSITIVE)).fetchone()[0]
    return out


def rates(c, cfg):
    t = (cfg or {}).get("targets") or {}
    base = int(t.get("min_base", 10))
    out = []
    for r in t.get("rates") or []:
        num, den = c.get(r["num"], 0), c.get(r["den"], 0)
        value = num / den if den else None
        target = float(r["target"])
        if value is None or den < base:
            tone = "none"
        elif value >= target:
            tone = "good"
        elif value >= 0.7 * target:
            tone = "warn"
        else:
            tone = "bad"
        out.append({"key": r["key"], "label": r["label"], "value": value, "num": num, "den": den,
                    "target": target, "tone": tone, "star": bool(r.get("star"))})
    return out


def objections(con, start, end, script=None, session=None, top=3, labels=None):
    where, args = _where(start, end, script, session)
    where += (" AND " if where else " WHERE ") + "c.objections IS NOT NULL"
    tally = Counter()
    for (raw,) in con.execute("SELECT c.objections FROM calls c" + where, args):
        try:
            for o in json.loads(raw):
                tally["OTHER" if str(o).startswith("OTHER") else o] += 1
        except (TypeError, ValueError):
            continue
    labels = labels or {}
    return [{"key": k, "label": labels.get(k, k.replace("_", " ").lower()), "count": n} for k, n in tally.most_common(top)]


def breakdown(con, field, start, end, script=None, session=None, limit=15):
    where, args = _where(start, end, script, session)
    rows = con.execute(
        f"""SELECT COALESCE(NULLIF(c.{field}, ''), '(none)') k, COUNT(*) dials, SUM(pickup) connects, SUM(dm) dms,
                   SUM(pitched) pitched, SUM(interested) interested, SUM(sample_asked) samples, SUM(demo_booked) demos
            FROM calls c{where} GROUP BY k ORDER BY dials DESC LIMIT {int(limit)}""", args).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["connect_rate"] = d["connects"] / d["dials"] if d["dials"] else None
        d["sample_rate"] = d["samples"] / d["dials"] if d["dials"] else None
        out.append(d)
    if field == "hour_ist":
        out.sort(key=lambda d: int(d["k"]) if str(d["k"]).isdigit() else 99)
    return out


def best_hours(hours, min_dials=5):
    """Connect rate by IST hour, best first. Hours with few dials go last."""
    ranked = [dict(h, enough=h["dials"] >= min_dials) for h in hours if str(h["k"]).isdigit()]
    ranked.sort(key=lambda h: (not h["enough"], -(h["connect_rate"] or 0), -h["dials"]))
    return ranked


def sessions_pace(con, start, end, session=None):
    """Dials an hour across the sessions in range (by their logged active time)."""
    sql, args = "SELECT id, active_seconds FROM sessions WHERE active_seconds > 0", []
    if session:
        sql, args = "SELECT id, active_seconds FROM sessions WHERE id = ?", [int(session)]
    else:
        if start:
            sql += " AND started_at >= ?"
            args.append(db.iso(start))
        if end:
            sql += " AND started_at < ?"
            args.append(db.iso(end))
    rows = con.execute(sql, args).fetchall()
    secs = sum(r["active_seconds"] or 0 for r in rows)
    if not rows:
        return {"sessions": 0, "active_seconds": 0, "dials_per_hour": None}
    ids = [r["id"] for r in rows]
    dials = con.execute(f"SELECT COUNT(*) FROM calls WHERE session_id IN ({','.join('?' * len(ids))})", ids).fetchone()[0]
    return {"sessions": len(rows), "active_seconds": secs, "dials_per_hour": round(dials / (secs / 3600.0), 1) if secs >= 60 else None}


def stats(cfg, rng="today", script=None, session=None, now_utc=None, detail=True):
    t = now_utc or db.now()
    start, end = (None, None) if session else range_bounds(rng, t)
    labels = {o["key"]: o["label"] for o in (cfg or {}).get("objection_tags") or []}
    with db.connect() as con:
        c = counts(con, start, end, script, session)
        out = {"range": "session" if session else rng, "script": script or "", "counts": c, "rates": rates(c, cfg),
               "objections": objections(con, start, end, script, session, labels=labels),
               "positive_target": int(((cfg or {}).get("targets") or {}).get("positive_conversations_per_day", 10)),
               "pace": sessions_pace(con, start, end, session),
               "scripts": [r[0] for r in con.execute("SELECT DISTINCT script_version FROM calls WHERE script_version != '' ORDER BY 1")]}
        if detail:
            out["breakdowns"] = OrderedDict((f, {"label": label, "rows": breakdown(con, f, start, end, script, session)})
                                            for f, label in BREAKDOWNS)
            out["best_hours"] = best_hours(out["breakdowns"]["hour_ist"]["rows"])
        if session:
            s = con.execute("SELECT * FROM sessions WHERE id=?", (int(session),)).fetchone()
            out["session"] = dict(dict(s) if s else {}, avg_talk=c["avg_talk"], positive=c["positive"],
                                  positive_target=out["positive_target"], dials=c["dials"])
    return out


# ----------------------------------------------------------------- exports --

TRACKER_HEADER = ["Date", "Calls", "DM's Pitched", "Resonations", "Call Booked", "Sales Calls Done", "Sales", "Sales ₹", "Notes"]


def tracker_rows(cfg, rng="all", script=None, now_utc=None):
    """One row per IST day with dials, in the Imperium sheet's columns."""
    start, end = range_bounds(rng, now_utc)
    where, args = _where(start, end, script)
    day_expr = "date(datetime(c.dialed_at, '+330 minutes'))"
    labels = {o["key"]: o["label"] for o in (cfg or {}).get("objection_tags") or []}
    with db.connect() as con:
        days = OrderedDict()
        for r in con.execute(
                f"""SELECT {day_expr} d, COUNT(*) calls, SUM(pitched) pitched, SUM(interested) interested,
                           SUM(sample_asked) + SUM(demo_booked) booked, SUM(sample_asked) samples, SUM(demo_booked) demos
                    FROM calls c{where} GROUP BY d ORDER BY d""", args):
            days[r["d"]] = dict(r, delivered=0, won=0, inr=0)
        for r in con.execute(
                f"""SELECT {day_expr} d, SUM(CASE WHEN s.delivered_at IS NOT NULL THEN 1 ELSE 0 END) delivered,
                           SUM(CASE WHEN s.stage='won' THEN 1 ELSE 0 END) won,
                           SUM(CASE WHEN s.stage='won' THEN s.deal_value_inr ELSE 0 END) inr
                    FROM samples s JOIN calls c ON c.id = s.origin_call_id{where} GROUP BY d""", args):
            if r["d"] in days:
                days[r["d"]].update(delivered=r["delivered"] or 0, won=r["won"] or 0, inr=r["inr"] or 0)
        rows = []
        for d, v in days.items():
            day_start = policy.to_utc(datetime.strptime(d, "%Y-%m-%d"))
            notes = []
            if v["samples"]:
                notes.append(f"{v['samples']} sample{'s' if v['samples'] != 1 else ''} asked")
            if v["demos"]:
                notes.append(f"{v['demos']} demo{'s' if v['demos'] != 1 else ''} booked")
            top = objections(con, day_start, day_start + timedelta(days=1), script, top=1, labels=labels)
            if top:
                notes.append("top objection: " + top[0]["label"].lower())
            rows.append([d, v["calls"], v["pitched"] or 0, v["interested"] or 0, v["booked"] or 0,
                         v["delivered"], v["won"], v["inr"], "; ".join(notes)])
    return rows


def tracker_csv(cfg, rng="all", script=None, now_utc=None):
    return _csv(TRACKER_HEADER, tracker_rows(cfg, rng, script, now_utc))


CALLS_HEADER = ["Call id", "Date (IST)", "Time (IST)", "Company", "Tier", "Type", "City", "State", "Number", "Landline or mobile",
                "Attempt", "Tried next number", "Outcome", "Picked up", "DM", "Pitched", "Interested", "Sample asked",
                "Demo booked", "Ring seconds", "Talk seconds", "Objections", "Pain", "Notes", "Drawings a month",
                "Method", "Software", "Demo at (IST)", "Callback at (IST)", "Script", "Session", "Agent"]


def calls_csv(cfg, rng="all", script=None, now_utc=None):
    start, end = range_bounds(rng, now_utc)
    where, args = _where(start, end, script)
    label = {o["key"]: o["label"] for o in (cfg or {}).get("outcomes") or []}

    def ist(v, fmt):
        dt = db._dt(v)
        return policy.to_ist(dt).strftime(fmt) if dt else ""

    rows = []
    with db.connect() as con:
        for c in con.execute(f"SELECT c.*, l.company FROM calls c JOIN leads l ON l.id=c.lead_id{where} ORDER BY c.dialed_at, c.id", args):
            try:
                objs = ", ".join(json.loads(c["objections"])) if c["objections"] else ""
            except ValueError:
                objs = ""
            yes = lambda v: "yes" if v else ""  # noqa: E731
            rows.append([c["id"], ist(c["dialed_at"], "%Y-%m-%d"), ist(c["dialed_at"], "%H:%M"), c["company"], c["tier"], c["segment"],
                         c["city"], c["state"], c["number"], c["number_kind"], c["attempt_no"], yes(c["continued"]),
                         label.get(c["outcome"], c["outcome"]), yes(c["pickup"]), yes(c["dm"]), yes(c["pitched"]),
                         yes(c["interested"]), yes(c["sample_asked"]), yes(c["demo_booked"]), c["ring_seconds"], c["talk_seconds"],
                         objs, c["pain"], c["notes"], c["actual_drawings_month"], c["current_method"], c["software_used"],
                         ist(c["demo_at"], "%Y-%m-%d %H:%M"), ist(c["callback_at"], "%Y-%m-%d %H:%M"), c["script_version"],
                         c["session_id"] or "", c["agent"]])
    return _csv(CALLS_HEADER, rows)


def _csv(header, rows):
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(header)
    w.writerows(rows)
    return "﻿" + buf.getvalue()            # BOM so Excel reads the rupee sign
