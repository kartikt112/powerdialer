# powerdialer

Browser power dialer and list-prep pipeline for the **PXL Kraft PPAP campaign**.
One caller, dialing US machining, stamping, forging, casting and fab shops
during their business hours. The only goal of a call is a booked
fifteen-minute call. Sales method: Imperium cold-calling structure, PXL Kraft
wording, every string in `config.yaml`.

Python server + SQLite + a single-page cockpit + Twilio Voice SDK. No
framework, no build step. The carrier sits behind one adapter
(`dialer/js/carrier.js`), so Telnyx can replace Twilio without touching the
cockpit. [RUNBOOK.md](RUNBOOK.md) covers the older self-hosted VICIdial +
Telnyx stack; its load files are still written by list prep.

## Setup

```bash
uv venv .venv && uv pip install --python .venv/bin/python -r requirements.txt
```

`python3.12 -m pip install -r requirements.txt` works too. If PyYAML is missing
from the Python you launch with and `.venv/` exists, `serve.py` re-runs itself
inside it.

## Usage

```bash
# Prep a list: the cold-call list shape or a Sales Navigator export, .csv or .xlsx
python3.12 listprep.py --input ~/lists/us-shops.csv
#   -> out/ppap_list_<date>.csv   the dialer's list
#      out/prep_report.md         counts per timezone and per reject reason
#      out/vicidial_ppap_*.csv    VICIdial load files

# Optional: tag line types before dialing (about $0.004 a number). Rows tagged
# mobile are blocked by the dialer unless compliance.allow_mobile is true.
export TELNYX_API_KEY=...
python3.12 telnyx_lookup.py --input out/ppap_list_<date>.csv

# Run the dialer on the newest prepped list for list id 101
python3.12 dialer/serve.py --list 101
```

With no `TWILIO_*` variables the dialer is a **simulator**: calls are faked,
everything else is real. An empty database is seeded with sample shops and the
calling windows are left open so you can practise at any hour
(`--strict-windows` enforces them anyway). With credentials the windows are
always enforced. See [dialer/TWILIO.md](dialer/TWILIO.md), including the two
console changes the caller-ID pool and the `pawan` seat need.

```bash
.venv/bin/python -m unittest discover tests      # 70+ tests, under a second
```

## The funnel

Every saved call is stamped with funnel flags from its outcome
(`dialer.outcomes` in `config.yaml`), so later config edits never rewrite
history.

| Key | Outcome | Counts as |
|---|---|---|
| 1 | Booked call | pickup, DM pitched, resonation, offered, booked |
| 2 | Callback (DM) | pickup, DM pitched |
| 3 | Resonated, no book | pickup, DM pitched, resonation, offered |
| 4 | Pitched, not a fit | pickup, DM pitched |
| 5 | DM reached, no pitch | pickup, DM reached |
| 6 | Gatekeeper blocked | pickup |
| 7 | Voicemail left | dial |
| 8 | No answer / busy | dial |
| 9 | Wrong number / disconnected | dial |
| 0 | Do not call | pickup; number blocked for good |

A pickup opens wrap-up level 2: objections heard (multi-select), whether you
asked for the meeting, the decision maker's name, email and mobile, and their
pain in their words. A booking needs their email and the time **on their
clock**.

| Rate | Definition | Target |
|---|---|---|
| **ABR** booking rate | booked / dials | **2%+**, team benchmark 4.5%. The star metric. |
| Pickup rate | pickups / dials | 20%+. Under 20% across 50+ dials on one caller ID: that number is probably spam-labelled. |
| DM reach rate | DMs pitched / pickups | 50%+ |
| PR pitch rate | DMs pitched / dials | tracked |
| RR resonation rate | resonations / DMs pitched | tracked |
| Offer rate | offered / resonations | 33%+ |
| SUR show-up rate | showed / booked calls that have come due | 60%+ |
| SCR sales conversion | sales / showed | 25%+ |
| Effective conversations | unique leads a day with a pickup where you offered or they resonated | 10 a day. The beginner star metric. |

Rates are coloured good, warn (within 75% of target) or bad, and stay grey
until the denominator reaches `targets.min_sample`.

**Attribution.** Show, no-show and sale are marked later from the Bookings or
Calls rail. A sales call done and a sale are credited to the **date of the dial
that booked them**, not the date of the meeting. **Days** run on
`dialer.stats_timezone` (US Eastern): you dial from India, and US business
hours cross midnight in both IST and UTC.

The funnel bar shows today, this week or all time, per-hour rates for the
running session and today's top two objections. `t` opens the full sheet with
a per-script-version breakdown. `/api/funnel.csv?range=today|week|all` exports
the Imperium tracker columns exactly: `Date, Calls, DM's Pitched, Resonations,
Call Booked, Sales Calls Done, Sales, Sales $, Notes`.

## How the dialer decides

