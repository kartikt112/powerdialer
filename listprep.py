#!/usr/bin/env python3.12
"""
List-prep pipeline: Excel/CSV -> a ranked, timezone-tagged PPAP call list.

Excel/CSV -> column aliases (cold-call list shape, Sales Navigator exports)
          -> normalize E.164 -> US/CA filter -> ITAR / size / DNC rejects
          -> dedupe by number, then one best contact per company
          -> toll-free split -> timezone (area code, then state)
          -> priority score -> percentile rank
          -> out/ppap_list_<date>.csv  (browser dialer)
             out/prep_report.md        (counts per timezone and reject reason)
             out/vicidial_*.csv        (VICIdial load files, unchanged path)

Usage:
    python3.12 listprep.py --input ~/lead-automation/output/final_leads_master_20260421_1443.xlsx
    python3.12 listprep.py --input leads.xlsx --campaign Q4PUSH --list-id 201 --tollfree-list-id 202
"""

import argparse
import csv
import hashlib
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone as dt_timezone
from zoneinfo import ZoneInfo

import pandas as pd
import phonenumbers
import yaml
from phonenumbers import timezone as pn_timezone

HERE = os.path.dirname(os.path.abspath(__file__))

# VICIdial vicidial_list column widths. Exceeding these silently truncates on
# load or rejects the row, depending on MySQL strict mode. Enforced on export.
VICI_LIMITS = {
    "vendor_lead_code": 20,
    "first_name": 30,
    "last_name": 30,
    "address3": 100,   # repurposed as company name; VICIdial has no company field
    "city": 50,
    "state": 2,
    "email": 70,
    "comments": 255,
}

# NPAs that are toll-free. These are switchboards, not people: different script,
# different connect rate, so they go to their own list and campaign.
TOLLFREE_NPA = {"800", "833", "844", "855", "866", "877", "888", "880", "881", "882", "889"}

STATE_TO_ABBR = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR", "california": "CA",
    "colorado": "CO", "connecticut": "CT", "delaware": "DE", "district of columbia": "DC",
    "florida": "FL", "georgia": "GA", "hawaii": "HI", "idaho": "ID", "illinois": "IL",
    "indiana": "IN", "iowa": "IA", "kansas": "KS", "kentucky": "KY", "louisiana": "LA",
    "maine": "ME", "maryland": "MD", "massachusetts": "MA", "michigan": "MI",
    "minnesota": "MN", "mississippi": "MS", "missouri": "MO", "montana": "MT",
    "nebraska": "NE", "nevada": "NV", "new hampshire": "NH", "new jersey": "NJ",
    "new mexico": "NM", "new york": "NY", "north carolina": "NC", "north dakota": "ND",
    "ohio": "OH", "oklahoma": "OK", "oregon": "OR", "pennsylvania": "PA",
    "rhode island": "RI", "south carolina": "SC", "south dakota": "SD", "tennessee": "TN",
    "texas": "TX", "utah": "UT", "vermont": "VT", "virginia": "VA", "washington": "WA",
    "west virginia": "WV", "wisconsin": "WI", "wyoming": "WY", "puerto rico": "PR",
    # Canada
    "alberta": "AB", "british columbia": "BC", "manitoba": "MB", "new brunswick": "NB",
    "newfoundland and labrador": "NL", "nova scotia": "NS", "ontario": "ON",
    "prince edward island": "PE", "quebec": "QC", "saskatchewan": "SK",
    "northwest territories": "NT", "nunavut": "NU", "yukon": "YT",
}

