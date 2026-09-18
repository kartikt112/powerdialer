#!/usr/bin/env python3.12
"""
List-prep pipeline: Excel/CSV -> VICIdial-ready lead files.

Excel -> normalize E.164 -> US/CA filter -> dedupe -> toll-free split
      -> timezone -> DNC suppress -> priority score -> VICIdial CSV

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
    "company": ["company name", "company", "brand name", "brand", "account name"],
    "first_name": ["first name", "first_name", "firstname", "fname"],
    "last_name": ["last name", "last_name", "lastname", "lname"],
    "email": ["email", "email address", "work email"],
    "job_title": ["job title", "title", "position", "role"],
    "domain": ["domain", "website", "web site"],
    "url": ["url", "product page link", "store url"],
    "phone": ["phone", "phone number", "telephone", "mobile", "direct dial"],
    "city": ["city", "town"],
    "state": ["state", "province", "region"],
    "categories": ["categories", "category", "niche"],
    "tiktok": ["tiktok", "tiktok handle", "tiktok url"],
    "tiktok_followers": ["tiktok followers", "tiktok_followers", "followers"],
    "instagram": ["instagram", "ig"],
    "industry": ["industry", "vertical"],
    "company_size": ["company size", "employees", "employee count", "headcount"],
    "screenshot_url": ["screenshot url", "screenshot"],
    "linkedin": ["linkedin"],
}


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


def gmt_offset(e164):
    """Current UTC offset for the number's area code, as VICIdial expects it."""
    try:
        parsed = phonenumbers.parse(e164, None)
        zones = pn_timezone.time_zones_for_number(parsed)
    except Exception:
        return None, None
    if not zones:
        return None, None
    tz_name = zones[0]
    if tz_name in ("Etc/Unknown",):
        return None, None
    try:
        offset = ZoneInfo(tz_name).utcoffset(datetime.now(dt_timezone.utc))
    except Exception:
        return tz_name, None
    return tz_name, round(offset.total_seconds() / 3600, 2)


def seniority_bucket(job_title):
    title = clean(job_title).lower()
    if not title:
        return "other"
    if re.search(r"\b(founder|co-?founder|owner|ceo|president|proprietor)\b", title):
        return "founder"
    if re.search(r"\b(cmo|cro|coo|cfo|cto|vp|vice president|head of|director|chief)\b", title):
        return "exec"
    if re.search(r"\b(manager|lead|specialist|coordinator|strategist)\b", title):
        return "manager"
    return "other"


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
        return table.get("0", 0)
    for threshold in sorted((int(k) for k in table), reverse=True):
        if count >= threshold:
            return table[str(threshold)]
    return table.get("0", 0)