- **Windows, on the prospect's clock.** Power 08:15-10:15 and 15:45-17:45,
  secondary 10:15-11:30 and 13:30-15:45, never 11:30-13:30, never before 08:00
  or after 18:00, never weekends. Power-window zones are dialed first, so the
  queue walks ET, CT, MT, PT by itself. A callback the prospect asked for may
  ring any time inside 08:00-18:00. Config that reaches outside the TCPA hours
  (08:00-21:00) is refused at startup.
- **Retries.** Six tries over about three weeks: never the same day, never the
  same weekday twice running, mornings and afternoons alternating. Voicemail on
  tries 1, 3 and 5 only. After try 6 the lead closes as EXHAUSTED, is tagged
  `email_only` and is written to `out/exhausted_for_email.csv`.
- **Caller IDs.** `numbers.pool`: area code match, then same state, then round
  robin. 30 dials a day for a number's first 21 days, 150 after, and that is a
  hard stop. A number whose 7-day pickup rate drops under 15% across 100+ dials
  is parked automatically. The Numbers rail shows all of it.
- **Sessions.** Pick a target (100 dials or 90 minutes by default) and a script
  version. The ETA is time and a half: dials left x average seconds per dial x
  1.5. The session stops itself at the target and shows its own funnel and its
  most common objection.
- **Compliance.** Internal do-not-call at prep and at dial time, an optional
  national scrub file checked at both (`compliance.dnc_scrub_file`), mobiles
  blocked unless `compliance.allow_mobile`, 30-day recall suppression at prep,
  ITAR and defense shops rejected at prep. Nothing is ever played to the person
  you call.

## Campaigns, targets, analytics, recording

- **Campaigns.** The Campaign menu (top bar) picks what you are calling: one
  campaign or all of them. Load a list (Campaign menu, or the agent menu) into
  a new campaign or add it to an existing one, any time. The queue, callbacks,
  bookings, today's calls and the stats all narrow to the campaign. Each
  campaign has its own script version and daily target (Campaigns > Edit).
  A number in two campaigns is never called twice: tries, callbacks and
  do-not-call are shared. Archive a campaign to stop calling it, or delete it:
  its leads go unless another campaign has them, and calls already made stay
  in the history and the numbers. On the first start after this landed, every
  list already loaded became its own campaign.
- **Removing leads.** Remove lead on the lead card, or Select in the queue to
  pick several. Remove from this campaign only, or delete everywhere, with
  undo. Deleted leads leave every list, a re-upload doesn't bring them back,
  and their calls still count.
- **Daily target.** The strip under the stats bar: dials today against the
  target, the pace so far, when that pace reaches the target and whether it
  does before calling hours end on the prospects' clocks, and how many calling
  days the campaign needs to reach every lead once. Default target:
  `dialer.daily_target` (150).
- **Analytics.** Stats bar > Analytics (or the agent menu): day, week and month
  views with totals, dials per period against the target line, and the full
  funnel table. The stats bar and the tracker CSV also have a Month range, and
  exports follow the campaign you are calling.
- **Recording.** With `dialer.recording: true` every call is recorded from
  pickup through Twilio's API (two channels, you and them). Each saved call
  gets a Play link in today's Calls and the lead's history. No disclosure is
  played or prompted. `compliance.hold_recording_in_all_party_states: true`
  holds the recording for leads in the all-party-consent states until you've
  said `compliance.recording_disclosure` and pressed R. Recording stays off on
  a public server until `DIALER_PASSWORD` is set.

## After the call: transcripts, pipeline, Claude

- **Transcripts.** Every recorded call is sent to Deepgram once Twilio finishes
  the file, and the text (you and them, in order) shows under the call in the
  lead's History. The key is `DEEPGRAM_API_KEY` or `DATA_DIR/secrets/deepgram.key`.
  If you show up as "Them", set `dialer.transcripts.agent_channel: 1`.
- **Pipeline.** Each lead can have a stage (Interested, Invite sent, Signed up,
  Using it, Call booked, Showed, No-show, Sold, Lost), a follow-up date and a
  note, set on the lead card. Resonated and Booked outcomes, shows, no-shows
  and sales move it on their own. The Pipeline tab lists them by stage, with
  the follow-ups that are due. Every change lands on the lead's timeline.
- **Copy for Claude** (History) puts the lead, its calls, transcripts and
  timeline on the clipboard for a Claude chat.
- **The Claude connector.** `dialer/mcp_server.py` is an MCP server (standard
  library only) that lets Claude read new calls, transcripts, the pipeline and
  follow-ups due, and set stages, follow-ups and contact details, log drafts and
  invite links, and mark calls processed. It never dials or sends. Add it with
  `claude mcp add dialer --scope user -- python3 /path/to/dialer/mcp_server.py`;
  it reads `DIALER_URL` and the password from `DIALER_PASSWORD` or
  `~/.pxl-dialer-password`. Transcripts and the connector's endpoints stay off
  on a public server until `DIALER_PASSWORD` is set.

## Deploying on Railway

