# India dialer: plan

A separate app at `india-dialer/`. It does not touch `dialer/`,
`config.yaml` or `listprep.py`. It copies their patterns: a stdlib HTTP
server, SQLite, a single-page keyboard-first cockpit, a dispositions table
with funnel flags, the stats endpoint and the tracker CSV. There is no carrier
layer. Pawan dials on his own phone and the laptop runs everything else.

Branch `india-dialer`. One commit per numbered section. Screenshots go in
`india-dialer/docs/screenshots/`.

## What I found before building

- **The list is messier than its header.** About 55 of 126 rows carry the state
  or a stray `a` / `b` / `c` in `type`. States mix full names and codes (`MH`,
  `TN`, `Haryana (NCR)`). Volume is `500+`, `300-800` or, once, a sentence. So
  the importer keeps the raw `type` and derives a `segment` (marketplace, lab,
  consultancy, engineering services, manufacturer) from `type` and `why`. It
  normalises states, and reads the low end of a volume range, as the US dialer
  did.
- **"Starts with 6-9 means mobile" is wrong for this list.** Bangalore
  (`080 4…`), Ahmedabad (`079 4…`), Mysore, Belgaum and Pithampur landlines all
  start with 7 or 8, and the IndiaMART virtual numbers are nearly all of this
  kind. The importer uses `phonenumbers` (already in `requirements.txt`),
  which knows India's number ranges. Where it cannot tell, it falls back on how
  the number was written. A stdlib-only fallback exists, but it is marked as
  less accurate.
- **Phone cells use shorthand.** `+91 80 47780293 / 4692` means "same number,
  last four digits 4692". `0422-4330330 / 2332100` means "same STD code".
  `1800 425 5758 (toll free)` is a third kind. Two companies list only `+1`
  numbers, which go to the international list. 14 rows have no number at all.
  They are imported for research and kept out of the queue.
- **Where the file lives.** The spec's command reads `Me/India-Ballooning-…csv`,
  but the file is in `~/Documents/Claude/Projects/Me/`. `Me` in the repo root
  is a git-ignored symlink to it. The list holds named people's mobiles, and
  the repo is public, so it is never committed.
- **`python3` on this Mac is the system 3.9 without PyYAML.** Like
  `dialer/serve.py`, both scripts re-run themselves inside the repo's `.venv`
  when PyYAML is missing. All code stays 3.9-compatible.

## Decisions where the spec left room

1. **DMs reached vs pitched.** In the outcome table every "DM" outcome is also
   "DM pitched", so the two counts match today. The config gives each outcome
   separate `dm` and `pitched` flags, so an outcome such as "DM reached, no
   pitch" can be added later.
2. **Positive conversation.** This is a unique lead today where the decision
   maker was pitched and ended interested, asked for a sample, booked a demo or
   asked for a callback.
3. **Tries and attempts.** Trying the next number stays inside the same
   attempt. Every number tried is a dial, a row with its own number type, but
   only one attempt counts toward the five.
4. **Off-window hours.** 09:00-10:00 and 18:30-21:00 are legal but not in any
   window. Power mode does not cold-dial then. Hand-picked leads, callbacks and
   follow-up calls may be dialled from 09:00 to 21:00.
5. **Sundays and holidays.** No cold dials. A callback the DM asked for may
   still ring, with a warning.
6. **Demos in the pipeline.** A demo gets a pipeline card like a sample. After
   the demo it moves to Asked (they send drawings) or to Won or Lost. Wins,
   rupees and drawings are credited to the dial that created the card.
7. **Power-mode countdown.** It opens the dial screen: the big number, the QR
   and the timer. Browsers only open `tel:` links from a real key press or
   click, so the phone link fires on `space` or a click. The QR works either
   way.
8. **The tracker CSV** keeps the Imperium column order, with the last money
   column headed `Sales ₹` and filled with plain rupee integers so the sheet
   can still sum it.
9. **Script editing** in the app is carried over from the US dialer, with a
   Hinglish field next to each English line. Edits live in
   `india-dialer/data/scripts.json`, laid over `config.yaml`.
10. **Phone companion `/m`.** A token-protected page shows the current number
    as a tap-to-call link and follows the laptop. It binds to localhost unless
    `--lan` is given.

## Order of work

0. Plan, skeleton, `Me` link.
1. Data and import: `phones.py`, `intake.py`, `import.py`, scoring, report.
2. Phone handoff: tel link, local QR encoder, ringing and connected timers,
   try next number, power mode.
3. Outcomes, level-2 fields, sample and demo pipeline, attribution.
4. Metrics: stats bar, breakdowns, best hour, both CSVs.
5. IST windows, holidays, retries, callbacks with notifications, queue order.
6. Script rail with Hinglish, objections panel, referrals, in-app editor.
7. Follow-ups: WhatsApp and email templates, logging, Follow-ups rail.
8. Cockpit layout pass, the Stats rail, the `/m` companion.
9. Compliance: DNC at import and dial time, holds, hours, no auto-send.
10. Definition of done, README, tests and screenshots.