# Source column -> canonical name. Extend here when a new list shape shows up.
COLUMN_ALIASES = {
    "company": ["company name", "company", "brand name", "brand", "account name", "account", "current company",
                "organization", "organization name"],
    "first_name": ["first name", "first_name", "firstname", "fname"],
    "last_name": ["last name", "last_name", "lastname", "lname"],
    "full_name": ["name", "full name", "full_name", "contact", "contact name", "lead name"],
    "email": ["email", "email address", "work email", "business email"],
    "job_title": ["job title", "title", "position", "role", "current title", "headline"],
    "website": ["website", "web site", "domain", "company website", "company domain", "company url", "url"],
    "phone": ["phone", "phone number", "telephone", "direct dial", "company phone", "work phone", "corporate phone",
              "hq phone", "office phone"],
    "mobile": ["mobile", "mobile phone", "cell", "cell phone"],
    "city": ["city", "town"],
    "state": ["state", "province", "region", "company state", "state/region"],
    "location": ["location", "geography", "person location", "company location", "lead location", "hq location"],
    "industry": ["industry", "vertical", "categories", "category"],
    "employees": ["employees", "employee count", "headcount", "company size", "company headcount",
                  "# employees", "number of employees"],
    "process": ["process", "processes", "capability", "capabilities", "primary process"],
    "oem": ["oem", "oems", "customers", "key customers", "supplies"],
    "linkedin_url": ["linkedin", "linkedin url", "linkedin_url", "profile url", "person linkedin url",
                     "linkedin profile", "sales navigator url", "lead linkedin url"],
    "li_status": ["li_status", "li status", "linkedin status", "invite status", "connection status"],
    "source": ["source", "lead source", "list"],
    "dm_name": ["dm_name", "dm name", "decision maker", "decision maker name", "owner name"],
    "gatekeeper_name": ["gatekeeper_name", "gatekeeper name", "gatekeeper", "receptionist"],
    "notes": ["notes", "note", "comments", "description", "about"],
    "line_type": ["line_type", "line type", "phone type"],
}


# Dominant IANA zone per state/province. Used when the area code gives no
# answer, and to pick among several when an area code spans zones.
STATE_TZ = {
    **dict.fromkeys(["CT", "DE", "DC", "FL", "GA", "ME", "MD", "MA", "NH", "NJ", "NY", "NC", "OH", "PA", "RI", "SC",
                     "VT", "VA", "WV", "ON", "QC"], "America/New_York"),
    "MI": "America/Detroit", "IN": "America/Indiana/Indianapolis", "KY": "America/New_York",
    **dict.fromkeys(["AL", "AR", "IL", "IA", "KS", "LA", "MN", "MS", "MO", "NE", "ND", "OK", "SD", "TN", "TX", "WI",
                     "MB", "SK"], "America/Chicago"),
    **dict.fromkeys(["CO", "MT", "NM", "UT", "WY", "ID", "AB"], "America/Denver"),
    "AZ": "America/Phoenix",
    **dict.fromkeys(["CA", "NV", "OR", "WA", "BC"], "America/Los_Angeles"),
    "AK": "America/Anchorage", "HI": "Pacific/Honolulu", "PR": "America/Puerto_Rico",
    **dict.fromkeys(["NB", "NS", "PE"], "America/Halifax"), "NL": "America/St_Johns",
}


def state_abbr(raw):
    text = clean(raw)
    return STATE_TO_ABBR.get(text.lower(), text.upper()[:2] if 0 < len(text) <= 3 else "")


def split_location(raw):
    """'Dayton, Ohio, United States' -> ('Dayton', 'OH'). Sales Navigator gives
    one location string; 'Greater Chicago Area' style values yield nothing."""
    parts = [p.strip() for p in clean(raw).split(",") if p.strip()]
    for i, part in enumerate(parts):
        abbr = STATE_TO_ABBR.get(part.lower()) or (part.upper() if part.upper() in STATE_TZ and len(part) == 2 else "")
        if abbr:
            return (parts[i - 1] if i > 0 else ""), abbr
    return "", ""


def offset_for(tz_name):
    try:
        offset = ZoneInfo(tz_name).utcoffset(datetime.now(dt_timezone.utc))
        return round(offset.total_seconds() / 3600, 2)
    except Exception:
        return None


def lead_timezone(e164, state):
    """(zone, offset, how). Area code first; the state breaks ties when an
    area code spans zones and is the fallback when it gives nothing."""
    zones = []
    try:
        zones = [z for z in pn_timezone.time_zones_for_number(phonenumbers.parse(e164, None)) if z != "Etc/Unknown"]
    except Exception:
        pass
    by_state = STATE_TZ.get(state or "")
    if len(zones) == 1:
        return zones[0], offset_for(zones[0]), "area_code"
    if zones and by_state in zones:
        return by_state, offset_for(by_state), "area_code+state"
    if zones and len(zones) <= 3:             # a split area code: take its first zone
        return zones[0], offset_for(zones[0]), "area_code"
    if by_state:                              # toll-free and other non-geographic numbers land here
        return by_state, offset_for(by_state), "state"
    return "", None, "unknown"