def score_lead(row, cfg, engaged):
    """
    Raw fit score. Only discriminating features count - see config.yaml for why
    has_email and direct_dial are excluded. Converted to a percentile rank later.
    """
    weights = cfg["scoring"]
    points = 0
    has_tiktok = bool(clean(row.get("tiktok")))
    if has_tiktok:
        points += weights["tiktok_present"]
        points += bucket_points(row.get("tiktok_followers"), weights["tiktok_followers"])
    elif clean(row.get("instagram")):
        points += weights["instagram_no_tiktok"]
    points += bucket_points(row.get("company_size"), weights["company_size_fit"])
    points += weights["title_seniority"][seniority_bucket(row.get("job_title"))]
    if engaged:
        points += weights["engaged"]
    return max(0, points)


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
    for label, key in (
        ("Cat", "categories"), ("Ind", "industry"), ("TT", "tiktok_followers"),
        ("Size", "company_size"), ("Site", "domain"),
    ):
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
        e164, npa, region, extension, error = normalize_phone(record.get("phone"))

        def reject(reason):
            rejected.append({
                "company": clean(record.get("company")),
                "phone_raw": clean(record.get("phone")),
                "email": clean(record.get("email")),
                "reason": reason,
            })

        if error:
            reject(error)
            continue
        if region not in allowed_regions:
            reject(f"out_of_region:{region or 'unknown'}")
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
        company_key = clean(record.get("domain")).lower() or clean(record.get("company")).lower()
        company_dupe = company_key and company_key in seen_companies
        if company_key:
            seen_companies.setdefault(company_key, e164)

        tz_name, offset = gmt_offset(e164)
        is_direct = npa not in TOLLFREE_NPA
        is_engaged = bool(
            engaged_keys
            and (clean(record.get("email")).lower() in engaged_keys
                 or clean(record.get("domain")).lower() in engaged_keys)
        )

        state_raw = clean(record.get("state"))
        state = STATE_TO_ABBR.get(state_raw.lower(), state_raw.upper()[:2] if state_raw else "")

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
            "job_title": clean(record.get("job_title")),
            "domain": clean(record.get("domain")),
            "url": clean(record.get("url")),
            "tiktok": clean(record.get("tiktok")),
            "tiktok_followers": clean(record.get("tiktok_followers")),
            "instagram": clean(record.get("instagram")),
            "industry": clean(record.get("industry")),
            "categories": clean(record.get("categories")),
            "company_size": clean(record.get("company_size")),
            "screenshot_url": clean(record.get("screenshot_url")),
            "linkedin": clean(record.get("linkedin")),
            # --- our own bookkeeping, not loaded into VICIdial ---
            "_e164": e164,
            "_npa": npa,
            "_region": region,
            "_timezone": tz_name or "",
            "_extension": extension,
            "_company_dupe": "yes" if company_dupe else "",
            "_engaged": "yes" if is_engaged else "",
            "_raw_score": score_lead(record, cfg, is_engaged),
        }
        (direct if is_direct else tollfree).append(out)

    # Percentile-rank each list independently: a toll-free switchboard queue has a
    # different score distribution than direct dials and shouldn't share a scale.
    for bucket in (direct, tollfree):
        apply_ranks(bucket, cfg)
        bucket.sort(key=lambda r: r["rank"], reverse=True)
    return direct, tollfree, rejected, len(df)


VICI_COLUMNS = [
    "vendor_lead_code", "source_id", "list_id", "phone_code", "phone_number",
    "first_name", "last_name", "address3", "city", "state", "email", "comments",
    "rank", "gmt_offset_now",
    "job_title", "domain", "url", "tiktok", "tiktok_followers", "instagram",
    "industry", "categories", "company_size", "screenshot_url", "linkedin",
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
    tz_counts = Counter(r["_timezone"] for r in direct if r["_timezone"])
    reject_counts = Counter(r["reason"].split(":")[0] for r in rejected)
    engaged = sum(1 for r in direct if r["_engaged"])
    company_dupes = sum(1 for r in direct if r["_company_dupe"])

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
        f"| **Dialable — direct** | **{len(direct)}** |",
        f"| Dialable — toll-free/switchboard | {len(tollfree)} |",
        f"| Same-company duplicates (kept, flagged) | {company_dupes} |",
        f"| Engaged w/ email sequence | {engaged} |",
        "",
        "## Rejection reasons",
        "",
        "| Reason | Count |",
        "|---|---|",
    ]
    lines += [f"| {reason} | {count} |" for reason, count in reject_counts.most_common()]

    lines += [
        "",
        "## Top area codes — buy your DIDs here",
        "",
        "| NPA | Leads | % of direct |",
        "|---|---|---|",
    ]
    for npa, count in npa_counts.most_common(12):
        lines.append(f"| {npa} | {count} | {count / max(len(direct), 1) * 100:.1f}% |")

    lines += ["", "## Timezone distribution — staff your shifts here", "",
              "| Timezone | Leads |", "|---|---|"]
    for tz_name, count in tz_counts.most_common():
        lines.append(f"| {tz_name} | {count} |")

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
        "report": os.path.join(args.outdir, f"prep_report_{tag}_{stamp}.md"),
    }

    write_csv(direct, paths["direct"], VICI_COLUMNS)
    write_csv(tollfree, paths["tollfree"], VICI_COLUMNS)
    write_csv(rejected, paths["rejected"], ["company", "phone_raw", "email", "reason"])
    write_report(paths["report"], direct, tollfree, rejected, total, cfg, input_path)

    print(f"  input rows        {total}")
    print(f"  dialable direct   {len(direct)}   -> {paths['direct']}")
    print(f"  toll-free         {len(tollfree)}   -> {paths['tollfree']}")
    print(f"  rejected          {len(rejected)}   -> {paths['rejected']}")
    print(f"  report            {paths['report']}")

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
