"""Call transcripts: Twilio recording -> Deepgram -> text on the call.

After a recorded call is saved, its row is marked `queued`. A background
thread waits for Twilio to finish the recording, sends the audio to Deepgram
(two channels: you and them), and stores a plain transcript on the call:

    Pawan: Hi, is Dale in?
    Them: Speaking.

The key is DEEPGRAM_API_KEY, or the file DATA_DIR/secrets/deepgram.key (kept
on the volume so it survives redeploys and never sits in the repo).
"""

import json
import os
import queue
import threading
import time
import urllib.parse
import urllib.request

import db

KEYTERMS = ["PXL Kraft", "PPAP", "AS9102", "FAI", "ballooning", "ballooned", "PSW", "PFMEA", "control plan"]
DEEPGRAM_URL = ("https://api.deepgram.com/v1/listen?model=nova-3&multichannel=true&smart_format=true"
                "&punctuate=true&utterances=true&language=en-US"
                + "".join("&keyterm=" + urllib.parse.quote(k) for k in KEYTERMS))
AGENT_CHANNEL = 0          # which recording channel is you; config dialer.transcripts.agent_channel
AGENT_NAME = "Pawan"
MAX_WAIT_SEC = 15 * 60     # give up waiting for Twilio's recording after this long

_q = queue.Queue()
_started = False
_fetch_json = None          # Recordings/{sid}.json, injected by serve.py
_fetch_mp3 = None           # Recordings/{sid}.mp3


def api_key():
    key = os.environ.get("DEEPGRAM_API_KEY", "").strip()
    if key:
        return key
    path = os.path.join(db.DATA_DIR or db.ROOT, "secrets", "deepgram.key")
    try:
        with open(path) as fh:
            return fh.read().strip()
    except OSError:
        return ""


def enabled():
    return bool(api_key()) and _fetch_mp3 is not None


def start(fetch_json, fetch_mp3, agent_channel=0, agent_name="Pawan"):
    """Start the worker and re-queue anything a restart interrupted."""
    global _started, _fetch_json, _fetch_mp3, AGENT_CHANNEL, AGENT_NAME
    _fetch_json, _fetch_mp3 = fetch_json, fetch_mp3
    AGENT_CHANNEL, AGENT_NAME = int(agent_channel or 0), agent_name or "Pawan"
    if _started:
        return
    _started = True
    threading.Thread(target=_worker, name="transcribe", daemon=True).start()
    with db.connect() as con:
        for (dispo_id,) in con.execute("SELECT id FROM dispositions WHERE transcript_status IN ('queued','working') "
                                       "AND recording_sid != '' ORDER BY id").fetchall():
            _q.put(dispo_id)


def enqueue(dispo_id):
    """Mark a saved call for transcription. No recording or no key: nothing to do."""
    with db.connect() as con:
        row = con.execute("SELECT recording_sid FROM dispositions WHERE id=?", (dispo_id,)).fetchone()
        if row is None or not row["recording_sid"] or not enabled():
            return False
        con.execute("UPDATE dispositions SET transcript_status='queued', transcript_error='' WHERE id=?", (dispo_id,))
    _q.put(dispo_id)
    return True


def _set(dispo_id, **fields):
    with db.connect() as con:
        con.execute(f"UPDATE dispositions SET {', '.join(k + '=?' for k in fields)} WHERE id=?",
                    list(fields.values()) + [dispo_id])


def format_utterances(result, agent_channel=0, agent_name="Pawan"):
    """Deepgram multichannel result -> 'Name: text' lines in time order."""
    utterances = (result.get("results") or {}).get("utterances") or []
    lines, last_who = [], None
    for u in sorted(utterances, key=lambda u: u.get("start", 0)):
        text = (u.get("transcript") or "").strip()
        if not text:
            continue
        who = agent_name if int(u.get("channel", 0)) == agent_channel else "Them"
        if who == last_who and lines:
            lines[-1] += " " + text
        else:
            lines.append(f"{who}: {text}")
        last_who = who
    return "\n".join(lines)


def transcribe_audio(audio, key):
    req = urllib.request.Request(DEEPGRAM_URL, data=audio, method="POST",
                                 headers={"Authorization": f"Token {key}", "Content-Type": "audio/mpeg"})
    with urllib.request.urlopen(req, timeout=120) as resp:
        return json.loads(resp.read())


def _process(dispo_id):
    with db.connect() as con:
        row = con.execute("SELECT recording_sid, at FROM dispositions WHERE id=?", (dispo_id,)).fetchone()
    if row is None or not row["recording_sid"]:
        return
    sid = row["recording_sid"]
    _set(dispo_id, transcript_status="working")
    started = time.time()
    while True:                                   # Twilio finishes the file a little after hang-up
        info = _fetch_json(sid) or {}
        status = (info.get("status") if isinstance(info, dict) else "") or ""
        if status == "completed":
            break
        if status in ("failed", "absent", "deleted"):
            _set(dispo_id, transcript_status="failed", transcript_error=f"Twilio recording {status}")
            return
        if time.time() - started > MAX_WAIT_SEC:
            _set(dispo_id, transcript_status="failed", transcript_error="recording never finished")
            return
        time.sleep(10)
    audio = _fetch_mp3(sid)
    if not isinstance(audio, (bytes, bytearray)) or len(audio) < 1000:
        _set(dispo_id, transcript_status="failed", transcript_error="recording audio unavailable")
        return
    result = transcribe_audio(bytes(audio), api_key())
    text = format_utterances(result, AGENT_CHANNEL, AGENT_NAME)
    _set(dispo_id, transcript=text or "(no speech)", transcript_status="done", transcript_error="")
    print(f"  TRANSCRIPT call {dispo_id}: {len(text)} chars")


def _worker():
    while True:
        dispo_id = _q.get()
        for attempt in range(3):
            try:
                _process(dispo_id)
                break
            except Exception as e:                # network, Deepgram, Twilio: retry, then give up
                if attempt == 2:
                    _set(dispo_id, transcript_status="failed", transcript_error=str(e)[:300])
                    print(f"  TRANSCRIPT call {dispo_id} failed: {e}")
                else:
                    time.sleep(20 * (attempt + 1))