def load_config(path):
    with open(path) as fh:
        return yaml.safe_load(fh)


def map_columns(df):
    """Rename source columns to canonical names; leave unknown columns alone."""
    lookup = {}
    for canonical, aliases in COLUMN_ALIASES.items():
        for col in df.columns:
            if str(col).strip().lower() in aliases and canonical not in lookup.values():
                lookup[col] = canonical
                break
    return df.rename(columns=lookup)


def clean(value):
    if value is None:
        return ""
    text = str(value).strip()
    if text.lower() in ("nan", "none", "null", "n/a", "-"):
        return ""
    return text


def normalize_phone(raw):
    """
    Return (e164, npa, region, extension, error).

    Handles the formats actually present in the master file: bare 10-digit,
    bare 11-digit with leading 1, '+1 555-123-4567', and trailing extensions.
    """
    text = clean(raw)
    if not text:
        return None, None, None, "", "missing_phone"

    # Pull off an extension before touching the rest of the string.
    extension = ""
    ext_match = re.search(r"(?:ext|x|extension)[\s.:]*(\d{1,6})\s*$", text, re.IGNORECASE)
    if ext_match:
        extension = ext_match.group(1)
        text = text[: ext_match.start()]

    digits = re.sub(r"\D", "", text)
    if not digits:
        return None, None, None, extension, "no_digits"

    if text.strip().startswith("+"):
        candidate = "+" + digits
    elif len(digits) == 10:
        candidate = "+1" + digits
    elif len(digits) == 11 and digits.startswith("1"):
        candidate = "+" + digits
    else:
        candidate = "+" + digits

    try:
        parsed = phonenumbers.parse(candidate, "US")
    except phonenumbers.NumberParseException:
        return None, None, None, extension, "unparseable"

    if not phonenumbers.is_valid_number(parsed):
        return None, None, None, extension, "invalid_number"

    e164 = phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)
    region = phonenumbers.region_code_for_number(parsed) or ""
    npa = e164[2:5] if e164.startswith("+1") and len(e164) == 12 else ""
    return e164, npa, region, extension, None


def seniority_bucket(job_title):
    """owner | ops | quality | other. Quality is checked before ops so a
    'VP Quality' lands with the people who own the PPAP pain."""
    title = clean(job_title).lower()
    if not title:
        return "other"
    if re.search(r"\b(owner|co-?owner|president|ceo|chief executive|founder|co-?founder|proprietor|principal)\b", title):
        return "owner"
    if re.search(r"\bquality\b|\bqa\b|\bqc\b", title):
        return "quality"
    if re.search(r"\b(gm|general manager|plant manager|operations manager|"
                 r"(vp|vice president)[ ,of]*(operations|engineering|manufacturing)|"
                 r"director of (operations|engineering|manufacturing)|coo)\b", title):
        return "ops"
    return "other"


def keyword_hit(text, words):
    """True when any configured word appears as a whole word in `text`.
    A trailing * makes it a prefix match: 'fab*' hits fab, fabrication,
    fabricators."""
    text = text.lower()
    for word in words or []:
        word = str(word).lower().strip()
        if not word:
            continue
        pattern = (r"\b" + re.escape(word[:-1]) + r"\w*") if word.endswith("*") \
            else (r"(?<!\w)" + re.escape(word) + r"(?!\w)")
        if re.search(pattern, text):
            return True
    return False


def lead_text(row):
    """Everything a keyword rule may look at."""
    return " | ".join(clean(row.get(k)) for k in ("company", "process", "industry", "notes", "oem"))


# First match wins; used to fill {process} in the scripts when the list has
# no process column.
PROCESS_WORDS = [
    (r"\bstamp", "stamping"), (r"\bforg", "forging"), (r"\b(cast|foundry)", "casting"),
    (r"\btool\s*(&|and)\s*die\b|\btooling\b", "tool and die"),
    (r"\bfab", "fabrication"), (r"\b(cnc|machin|precision)", "machining"),
]


