"""List-prep scoring and rejects for the PPAP campaign.  Run:  python -m unittest discover tests"""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import listprep  # noqa: E402

CFG = listprep.load_config(os.path.join(ROOT, "config.yaml"))


class Seniority(unittest.TestCase):
    def test_buckets(self):
        cases = {
            "Owner": "owner", "President & CEO": "owner", "Co-Founder": "owner",
            "Quality Manager": "quality", "Director of Quality": "quality", "VP Quality": "quality",
            "General Manager": "ops", "Plant Manager": "ops", "VP Operations": "ops",
            "VP of Engineering": "ops", "Buyer": "other", "": "other",
        }
        for title, want in cases.items():
            self.assertEqual(listprep.seniority_bucket(title), want, title)


class Scoring(unittest.TestCase):
    def score(self, **row):
        return listprep.score_lead(row, CFG, row.pop("_engaged", False))

    def test_best_fit_outranks_poor_fit(self):
        best = self.score(company="Acme Precision Machining", job_title="Owner", employees="48",
                          industry="automotive tier 2", li_status="accepted")
        poor = self.score(company="Acme Holdings", job_title="Buyer", employees="600")
        self.assertEqual(best, 15 + 15 + 10 + 8 + 20)
        self.assertEqual(poor, 0)

    def test_size_bands(self):
        want = {"": 4, "5": 2, "10": 10, "24": 10, "25": 15, "99": 15, "100": 12, "199": 12, "250": 5, "900": 0}
        for employees, points in want.items():
            self.assertEqual(self.score(company="X", employees=employees), points, employees)

    def test_keywords_are_whole_words(self):
        self.assertEqual(self.score(company="Prefab Homes"), 4)            # 'fab*' is a prefix rule, not a substring
        self.assertEqual(self.score(company="Midwest Fabricators"), 4 + 10)
        self.assertEqual(self.score(company="Tool & Die Works"), 4 + 10)

    def test_engaged_is_the_strongest_signal(self):
        self.assertEqual(self.score(company="X", _engaged=True), 4 + 40)


class Itar(unittest.TestCase):
    def test_reject_words(self):
        for company in ("Patriot Defense Machining", "ITAR Registered CNC", "DoD Parts Co", "Military Spec Fab"):
            self.assertTrue(listprep.is_itar({"company": company}, CFG), company)
        for company in ("Dodge City Machining", "Guitar Parts CNC"):
            self.assertFalse(listprep.is_itar({"company": company}, CFG), company)


class Process(unittest.TestCase):
    def test_inferred_from_name(self):
        self.assertEqual(listprep.infer_process({"company": "Keystone Stamping"}), "stamping")
        self.assertEqual(listprep.infer_process({"company": "Badger Foundry"}), "casting")
        self.assertEqual(listprep.infer_process({"company": "Lone Star CNC"}), "machining")
        self.assertEqual(listprep.infer_process({"company": "X", "process": "Forging, machining"}), "forging")
        self.assertEqual(listprep.infer_process({"company": "Acme Holdings"}), "")


class Locations(unittest.TestCase):
    def test_sales_navigator_location_strings(self):
        self.assertEqual(listprep.split_location("Dayton, Ohio, United States"), ("Dayton", "OH"))
        self.assertEqual(listprep.split_location("Erie, PA"), ("Erie", "PA"))
        self.assertEqual(listprep.split_location("Greater Chicago Area"), ("", ""))

    def test_timezone_prefers_area_code_then_state(self):
        zone, offset, how = listprep.lead_timezone("+19372040101", "OH")
        self.assertEqual((zone, how), ("America/New_York", "area_code"))
        self.assertIn(offset, (-4.0, -5.0))
        zone, _, how = listprep.lead_timezone("+18002040101", "TX")        # toll-free: no geography
        self.assertEqual((zone, how), ("America/Chicago", "area_code+state"))
        self.assertEqual(listprep.lead_timezone("+18002040101", "")[2], "unknown")