The `dialer` service builds from this repo's `main` with the Dockerfile. Set
`DIALER_PASSWORD` (and optionally `DIALER_USER`, default `agent`) in the
service's Variables before anything else: without it the dialer is open to
anyone with the link. Sign in once with `https://<domain>/?key=<password>`;
the cookie lasts 30 days. Turn on automatic deploys under Settings > Source,
or deploy the newest commit from Deployments after every push. Data lives on
the volume at `DATA_DIR=/data`.

## The cockpit

- **Left rail.** Queue (search every list), Callbacks, Bookings (mark show,
  no-show, sale), today's Calls, Inbox (missed calls and voicemails), Numbers.
- **Lead.** Their local time and which window they are in, try n of 6, the
  caller ID they will see, process, OEM, headcount, LinkedIn status, the last
  pain line, research links, live notes, and discovery fields that stick to the
  lead.
- **Script rail.** The call as a tree: gatekeeper, permission, pull pitch,
  negative branch, qualify, ask, book. The current step is big, the next two
  small. Voicemail and they-called-back are their own flows. `o` opens a
  searchable objections panel in the anchor / pattern disrupt / question shape;
  rebuttals edited there are kept in `DATA_DIR/objections.json`. Script versions
  (`dialer.scripts.tree`) are picked per session and split every stat.
- **After a booking.** The follow-up email (subject: their first name) copies
  with `e`, and `dialer.booking_webhook_url` is called so the invite can be
  created outside the app.

- **Editing scripts.** *Edit* on the script rail (or *Edit scripts* in the
  agent menu) opens every step of every version, both follow-up emails and the
  objections rule, with a live preview against the lead on screen. You can
  change wording mid-session. Edits are kept in `DATA_DIR/scripts.json` over
  `config.yaml`, survive a redeploy, and each one has a reset back to the
  shipped text. *New version* copies a version so you can change one step and
  A/B it. Objection cards are edited in their own panel (`o`, then *Edit*).
  Em dashes and unclosed `{?token}` blocks are refused; unknown tokens are
  flagged.

The interface follows a few rules, written at the top of `dialer/app.css`:
type carries the hierarchy, one neutral palette, colour only where it means
something (green go, red stop, amber attention), metadata as plain text rather
than pills, hairlines instead of boxes, sentence-case labels.

Keys: `p` session, `space` dial or hang up, `1`-`9` `0` outcomes, `enter` save
or accept the suggestion, `z` undo, arrows walk the script, `b` book, `o`
objections, `e` copy email, `t` stats, `n` notes, `/` search, `d` dial a
number, `?` everything else.

## Pipeline

```
Excel/CSV
  -> column mapping (alias table: cold-call list shape, Sales Navigator exports)
  -> E.164 normalize (bare 10/11-digit, +1 formats, extensions)
  -> US/CA region filter
  -> rejects: ITAR / defense words, over prep.max_employees, internal DNC,
     national scrub file, called within 30 days, duplicate number
  -> one contact per company: the best-scored one
  -> toll-free/switchboard split (separate list + script)
  -> timezone from area code, the state as tie-break and fallback
  -> priority score -> percentile rank
  -> ppap_list_<date>.csv + prep_report.md + VICIdial CSVs + rejection log
```

## Files

| Path | Purpose |
|---|---|
| `listprep.py` | List prep pipeline |
| `telnyx_lookup.py` | Number validation + line-type tagging, cached |
| `config.yaml` | Scoring, retry cadence, caller-ID pool, windows, outcomes, targets, every script string |
| `dialer/serve.py` | HTTP server: queue API, config, carrier tokens and REST, uploads, webhook |
| `dialer/db.py` | SQLite state: checkout, windows, retries, callbacks, caps, DNC, sessions, funnel rows |
| `dialer/policy.py` | Pure rules: windows, retry scheduler, voicemail tries, caller-ID picker, caps, parking |
| `dialer/funnel.py` | Pure funnel maths: counts, rates, attribution, the Imperium sheet |
| `dialer/index.html`, `dialer/app.css` | Cockpit markup and design system |
| `dialer/js/*.js` | Cockpit modules: app, wrap, script, editor, funnel, rails, session, modals, carrier, ui, util, state, api |
| `dialer/demo_leads.csv` | Sample shops seeded into an empty database in simulator mode |
| `dialer/TWILIO.md` | Twilio setup, the caller-ID pool Function, the Telnyx path |
| `tests/` | Policy, funnel, database (frozen clock), config lint, list prep |
| `docs/` | The upgrade plan and cockpit screenshots |
| `out/` | Generated lists, rejection logs, reports, `exhausted_for_email.csv` |

## Scoring

Raw fit score -> **percentile rank** within each load. The queue dials down
rank inside whichever window is open, so the best-fit shops get the first power
window of their day. Only features that **vary** across the list are weighted.

Signals: title (owner / president / CEO / founder 15, quality manager /
engineer / director 15, GM / plant manager / VP operations or engineering 12),
headcount (25-99 scores highest, 500+ is rejected), a process word in the name
or notes (+10), an automotive / aerospace / medical word (+8), LinkedIn invite
accepted (+20), engaged with the cold email (+40). Weights live in
`config.yaml`.