def infer_process(row):
    given = clean(row.get("process")).lower()
    if given:
        return given.split(",")[0].split("/")[0].strip()
    text = lead_text(row).lower()
    for pattern, label in PROCESS_WORDS:
        if re.search(pattern, text):
            return label
    return ""


def to_number(raw):
    """
    Parse the numeric forms that actually appear in these lists:
    '67600.0', '21,700', '1.2k', '3M', '50-200' (takes the low end).
    """
    text = clean(raw).lower().replace(",", "").replace("+", "")
    if not text:
        return None
    text = text.split("-")[0].strip()          # ranges -> conservative low end
    multiplier = 1
    if text.endswith("k"):
        multiplier, text = 1_000, text[:-1]
    elif text.endswith("m"):
        multiplier, text = 1_000_000, text[:-1]
    match = re.search(r"\d+(?:\.\d+)?", text)
    if not match:
        return None
    return float(match.group()) * multiplier


def bucket_points(raw, table):
    """Map a numeric value onto a {threshold: points} table (highest match wins)."""
    count = to_number(raw)
    if count is None:
        return table.get("unknown", table.get("0", 0))
    for threshold in sorted((int(k) for k in table if str(k).isdigit()), reverse=True):
        if count >= threshold:
            return table[str(threshold)]
    return table.get("0", 0)


def score_lead(row, cfg, engaged):
    """
    Raw fit score for a PPAP prospect. Only discriminating features count; see
    config.yaml. Converted to a percentile rank later.
    """
    weights = cfg["scoring"]
    text = lead_text(row)
    points = weights["title_seniority"][seniority_bucket(row.get("job_title"))]
    points += bucket_points(row.get("employees"), weights["company_size_fit"])
    if keyword_hit(text, weights["process_keywords"]["words"]):
        points += weights["process_keywords"]["points"]
    if keyword_hit(text, weights["auto_aero_keywords"]["words"]):
        points += weights["auto_aero_keywords"]["points"]
    if clean(row.get("li_status")).lower() == "accepted":
        points += weights["linkedin_accepted"]
    if engaged:
        points += weights["engaged"]
    return max(0, points)


def is_itar(row, cfg):
    return keyword_hit(lead_text(row), cfg["scoring"]["itar_defense"]["words"])


def apply_ranks(rows, cfg):
    """
    Convert raw scores to VICIdial `rank`. Percentile normalisation guarantees the
    queue spreads across the full range whatever the list's raw distribution looks
    like; ties share the same rank, which is honest rather than arbitrary.
    """
    if not rows:
        return
    if not cfg["scoring"].get("normalize_to_percentile", True):
        for row in rows:
            row["rank"] = min(int(row["_raw_score"]), cfg["scoring"]["max_rank"])
        return
    ordered = sorted(rows, key=lambda r: r["_raw_score"])
    total = len(ordered)
    # Rank by number of leads strictly worse than this one -> ties collapse together.
    counts_below, seen = {}, 0
    for score, group in _group_by_score(ordered):
        counts_below[score] = seen
        seen += group
    for row in rows:
        row["rank"] = int(round(counts_below[row["_raw_score"]] / max(total - 1, 1) * 99))


def _group_by_score(ordered_rows):
    current, size = None, 0
    for row in ordered_rows:
        score = row["_raw_score"]
        if current is None:
            current, size = score, 1
        elif score == current:
            size += 1
        else:
            yield current, size
            current, size = score, 1
    if current is not None:
        yield current, size


