"""config.yaml lint: the script tree is complete, tokens are known, no em dashes.  python -m unittest discover tests"""
import json
import os
import re
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "dialer"))
import db  # noqa: E402
import serve  # noqa: E402

CFG = serve.load_dialer_config()
TOKENS = {"first", "last", "dm_first", "dm_name", "company", "title", "process", "oem", "agent", "city", "state",
          "email", "pain", "callback_number", "calendly", "booked_when"}


def strings(node):
    if isinstance(node, str):
        yield node
    elif isinstance(node, dict):
        for v in node.values():
            yield from strings(v)
    elif isinstance(node, list):
        for v in node:
            yield from strings(v)


class Scripts(unittest.TestCase):
    def test_every_ordered_step_exists_in_every_version(self):
        for version, tree in CFG["scripts"]["tree"].items():
            self.assertEqual(set(tree["order"]), {"call", "voicemail", "inbound"}, version)
            for flow, ids in tree["order"].items():
                for step_id in ids:
                    self.assertIn(step_id, tree["steps"], (version, flow, step_id))
                    self.assertTrue(tree["steps"][step_id].get("say"), (version, step_id))

    def test_branches_point_at_real_steps_and_outcomes(self):
        outcomes = {o["key"] for o in CFG["outcomes"]}
        tags = {t["key"] for t in CFG["objection_tags"]}
        for version, tree in CFG["scripts"]["tree"].items():
            for step_id, step in tree["steps"].items():
                for branch in step.get("branches") or []:
                    if "to" in branch:
                        self.assertIn(branch["to"], tree["steps"], (version, step_id))
                    if "outcome" in branch:
                        self.assertIn(branch["outcome"], outcomes)
                    if "tag" in branch:
                        self.assertIn(branch["tag"], tags)

    def test_variant_inherits_everything_it_does_not_override(self):
        v1, v2 = CFG["scripts"]["tree"]["v1"], CFG["scripts"]["tree"]["v2"]
        self.assertEqual(v2["order"], v1["order"])
        self.assertNotEqual(v2["steps"]["pitch"]["say"], v1["steps"]["pitch"]["say"])
        self.assertEqual(v2["steps"]["book"], v1["steps"]["book"])

    def test_only_known_tokens_and_balanced_conditionals(self):
        for text in strings(CFG["scripts"]):
            for token in re.findall(r"\{[?!/]?(\w+)\}", text):
                self.assertIn(token, TOKENS, text[:60])
            opens = re.findall(r"\{[?!](\w+)\}", text)
            closes = re.findall(r"\{/(\w+)\}", text)
            self.assertEqual(sorted(opens), sorted(closes), text[:60])

    def test_objections_have_the_three_part_shape(self):
        tags = {t["key"] for t in CFG["objection_tags"]}
        for o in CFG["scripts"]["objections"]:
            for part in ("key", "title", "anchor", "disrupt", "question"):
                self.assertTrue(o.get(part), (o.get("key"), part))
            if o.get("tag"):
                self.assertIn(o["tag"], tags)

    def test_the_book_step_has_three_mini_contracts_and_no_prices_anywhere(self):
        self.assertEqual(len(CFG["scripts"]["tree"]["v1"]["steps"]["book"]["contracts"]), 3)
        for text in strings(CFG["scripts"]):
            self.assertNotRegex(text, r"\$\s?\d", text[:60])

    def test_email_subject_is_their_first_name_only(self):
        for kind in ("booked_confirm", "no_book_intrigue"):
            self.assertEqual(CFG["scripts"]["emails"][kind]["subject"], "{dm_first}")
        self.assertIn("{calendly}", CFG["scripts"]["emails"]["no_book_intrigue"]["body"])


class NoEmDashes(unittest.TestCase):
    def test_user_facing_files(self):
        files = ["config.yaml", "dialer/index.html", "dialer/app.css", "dialer/serve.py", "dialer/db.py",
                 "dialer/policy.py", "dialer/funnel.py", "dialer/demo_leads.csv", "listprep.py"]
        files += [os.path.join("dialer/js", f) for f in os.listdir(os.path.join(ROOT, "dialer/js"))]
        for name in files:
            with open(os.path.join(ROOT, name), encoding="utf-8") as fh:
                self.assertNotIn("—", fh.read(), name)


class Outcomes(unittest.TestCase):
    def test_the_imperium_ten_in_key_order(self):
        self.assertEqual([o["key"] for o in CFG["outcomes"]],
                         ["BOOKED", "CALLBACK", "RESONATED_NO", "PITCHED_NO", "DM_NO_PITCH", "GATEKEEPER",
                          "VOICEMAIL", "NO_ANSWER", "BAD_NUMBER", "DNC"])
        connect = {o["key"] for o in CFG["outcomes"] if o.get("connect")}
        self.assertEqual(connect, {"BOOKED", "CALLBACK", "RESONATED_NO", "PITCHED_NO", "DM_NO_PITCH", "GATEKEEPER", "DNC"})
        self.assertEqual(CFG["agents"], [{"id": "pawan", "name": "Pawan"}])
        self.assertEqual(CFG["campaign"], "PPAP")


class Webhook(unittest.TestCase):
    def test_a_booking_posts_to_the_webhook_and_is_logged(self):
        got = []

        class Catch(BaseHTTPRequestHandler):
            def do_POST(self):
                got.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
                self.send_response(200); self.end_headers(); self.wfile.write(b"ok")

            def log_message(self, *a):
                pass

        server = HTTPServer(("127.0.0.1", 0), Catch)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        tmp = tempfile.mkdtemp()
        db.DATA_DIR, db.DB_PATH = tmp, os.path.join(tmp, "dialer.db")
        db.init()
        serve.DIALER.clear(); serve.DIALER.update(CFG)
        serve.DIALER["booking_webhook_url"] = f"http://127.0.0.1:{server.server_port}/hook"
        try:
            lead = {"phone": "+19375550101", "company": "Harlan Precision Machining", "tz_name": "America/New_York", "process": "machining"}
            self.assertEqual(serve.fire_booking_webhook(7, lead, "2026-09-28 18:00:00", "2026-09-28T14:00",
                                                        {"email": "dale@example.com", "pain": "three days a package", "dm_name": "Dale Harlan"}, "pawan"), "queued")
            for _ in range(50):
                if got:
                    break
                time.sleep(0.05)
            self.assertEqual((got[0]["event"], got[0]["email"], got[0]["booked_for_local"], got[0]["lead"]["company"]),
                             ("call_booked", "dale@example.com", "2026-09-28T14:00", "Harlan Precision Machining"))
            time.sleep(0.1)
            with db.connect() as con:
                self.assertEqual(con.execute("SELECT status FROM webhook_log").fetchone()[0], 200)
            serve.DIALER["booking_webhook_url"] = None
            self.assertEqual(serve.fire_booking_webhook(8, lead, "x", "y", {}, "pawan"), "off")
        finally:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    unittest.main()
