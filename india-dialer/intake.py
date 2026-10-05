"""List intake: CSV rows -> clean lead dicts. Pure functions, no database.

Handles the shapes seen so far: the India ballooning call list (with its
column-shifted rows), and IndiaMART / LinkedIn style exports through the alias
table in config.yaml. Python 3.9 compatible.
"""

import csv
import re

import phones

ROLE_WORDS = {
    "head", "manager", "director", "founder", "owner", "vp", "vice", "president", "lead", "ceo", "cto", "coo",
    "md", "engineer", "consultant", "proprietor", "quality", "operations", "sales", "npd", "supplier", "principal",
    "services", "delivery", "lab", "metrology", "production", "design", "control", "sourcing", "plant", "partner",
    "chief", "officer", "gm", "general", "executive", "in-charge", "incharge", "qa", "qc", "team", "service",
    "engineering", "procurement", "purchase", "buyer", "admin", "reception", "hr", "mentor", "business", "development",
    "technical", "tool", "room", "inspection", "cmm", "manufacturing", "division", "unit", "or", "and", "senior", "sr",
    "assistant", "asst", "deputy", "associate", "contact", "india", "services", "centre", "center",
}
TIER_ORDER = {"A": 0, "B": 1, "C": 2}
EM_DASH = "\u2014"
EN_DASH = "\u2013"


def clean(value):
    if value is None:
        return ""
    text = str(value).replace(" ", " ").strip()
    if text.lower() in ("nan", "none", "null", "n/a", "na", "-", "none found"):
        return ""
    # No em dashes anywhere the cockpit shows text.
    text = text.replace(" " + EM_DASH + " ", ", ").replace(EM_DASH, "-").replace(EN_DASH, "-")
    return re.sub(r"\s+", " ", text)


def _key(header):
    return re.sub(r"[^a-z0-9]+", " ", str(header or "").lower()).strip()


def map_headers(headers, aliases):
    """{source header: field}. Several source columns may map to `phone`."""
    wanted = {}
    for field, names in (aliases or {}).items():
        for name in names:
            wanted.setdefault(_key(name), field)
    out = {}
    taken = set()
    for header in headers:
        field = wanted.get(_key(header))
        if not field:
            continue
        if field in taken and field != "phone":
            continue
        out[header] = field
        taken.add(field)
    return out


def read_rows(path, aliases):
    """Yield raw rows as {field: value}; phone columns are joined with ' / '."""
    with open(path, newline="", encoding="utf-8-sig", errors="replace") as fh:
        reader = csv.DictReader(fh)
        mapping = map_headers(reader.fieldnames or [], aliases)
        for n, row in enumerate(reader, start=2):
            out = {"_line": n}
            phones_seen = []
            for header, field in mapping.items():
                value = row.get(header)
                if field == "phone":
                    if clean(value):
                        phones_seen.append(clean(value))
                else:
                    out[field] = value
            out["phone"] = " / ".join(phones_seen)
            yield out


# ------------------------------------------------------------------ fields --

def normalise_state(raw, states):
    text = clean(raw)
    if not text:
        return ""
    text = re.sub(r"\s*\(.*?\)\s*", "", text).strip()          # "Haryana (NCR)" -> "Haryana"
    code = text.upper()
    if code in (states or {}):
        return states[code]
    names = {v.lower(): v for v in (states or {}).values()}
    return names.get(text.lower(), text)


def split_location(raw, states):
    """'Pune, Maharashtra, India' -> ('Pune', 'Maharashtra')."""
    parts = [p.strip() for p in clean(raw).split(",") if p.strip()]
    known = {v.lower() for v in (states or {}).values()}
    for i, part in enumerate(parts):
        state = normalise_state(part, states)
        if state.lower() in known:
            return (parts[i - 1] if i else ""), state
    return (parts[0] if parts else ""), ""


