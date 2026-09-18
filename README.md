# powerdialer

List-prep pipeline for a self-hosted **VICIdial + Telnyx** power dialer.
US/CA outbound B2B, human agents, single-line dialing.

Excel in → validated, scored, timezone-bucketed, VICIdial-ready lead files out.

See [RUNBOOK.md](RUNBOOK.md) for the server build (Telnyx trunk, VICIbox install,
campaign config, compliance wiring, security).

## Setup

```bash
python3.12 -m pip install -r requirements.txt
```

## Usage

```bash
# Prep a list, split across 5 agent seats (list_id 101-105)
python3.12 listprep.py \
    --input ~/lead-automation/output/final_leads_master_20260421_1443.xlsx \
    --split-agents 5

# Validate numbers before dialing (~$0.004/number)
export TELNYX_API_KEY=...
python3.12 telnyx_lookup.py --input out/vicidial_tiktokshop_agent1_list101_*.csv

# Cost check without spending anything
python3.12 telnyx_lookup.py --input out/x.csv --estimate-only

# Browser dialer on the newest prepped list (simulator without credentials;
# real calls with the TWILIO_* env vars set — see dialer/TWILIO.md).
# The UI's "Load list…" button also accepts a raw .xlsx/.csv and runs
# this same prep pipeline server-side — no CLI needed.
python3.12 dialer/serve.py --list 101
```

## Pipeline

```
Excel/CSV
  → column mapping (alias table, tolerant of source-list shape changes)
  → E.164 normalize (handles bare 10/11-digit, +1 formats, extensions)
  → US/CA region filter
  → dedupe by number; flag same-company duplicates
  → toll-free/switchboard split (separate list + script)
  → timezone + gmt_offset from area code
  → internal DNC + recently-called suppression
  → priority score → percentile rank
  → per-agent stratified split
  → VICIdial CSV + rejection log + report
```

## Files

| Path | Purpose |
|---|---|
| `listprep.py` | Main pipeline |
| `telnyx_lookup.py` | Number validation + line-type tagging, cached |
| `dialer/serve.py` | Dialer server — queue API, Twilio tokens/REST, uploads |
| `dialer/db.py` | SQLite state: checkout, retries, callbacks, caps, DNC, notes |
| `dialer/index.html` | Agent screen — calling, wrap-up, callbacks, voicemails, inbound |
| `dialer/TWILIO.md` | Twilio setup + deployed architecture notes |
| `config.yaml` | Calling hours, retry policy, scoring weights, compliance settings |
| `dnc.csv` | Internal do-not-call. Export from VICIdial weekly. |
| `cache/called_log.csv` | Recently-dialed suppression (30-day default) |
| `out/` | Generated lead files, rejection logs, prep reports |

## Scoring

Raw fit score → **percentile rank** within each load, written to VICIdial's `rank`
column. Campaigns dial `DOWN RANK`, so the warmest leads get called first.

Only features that **vary** across the list are weighted. `has_email` (100%
coverage) and `direct_dial` (already encoded by the list split) are deliberately
excluded — a weight everyone earns is a constant, and a constant ranks nothing.

Current signals: TikTok presence and follower band (mid-tier scores highest —
real audience, no agency yet), company-size fit, title seniority, Instagram-
without-TikTok, and email-sequence engagement if available.

To wire up engagement (the strongest signal at +40), export replied/opened
contacts from PlusVibe and point `suppression.engagement_file` at the file.

## VICIdial schema constraints handled

These bite silently on load:

- `vendor_lead_code` `varchar(20)` — a 16-char hash of the E.164, stable across runs
- `comments` `varchar(255)` — screen-pop context, truncated
- `state` `varchar(2)` — "California" → "CA"
- `title` `varchar(4)` — a **salutation**, not a job title. Left empty; job title
  goes to a custom field.
- No company field — company name goes in `address3`
- `phone_number` is bare 10-digit NANP, with `phone_code` = `1`

Custom fields must be created on the VICIdial list **before** loading, or those
columns are discarded without warning.
