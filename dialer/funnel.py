"""The Imperium funnel: counts, rates, and the daily sheet.

Pure functions over disposition rows (dicts), so every rule here is unit
tested without a database. db.py fetches the rows; serve.py serialises.

Counts
    dials           every saved outcome except SKIP
    pickups         someone answered                       (connect)
    pitched         the DM heard the pull pitch            (DM's Pitched)
    resonated       they admitted the problem              (Resonations)
    offered         you asked for the meeting
    booked          a call went on the calendar            (Call Booked)
    showed          booked calls that happened             (Sales Calls Done)
    sales, sales_amount
    conversations   unique leads with a pickup where you offered or they
                    resonated: the beginner star metric

Rates
    pr        pitch rate          pitched / dials
    rr        resonation rate     resonated / pitched
    abr       booking rate        booked / dials          star metric
    pickup    pickup rate         pickups / dials
    dm_reach  DM reach rate       pitched / pickups
    offer     offer rate          offered / resonated
    sur       show-up rate        showed / booked calls that have come due
    scr       sales conversion    sales / showed

Attribution rule from the tracker: a sales call done and a sale belong to the
DATE OF THE DIAL THAT BOOKED IT, not the date of the meeting. show_status,
sale and sale_amount live on the BOOKED disposition row itself, so grouping
rows by their own dial date gives exactly that.
"""

from collections import Counter, OrderedDict
from datetime import datetime, timedelta, timezone

COUNT_KEYS = ("dials", "pickups", "dms", "pitched", "resonated", "offered", "booked",
              "showed", "no_shows", "due_bookings", "sales", "sales_amount",
              "conversations", "talk_seconds")

SHEET_COLUMNS = ["Date", "Calls", "DM's Pitched", "Resonations", "Call Booked",
                 "Sales Calls Done", "Sales", "Sales $", "Notes"]

# Outcome keys from before the funnel flags existed, so old history still
# counts sensibly after the migration.
LEGACY_FLAGS = {
    "INTERESTED": dict(pickup=1, dm=1, pitched=1, resonated=1),
    "NOT_INT": dict(pickup=1, dm=1, pitched=1),
    "CALLBACK": dict(pickup=1, dm=1, pitched=1),
    "GATEKEEPER": dict(pickup=1),
    "WRONG_NUMBER": dict(pickup=1),
    "DNC": dict(pickup=1),
}


def flags_for(outcome, offered_ticked=False):
    """Funnel flags to stamp on a call, from its outcome definition."""
    o = outcome or {}
    return {
        "pickup": int(bool(o.get("connect"))),
        "dm": int(bool(o.get("dm"))),
        "pitched": int(bool(o.get("pitched"))),
        "resonated": int(bool(o.get("resonated"))),
        "offered": int(bool(o.get("offered") or (offered_ticked and o.get("connect")))),
        "booked": int(bool(o.get("booked"))),
    }


def _zone(name):
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(name)
    except Exception:
        return timezone(timedelta(hours=-5))          # no tz database: fixed Eastern standard


def parse(stamp):
    return datetime.fromisoformat(stamp) if stamp else None


def local_date(stamp_utc, tz_name):
    """The stats day a UTC 'YYYY-MM-DD HH:MM:SS' stamp falls on."""
    return parse(stamp_utc).replace(tzinfo=timezone.utc).astimezone(_zone(tz_name)).date()


def range_start(kind, now_utc, tz_name):
    """UTC start of today / this week (Monday) / this month in the stats zone;
    None for all."""
    if kind == "all":
        return None
    zone = _zone(tz_name)
    local = now_utc.replace(tzinfo=timezone.utc).astimezone(zone)
    start = local.replace(hour=0, minute=0, second=0, microsecond=0)
    if kind == "week":
        start -= timedelta(days=start.weekday())
    elif kind == "month":
        start = start.replace(day=1)
    return start.astimezone(timezone.utc).replace(tzinfo=None)


def bucket_of(day, period):
    """The first day of the day / week (Monday) / month a date belongs to."""
    if period == "week":
        return day - timedelta(days=day.weekday())
    if period == "month":
        return day.replace(day=1)
    return day