def volume_low(raw):
    """'500+' -> 500, '300-800' -> 300, '1,200' -> 1200, text -> None."""
    text = clean(raw).replace(",", "")
    match = re.match(r"^\s*(\d+)\s*(\+|-\s*\d+)?\s*$", text)
    return int(match.group(1)) if match else None


def is_junk_type(value, state, states):
    """True when `type` holds a state name or a stray letter (shifted rows)."""
    text = clean(value)
    if len(text) <= 2:
        return True
    lowered = text.lower()
    state_names = {v.lower() for v in (states or {}).values()} | {k.lower() for k in (states or {})}
    return lowered in state_names or lowered == clean(state).lower()


def segment_for(type_value, why, rules, default="Other", junk=False, why_priority=None, company=""):
    order = {label: i for i, label in enumerate(why_priority or [])}
    why_rules = sorted(rules or [], key=lambda r: order.get(r["label"], len(order)))

    def match(text, ruleset):
        low = " " + clean(text).lower() + " "
        for rule in ruleset:
            for word in rule.get("words") or []:
                if re.search(r"(?<![a-z0-9])" + re.escape(str(word).lower()) + r"(?![a-z0-9])", low):
                    return rule["label"]
        return None
    return ((None if junk else match(type_value, rules or [])) or match(company, rules or [])
            or match(why, why_rules) or default)


def parse_ask_for(raw):
    """'Pankaj Tyagi (MD)' -> ('Pankaj Tyagi', 'MD'); 'VP Quality / Quality Head' -> ('', '')."""
    text = clean(raw)
    if not text:
        return "", ""
    match = re.match(r"^(.*?)\s*\((.*?)\)\s*$", text)
    name, title = (match.group(1).strip(), match.group(2).strip()) if match else (text, "")
    name = re.sub(r"^(mr|mrs|ms|dr|shri)\.?\s+", "", name, flags=re.I).strip()
    words = [w for w in re.split(r"[\s/,&]+", name.lower()) if w]
    if not words or any(w.strip(".") in ROLE_WORDS for w in words) or any(ch.isdigit() for ch in name):
        return "", ""
    if not all(w[:1].isalpha() for w in words):
        return "", ""
    return name, title


def company_key(name):
    text = clean(name).lower()
    text = re.sub(r"\(.*?\)", " ", text)
    text = re.sub(r"\b(pvt|private|ltd|limited|llp|inc|co|company|the|india|p)\b\.?", " ", text)
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def hold_reason(flags, needles):
    text = clean(flags).upper()
    for needle in needles or []:
        if str(needle).upper() in text:
            return clean(flags)
    return ""


def to_lead(row, cfg):
    """One raw row -> (lead dict | None, report entries)."""
    imp = cfg.get("import") or {}
    states = cfg.get("states") or {}
    company = clean(row.get("company"))
    if not company:
        return None, [("no_company", row.get("_line"))]
    city, state = clean(row.get("city")), normalise_state(row.get("state"), states)
    if row.get("location") and (not city or not state):
        loc_city, loc_state = split_location(row.get("location"), states)
        city, state = city or loc_city, state or loc_state
    type_raw = clean(row.get("type"))
    junk = is_junk_type(type_raw, state, states)
    tier = clean(row.get("tier")).upper()[:1]
    tier = tier if tier in TIER_ORDER else str(imp.get("default_tier") or "B")
    numbers, international, problems = phones.parse_cell(row.get("phone"))
    dm_name, dm_title = parse_ask_for(row.get("ask_for"))
    if not dm_name and (clean(row.get("first_name")) or clean(row.get("last_name"))):
        dm_name = (clean(row.get("first_name")) + " " + clean(row.get("last_name"))).strip()
    dm_title = dm_title or clean(row.get("dm_title"))
    est_raw = clean(row.get("est_drawings_month"))
    volume = volume_low(est_raw)
    whatsapp = phones.parse_one(row.get("whatsapp")) if clean(row.get("whatsapp")) else None
    lead = {
        "company": company,
        "company_key": company_key(company),
        "tier": tier,
        "city": city,
        "state": state,
        "type_raw": "" if junk else type_raw,
        "segment": segment_for(type_raw, row.get("why"), imp.get("segments"), imp.get("default_segment", "Other"), junk,
                               imp.get("why_priority"), company),
        "ask_for": clean(row.get("ask_for")) or clean(row.get("dm_title")),
        "why": clean(row.get("why")),
        "est_drawings_month": est_raw if volume is not None else "",
        "est_volume": volume or 0,
        "website": clean(row.get("website")),
        "flags": clean(row.get("flags")),
        "source": clean(row.get("source")),
        "phone_source": clean(row.get("phone_source")),
        "dm_name": dm_name,
        "dm_title": dm_title,
        "email": clean(row.get("email")).lower(),
        "whatsapp": whatsapp["e164"] if whatsapp else "",
        "language_pref": clean(row.get("language_pref")),
        "numbers": numbers,
        "international": international,
        "hold_reason": hold_reason(row.get("flags"), imp.get("hold_if_flags_contain"))
                       or hold_reason(row.get("why"), imp.get("hold_if_flags_contain")),
    }
    report = []
    if volume is None and est_raw:
        report.append(("volume_unreadable", company))
        lead["why"] = (lead["why"] + " | " if lead["why"] else "") + est_raw
    for bad in problems:
        report.append(("number_unreadable", company + ": " + bad))
    return lead, report


