"""The shipped copy follows the house rules, and in-app script edits layer
over config.yaml without touching it."""
import os
import re
import shutil
import tempfile
import unittest

from _base import APP, CFG
import common

DASHES = ("\u2014", "\u2013")
USER_FACING = ("config.yaml", "README.md", "static/index.html", "static/m.html")


def app_files():
    for root, dirs, files in os.walk(APP):
        dirs[:] = [d for d in dirs if d not in ("data", "fonts", "__pycache__", "screenshots")]
        for name in files:
            if name.endswith((".py", ".js", ".html", ".css", ".yaml", ".md", ".csv")):
                yield os.path.join(root, name)


def strings(node):
    if isinstance(node, str):
        yield node
    elif isinstance(node, dict):
        for v in node.values():
            yield from strings(v)
    elif isinstance(node, list):
        for v in node:
            yield from strings(v)


class Copy(unittest.TestCase):
    def test_no_em_or_en_dashes_anywhere_in_the_app(self):
        found = []
        for path in app_files():
            with open(path, encoding="utf-8") as fh:
                for n, line in enumerate(fh, 1):
                    if any(d in line for d in DASHES):
                        found.append(f"{os.path.relpath(path, APP)}:{n}")
        self.assertEqual(found, [], "dashes found")

    def test_scripts_and_templates_make_no_unbacked_claims(self):
        text = " ".join(strings(CFG.get("scripts"))).lower()
        for claim in ("engineer", "encrypt", " nda", "iso 9001", "certified", "guarantee", "secure"):
            self.assertNotIn(claim, text, claim)

    def test_script_copy_from_the_brief(self):
        steps = CFG["scripts"]["tree"]["v1"]["steps"]
        self.assertIn("{dm_first} ji se baat ho sakti hai", " ".join(steps["reception_known"]["say_hi"]))
        self.assertIn("this is a sales call. Give me 30 seconds", steps["permission"]["say"][0])
        self.assertIn("fastest AI ballooning tool", steps["pitch"]["say"][0])
        self.assertIn("send me 5 of your drawings on WhatsApp", steps["ask"]["say"][0])
        self.assertEqual(len(steps["qualify"]["chips"]), 4)
        keys = {o["tag"] for o in CFG["scripts"]["objections"]}
        self.assertTrue({"PRICE_ASK", "CONFIDENTIAL_DRAWINGS", "ALREADY_HAVE_SOFTWARE", "DO_IT_MANUALLY_FINE",
                         "SEND_DETAILS_WHATSAPP", "LOW_VOLUME"} <= keys)
        tags = {t["key"] for t in CFG["objection_tags"]}
        self.assertTrue(keys <= tags, keys - tags)
        self.assertTrue(CFG["scripts"]["objection_rule"].startswith("Ignore the first"))

    def test_every_step_has_hinglish(self):
        for version, tree in CFG["scripts"]["tree"].items():
            for step_id, step in (tree.get("steps") or {}).items():
                self.assertTrue(step.get("say_hi"), f"{version}.{step_id}")

    def test_templates_subject_is_the_first_name(self):
        for kind in ("sample_request", "after_call_intro", "sample_delivered"):
            t = CFG["scripts"]["templates"][kind]
            self.assertEqual(t["subject"], "{dm_first}")
            self.assertTrue(t["whatsapp"] and t["email"])
        self.assertIn("accuracy", CFG["scripts"]["templates"]["sample_delivered"]["whatsapp"])
        self.assertTrue(re.search(r"PDF or DWG", CFG["scripts"]["templates"]["sample_request"]["whatsapp"]))

    def test_no_recording_anywhere(self):
        for path in app_files():
            if "/tests/" in path:
                continue
            with open(path, encoding="utf-8") as fh:
                low = fh.read().lower()
            self.assertNotIn("getusermedia", low, path)
            self.assertNotIn("mediarecorder", low, path)


class Edits(unittest.TestCase):
    def setUp(self):
        import serve
        self.serve = serve
        self.tmp = tempfile.mkdtemp()
        self._dir = common.DATA_DIR
        common.DATA_DIR = self.tmp
        serve.CFG = serve.load_cfg()

    def tearDown(self):
        common.DATA_DIR = self._dir
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_step_edit_overlays_and_resets(self):
        s = self.serve
        err, warn = s.save_script_edit({"op": "step", "version": "v1", "id": "pitch", "fields": {"say": ["New pitch for {company}."]}})
        self.assertIsNone(err)
        self.assertEqual(s.CFG["scripts"]["tree"]["v1"]["steps"]["pitch"]["say"], ["New pitch for {company}."])
        self.assertTrue(s.CFG["scripts"]["tree"]["v1"]["steps"]["pitch"]["say_hi"])            # untouched fields stay
        self.assertEqual(s.CFG["_edited"]["steps"]["v1"], ["pitch"])
        s.save_script_edit({"op": "reset_step", "version": "v1", "id": "pitch"})
        self.assertIn("fastest AI ballooning", s.CFG["scripts"]["tree"]["v1"]["steps"]["pitch"]["say"][0])

    def test_dashes_refused_and_claims_flagged(self):
        s = self.serve
        err, _ = s.save_script_edit({"op": "step", "version": "v1", "id": "ask", "fields": {"say": ["Free \u2014 really"]}})
        self.assertIn("dash", err)
        err, warn = s.save_script_edit({"op": "step", "version": "v1", "id": "ask", "fields": {"say": ["An engineer checks each one."]}})
        self.assertIsNone(err)
        self.assertTrue(warn)

    def test_versions_extend_and_delete(self):
        s = self.serve
        self.assertIsNone(s.save_script_edit({"op": "version", "name": "V2", "extends": "v1"})[0])
        s.save_script_edit({"op": "step", "version": "v2", "id": "pitch", "fields": {"say": ["Rejections first."]}})
        tree = s.CFG["scripts"]["tree"]
        self.assertEqual(tree["v2"]["steps"]["pitch"]["say"], ["Rejections first."])
        self.assertNotEqual(tree["v1"]["steps"]["pitch"]["say"], ["Rejections first."])
        self.assertEqual(tree["v2"]["steps"]["ask"], tree["v1"]["steps"]["ask"])
        self.assertIsNotNone(s.save_script_edit({"op": "delete_version", "name": "v1"})[0])
        self.assertIsNone(s.save_script_edit({"op": "delete_version", "name": "v2"})[0])
        self.assertNotIn("v2", s.CFG["scripts"]["tree"])

    def test_objection_and_template_edits(self):
        s = self.serve
        self.assertIsNone(s.save_script_edit({"op": "objection", "title": "We use a consultant", "anchor": "Makes sense.",
                                              "question": "How long do they take per drawing?"})[0])
        self.assertIn("we_use_a_consultant", [o["key"] for o in s.CFG["scripts"]["objections"]])
        self.assertIsNotNone(s.save_script_edit({"op": "template", "kind": "sample_request", "whatsapp": "x", "subject": "", "email": "y"})[0])
        self.assertIsNone(s.save_script_edit({"op": "template", "kind": "sample_request", "whatsapp": "Hi {dm_first}",
                                              "subject": "{dm_first}", "email": "Hi"})[0])
        self.assertEqual(s.CFG["scripts"]["templates"]["sample_request"]["whatsapp"], "Hi {dm_first}")
