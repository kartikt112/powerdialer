#!/usr/bin/env python3
"""Import a call list into the India dialer.

    python3 india-dialer/import.py Me/India-Ballooning-Call-List_100_2026-09-28.csv
    python3 india-dialer/import.py leads.csv --dry-run      # parse and report, write nothing

Re-importing is safe: new companies are added, untouched ones pick up the
new fields and numbers, and anything already dialled keeps its history.
"""

import argparse
import os
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common  # noqa: E402

common.ensure_deps()

import db  # noqa: E402
import intake  # noqa: E402
import phones  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description="Import a call list into the India dialer.")
    parser.add_argument("csv", help="the list (.csv)")
    parser.add_argument("--dry-run", action="store_true", help="parse and report only")
    args = parser.parse_args()

    path = os.path.expanduser(args.csv)
    if not os.path.exists(path):
        sys.exit(f"Not found: {path}")
    cfg = common.load_config()
    rows = list(intake.read_rows(path, (cfg.get("import") or {}).get("aliases")))
    leads, merges, notes = intake.collect(rows, cfg)

    kinds = Counter(n["kind"] for l in leads for n in l["numbers"])
    primary = Counter(l["numbers"][0]["kind"] for l in leads if l["numbers"])
    tiers = Counter(l["tier"] for l in leads)
    multi = [l for l in leads if len(l["numbers"]) > 1]
    held = [l for l in leads if l["hold_reason"]]
    intl = [l for l in leads if l["international"]]
    none = [l for l in leads if not l["numbers"] and not l["international"]]

    print(f"\n  {os.path.basename(path)}")
    print(f"  rows            {len(rows)}")
    print(f"  leads           {len(leads)}" + (f"   ({len(merges)} duplicate rows merged: {', '.join(merges)})" if merges else ""))
    print("  by tier         " + "   ".join(f"{t} {tiers.get(t, 0)}" for t in ("A", "B", "C")))
    print(f"  numbers         {sum(kinds.values())}:  mobile {kinds.get('mobile', 0)}   landline {kinds.get('landline', 0)}"
          f"   toll-free {kinds.get('tollfree', 0)}")
    print(f"  first number    mobile {primary.get('mobile', 0)}   landline {primary.get('landline', 0)}"
          f"   toll-free {primary.get('tollfree', 0)}")
    print(f"  type detection  {'phonenumbers ranges' if phones.PRECISE else 'written-form heuristic (install phonenumbers for accuracy)'}")
    if multi:
        print(f"\n  More than one number ({len(multi)}): the first is dialled first, the rest are 'try next'")
        for l in multi:
            print(f"    {l['company'][:48]:48s} " + "  ".join(f"{phones.pretty(n['e164'], n['kind'])} ({n['kind']})" for n in l["numbers"]))
    if held:
        print(f"\n  HELD until you tick 'cleared' ({len(held)}): defence / aerospace check")
        for l in held:
            print(f"    {l['company'][:48]:48s} {l['hold_reason'][:70]}")
    if intl:
        print(f"\n  International numbers, not dialled ({len(intl)})")
        for l in intl:
            print(f"    {l['company'][:48]:48s} {' / '.join(l['international'])}" + ("   (has Indian numbers too)" if l["numbers"] else ""))
    if none:
        print(f"\n  No number yet ({len(none)}): imported for research, kept out of the queue")
        for l in none:
            print(f"    {l['company'][:48]:48s} tier {l['tier']}  {l['website'] or ''}")
    for kind, detail in notes:
        print(f"  note: {kind.replace('_', ' ')}: {detail}")

    if args.dry_run:
        print("\n  dry run: nothing written\n")
        return
    db.init()
    report = db.import_leads(leads, os.path.basename(path), cfg)
    counts = db.summary_counts()
    print(f"\n  written to {db.DB_PATH}")
    print(f"  added {len(report['added'])}   refreshed {len(report['refreshed'])}   already worked, left alone "
          f"{len(report['kept_worked'])}   new numbers {report['numbers_added']}")
    if report["dnc"]:
        print(f"  on the do-not-call list, blocked: {', '.join(report['dnc'])}")
    status = counts["status"]
    print(f"  queue now       {status.get('NEW', 0)} dialable   {counts['held']} held   {status.get('NO_PHONE', 0)} need a number"
          f"   {status.get('INTL', 0)} international only   {status.get('DNC', 0)} do not call\n")


if __name__ == "__main__":
    main()
