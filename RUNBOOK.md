# VICIdial + Telnyx power dialer — build runbook

Target: 5 agents, ~500 dials/day, US/CA outbound B2B, human agents, single-line power dialing.

**Verify exact UI paths and IP ranges against current Telnyx and VICIdial docs as you go.**
Both products move; the config shapes below are stable, the menu labels are not.

---

## 0. Decide these first

| Decision | Guidance |
|---|---|
| **Server region** | Put the server near the **agents**, not near the leads. Agent↔server is the interactive leg (WebRTC); server↔Telnyx↔PSTN is one hop on a good network. Agents in Canada → Toronto/NY. Agents in India → the agent audio is the bottleneck; test both a US-East and a Mumbai/Singapore box before committing. |
| **Static IP** | Mandatory. Telnyx IP authentication needs it and it is far more robust than SIP registration. |
| **Domain name** | Mandatory. Browsers refuse microphone access over plain HTTP, so the WebRTC softphone needs HTTPS + a real cert. Point an A record at the server before you start. |
| **Legal entity for CNAM** | The name that will display on caller ID. Decide it now — it must match across Telnyx, CNAM, and Free Caller Registry, or the registrations get rejected. |

---

## 1. Telnyx account setup

1. **Sign up and complete KYC.** You cannot buy numbers or originate traffic until this clears. Allow extra time if the purchasing entity is non-US.
2. **Set a hard spend limit** on the account. Non-negotiable — see the toll-fraud warning in §9.
3. **Buy numbers.** From your prep report, the densest area codes in this list are:

   | NPA | Metro | Leads |
   |---|---|---|
   | 917 / 646 / 212 / 516 | New York | 119 |
   | 310 / 323 / 805 / 949 | Los Angeles / SoCal | 90 |
   | 415 | San Francisco | 25 |
   | 801 | Salt Lake City | 22 |
   | 203 | Connecticut | 19 |
   | 503 | Portland | 15 |

   **Buy 6 local DIDs: 2× NY (917, 646), 2× LA (310, 323), 1× SF (415), 1× spare.**
   5 in rotation (one per agent) + 1 held back. At $1.00/number/month that is $6/month.

   Do *not* buy 50 numbers to rotate through. Numbers are brand assets; rotation to dodge labels burns reputation faster and makes callbacks impossible.

4. **Create a SIP Connection** — Voice → SIP Connections → Create.
   - Connection type: **IP Authentication** (simpler and more robust than credentials for a fixed-IP server)
   - Authorized IP: your server's static IP
   - Note the SIP host you must dial toward (`sip.telnyx.com` at time of writing)
5. **Create an Outbound Voice Profile**, attach the connection, allow **US + Canada only**, and set a **concurrent call limit of 8** (5 agents + headroom). This caps blast radius if anything goes wrong.
6. **Assign all 6 numbers** to the connection so inbound callbacks route to your server.
7. **Register for Number Reputation + Branded Calling.** Enterprise registration + a signed **Letter of Authorization**. Start this on day one — it is the long pole, and it is the single reason to be on Telnyx for this use case. Set auto-refresh to `business_daily`.
8. **Set CNAM** on every number to your legal business name. Never "Sales" or a person's name.
9. **Submit all 6 numbers to the Free Caller Registry** (freecallerregistry.com). Free, one form, feeds Hiya / First Orion / TNS simultaneously. Unregistered numbers get materially more scrutiny.

---

## 2. Server provisioning

**Specs for 5 agents (single all-in-one box):** 4 vCPU, 8 GB RAM, 80 GB SSD. Recordings accumulate — budget ~15 MB per agent-hour and plan to offload monthly.

**Provider must support custom ISO boot.** VICIbox ships as an ISO.
- **Vultr** — supports custom ISO upload. Easiest path, ~$40/month at these specs.
- Any bare metal with IPMI/KVM console also works.
- **DigitalOcean and Linode do not support custom ISO** — avoid unless you want a from-scratch Asterisk build.

Steps:
1. Download the current **VICIbox ISO** from vicibox.com.
2. Upload it as a custom ISO and boot the instance from it.
3. Complete the openSUSE installer (accept defaults; set a strong root password).
4. Reboot, remove the ISO, log in as root.
5. Run the VICIbox setup script — `vicibox-install` on current ISOs (`vicibox-express` on older ones). Choose the **standalone / single-server** option. It configures MySQL, Asterisk, and VICIdial together.
6. Reboot. Confirm the admin UI loads at `http://SERVER_IP/vicidial/admin.php`.

**Change the default credentials immediately.** VICIdial ships with user `6666` / password `1234`, and every scanner on the internet knows it.

