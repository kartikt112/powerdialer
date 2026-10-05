"""Section 9 rules, each pinned by a test: permanent DNC, the 09:00 to 21:00
IST hard block, defence and aerospace holds, nothing sent automatically,
no recording."""
import os
import re
from datetime import datetime

from _base import APP, CFG, DbCase
from test_calls import row
from test_intake import collect
import db
import policy


def at_ist(s):
    return policy.to_utc(datetime.strptime(s, "%Y-%m-%d %H:%M"))


class Compliance(DbCase):
    def setUp(self):
        super().setUp()
        db.import_leads(collect([row(1, "Alpha Gauges", "98200 11111"),
                                 row(2, "Held Aero", "98200 22222", flags="DEFENSE/AERO CHECK: supplies HAL")])[0], "t.csv", CFG)

    def test_hard_block_outside_nine_to_nine_ist(self):
        for when in ("2026-10-05 08:59", "2026-10-05 21:00", "2026-10-05 23:30"):
            self.assertIn("Outside calling hours", db.lead_payload(1, CFG, at_ist(when))["dial_block"], when)
        self.assertEqual(db.lead_payload(1, CFG, at_ist("2026-10-05 09:00"))["dial_block"], "")
        # Not even a callback rings outside the hours.
        with db.connect() as con:
            con.execute("UPDATE leads SET next_action_type='callback', next_action_at=? WHERE id=1", (db.iso(at_ist("2026-10-05 20:00")),))
        self.assertEqual(db.queue(at_ist("2026-10-05 21:05"), CFG), [])

    def test_defence_hold_blocks_until_cleared(self):
        lead = db.lead_payload(2, CFG, at_ist("2026-10-05 11:00"))
        self.assertTrue(lead["held"])
        self.assertIn("defence or aerospace", lead["dial_block"])
        self.assertNotIn(2, [l["id"] for _g, _r, l in db.queue(at_ist("2026-10-05 11:00"), CFG)])
        db.clear_hold(2, "pawan")
        self.assertEqual(db.lead_payload(2, CFG, at_ist("2026-10-05 11:00"))["dial_block"], "")
        # A re-import does not put a cleared hold back.
        db.import_leads(collect([row(2, "Held Aero", "98200 22222", flags="DEFENSE/AERO CHECK: supplies HAL")])[0], "t.csv", CFG)
        self.assertFalse(db.lead_payload(2, CFG)["held"])

    def test_dnc_survives_everything(self):
        lead = db.lead_payload(1, CFG)
        db.save_call({"lead_id": 1, "phone_id": lead["phones"][0]["id"], "outcome": "DNC"}, CFG, "pawan")
        # Undo is the only way back, and only for a few minutes; after that it stays.
        with db.connect() as con:
            con.execute("UPDATE calls SET created_at='2020-01-01 00:00:00'")
        cid = db.calls_between()[0]["id"]
        self.assertIsNone(db.undo(cid, "pawan", CFG)[0])
        self.assertTrue(db.lead_payload(1, CFG)["is_dnc"])
        db.rescore(CFG)
        self.assertEqual(db.lead_payload(1, CFG)["status"], "DNC")


class Code(DbCase):
    def py_and_js(self):
        for root, dirs, files in os.walk(APP):
            dirs[:] = [d for d in dirs if d not in ("data", "tests", "__pycache__", "fonts")]
            for name in files:
                if name.endswith((".py", ".js", ".html")):
                    path = os.path.join(root, name)
                    with open(path, encoding="utf-8") as fh:
                        yield path, fh.read()

    def test_nothing_is_sent_by_the_app(self):
        for path, text in self.py_and_js():
            for pattern in (r"\bsmtplib\b", r"urllib\.request", r"requests\.post", r"api\.whatsapp", r"graph\.facebook",
                            r"twilio", r"telnyx", r"sendBeacon\([^)]*wa\.me"):
                self.assertIsNone(re.search(pattern, text, re.I), f"{pattern} in {os.path.relpath(path, APP)}")

    def test_whatsapp_and_mail_only_open_on_a_click(self):
        with open(os.path.join(APP, "static", "js", "follow.js"), encoding="utf-8") as fh:
            text = fh.read()
        # wa.me and mailto: are only built inside go(), which only the Open button calls.
        self.assertEqual(len(re.findall(r"https://wa\.me/", text)), 1)
        self.assertIn('$("fu-go").addEventListener("click", go)', text)
        self.assertEqual(len(re.findall(r"\bgo\(\)", text)), 0)

    def test_no_recording(self):
        for path, text in self.py_and_js():
            low = text.lower()
            for word in ("getusermedia", "mediarecorder", "recording_url", "record="):
                self.assertNotIn(word, low, os.path.relpath(path, APP))
