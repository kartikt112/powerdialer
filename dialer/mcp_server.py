#!/usr/bin/env python3
"""Dialer connector for Claude (MCP over stdio).

Gives a Claude chat the dialer's after-call side: new calls with their
transcripts, a lead's calls and timeline, the pipeline, follow-ups due, and
the writes Claude needs (stage, follow-up, contact details, a logged draft or
invite link, call processed). It never dials and never sends anything.

    claude mcp add dialer --scope user -- python3 /path/to/dialer/mcp_server.py

Settings (environment):
    DIALER_URL        default https://dialer-production-e961.up.railway.app
    DIALER_USER       default agent
    DIALER_PASSWORD   default: the contents of ~/.pxl-dialer-password
Standard library only, so any python3 runs it.
"""

import base64
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

URL = os.environ.get("DIALER_URL", "https://dialer-production-e961.up.railway.app").rstrip("/")
USER = os.environ.get("DIALER_USER", "agent")


def _password():
    if os.environ.get("DIALER_PASSWORD"):
        return os.environ["DIALER_PASSWORD"]
    try:
        with open(os.path.expanduser("~/.pxl-dialer-password")) as fh:
            return fh.read().strip()
    except OSError:
        return ""


def call(method, path, body=None, campaign=None):
    headers = {"Content-Type": "application/json"}
    pw = _password()
    if pw:
        headers["Authorization"] = "Basic " + base64.b64encode(f"{USER}:{pw}".encode()).decode()
    if campaign:
        headers["X-Campaign"] = str(campaign)
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(URL + path, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read() or b"{}") or {"error": f"HTTP {e.code}"}
        except ValueError:
            return {"error": f"HTTP {e.code}" + (": wrong or missing DIALER_PASSWORD" if e.code == 401 else "")}
    except urllib.error.URLError as e:
        return {"error": f"dialer unreachable: {e.reason}"}


STAGES = ["interested", "invite_sent", "signed_up", "using", "call_booked", "showed", "no_show", "sold", "lost"]
PHONE = {"type": "string", "description": "The lead's phone in E.164, e.g. +19372040101"}
CAMPAIGN = {"type": "integer", "description": "Optional campaign id to narrow to; omit for every campaign"}

TOOLS = [
    {"name": "list_new_calls",
     "description": "Calls that have a finished transcript and haven't been processed by Claude yet, oldest first. "
                    "Each comes with the lead, its recent calls and timeline. Also says how many are still transcribing.",
     "inputSchema": {"type": "object", "properties": {"limit": {"type": "integer", "default": 10}, "campaign": CAMPAIGN}}},
    {"name": "get_lead",
     "description": "One lead: contact details, pipeline stage, follow-up, its calls (outcome, notes, transcript, summary) and timeline.",
     "inputSchema": {"type": "object", "properties": {"phone": PHONE}, "required": ["phone"]}},
    {"name": "list_pipeline",
     "description": "Leads in the pipeline with counts per stage. stage: one of " + ", ".join(STAGES) +
                    ", or 'due' for follow-ups that are due; omit for all.",
     "inputSchema": {"type": "object", "properties": {"stage": {"type": "string"}, "campaign": CAMPAIGN}}},
    {"name": "list_followups_due",
     "description": "Leads whose follow-up date has passed, oldest first, each with recent calls and timeline.",
     "inputSchema": {"type": "object", "properties": {"campaign": CAMPAIGN}}},
    {"name": "set_stage",
     "description": "Move a lead to a pipeline stage (" + ", ".join(STAGES) + "), or '' to take it out. Logged on the timeline.",
     "inputSchema": {"type": "object", "properties": {"phone": PHONE, "stage": {"type": "string", "enum": STAGES + [""]},
                                                       "note": {"type": "string"}}, "required": ["phone", "stage"]}},
    {"name": "set_follow_up",
     "description": "Set when to follow up with a lead and why, or clear it with at=null. at is UTC 'YYYY-MM-DD HH:MM'.",
     "inputSchema": {"type": "object", "properties": {"phone": PHONE, "at": {"type": ["string", "null"]},
                                                       "note": {"type": "string"}}, "required": ["phone", "at"]}},
    {"name": "update_lead_details",
     "description": "Save contact details learned on a call. Allowed fields: email, dm_name, first, last, title, mobile, lead_notes.",
     "inputSchema": {"type": "object", "properties": {"phone": PHONE, "fields": {"type": "object"}}, "required": ["phone", "fields"]}},
    {"name": "log_event",
     "description": "Add a line to the lead's timeline. kind: 'draft' (an email draft was created; include the subject), "
                    "'invite' (a trial invite link was made; include the link), or 'note'.",
     "inputSchema": {"type": "object", "properties": {"phone": PHONE, "kind": {"type": "string", "enum": ["draft", "invite", "note"]},
                                                       "text": {"type": "string"}}, "required": ["phone", "kind", "text"]}},
    {"name": "mark_call_processed",
     "description": "Mark a call as handled, with a one or two sentence summary shown on the call in the dialer. "
                    "Do this last, after the stage, follow-up, details and any draft are done.",
     "inputSchema": {"type": "object", "properties": {"call_id": {"type": "integer"}, "summary": {"type": "string"}},
                     "required": ["call_id", "summary"]}},
    {"name": "list_campaigns",
     "description": "Campaigns with lead counts and today's dials.",
     "inputSchema": {"type": "object", "properties": {}}},
]