---

## 3. TLS — required for the browser softphone

The agent screen uses WebRTC; browsers will not grant microphone access over plain HTTP.

```bash
zypper install certbot
certbot certonly --standalone -d dialer.yourdomain.com
```

Point Apache at the cert, force HTTPS, and confirm the WSS websocket endpoint is reachable. Agents then use `https://dialer.yourdomain.com/agc/vicidial.php`.

Renewal is every 90 days — put `certbot renew` on cron now, not after the first expiry outage.

---

## 4. Telnyx trunk in VICIdial

Admin → Carriers → **Add New Carrier**.

**Account Entry** (becomes a `sip.conf` peer):

```
[telnyx]
type=peer
host=sip.telnyx.com
context=trunkinbound
disallow=all
allow=ulaw
allow=alaw
dtmfmode=rfc2833
qualify=yes
insecure=port,invite
nat=no
directmedia=no
canreinvite=no
```

`nat=no` and `directmedia=no` assume a VPS with a directly attached public IP (no NAT). If your provider NATs the instance, you must set `externip` and `localnet` in `sip.conf` or you will get one-way audio — the single most common VICIdial-on-VPS failure.

**Dialplan Entry** (becomes `extensions.conf`):

```
exten => _91NXXNXXXXXX,1,AGI(agi://127.0.0.1:4577/call_log)
exten => _91NXXNXXXXXX,n,Dial(SIP/telnyx/+${EXTEN:1},,tTo)
exten => _91NXXNXXXXXX,n,Hangup()
```

The leading `9` is VICIdial's dial prefix and is stripped by `${EXTEN:1}`, leaving `1NXXNXXXXXX`; the `+` makes it E.164. **Verify the exact format Telnyx expects on your connection** — some configurations want the bare `1NXXNXXXXXX`.

Set **Server IP** to your server, **Protocol** SIP, **Active** Y. Reload Asterisk, then confirm the peer is reachable:

```bash
asterisk -rx "sip show peers" | grep telnyx     # expect OK with a latency figure
```

---

## 5. Campaigns — one per agent

Each agent needs their own caller ID, and in VICIdial caller ID binds to the **campaign**. So: 5 campaigns, one per agent, one DID each. `listprep.py --split-agents 5` already produced five rank-balanced lists (`list_id` 101–105) to match.

Create campaigns `TTS_A1` … `TTS_A5`. Settings that matter:

| Setting | Value | Why |
|---|---|---|
| Dial Method | `RATIO` | |
| **Auto Dial Level** | **`1.0`** | One line per agent = true power dialing. This is what keeps you out of abandoned-call territory and out of predictive-dialer spam patterns. |
| Dial Prefix | `9` | Matches the dialplan above |
| Dial Timeout | `35` | ~6 rings |
| **Campaign CallerID** | that agent's DID | The whole point of one campaign per agent |
| **Answering Machine Detection** | **`DISABLED`** | See below |
| Lead Order | a RANK-based order (`DOWN RANK`) | Uses the `rank` column so the warmest leads get dialed first. Verify the exact option name in your build. |
| Local Call Time | custom, see §6 | |
| Campaign Recording | `ALLFORCE` | Every call recorded, no agent discretion |
| Use Internal DNC | `Y` | |
| Use Campaign DNC | `Y` | |

**On disabling AMD:** the default instinct is to turn it on. Don't, at this scale. At dial level 1.0 the agent is already on the line, so AMD only inserts a 2–4 second delay before they hear anything — and its false positives hang up on real humans mid-"hello". A human decides in one second what AMD takes four seconds to get wrong. Revisit only if you move to multi-line dialing.

Assign list 101 → `TTS_A1`, 102 → `TTS_A2`, and so on.

**Toll-free list (200)** gets its own campaign and its own script. Those 193 numbers are switchboards, not people — a different conversation, a worse connect rate, and mixing them into the main queue will corrupt your metrics.

---

## 6. Calling hours

Admin → Call Times → create a custom call time and attach it to every campaign.

| | Weekday | Weekend |
|---|---|---|
| US | 08:00 – 21:00 | 08:00 – 21:00 |
| Canada | 09:00 – 21:30 | **10:00 – 18:00** |

Recipient local time, enforced from the lead's `gmt_offset_now`. The Canadian weekend window is much tighter than the US one — do not copy the weekday row across.

On the lead loader, enable GMT-offset calculation from area code (the prep script also emits `gmt_offset_now` as a cross-check).

Your list is 533 Eastern / 306 Pacific / 229 Central, so a single Eastern-hours shift misses a third of the list. Staff accordingly.

---

## 7. Load the leads