def load_suppression(cfg):
    """Internal DNC (E.164) + recently-dialed numbers. Both checked before export."""
    dnc = set()
    dnc_path = os.path.join(HERE, cfg["suppression"]["dnc_file"])
    if os.path.exists(dnc_path):
        with open(dnc_path, newline="") as fh:
            for record in csv.DictReader(fh):
                number = clean(record.get("phone_e164"))
                if number:
                    dnc.add(number)

    recent = set()
    called_path = os.path.join(HERE, cfg["suppression"]["called_log"])
    cutoff = datetime.now() - timedelta(days=cfg["retry"]["recall_suppression_days"])
    if os.path.exists(called_path):
        with open(called_path, newline="") as fh:
            for record in csv.DictReader(fh):
                try:
                    when = datetime.fromisoformat(clean(record.get("last_called_at")))
                except ValueError:
                    continue
                if when > cutoff:
                    recent.add(clean(record.get("phone_e164")))

    engaged = set()
    eng_path = cfg["suppression"].get("engagement_file")
    if eng_path:
        eng_path = eng_path if os.path.isabs(eng_path) else os.path.join(HERE, eng_path)
        if os.path.exists(eng_path):
            with open(eng_path) as fh:
                payload = json.load(fh) if eng_path.endswith(".json") else [
                    r.get("email") or r.get("domain") for r in csv.DictReader(fh)
                ]
            engaged = {clean(x).lower() for x in payload if clean(x)}
    return dnc, recent, engaged


def truncate(value, field):
    text = clean(value)
    limit = VICI_LIMITS.get(field)
    return text[:limit] if limit else text


def short_id(e164):
    return hashlib.sha1(e164.encode()).hexdigest()[:16]  # fits varchar(20)


def build_comments(row):
    """Agent screen-pop context. Hard-capped at VICIdial's 255-char comments field."""
    parts = []
    for label, key in (("Proc", "process"), ("OEM", "oem"), ("Ind", "industry"),
                       ("Size", "employees"), ("Site", "website")):
        value = clean(row.get(key))
        if value:
            parts.append(f"{label}:{value}")
    return truncate(" | ".join(parts), "comments")