class EndToEnd(unittest.TestCase):
    """Both list shapes through process(), then into the dialer database."""

    def run_prep(self, header, rows):
        import csv, tempfile
        from types import SimpleNamespace
        self.tmp = tempfile.mkdtemp()
        src = os.path.join(self.tmp, "in.csv")
        with open(src, "w", newline="") as fh:
            w = csv.writer(fh); w.writerow(header); w.writerows(rows)
        cfg = dict(CFG, suppression=dict(CFG["suppression"], dnc_file=os.path.join(self.tmp, "none.csv"),
                                         called_log=os.path.join(self.tmp, "none2.csv")))
        return listprep.process(src, cfg, SimpleNamespace()), src

    def test_cold_call_list_shape(self):
        (direct, tollfree, rejected, total), _ = self.run_prep(
            ["company", "phone", "website", "city", "state", "process"],
            [["Harlan Precision Machining", "(937) 204-0101", "harlan.example", "Dayton", "Ohio", "CNC machining"],
             ["Patriot Defense Machining", "703-204-0103", "patriot.example", "Reston", "VA", "machining"],
             ["Acme Holdings", "800-204-0105", "acme.example", "Chicago", "IL", ""],
             ["Harlan Precision Machining", "(937) 204-0101", "harlan.example", "Dayton", "Ohio", "CNC machining"]])
        self.assertEqual([r["address3"] for r in direct], ["Harlan Precision Machining"])
        self.assertEqual((direct[0]["state"], direct[0]["timezone"], direct[0]["process"]), ("OH", "America/New_York", "cnc machining"))
        self.assertEqual([r["address3"] for r in tollfree], ["Acme Holdings"])
        self.assertEqual(sorted(r["reason"].split(":")[0] for r in rejected), ["duplicate_of", "itar"])

    def test_sales_navigator_shape_keeps_the_best_contact_per_company(self):
        (direct, _, rejected, _), _ = self.run_prep(
            ["First Name", "Last Name", "Title", "Company Name", "Company Website", "Person Linkedin Url", "Location", "Company Headcount", "Phone", "LI Status"],
            [["Tina", "Brooks", "Buyer", "Harlan Precision Machining", "https://www.harlan.example/about", "", "Dayton, Ohio, United States", "48", "937-204-0177", ""],
             ["Dale", "Harlan", "Owner", "Harlan Precision Machining", "harlan.example", "https://linkedin.com/in/x", "Dayton, Ohio, United States", "48", "937-204-0101", "accepted"],
             ["Sam", "Big", "President", "Mega Stamping", "mega.example", "", "Detroit, Michigan, United States", "1,200", "313-204-0120", ""]])
        self.assertEqual([(r["first_name"], r["title"], r["city"], r["state"], r["li_status"]) for r in direct],
                         [("Dale", "Owner", "Dayton", "OH", "accepted")])
        self.assertEqual(sorted(r["reason"].split(":")[0] for r in rejected), ["duplicate_company", "too_large"])

    def test_national_dnc_scrub_rejects_at_prep(self):
        listprep.NATIONAL_DNC.clear()
        listprep.NATIONAL_DNC.add("+19372040101")
        try:
            (direct, _, rejected, _), _ = self.run_prep(["company", "phone", "state"], [["Harlan Machining", "937-204-0101", "OH"]])
        finally:
            listprep.NATIONAL_DNC.clear()
        self.assertEqual((direct, [r["reason"] for r in rejected]), ([], ["national_dnc"]))

    def test_a_lead_with_only_a_mobile_is_flagged(self):
        (direct, _, _, _), _ = self.run_prep(["company", "mobile", "state"], [["Harlan Machining", "937-204-0101", "OH"]])
        self.assertEqual((direct[0]["is_mobile"], direct[0]["_e164"]), ("1", "+19372040101"))
        (direct, _, _, _), _ = self.run_prep(["company", "phone", "line_type"], [["Keystone Stamping", "814-204-0102", "mobile"]])
        self.assertEqual(direct[0]["is_mobile"], "1")

    def test_dialer_csv_round_trips_into_the_database(self):
        import tempfile
        sys.path.insert(0, os.path.join(ROOT, "dialer"))
        import db
        (direct, _, _, _), _ = self.run_prep(
            ["company", "phone", "website", "city", "state", "process", "employees", "oem", "email", "dm_name"],
            [["Keystone Stamping", "814-204-0102", "keystone.example", "Erie", "PA", "stamping", "85", "GM", "m@keystone.example", "Marie Kowalski"]])
        out = os.path.join(self.tmp, "ppap_list_test.csv")
        listprep.write_csv(listprep.dialer_rows(direct), out, listprep.DIALER_COLUMNS)
        db.DATA_DIR = self.tmp
        db.DB_PATH = os.path.join(self.tmp, "dialer.db")
        db.init()
        self.assertEqual(db.import_list_csv(out), (1, 0))
        lead = db.lookup("+18142040102")
        self.assertEqual((lead["company"], lead["process"], lead["oem"], lead["employees"], lead["dm_name"], lead["tz_name"], lead["list_id"]),
                         ("Keystone Stamping", "stamping", "GM", "85", "Marie Kowalski", "America/New_York", "101"))


if __name__ == "__main__":
    unittest.main()
