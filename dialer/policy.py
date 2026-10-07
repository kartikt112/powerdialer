"""Dialing policy: when a lead may be called, when to retry it, when to leave
a voicemail, and which caller ID to show. Pure functions over plain values,
no database, so every rule is unit tested (tests/test_policy.py).

All datetimes here are the PROSPECT'S wall clock (naive). db.py converts to
and from UTC with the lead's zone.
"""

from datetime import datetime, timedelta

DEFAULT_WINDOWS = {
    "power": [["08:15", "10:15"], ["15:45", "17:45"]],
    "secondary": [["10:15", "11:30"], ["13:30", "15:45"]],
    "hard": ["08:00", "18:00"],          # never outside this, whatever the reason
    "weekends": False,
}
DEFAULT_RETRY = {
    "max_attempts": 6,
    "gap_days": [2, 3, 4, 5, 6],         # after attempt 1, 2, 3, 4, 5: six tries over three weeks
    "voicemail_attempts": [1, 3, 5],
}
DEFAULT_NUMBERS = {
    "max_dials_per_number_per_day": 150,
    "warmup_days": 21,
    "warmup_dials_per_day": 30,
    "park_below_pickup_rate": 0.15,      # 7-day pickup rate ...
    "park_min_dials": 100,               # ... over at least this many dials
    "spam_flag_pickup_rate": 0.20,
    "spam_flag_min_dials": 50,
}


TCPA_EARLIEST, TCPA_LATEST = 8.0, 21.0      # federal floor on the recipient's clock; not configurable


def check_windows(windows):
    """Refuse a config that reaches outside the TCPA hours. Returns the
    windows unchanged or raises ValueError naming the offender."""
    spans = [tuple(windows["hard"])] + [tuple(x) for t in ("power", "secondary") for x in (windows.get(t) or [])]
    for lo, hi in spans:
        if hhmm(lo) < TCPA_EARLIEST or hhmm(hi) > TCPA_LATEST or hhmm(lo) >= hhmm(hi):
            raise ValueError(f"calling window {lo}-{hi} is outside 08:00-21:00 on the prospect's clock (TCPA) or is empty")
    lo, hi = hhmm(windows["hard"][0]), hhmm(windows["hard"][1])
    for tier_name in ("power", "secondary"):
        for a, b in windows.get(tier_name) or []:
            if hhmm(a) < lo or hhmm(b) > hi:
                raise ValueError(f"{tier_name} window {a}-{b} reaches outside the hard limits {windows['hard'][0]}-{windows['hard'][1]}")
    return windows


def load_number_file(path):
    """Numbers from a scrub file as a set of E.164 strings. One per line or a
    CSV; every 10 or 11 digit run on a line is taken, so any export works."""
    import re
    out = set()
    with open(path, encoding="utf-8-sig", errors="replace") as fh:
        for line in fh:
            for run in re.findall(r"(?<!\d)1?[\s.\-()]*[2-9]\d{2}[\s.\-()]*\d{3}[\s.\-]*\d{4}(?!\d)", line):
                digits = re.sub(r"\D", "", run)
                out.add("+1" + digits[-10:])
    return out


def hhmm(text):
    """'08:15' -> 8.25"""
    h, _, m = str(text).partition(":")
    return int(h) + int(m or 0) / 60.0


def _hour(local):
    return local.hour + local.minute / 60.0


def _spans(windows, tier):
    return [(hhmm(a), hhmm(b)) for a, b in (windows.get(tier) or [])]


# ------------------------------------------------------------------ windows --

def in_hard(local, windows=DEFAULT_WINDOWS):
    """Inside the outer limit: a weekday between hard[0] and hard[1]."""
    if local.weekday() >= 5 and not windows.get("weekends"):
        return False
    lo, hi = hhmm(windows["hard"][0]), hhmm(windows["hard"][1])
    return lo <= _hour(local) < hi


def tier(local, windows=DEFAULT_WINDOWS):
    """'power', 'secondary' or None for a cold dial at this local time.
    Lunch (the gap between secondary windows), weekends and anything outside
    the hard limits are None."""
    if not in_hard(local, windows):
        return None
    h = _hour(local)
    for name in ("power", "secondary"):
        if any(lo <= h < hi for lo, hi in _spans(windows, name)):
            return name
    return None


def half(local):
    return "am" if local.hour < 12 else "pm"


def half_start(day, which, windows=DEFAULT_WINDOWS):
    """First minute of the first calling window in that half of `day`."""
    starts = sorted(lo for name in ("power", "secondary") for lo, _ in _spans(windows, name))
    picks = [s for s in starts if (s < 12) == (which == "am")] or starts
    start = picks[0]
    return datetime(day.year, day.month, day.day, int(start), int(round((start % 1) * 60)))


def next_open(local, windows=DEFAULT_WINDOWS):
    """The next moment a cold dial becomes possible, for the 'nothing to dial' message."""
    probe = local.replace(second=0, microsecond=0)
    for _ in range(8 * 24 * 4):
        if tier(probe, windows):
            return probe
        probe += timedelta(minutes=15 - probe.minute % 15 or 15)
    return None


ZONE_BY_NAME = {"America/New_York": "ET", "America/Detroit": "ET", "America/Indiana/Indianapolis": "ET",
                "America/Kentucky/Louisville": "ET", "America/Toronto": "ET",
                "America/Chicago": "CT", "America/Winnipeg": "CT", "America/Menominee": "CT",
                "America/Denver": "MT", "America/Boise": "MT", "America/Phoenix": "MT", "America/Edmonton": "MT",
                "America/Los_Angeles": "PT", "America/Vancouver": "PT",
                "America/Anchorage": "AKT", "Pacific/Honolulu": "HT", "America/Halifax": "AT"}