def process(input_path, cfg, args):
    df = map_columns(pd.read_excel(input_path) if input_path.lower().endswith((".xlsx", ".xls"))
                     else pd.read_csv(input_path))
    dnc, recent, engaged_keys = load_suppression(cfg)
    allowed_regions = set(cfg["regions"]["allowed"])

    direct, tollfree, rejected = [], [], []
    seen_numbers, seen_companies = {}, {}

    for _, row in df.iterrows():
        record = row.to_dict()
        phone_raw, dialing_mobile = record.get("phone"), False
        if not clean(phone_raw) and clean(record.get("mobile")):
            phone_raw, dialing_mobile = record.get("mobile"), True     # only number we have
        e164, npa, region, extension, error = normalize_phone(phone_raw)
        record["process"] = infer_process(record)
        if not clean(record.get("first_name")) and clean(record.get("full_name")):      # "Dale Harlan" in one column
            first, _, last = clean(record.get("full_name")).partition(" ")
            record["first_name"], record["last_name"] = first, last
        if clean(record.get("location")):                                               # Sales Navigator location string
            city, st = split_location(record.get("location"))
            record["city"] = clean(record.get("city")) or city
            record["state"] = clean(record.get("state")) or st

        def reject(reason):
            rejected.append({
                "company": clean(record.get("company")),
                "phone_raw": clean(phone_raw),
                "email": clean(record.get("email")),
                "reason": reason,
            })

        if error:
            reject(error)
            continue
        if region not in allowed_regions:
            reject(f"out_of_region:{region or 'unknown'}")
            continue
        if is_itar(record, cfg):
            reject("itar")
            continue
        headcount = to_number(record.get("employees"))
        if headcount is not None and headcount > cfg.get("prep", {}).get("max_employees", 500):
            reject("too_large")
            continue
        if e164 in dnc:
            reject("internal_dnc")
            continue
        if e164 in recent:
            reject("called_recently")
            continue
        if e164 in seen_numbers:
            reject(f"duplicate_of:{seen_numbers[e164]}")
            continue

        seen_numbers[e164] = clean(record.get("company")) or e164
        website = re.sub(r"^(https?://)?(www\.)?", "", clean(record.get("website")).lower()).split("/")[0]
        company_key = website or re.sub(r"[^a-z0-9]+", " ", clean(record.get("company")).lower()).strip()

        state = state_abbr(record.get("state"))
        tz_name, offset, tz_how = lead_timezone(e164, state)
        is_direct = npa not in TOLLFREE_NPA
        is_engaged = bool(
            engaged_keys
            and (clean(record.get("email")).lower() in engaged_keys
                 or clean(record.get("website")).lower() in engaged_keys)
        )

        out = {
            # --- VICIdial standard load fields ---
            "vendor_lead_code": short_id(e164),
            "source_id": cfg["campaign"]["source_id"],
            "list_id": cfg["campaign"]["list_id_direct"] if is_direct
                       else cfg["campaign"]["list_id_tollfree"],
            "phone_code": "1",
            "phone_number": e164[2:],                      # VICIdial stores NANP without +1
            "first_name": truncate(record.get("first_name"), "first_name"),
            "last_name": truncate(record.get("last_name"), "last_name"),
            "address3": truncate(record.get("company"), "address3"),  # company goes here
            "city": truncate(record.get("city"), "city"),
            "state": state,
            "email": truncate(record.get("email"), "email"),
            "comments": build_comments(record),
            "rank": 0,                                     # filled after scoring
            "gmt_offset_now": offset if offset is not None else "",
            # --- list custom fields (create these on the VICIdial list) ---
            "title": clean(record.get("job_title")),
            "employees": clean(record.get("employees")),
            "process": clean(record.get("process")),
            "oem": clean(record.get("oem")),
            "industry": clean(record.get("industry")),
            "website": clean(record.get("website")),
            "linkedin_url": clean(record.get("linkedin_url")),
            "li_status": clean(record.get("li_status")).lower(),
            "mobile": clean(record.get("mobile")),
            "source": clean(record.get("source")) or os.path.basename(input_path),
            "dm_name": clean(record.get("dm_name")),
            "gatekeeper_name": clean(record.get("gatekeeper_name")),
            "notes": clean(record.get("notes"))[:500],
            "is_mobile": "1" if (dialing_mobile or clean(record.get("line_type")).lower() == "mobile") else "",
            "timezone": tz_name or "",
            # --- our own bookkeeping, not loaded into VICIdial ---
            "_e164": e164,
            "_npa": npa,
            "_region": region,
            "_timezone": tz_name or "",
            "_tz_how": tz_how,
            "_extension": extension,
            "_company_key": company_key,
            "_phone_raw": clean(phone_raw),
            "_engaged": "yes" if is_engaged else "",
            "_raw_score": score_lead(record, cfg, is_engaged),
        }
        (direct if is_direct else tollfree).append(out)

    # One contact per company: the best-scored one, a direct line beating a
    # switchboard on a tie. Everyone else at that company is a reject, so the
    # owner does not get three calls in a week from the same stranger.
    best = {}
    for bucket_rank, bucket in ((0, direct), (1, tollfree)):
        for row in bucket:
            key = row["_company_key"]
            if key and (key not in best or (-row["_raw_score"], bucket_rank) < best[key][0]):
                best[key] = ((-row["_raw_score"], bucket_rank), row)
    keep = {id(v[1]) for v in best.values()}
    for bucket in (direct, tollfree):
        for row in list(bucket):
            if row["_company_key"] and id(row) not in keep:
                bucket.remove(row)
                rejected.append({"company": row["address3"], "phone_raw": row["_phone_raw"], "email": row["email"],
                                 "reason": f"duplicate_company:{best[row['_company_key']][1]['_e164']}"})

    # Percentile-rank each list independently: a toll-free switchboard queue has a
    # different score distribution than direct dials and shouldn't share a scale.
    for bucket in (direct, tollfree):
        apply_ranks(bucket, cfg)
        bucket.sort(key=lambda r: r["rank"], reverse=True)
    return direct, tollfree, rejected, len(df)


# What the browser dialer imports (dialer/db.py import_list_csv).
DIALER_COLUMNS = [
    "phone_e164", "first_name", "last_name", "company", "title", "city", "state", "timezone",
    "gmt_offset_now", "rank", "employees", "process", "oem", "industry", "website", "linkedin_url",
    "li_status", "email", "mobile", "is_mobile", "dm_name", "gatekeeper_name", "notes", "list_id", "source",
]


def dialer_rows(rows):
    return [dict(row, phone_e164=row["_e164"], company=row["address3"]) for row in rows]


