# PPAP campaign upgrade: plan

Branch `ppap-campaign`. One commit per numbered section, nothing pushed until
you say so. Each commit ships with a diff summary and screenshots under
`docs/screenshots/`.

## What I found reading the repo

- `serve.py` has no `--list` flag even though the README uses it. It gets one.
- The `/dial` Twilio Function forces one server-side caller ID. A caller-ID
  pool needs a small change to that Function (an allow-list). I cannot deploy
  it from here, so the new Function code goes into `dialer/TWILIO.md`.
- Inbound calls ring the Twilio client `agent1`. With the seat renamed to
  `pawan`, the Function's `AGENT_CLIENT` variable must change to `pawan` too.
- Lead time zones are stored as a fixed UTC offset taken at prep time. That
  goes stale at the November clock change. Leads will carry an IANA zone name
  and the offset is computed live.
- "Today" is a UTC day right now. You dial US hours from India, which crosses
  midnight in both IST and UTC. Stats days, caps and "same day" rules move to
  US Eastern (`dialer.stats_timezone`).
- The homebrew `python3.12` on this Mac has no PyYAML or pandas. The whole
  script tree now lives in `config.yaml`, so PyYAML becomes required. If it
  is missing and `.venv/` exists, `serve.py` re-runs itself inside it.

## Decisions where the spec left room

1. **`mobile` means two things in the spec.** `mobile` stays the captured
   mobile number. The dial-blocking flag is `is_mobile` (from a `line_type`
   column or the Telnyx lookup output).
2. **Windows.** Cold dials only inside power and secondary windows. Callbacks
   the prospect asked for, and hand-picked leads, may be dialed anywhere
   inside 08:00 to 18:00 local on weekdays, lunch included.
3. **Simulator.** With no carrier credentials no real call is placed, so
   windows are not enforced and an empty database is seeded with demo shops.
   `--strict-windows` turns enforcement back on to watch the ET to PT rotation.
4. **Voicemail on attempts 1, 3, 5.** On other attempts Drop VM is disabled,
   the voicemail script is replaced by "hang up, no message", and the outcome
   is saved as no answer.
5. **EXHAUSTED.** The dial log keeps what really happened on dial 6. The lead
   is closed as EXHAUSTED, tagged `email_only`, and exported.
6. **Retry spacing.** Gaps of 2, 3, 4, 5, 6 days (20 days, six attempts),
   pushed off weekends and off the previous attempt's weekday, in the opposite
   half of the day.
7. **Front end.** `app.js` is split into small ES modules (no build step, no
   framework) with the carrier behind an adapter so Telnyx can slot in.
8. **Preview mode without a server is removed.** Simulator mode replaces it.
9. **Spam lookup links** are a configurable list. I will not invent a lookup
   URL for a site whose URL format I cannot confirm.

## One concern to flag

Three scripted lines state things that may be untrue on a given call: "an
email I sent", "personal business", and the cut-off voicemail followed by "I
got cut off". They go into `config.yaml` exactly as specified, because they
are your words for your calls and they are editable. The risk is yours to
weigh: a prospect who notices starts the booked call distrustful, and some
states treat misrepresenting the purpose of a sales call as a deceptive
practice.

## Order of work

0. Prep: split the front end into modules, carrier adapter, `--list`, venv hop.
1. Retarget: campaign, scoring, tokens, lead columns.
2. Funnel: outcomes, two-level wrap-up, follow-through fields, stats bar,
   stats API, Imperium CSV.
3. Sessions, power windows, retry cadence, caller-ID pool.
4. Script rail as a call tree, objections panel, follow-up emails, webhook.
5. Cockpit: Bookings and Numbers rails, lead pane, keyboard.
6. List prep: new shapes, rejects, `ppap_list_<date>.csv`, `prep_report.md`.
7. Compliance: national DNC scrub, mobile block, hard cap stop.
8. Definition of done: tests, grep checks, README.
