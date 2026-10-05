"""List intake, merging and scoring."""
import csv
import os
import tempfile
import unittest

from _base import CFG, DbCase
import db
import intake

HEADER = ["#", "tier", "company", "city", "state", "type", "phone", "phone_source", "ask_for", "why",
          "est_drawings_month", "website", "flags", "source"]


def write(rows, header=HEADER):
    fh = tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False, newline="")
    w = csv.writer(fh)
    w.writerow(header)
    w.writerows(rows)
    fh.close()
    return fh.name


def collect(rows, header=HEADER):
    path = write(rows, header)
    try:
        return intake.collect(list(intake.read_rows(path, CFG["import"]["aliases"])), CFG)
    finally:
        os.unlink(path)


class Fields(unittest.TestCase):
    def test_ask_for_names_and_roles(self):
        self.assertEqual(intake.parse_ask_for("Pankaj Tyagi (MD)"), ("Pankaj Tyagi", "MD"))
        self.assertEqual(intake.parse_ask_for("Mr. Satish M Kavale"), ("Satish M Kavale", ""))
        self.assertEqual(intake.parse_ask_for("Sathishkumar H ()"), ("Sathishkumar H", ""))
        for role in ("VP Quality / Quality Head", "NPD Head or Quality Head", "Lab Manager", "Founder/Director"):
            self.assertEqual(intake.parse_ask_for(role), ("", ""), role)

    def test_volume_ranges_use_the_low_end(self):
        self.assertEqual([intake.volume_low(v) for v in ("500+", "300-800", "1,200", "80+", "big")], [500, 300, 1200, 80, None])

    def test_states_and_locations(self):
        states = CFG["states"]
        self.assertEqual(intake.normalise_state("TN", states), "Tamil Nadu")
        self.assertEqual(intake.normalise_state("Haryana (NCR)", states), "Haryana")
        self.assertEqual(intake.split_location("Pune, Maharashtra, India", states), ("Pune", "Maharashtra"))

    def test_em_dashes_never_reach_the_cockpit(self):
        self.assertEqual(intake.clean("clients \u2014 confirm"), "clients, confirm")


class Rows(unittest.TestCase):
    def test_shifted_row_takes_its_segment_from_why(self):
        leads, _, _ = collect([["37", "B", "Rucha Engineers", "Pune", "MH", "Maharashtra", "+91 8045849995", "",
                                "NPD Head or Quality Head", "Tier-1 press-parts maker with its own CMM room", "800-2000", "", "", ""]])
        lead = leads[0]
        self.assertEqual((lead["state"], lead["type_raw"], lead["segment"], lead["est_volume"]),
                         ("Maharashtra", "", "Exporter / manufacturer", 800))

    def test_defence_check_holds_but_a_denial_does_not(self):
        leads, _, _ = collect([
            ["1", "B", "Aequs", "Belgaum", "KA", "aerospace", "+91 831 2499000", "", "", "", "100+", "", "DEFENSE/AERO CHECK before pitching", ""],
            ["2", "B", "Tsugami", "Chennai", "TN", "machine tools", "+91 44 1234 5678", "", "", "", "100+", "", "none - no ITAR/defence indication found", ""]])
        self.assertTrue(leads[0]["hold_reason"])
        self.assertFalse(leads[1]["hold_reason"])

    def test_duplicate_companies_merge(self):
        leads, merges, _ = collect([
            ["1", "B", "Menon Pistons Ltd", "Kolhapur", "MH", "auto", "0230-2468041", "", "Quality Head", "a", "500-1200", "", "", ""],
            ["2", "A", "Menon Pistons Limited", "", "", "auto", "+91 98220 11111", "", "", "b", "100+", "", "", ""]])
        self.assertEqual((len(leads), merges), (1, ["Menon Pistons Limited"]))
        self.assertEqual((leads[0]["tier"], [n["e164"] for n in leads[0]["numbers"]], leads[0]["est_volume"]),
                         ("A", ["+912302468041", "+919822011111"], 500))

    def test_indiamart_style_export_through_aliases(self):
        leads, _, _ = collect([["Keystone Forge", "Rajesh Kumar", "Pune", "Maharashtra", "9822012345", "020 2712 3456", "rk@keystone.example"]],
                              header=["Company Name", "Contact Person", "City", "State", "Mobile", "Phone", "Email"])
        lead = leads[0]
        self.assertEqual((lead["tier"], lead["dm_name"], lead["email"]), ("B", "Rajesh Kumar", "rk@keystone.example"))
        self.assertEqual([n["kind"] for n in lead["numbers"]], ["mobile", "landline"])


class Scoring(unittest.TestCase):
    def test_points(self):
        s = CFG["scoring"]
        base = {"tier": "A", "est_volume": 500, "dm_name": "Dale"}
        self.assertEqual(intake.raw_score(base, s, True, False), 30 + 25 + 5 + 10)
        self.assertEqual(intake.raw_score(dict(base, tier="C", est_volume=60, dm_name=""), s, False, False), 5 + 8)
        self.assertEqual(intake.raw_score(dict(base, tier="B", est_volume=250), s, False, True), 15 + 15 + 10 + 40)

    def test_percentile(self):
        self.assertEqual(intake.percentile_ranks({1: 10, 2: 20, 3: 20, 4: 70}), {1: 0, 2: 33, 3: 33, 4: 99})


class Import(DbCase):
    def test_import_is_idempotent_and_blocks_dnc(self):
        leads, _, _ = collect([
            ["1", "A", "Zetwerk", "Bengaluru", "KA", "marketplace", "+91 7624 9748 90", "", "VP Quality", "", "500+", "", "", ""],
            ["2", "B", "IndiaCADworks", "", "", "CAD drafting", "+1-855-259-3709", "", "", "", "100+", "", "", ""],
            ["3", "C", "No Number Co", "", "", "", "", "", "", "", "", "", "", ""]])
        with db.connect() as con:
            con.execute("INSERT INTO dnc (key) VALUES ('company:zetwerk')")
        report = db.import_leads(leads, "list.csv", CFG)
        self.assertEqual(len(report["added"]), 3)
        self.assertEqual(db.summary_counts()["status"], {"DNC": 1, "INTL": 1, "NO_PHONE": 1})
        again = db.import_leads(leads, "list.csv", CFG)
        self.assertEqual((len(again["added"]), again["numbers_added"]), (0, 0))


if __name__ == "__main__":
    unittest.main()