VICI_COLUMNS = [
    "vendor_lead_code", "source_id", "list_id", "phone_code", "phone_number",
    "first_name", "last_name", "address3", "city", "state", "email", "comments",
    "rank", "gmt_offset_now",
    # list custom fields (create these on the VICIdial list) + what the browser dialer reads
    "title", "employees", "process", "oem", "industry", "website", "linkedin_url",
    "li_status", "mobile", "source", "dm_name", "gatekeeper_name", "notes",
    "is_mobile", "timezone",
]


def write_csv(rows, path, columns):
    with open(path, "w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def split_for_agents(rows, agent_count, base_list_id):
    """
    Deal rank-sorted leads round-robin into one sub-list per agent.

    Each agent gets their own VICIdial list -> own campaign -> own caller ID, which
    is how you keep dials-per-number under the ~150/day ceiling and preserve a
    stable number<->agent identity. Round-robin over a sorted list means every
    agent gets the same mix of hot and cold leads, not agent 1 taking all the good
    ones.
    """
    buckets = [[] for _ in range(agent_count)]
    for index, row in enumerate(rows):
        seat = index % agent_count
        row = dict(row)
        row["list_id"] = base_list_id + seat
        buckets[seat].append(row)
    return buckets


def write_report(path, direct, tollfree, rejected, total, cfg, input_path):
    from collections import Counter

    npa_counts = Counter(r["_npa"] for r in direct if r["_npa"])
    tz_counts = Counter(r["_timezone"] or "unknown" for r in direct)
    tz_how = Counter(r["_tz_how"] for r in direct)
    reject_counts = Counter(r["reason"].split(":")[0] for r in rejected)
    engaged = sum(1 for r in direct if r["_engaged"])
    mobiles = sum(1 for r in direct if r["is_mobile"])
    accepted = sum(1 for r in direct if r["li_status"] == "accepted")

    lines = [
        "# List-prep report",
        "",
        f"- Source: `{input_path}`",
        f"- Generated: {datetime.now().strftime('%Y-%m-%d %H:%M')}",
        f"- Campaign: {cfg['campaign']['name']}",
        "",
        "## Funnel",
        "",
        "| Stage | Count |",
        "|---|---|",
        f"| Input rows | {total} |",
        f"| Rejected | {len(rejected)} |",
        f"| **Dialable: direct** | **{len(direct)}** |",
        f"| Dialable: toll-free/switchboard | {len(tollfree)} |",
        f"| Engaged w/ email sequence | {engaged} |",
        f"| LinkedIn invite accepted | {accepted} |",
        f"| Flagged mobile (blocked unless compliance.allow_mobile) | {mobiles} |",
        "",
        "## Rejection reasons",
        "",
        "| Reason | Count |",
        "|---|---|",
    ]
    lines += [f"| {reason} | {count} |" for reason, count in reject_counts.most_common()]

    lines += [
        "",
        "## Top area codes: buy your DIDs here",
        "",
        "| NPA | Leads | % of direct |",
        "|---|---|---|",
    ]
    for npa, count in npa_counts.most_common(12):
        lines.append(f"| {npa} | {count} | {count / max(len(direct), 1) * 100:.1f}% |")

    lines += ["", "## Leads per timezone", "",
              "The dialer walks these east to west through each zone's power windows.", "",
              "| Timezone | Leads | % of direct |", "|---|---|---|"]
    for tz_name, count in tz_counts.most_common():
        lines.append(f"| {tz_name} | {count} | {count / max(len(direct), 1) * 100:.1f}% |")
    lines += ["", "| Timezone came from | Leads |", "|---|---|"]
    lines += [f"| {how} | {count} |" for how, count in tz_how.most_common()]

    raw_counts = Counter(r["_raw_score"] for r in direct)
    lines += [
        "",
        "## Rank distribution (percentile within this load)",
        "",
        "| Band | Leads | Raw score range |",
        "|---|---|---|",
    ]
    bands = [(80, 100, "80-99 (call first)"), (60, 80, "60-79"),
             (40, 60, "40-59"), (20, 40, "20-39"), (0, 20, "0-19 (backfill)")]
    for low, high, label in bands:
        band = [r for r in direct if low <= r["rank"] < high]
        span = (f"{min(r['_raw_score'] for r in band)}-{max(r['_raw_score'] for r in band)}"
                if band else "-")
        lines.append(f"| {label} | {len(band)} | {span} |")

    lines += ["", "## Raw fit-score spread", "", "| Raw score | Leads |", "|---|---|"]
    for score, count in sorted(raw_counts.items(), reverse=True)[:14]:
        lines.append(f"| {score} | {count} |")

    with open(path, "w") as fh:
        fh.write("\n".join(lines) + "\n")


def main():
    parser = argparse.ArgumentParser(description="Prep a lead list for VICIdial + Telnyx.")
    parser.add_argument("--input", required=True, help="Source .xlsx or .csv")
    parser.add_argument("--config", default=os.path.join(HERE, "config.yaml"))
    parser.add_argument("--campaign", help="Override campaign name")
    parser.add_argument("--list-id", type=int, help="Override direct-dial VICIdial list id")
    parser.add_argument("--tollfree-list-id", type=int, help="Override toll-free VICIdial list id")
    parser.add_argument("--outdir", default=os.path.join(HERE, "out"))
    parser.add_argument("--split-agents", type=int, default=0, metavar="N",
                        help="Split direct-dial leads into N per-agent lists "
                             "(list_id, list_id+1, ...) so each agent gets their own "
                             "campaign and caller ID. Keeps dials/number under the cap.")
    args = parser.parse_args()

    cfg = load_config(args.config)
    if args.campaign:
        cfg["campaign"]["name"] = args.campaign
    if args.list_id:
        cfg["campaign"]["list_id_direct"] = args.list_id
    if args.tollfree_list_id:
        cfg["campaign"]["list_id_tollfree"] = args.tollfree_list_id

    input_path = os.path.expanduser(args.input)
    if not os.path.exists(input_path):
        sys.exit(f"Input not found: {input_path}")

    direct, tollfree, rejected, total = process(input_path, cfg, args)

    os.makedirs(args.outdir, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M")
    tag = cfg["campaign"]["name"].lower()
    paths = {
        "direct": os.path.join(args.outdir, f"vicidial_{tag}_direct_{stamp}.csv"),
        "tollfree": os.path.join(args.outdir, f"vicidial_{tag}_tollfree_{stamp}.csv"),
        "rejected": os.path.join(args.outdir, f"rejected_{tag}_{stamp}.csv"),
        "report": os.path.join(args.outdir, "prep_report.md"),
        "dialer": os.path.join(args.outdir, f"{tag}_list_{datetime.now().strftime('%Y-%m-%d')}.csv"),
    }

    write_csv(direct, paths["direct"], VICI_COLUMNS)
    write_csv(tollfree, paths["tollfree"], VICI_COLUMNS)
    write_csv(rejected, paths["rejected"], ["company", "phone_raw", "email", "reason"])
    write_report(paths["report"], direct, tollfree, rejected, total, cfg, input_path)
    write_csv(dialer_rows(direct), paths["dialer"], DIALER_COLUMNS)

    print(f"  input rows        {total}")
    print(f"  dialable direct   {len(direct)}   -> {paths['direct']}")
    print(f"  toll-free         {len(tollfree)}   -> {paths['tollfree']}")
    print(f"  rejected          {len(rejected)}   -> {paths['rejected']}")
    print(f"  report            {paths['report']}")
    print(f"  dialer list       {paths['dialer']}   (dialer/serve.py --list {cfg['campaign']['list_id_direct']})")

    if args.split_agents > 0:
        base = cfg["campaign"]["list_id_direct"]
        print(f"\n  per-agent split ({args.split_agents} seats):")
        for seat, bucket in enumerate(split_for_agents(direct, args.split_agents, base)):
            seat_path = os.path.join(
                args.outdir, f"vicidial_{tag}_agent{seat + 1}_list{base + seat}_{stamp}.csv")
            write_csv(bucket, seat_path, VICI_COLUMNS)
            avg = sum(r["rank"] for r in bucket) / max(len(bucket), 1)
            print(f"    agent {seat + 1}  list_id {base + seat}  "
                  f"{len(bucket):5d} leads  avg rank {avg:5.1f}  -> {os.path.basename(seat_path)}")


if __name__ == "__main__":
    main()
