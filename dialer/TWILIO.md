# Twilio wiring for the browser dialer

> **Status: deployed 2026-09-16.** Everything in §1 already exists on the
> account (created via API) and the credentials live in `../.env`, which
> serve.py loads automatically — just run `python3.12 dialer/serve.py`.
>
> - Function: `https://powerdialer-2168-prod.twil.io/dial` (service `powerdialer`, env `prod`, `CALLER_ID` set)
> - TwiML App: `APb5557d0759f1e8d1ff0d118af2f25fbd`
> - API key: `SKe93e249af5c0bb072a41269665c8747e` (secret only in `.env`)
> - Number: `+1 989 375 1429`
>
> The account is still a **trial**: calls only connect to verified numbers
> and play a trial notice first. Upgrade the account before dialing leads.
> Still open: Trust Hub / SHAKEN-STIR / CNAM registration (§1 step 4).
>
> **Hosted at https://powerdialer-production.up.railway.app** (Railway
> project `powerdialer`, personal workspace) behind HTTP Basic auth —
> username `agent`, password in the comment at the bottom of `../.env` and
> in the service's `DIALER_PASSWORD` variable. Deploy code updates with
> `railway up` from the repo root; lists load through the UI, no redeploy.
>
> **State lives in SQLite** (`/data/dialer.db` on the volume; `dialer.db`
> at repo root locally). The server owns the queue: atomic per-agent lead
> checkout, lead-local calling-hours enforcement (wk 9:00–20:30 / wknd
> 10:00–18:00, override via `WINDOW_WEEKDAY="9-20.5"`), automatic retry of
> NO_ANSWER/VOICEMAIL after 24 h up to 5 attempts, scheduled callbacks
> that jump the queue when due, per-day dial cap (150) computed from
> actual history, notes on every disposition, and `/api/dnc.csv` export.
>
> **Inbound**: the number's voice URL is the `/inbound` Function — return
> calls ring the `agent1` browser client (screen-pop from the DB via
> `/api/lookup`); after 15 s unanswered they get the `VM_GREETING` and
> record a voicemail, which shows up in the UI's Inbox tab, next to missed calls
> (played through the server proxy `/api/voicemail/<sid>.mp3`).
>
> **VM drop**: the Drop-VM button redirects the callee leg to a spoken
> message (`VM_DROP_TEXT` env var on the *dialer* service, Polly voice)
> and auto-dispositions VOICEMAIL. Functions on the service: `/dial`,
> `/inbound`, `/voicemail`; env vars there: `CALLER_ID`, `AGENT_CLIENT`,
> `VM_GREETING`.

The UI places calls with the Twilio Voice JS SDK: the browser connects to
Twilio over WebRTC, Twilio hits a TwiML App for instructions, and the TwiML
dials the lead's number with your caller ID. `serve.py` mints the access
token locally, so no Twilio helper library and no public server are needed
on this side — only the TwiML must be hosted on Twilio (a Function).

## 1. Console setup (one time, ~15 min)

1. **API key** — Console → Account → API keys & tokens → Create API key
   (Standard). Note the SID (`SK...`) and Secret — the secret is shown once.

2. **TwiML Function** — Console → Functions & Assets → Services → Create.
   Add a function at path `/dial`, visibility *public*, with:

   ```js
   exports.handler = function (context, event, callback) {
     const twiml = new Twilio.twiml.VoiceResponse();
     const to = (event.To || "").trim();

     // NANP E.164 only — this dialer is US/CA outbound, nothing else.
     if (!/^\+1\d{10}$/.test(to)) {
       twiml.say("Invalid number.");
       return callback(null, twiml);
     }

     const dial = twiml.dial({
       callerId: context.CALLER_ID || event.CallerId,
       answerOnBridge: true,   // browser leg stays ringing until the human answers
       timeout: 25,            // ~5 rings, then give the leg back to the agent
     });
     dial.number(to);
     callback(null, twiml);
   };
   ```

   In the service's Environment Variables, set `CALLER_ID` to your Twilio
   number in E.164 (`+1...`). That server-side value wins over anything the
   browser sends, so a stolen token can't spoof caller ID. Deploy, then copy
   the function URL (`https://<service>-<id>.twil.io/dial`).

   `answerOnBridge` matters: without it Twilio answers the browser leg
   immediately and the UI can't tell ringing from connected. With it, the
   SDK's `accept` event fires only when the lead actually picks up, which is
   what drives the connect counter.

3. **TwiML App** — Console → Voice → TwiML apps → Create. Set the Voice
   *Request URL* to the function URL from step 2 (POST). Note the SID
   (`AP...`).

4. **Number** — buy a local US number (Phone Numbers → Buy) or port one in.
   One number per agent seat, permanently — same no-rotation rule as before.
   Register the number + your business in **Trust Hub** and create a
   **SHAKEN/STIR** profile so calls get A-attestation; unregistered Twilio
   traffic gets labelled spam fast. Also register for **CNAM** so your
   business name shows on caller ID.

## 2. Run

```bash
export TWILIO_ACCOUNT_SID=AC...
export TWILIO_API_KEY_SID=SK...
export TWILIO_API_KEY_SECRET=...
export TWILIO_TWIML_APP_SID=AP...
export TWILIO_CALLER_ID="+1..."       # also the number shown in the UI strip

python3.12 dialer/serve.py --list 101
```

Startup prints `twilio credentials found — real calls` when the env is
complete; otherwise the UI runs in simulator mode exactly as before. The
browser asks for microphone access on the first dial — allow it.

## 3. Call flow → UI states

| Twilio event | UI |
|---|---|
| session countdown hits 0, or `space` | `connect()` issued |
| `connect()` issued | DIALING, lamp "Dialing" |
| `ringing` | lamp "Ringing" |
| `accept` (human answered) | LIVE, timer starts, connect counted |
| `disconnect` while LIVE | wrap-up → outcome grid |
| `disconnect`/`cancel` while DIALING | outcome grid, *No answer* suggested on `enter` |
| Hang up / space while DIALING | abandons the ringing call |

Dispositions still post to `/api/disposition` and land in `called_log.csv`
and `dnc.csv` — nothing about the suppression loop changed.

## 4. Costs and caveats

- ~$0.014/min for the browser (WebRTC) leg + ~$0.014/min for the outbound
  PSTN leg while bridged, plus $1.15/mo per number. Roughly 2–3× the Telnyx
  per-minute plan, but with zero server to maintain.
- Twilio has no equivalent of Telnyx's number-reputation remediation API —
  monitor spam labelling yourself (Free Caller Registry, Hiya) and stay
  under the 150 dials/day/number cap the UI already enforces.
- Recording: add `record: "record-from-answer-dual"` to the `dial()` options
  in the Function if you want it, then set `dialer.recording: true` in
  `config.yaml` so the agent sees a REC badge on live calls and a
  "Say first" recording-disclosure prompt above the opener. Nothing is ever
  played to the callee automatically: with `answerOnBridge` the agent is live
  from the moment the lead picks up. The one automated voice you may hear is
  Twilio's own trial-account notice, which goes away when the account is upgraded.
