"""When a lead may be called, when to try again, and which lead comes next.

Pure functions over plain values, so every rule is unit tested
(tests/test_policy.py). Times passed in are naive; `*_ist` means India
Standard Time (UTC+5:30, no daylight saving), everything else is UTC.
Python 3.9 compatible.
"""

from datetime import date, datetime, timedelta

IST_OFFSET = timedelta(hours=5, minutes=30)

DEFAULT_CALLING = {
    "power": [["10:00", "13:00"], ["14:30", "17:30"]],
    "soft": [["17:30", "18:30"]],
    "blocked": [["13:00", "14:30"]],
    "hard": ["09:00", "21:00"],
    "saturday_note": "Saturday: many plants work a half day",
    "holidays": [],
}
DEFAULT_RETRY = {"max_attempts": 5, "gap_days": [1, 2, 3, 4], "alternate_halves": True}

GROUPS = [
    # id, label, needs a cold-call window
    (1, "Callback due", False),
    (2, "Sample follow-up", False),
    (3, "New, tier A", True),
    (4, "Retry", True),
    (5, "New, tier B", True),
    (6, "New, tier C", True),
]
GROUP_LABEL = {g: label for g, label, _ in GROUPS}


def to_ist(utc):
    return utc + IST_OFFSET


def to_utc(ist):
    return ist - IST_OFFSET


def hhmm(text):
    h, _, m = str(text).partition(":")
    return int(h) + int(m or 0) / 60.0


def _h(dt):
    return dt.hour + dt.minute / 60.0


def _in(spans, h):
    return any(hhmm(a) <= h < hhmm(b) for a, b in spans or [])


def holidays(cal):
    out = {}
    for entry in (cal or {}).get("holidays") or []:
        day = entry.get("date")
        if isinstance(day, str):
            day = date.fromisoformat(day)
        out[day] = entry.get("name") or "Holiday"
    return out


def day_kind(ist, cal):
    """('sunday'|'holiday'|'saturday'|'weekday', note)."""
    hol = holidays(cal)
    if ist.date() in hol:
        return "holiday", hol[ist.date()]
    if ist.weekday() == 6:
        return "sunday", "Sunday"
    if ist.weekday() == 5:
        return "saturday", (cal or {}).get("saturday_note") or "Saturday"
    return "weekday", ""


def legal(ist, cal=DEFAULT_CALLING):
    """Inside 09:00-21:00 IST: the hard limit for any call at all (TRAI)."""
    lo, hi = (cal or DEFAULT_CALLING).get("hard") or DEFAULT_CALLING["hard"]
    return hhmm(lo) <= _h(ist) < hhmm(hi)


def tier(ist, cal=DEFAULT_CALLING):
    """'power' | 'soft' | 'lunch' | 'off' | 'closed' at this IST moment.

    closed  outside 09:00-21:00, or a Sunday or holiday: no cold dials
    lunch   13:00-14:30: blocked for cold dials
    off     legal but outside every window (09:00-10:00, 18:30-21:00)
    """
    cal = cal or DEFAULT_CALLING
    if not legal(ist, cal) or day_kind(ist, cal)[0] in ("sunday", "holiday"):
        return "closed"
    h = _h(ist)
    if _in(cal.get("blocked"), h):
        return "lunch"
    if _in(cal.get("power"), h):
        return "power"
    if _in(cal.get("soft"), h):
        return "soft"
    return "off"


def cold_ok(ist, cal=DEFAULT_CALLING):
    return tier(ist, cal) in ("power", "soft")


def half(ist):
    return "am" if ist.hour < 13 else "pm"


def next_cold_open(ist, cal=DEFAULT_CALLING):
    """The next IST moment a cold dial is allowed, for the status line."""
    probe = ist.replace(second=0, microsecond=0)
    for _ in range(15 * 24 * 4):
        if cold_ok(probe, cal):
            return probe
        probe += timedelta(minutes=15 - probe.minute % 15 or 15)
    return None


def window_status(ist, cal=DEFAULT_CALLING):
    """Everything the cockpit's top line says about the clock."""
    t = tier(ist, cal)
    kind, note = day_kind(ist, cal)
    labels = {"power": "Power window", "soft": "Soft window", "lunch": "Lunch, no cold calls",
              "off": "Between windows", "closed": "Closed"}
    out = {"tier": t, "label": labels[t], "day": kind, "day_note": note, "legal": legal(ist, cal),
           "ist": ist.strftime("%a %-d %b, %-I:%M %p").replace("AM", "am").replace("PM", "pm")}
    if t not in ("power", "soft"):
        nxt = next_cold_open(ist, cal)
        if nxt:
            same_day = nxt.date() == ist.date()
            out["next_open"] = nxt.strftime("%-I:%M %p" if same_day else "%a %-I:%M %p").replace("AM", "am").replace("PM", "pm")
    return out


