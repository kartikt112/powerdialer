#!/usr/bin/env python3.12
"""
Telnyx Number Lookup validation pass.

Strips disconnected/invalid numbers and tags line type (mobile/landline/voip)
before anything enters the dial queue. Dialing dead numbers damages caller-ID
reputation faster than almost anything else, so this runs before the first dial,
not after the first complaint.

Results are cached, so re-runs over an overlapping list cost nothing.

Usage:
    export TELNYX_API_KEY=KEY...
    python3.12 telnyx_lookup.py --input out/vicidial_tiktokshop_direct_*.csv
    python3.12 telnyx_lookup.py --input out/x.csv --estimate-only
"""

import argparse
import csv
import json
import os
import sys
import threading
from concurrent.futures import ThreadPoolExecutor

import requests

HERE = os.path.dirname(os.path.abspath(__file__))
CACHE_PATH = os.path.join(HERE, "cache", "lookup_cache.json")
API = "https://api.telnyx.com/v2/number_lookup/{}"
COST_PER_LOOKUP = 0.004          # published carrier-lookup rate; confirm on your plan

# Line types we refuse to hand to a power dialer.
DEAD_TYPES = {"invalid", "unknown", ""}

_lock = threading.Lock()


def load_cache():
    if os.path.exists(CACHE_PATH):
        with open(CACHE_PATH) as fh:
            try:
                return json.load(fh)
            except json.JSONDecodeError:
                return {}
    return {}


def save_cache(cache):
    os.makedirs(os.path.dirname(CACHE_PATH), exist_ok=True)
    with open(CACHE_PATH, "w") as fh:
        json.dump(cache, fh, indent=0, sort_keys=True)


def lookup(e164, api_key, session):
    """Return a dict of lookup facts, or an error marker. Never raises."""
    try:
        response = session.get(
            API.format(e164),
            headers={"Authorization": f"Bearer {api_key}"},
            params={"type": "carrier"},
            timeout=20,
        )
    except requests.RequestException as exc:
        return {"status": "error", "detail": str(exc)[:120]}

    if response.status_code == 404:
        return {"status": "invalid", "line_type": "invalid", "carrier": ""}
    if response.status_code == 429:
        return {"status": "rate_limited", "detail": "429"}
    if response.status_code >= 400:
        return {"status": "error", "detail": f"http_{response.status_code}"}

    payload = response.json().get("data", {}) or {}
    carrier = payload.get("carrier") or {}
    # Telnyx signals an unroutable number via carrier.error_code rather than a 404.
    if carrier.get("error_code"):
        return {"status": "invalid", "line_type": "invalid",
                "carrier": carrier.get("name", "")}
    return {
        "status": "ok",
        "line_type": (carrier.get("type") or "").lower(),
        "carrier": carrier.get("name") or "",
        "portable": bool(payload.get("portability")),
    }


def to_e164(row):
    code = (row.get("phone_code") or "1").strip()
    number = (row.get("phone_number") or "").strip()
    return f"+{code}{number}" if number else ""


def main():
    parser = argparse.ArgumentParser(description="Validate numbers via Telnyx Lookup.")
    parser.add_argument("--input", required=True, help="Prepped VICIdial CSV")
    parser.add_argument("--out", help="Validated output CSV (default: <input>_validated.csv)")
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--estimate-only", action="store_true",
                        help="Report how many uncached lookups would run, and cost")
    parser.add_argument("--keep-voip", action="store_true",
                        help="Keep VoIP lines (default keeps them; use --drop-voip to remove)")
    parser.add_argument("--drop-voip", action="store_true")
    args = parser.parse_args()

    with open(os.path.expanduser(args.input), newline="") as fh:
        rows = list(csv.DictReader(fh))
    if not rows:
        sys.exit("Input CSV is empty.")

    cache = load_cache()
    numbers = [to_e164(r) for r in rows]
    pending = sorted({n for n in numbers if n and n not in cache})

    print(f"  rows            {len(rows)}")
    print(f"  cached          {len(rows) - len(pending)}")
    print(f"  to look up      {len(pending)}")
    print(f"  est. cost       ${len(pending) * COST_PER_LOOKUP:,.2f} "
          f"(@ ${COST_PER_LOOKUP}/lookup)")

    if args.estimate_only:
        return

    api_key = os.environ.get("TELNYX_API_KEY")
    if not api_key and pending:
        sys.exit("TELNYX_API_KEY not set. Export it, or re-run with --estimate-only.")

    if pending:
        session = requests.Session()
        done = 0

        def work(number):
            nonlocal done
            result = lookup(number, api_key, session)
            with _lock:
                cache[number] = result
                done += 1
                if done % 100 == 0:
                    print(f"    ... {done}/{len(pending)}")
                    save_cache(cache)

        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            list(pool.map(work, pending))
        save_cache(cache)

    out_path = args.out or os.path.expanduser(args.input).replace(".csv", "_validated.csv")
    dropped_path = out_path.replace("_validated.csv", "_dropped.csv")

    kept, dropped = [], []
    counts = {}
    for row, number in zip(rows, numbers):
        result = cache.get(number, {"status": "unchecked", "line_type": "", "carrier": ""})
        line_type = result.get("line_type", "")
        counts[line_type or result["status"]] = counts.get(line_type or result["status"], 0) + 1

        row["line_type"] = line_type
        row["carrier"] = result.get("carrier", "")
        row["lookup_status"] = result["status"]

        is_dead = result["status"] == "invalid" or line_type in DEAD_TYPES
        is_unwanted_voip = args.drop_voip and line_type == "voip"
        # Transient errors are kept: a network blip is not evidence of a bad number.
        if result["status"] == "ok" and not is_dead and not is_unwanted_voip:
            kept.append(row)
        elif result["status"] in ("error", "rate_limited", "unchecked"):
            kept.append(row)
        else:
            row["drop_reason"] = "voip" if is_unwanted_voip else "invalid_or_disconnected"
            dropped.append(row)

    columns = list(rows[0].keys())
    for extra in ("line_type", "carrier", "lookup_status"):
        if extra not in columns:
            columns.append(extra)

    with open(out_path, "w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(kept)
    if dropped:
        with open(dropped_path, "w", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=columns + ["drop_reason"],
                                    extrasaction="ignore")
            writer.writeheader()
            writer.writerows(dropped)

    print("\n  line types:")
    for name, count in sorted(counts.items(), key=lambda kv: -kv[1]):
        print(f"    {name or '(blank)':16s} {count}")
    print(f"\n  kept            {len(kept)}  -> {out_path}")
    print(f"  dropped         {len(dropped)}" + (f"  -> {dropped_path}" if dropped else ""))


if __name__ == "__main__":
    main()