def merge(into, other):
    """Two rows for the same company become one lead: best tier, every number."""
    if TIER_ORDER.get(other["tier"], 9) < TIER_ORDER.get(into["tier"], 9):
        into["tier"] = other["tier"]
    seen = {n["e164"] for n in into["numbers"]}
    into["numbers"] += [n for n in other["numbers"] if n["e164"] not in seen]
    into["international"] += [x for x in other["international"] if x not in into["international"]]
    for field in ("why", "flags", "source"):
        if other[field] and other[field] not in into[field]:
            into[field] = (into[field] + " | " if into[field] else "") + other[field]
    for field in ("city", "state", "website", "dm_name", "dm_title", "email", "whatsapp", "ask_for", "type_raw"):
        if not into[field] and other[field]:
            into[field] = other[field]
    if other["est_volume"] > into["est_volume"]:
        into["est_volume"], into["est_drawings_month"] = other["est_volume"], other["est_drawings_month"]
    into["hold_reason"] = into["hold_reason"] or other["hold_reason"]
    return into


def collect(rows, cfg):
    """Rows -> (leads in list order, merges, report)."""
    leads, by_key, merges, report = [], {}, [], []
    for row in rows:
        lead, notes = to_lead(row, cfg)
        report += notes
        if lead is None:
            continue
        existing = by_key.get(lead["company_key"])
        if existing is not None:
            merge(existing, lead)
            merges.append(lead["company"])
            continue
        by_key[lead["company_key"]] = lead
        leads.append(lead)
    return leads, merges, report


# ---------------------------------------------------------------- scoring --

def raw_score(lead, scoring, has_mobile, interested_before):
    points = int((scoring.get("tier") or {}).get(lead.get("tier"), 0))
    volume = int(lead.get("est_volume") or 0)
    for band in sorted(scoring.get("volume") or [], key=lambda b: -int(b["min"])):
        if volume >= int(band["min"]):
            points += int(band["points"])
            break
    if has_mobile:
        points += int(scoring.get("mobile") or 0)
    if lead.get("dm_name"):
        points += int(scoring.get("named_dm") or 0)
    if interested_before:
        points += int(scoring.get("interested_before") or 0)
    return points


def percentile_ranks(scores):
    """{id: raw} -> {id: 0..99}. Ties share a rank; the best lead gets 99."""
    if not scores:
        return {}
    ordered = sorted(scores.values())
    total = len(ordered)
    below = {}
    for i, value in enumerate(ordered):
        below.setdefault(value, i)
    return {k: int(round(below[v] / max(total - 1, 1) * 99)) for k, v in scores.items()}
