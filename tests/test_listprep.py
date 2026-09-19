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


if __name__ == "__main__":
    unittest.main()