def zone_label(tz_name, offset):
    """ET / CT / MT / PT for the status line."""
    if tz_name in ZONE_BY_NAME:
        return ZONE_BY_NAME[tz_name]
    if tz_name and tz_name.startswith("America/Indiana"):
        return "ET"
    # Without a zone name, a bare offset is ambiguous across the clock change;
    # standard-time mapping is the honest fallback.
    return {-5: "ET", -6: "CT", -7: "MT", -8: "PT", -4: "ET"}.get(int(round(offset or -5)), "UTC%+d" % int(round(offset or 0)))


# -------------------------------------------------------------------- retry --

def voicemail_allowed(attempt_no, retry=DEFAULT_RETRY):
    """Leave a message on attempts 1, 3 and 5 only. Every attempt = pest."""
    return attempt_no in (retry.get("voicemail_attempts") or [])


def exhausted(attempts_made, retry=DEFAULT_RETRY):
    return attempts_made >= retry.get("max_attempts", 6)


def next_attempt(last_local, attempts_made, retry=DEFAULT_RETRY, windows=DEFAULT_WINDOWS):
    """When to try again after attempt number `attempts_made` at `last_local`.

    Returns (local_datetime, half) or (None, None) once attempts are used up.
      - never the same day
      - never the same weekday as the attempt just made
      - the opposite half of the day (morning <-> afternoon)
      - weekdays only, spaced by retry.gap_days
    """
    if exhausted(attempts_made, retry):
        return None, None
    gaps = retry.get("gap_days") or [2]
    gap = max(1, int(gaps[min(attempts_made, len(gaps)) - 1]))
    day = last_local.date() + timedelta(days=gap)
    while (day.weekday() >= 5 and not windows.get("weekends")) or day.weekday() == last_local.weekday():
        day += timedelta(days=1)
    want = "pm" if half(last_local) == "am" else "am"
    return half_start(day, want, windows), want


def same_local_day(a_local, b_local):
    return a_local.date() == b_local.date()


# ---------------------------------------------------------------- caller id --

def area_code(number):
    digits = "".join(ch for ch in str(number) if ch.isdigit())
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    return digits[:3] if len(digits) == 10 else ""


def spoken(number):
    """'+19893751429' -> '989, 375, 1429' for reading out on a voicemail."""
    digits = "".join(ch for ch in str(number) if ch.isdigit())[-10:]
    return f"{digits[:3]}, {digits[3:6]}, {digits[6:]}" if len(digits) == 10 else str(number)


def daily_cap(entry, today, numbers=DEFAULT_NUMBERS):
    """30 a day while the number is warming up, 150 after. `entry` may carry
    warmup_start (a date); without it the number is treated as warm."""
    start = entry.get("warmup_start")
    if start and (today - start).days < numbers.get("warmup_days", 21):
        return numbers.get("warmup_dials_per_day", 30)
    return numbers.get("max_dials_per_number_per_day", 150)


def pick_caller_id(lead_phone, lead_state, pool, used_today, today, parked=(), cursor=0, numbers=DEFAULT_NUMBERS,
                   over_cap=False):
    """Local presence. Returns (entry, reason, next_cursor) or (None, why, cursor).

    Order: a number in the lead's area code, else one in the lead's state,
    else round-robin. Parked numbers and numbers at their daily cap (warm-up
    cap included) are never offered, which is the hard stop at 150.
    over_cap: the agent chose "Dial anyway" on one lead, so a number at its
    cap (never a parked one, unless every number is parked) may be used.
    """
    if not pool:
        return None, "No caller ID is configured.", cursor
    open_pool = [e for e in pool
                 if e["number"] not in parked and used_today.get(e["number"], 0) < daily_cap(e, today, numbers)]
    if not open_pool and over_cap:
        open_pool = [e for e in pool if e["number"] not in parked] or list(pool)
    if not open_pool:
        if all(e["number"] in parked for e in pool):
            return None, "Every caller ID is parked for a low pickup rate. Rest them or add numbers.", cursor
        return None, ("Every caller ID has reached its daily dial cap. The queue is closed until tomorrow; "
                      "you can still open a lead from the list and dial it anyway."), cursor

    code = area_code(lead_phone)
    for entry in open_pool:
        if code and str(entry.get("area_code") or area_code(entry["number"])) == code:
            return entry, "area code match", cursor
    state = (lead_state or "").upper()
    for entry in open_pool:
        if state and str(entry.get("state") or "").upper() == state:
            return entry, "same state", cursor
    entry = open_pool[cursor % len(open_pool)]
    return entry, "round robin", cursor + 1


def should_park(dials_7d, pickups_7d, numbers=DEFAULT_NUMBERS):
    """Auto-park: 7-day pickup rate under 15% across 100+ dials."""
    if dials_7d < numbers.get("park_min_dials", 100):
        return False
    return pickups_7d / dials_7d < numbers.get("park_below_pickup_rate", 0.15)


def spam_suspect(dials, pickups, numbers=DEFAULT_NUMBERS):
    """Softer warning: under 20% pickup across 50+ dials on one caller ID."""
    if dials < numbers.get("spam_flag_min_dials", 50):
        return False
    return pickups / dials < numbers.get("spam_flag_pickup_rate", 0.20)


# ------------------------------------------------------------------ session --

def eta_seconds(dials_remaining, active_seconds, dials_done, default_per_dial=45):
    """'Time and a half': dials left x average seconds per dial x 1.5."""
    per_dial = (active_seconds / dials_done) if dials_done >= 3 else default_per_dial
    return int(max(0, dials_remaining) * per_dial * 1.5)