def run_tool(name, a):
    camp = a.get("campaign")
    if name == "list_new_calls":
        return call("GET", "/api/agent/inbox?limit=" + str(int(a.get("limit") or 10)), campaign=camp)
    if name == "get_lead":
        return call("GET", "/api/agent/lead?phone=" + urllib.parse.quote(a["phone"]))
    if name == "list_pipeline":
        return call("GET", "/api/agent/pipeline" + ("?stage=" + urllib.parse.quote(a["stage"]) if a.get("stage") else ""), campaign=camp)
    if name == "list_followups_due":
        return call("GET", "/api/agent/followups", campaign=camp)
    if name == "set_stage":
        return call("POST", "/api/agent/stage", {"phone": a["phone"], "stage": a.get("stage", ""), "note": a.get("note", "")})
    if name == "set_follow_up":
        return call("POST", "/api/agent/follow-up", {"phone": a["phone"], "at": a.get("at"), "note": a.get("note", "")})
    if name == "update_lead_details":
        return call("POST", "/api/agent/details", {"phone": a["phone"], "fields": a.get("fields") or {}})
    if name == "log_event":
        return call("POST", "/api/agent/event", {"phone": a["phone"], "kind": a.get("kind", "note"), "text": a.get("text", "")})
    if name == "mark_call_processed":
        return call("POST", "/api/agent/processed", {"id": a["call_id"], "summary": a.get("summary", "")})
    if name == "list_campaigns":
        return call("GET", "/api/campaigns")
    return {"error": f"unknown tool {name}"}


def reply(msg_id, result=None, error=None):
    out = {"jsonrpc": "2.0", "id": msg_id}
    if error is not None:
        out["error"] = error
    else:
        out["result"] = result
    sys.stdout.write(json.dumps(out) + "\n")
    sys.stdout.flush()


def handle(msg):
    method, msg_id = msg.get("method"), msg.get("id")
    if msg_id is None:                                   # a notification: nothing to answer
        return
    if method == "initialize":
        version = (msg.get("params") or {}).get("protocolVersion") or "2025-06-18"
        return reply(msg_id, {"protocolVersion": version, "capabilities": {"tools": {}},
                              "serverInfo": {"name": "dialer", "version": "1.0"},
                              "instructions": "PXL Kraft dialer: calls, transcripts, pipeline and follow-ups. "
                                              "It never dials or sends email; drafts go through Gmail."})
    if method == "ping":
        return reply(msg_id, {})
    if method == "tools/list":
        return reply(msg_id, {"tools": TOOLS})
    if method == "tools/call":
        params = msg.get("params") or {}
        result = run_tool(params.get("name"), params.get("arguments") or {})
        failed = isinstance(result, dict) and "error" in result and len(result) == 1
        return reply(msg_id, {"content": [{"type": "text", "text": json.dumps(result, indent=1, default=str)}],
                              "isError": failed})
    return reply(msg_id, error={"code": -32601, "message": f"method not found: {method}"})


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        try:
            handle(msg)
        except Exception as e:                           # never die mid-session
            if isinstance(msg, dict) and msg.get("id") is not None:
                reply(msg["id"], error={"code": -32603, "message": str(e)})


if __name__ == "__main__":
    main()