Admin → Lists → create lists 101–105 and 200 first, then add the **custom fields** (`job_title`, `domain`, `url`, `tiktok`, `tiktok_followers`, `instagram`, `industry`, `categories`, `company_size`, `screenshot_url`, `linkedin`) on each list *before* loading, or those columns get silently discarded.

Then Admin → Lists → **Load New Leads**, one file per list.

Field mapping notes baked into the export:
- `phone_number` is bare 10-digit NANP; `phone_code` is `1`
- **Company name is in `address3`** — VICIdial has no company field
- **`title` is deliberately empty.** VICIdial's `title` is `varchar(4)`, a salutation, not a job title. Job title is a custom field.
- `comments` is capped at 255 chars and holds the screen-pop context
- `vendor_lead_code` is a stable 16-char hash of the E.164 — use it to reconcile dispositions back to the master list

Run the Telnyx validation pass first (~$4.74 for the full list):

```bash
export TELNYX_API_KEY=...
python3.12 telnyx_lookup.py --input out/vicidial_tiktokshop_agent1_list101_*.csv
```

---

## 8. Compliance wiring

- **Recording disclosure** — make it the mandatory first line of the VICIdial Script on every campaign: *"This call is being recorded for quality and training purposes."* One policy for all states beats per-state all-party-consent analysis. (You can automate it in the dialplan instead, but the script line is reliable and ships today.)
- **Company identification on connect** — required by CRTC for Canadian numbers. Put it in the same opening line.
- **DNC** — enable both internal and campaign DNC. Train agents that the `DNC` disposition is immediate and irreversible. Export the internal DNC into `dnc.csv` weekly so the prep pipeline suppresses at load time too — belt and braces.
- **Opt-out SLA** — 10 business days (US, FCC revocation rule), 14 days (Canada, CRTC).
- **Retention** — keep call logs 5 years. Nightly `mysqldump` of the `asterisk` database, off-server.
- **State law** — get a TCPA attorney to review your state list before you scale past the pilot. Florida's FTSA and the other mini-TCPAs do not always exempt B2B, and that is where the real exposure sits.

---

## 9. Security — read this one

A VICIdial box on the public internet is scanned continuously, and a compromised Asterisk gets used for toll fraud measured in thousands of dollars per hour.

1. **Hard spend limit on the Telnyx account.** Your actual backstop.
2. **Firewall SIP (5060/UDP+TCP) to Telnyx signaling IPs only.** Get the current ranges from Telnyx docs — do not leave 5060 open to the world.
3. **Restrict the admin UI** (`/vicidial/admin.php`) to your team's IPs.
4. **Install and enable fail2ban** with the Asterisk jail.
5. **Change every default password** — VICIdial admin, MySQL, and the Asterisk manager interface.
6. Concurrent-call limit of 8 on the Telnyx outbound profile caps the damage even if everything else fails.

---

## 10. Number warm-up

Do not buy 6 numbers on Monday and dial 500 on Tuesday.

| Week | Dials/number/day | Team total/day |
|---|---|---|
| 1 | 30 | 150 |
| 2 | 60 | 300 |
| 3 | 100 | 500 |
| 4+ | up to 150 (hard ceiling) | 750 capacity |

Check Telnyx Number Reputation daily during warm-up. If a number gets flagged, **submit it for remediation — do not discard it.**

---

## 11. Go-live checklist

- [ ] `sip show peers` shows telnyx OK with sane latency
- [ ] Outbound test call connects with **two-way audio** (one-way audio = NAT config, see §4)
- [ ] Caller ID displays the correct DID and CNAM name on a real mobile
- [ ] Inbound call to each DID routes to a human or an IVR — a number that never receives callbacks looks like a robocaller
- [ ] Recording produced, stored, and playable
- [ ] Calling-hours rule blocks an out-of-hours lead (test with a Pacific lead at 7am Pacific)
- [ ] `DNC` disposition removes a lead and blocks redial
- [ ] Agent softphone works over HTTPS in Chrome from the agents' actual location
- [ ] Telnyx spend limit set; 5060 firewalled; defaults changed
- [ ] All 6 numbers submitted to Free Caller Registry; CNAM set; LOA filed

---

## 12. What to watch weekly

| Metric | Healthy | Meaning if off |
|---|---|---|
| Connect rate | 8–15% | <5% = number reputation, not script |
| Avg call duration | stable | **Falling = you are being labeled.** Leading indicator, moves before connect rate does. |
| Answer→hangup <5s | <20% | Spike = showing as spam |
| Dials/number/day | <150 | Hard ceiling |
| Telnyx reputation | clean | Remediate immediately, never rotate away |