def series(rows, tz_name, period="day", count=30, now_utc=None, workdays_only=False):
    """Per-period funnel for analytics: the last `count` days, weeks or months
    in the stats zone, oldest first, empty periods included (0 dials)."""
    now_utc = now_utc or datetime.now(timezone.utc).replace(tzinfo=None)
    today = now_utc.replace(tzinfo=timezone.utc).astimezone(_zone(tz_name)).date()
    keys, cur = [], bucket_of(today, period)
    while len(keys) < count:
        if not (workdays_only and period == "day" and cur.weekday() >= 5):
            keys.append(cur)
        if period == "day":
            cur -= timedelta(days=1)
        elif period == "week":
            cur -= timedelta(days=7)
        else:
            cur = (cur - timedelta(days=1)).replace(day=1)
    keys.reverse()
    groups = OrderedDict((k, []) for k in keys)
    for r in rows:
        if r.get("disposition") == "SKIP":
            continue
        k = bucket_of(local_date(r["at"], tz_name), period)
        if k in groups:
            groups[k].append(r)
    out = []
    for k, group in groups.items():
        c = summarise(group, now_utc)
        out.append({"start": k.isoformat(), "dials": c["dials"], "pickups": c["pickups"], "pitched": c["pitched"],
                    "resonated": c["resonated"], "booked": c["booked"], "showed": c["showed"], "sales": c["sales"],
                    "sales_amount": c["sales_amount"], "conversations": c["conversations"],
                    "talk_seconds": c["talk_seconds"], "rates": c["rates"]})
    return out


def _rate(top, bottom):
    return (top / bottom) if bottom else None


def summarise(rows, now_utc=None):
    """Counts and rates for a set of disposition rows."""
    now_utc = now_utc or datetime.now(timezone.utc).replace(tzinfo=None)
    c = dict.fromkeys(COUNT_KEYS, 0)
    talkers = set()
    for r in rows:
        if r.get("disposition") == "SKIP":
            continue
        c["dials"] += 1
        c["pickups"] += r.get("pickup") or 0
        c["dms"] += r.get("dm") or 0
        c["pitched"] += r.get("pitched") or 0
        c["resonated"] += r.get("resonated") or 0
        c["offered"] += r.get("offered") or 0
        if r.get("pickup"):
            c["talk_seconds"] += r.get("talk_seconds") or r.get("duration") or 0
            if r.get("offered") or r.get("resonated"):
                talkers.add(r.get("phone"))
        if r.get("booked"):
            c["booked"] += 1
            status = (r.get("show_status") or "").upper()
            due = bool(status) or (r.get("booked_for") and parse(r["booked_for"]) <= now_utc)
            if due and status != "RESCHEDULED":
                c["due_bookings"] += 1
            if status == "SHOWED":
                c["showed"] += 1
            elif status == "NO_SHOW":
                c["no_shows"] += 1
            if r.get("sale"):
                c["sales"] += 1
                c["sales_amount"] += float(r.get("sale_amount") or 0)
    c["conversations"] = len(talkers)
    c["rates"] = {
        "pr": _rate(c["pitched"], c["dials"]),
        "rr": _rate(c["resonated"], c["pitched"]),
        "abr": _rate(c["booked"], c["dials"]),
        "pickup": _rate(c["pickups"], c["dials"]),
        "dm_reach": _rate(c["pitched"], c["pickups"]),
        "offer": _rate(c["offered"], c["resonated"]),
        "sur": _rate(c["showed"], c["due_bookings"]),
        "scr": _rate(c["sales"], c["showed"]),
    }
    return c


def by_script(rows, now_utc=None):
    groups = OrderedDict()
    for r in rows:
        groups.setdefault(r.get("script_version") or "unversioned", []).append(r)
    return [dict(summarise(g, now_utc), script_version=k) for k, g in groups.items()]


def top_objections(rows, labels=None, limit=2):
    import json
    counts = Counter()
    for r in rows:
        raw = r.get("objections")
        if not raw:
            continue
        try:
            for tag in json.loads(raw):
                counts[str(tag).split(":")[0]] += 1     # "OTHER:free text" counts as OTHER
        except (TypeError, ValueError):
            continue
    labels = labels or {}
    return [{"key": k, "label": labels.get(k, k.replace("_", " ").title()), "count": n}
            for k, n in counts.most_common(limit)]


def daily_sheet(rows, tz_name, labels=None, now_utc=None):
    """One row per dial date, in the Imperium tracker's exact column order."""
    days = OrderedDict()
    for r in sorted(rows, key=lambda r: r["at"]):
        if r.get("disposition") != "SKIP":
            days.setdefault(local_date(r["at"], tz_name), []).append(r)
    out = []
    for day, group in days.items():
        c = summarise(group, now_utc)
        objections = top_objections(group, labels)
        note = ("Top objections: " + ", ".join(f"{o['label']} ({o['count']})" for o in objections)) if objections else ""
        amount = c["sales_amount"]
        out.append([day.isoformat(), c["dials"], c["pitched"], c["resonated"], c["booked"],
                    c["showed"], c["sales"], int(amount) if amount == int(amount) else round(amount, 2), note])
    return out


def grade(value, target, sample, min_sample=10):
    """good | warn | bad | none, for colouring a rate against its target."""
    if value is None or target is None or sample < min_sample:
        return "none"
    if value >= target:
        return "good"
    return "warn" if value >= target * 0.75 else "bad"