# ------------------------------------------------------------------ retries --

def _workday(d, cal):
    kind, _ = day_kind(datetime(d.year, d.month, d.day, 12), cal)
    return kind not in ("sunday", "holiday")


def half_start(d, which, cal=DEFAULT_CALLING):
    """First minute of the power window in that half of the day, IST."""
    starts = sorted(hhmm(a) for a, _ in (cal or DEFAULT_CALLING).get("power") or DEFAULT_CALLING["power"])
    pick = [s for s in starts if (s < 13) == (which == "am")] or starts
    start = pick[0]
    return datetime(d.year, d.month, d.day, int(start), int(round((start % 1) * 60)))


def exhausted(attempts_made, retry=DEFAULT_RETRY):
    return attempts_made >= int((retry or DEFAULT_RETRY).get("max_attempts", 5))


def next_attempt(last_ist, attempts_made, retry=DEFAULT_RETRY, cal=DEFAULT_CALLING):
    """When to try again after attempt `attempts_made` at `last_ist`.

    Returns (ist_datetime, half) or (None, None) once attempts are used up.
    Never the same day; Sundays and holidays skipped; mornings and
    afternoons alternate so a 10:30 no-answer is tried at 14:30 next.
    """
    retry = retry or DEFAULT_RETRY
    if exhausted(attempts_made, retry):
        return None, None
    gaps = retry.get("gap_days") or [1]
    gap = max(1, int(gaps[min(attempts_made, len(gaps)) - 1]))
    d = last_ist.date() + timedelta(days=gap)
    while not _workday(d, cal):
        d += timedelta(days=1)
    want = ("pm" if half(last_ist) == "am" else "am") if retry.get("alternate_halves", True) else half(last_ist)
    return half_start(d, want, cal), want


def same_ist_day(a_utc, b_utc):
    return a_utc is not None and b_utc is not None and to_ist(a_utc).date() == to_ist(b_utc).date()


def ist_day_start_utc(now_utc):
    ist = to_ist(now_utc)
    return to_utc(ist.replace(hour=0, minute=0, second=0, microsecond=0))


# -------------------------------------------------------------------- queue --

def group_for(lead, now_utc, cal=DEFAULT_CALLING, followup_due=False):
    """(group id, reason) for a lead dict, or (None, why not).

    1 callback due     2 sample follow-up due     3 new tier A
    4 retry due        5 new tier B               6 new tier C
    Groups 1-2 may ring any time 09:00-21:00; 3-6 only in a power or soft window.
    """
    ist = to_ist(now_utc)
    if lead.get("hold") and not lead.get("cleared_at"):
        return None, "held"
    skip = _dt(lead.get("skip_until"))
    if skip and skip > now_utc:
        return None, "skipped for now"
    status = lead.get("status")
    nxt = lead.get("next_action_at")
    nxt = datetime.fromisoformat(nxt) if isinstance(nxt, str) and nxt else nxt
    if lead.get("next_action_type") == "callback" and status in ("NEW", "OUT", "PIPELINE"):
        if nxt and nxt <= now_utc:
            return (1, "callback") if legal(ist, cal) else (None, "outside hours")
        return None, "callback later"
    if status == "PIPELINE":
        if followup_due and not same_ist_day(_dt(lead.get("last_called_at")), now_utc):
            return (2, "sample follow-up") if legal(ist, cal) else (None, "outside hours")
        return None, "in pipeline"
    if status != "NEW":
        return None, status.lower()
    if not cold_ok(ist, cal):
        return None, "outside window"
    if int(lead.get("attempts") or 0) > 0:
        if lead.get("next_action_type") != "retry" or not nxt or nxt > now_utc:
            return None, "retry later"
        if lead.get("next_half") and lead.get("next_half") != half(ist):
            return None, "retry in the other half of the day"
        return 4, "retry"
    return {"A": 3, "B": 5, "C": 6}.get(lead.get("tier") or "B", 5), "new"


def _dt(value):
    if isinstance(value, str) and value:
        return datetime.fromisoformat(value)
    return value


def order_key(group, lead):
    """Sort key within the queue: group first, then best rank, then fewest tries."""
    return (group, -int(lead.get("rank") or 0), int(lead.get("attempts") or 0), lead.get("id") or 0)
