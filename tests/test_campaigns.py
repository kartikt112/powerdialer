"""Campaigns, deleting leads, the daily target and analytics.  python -m unittest discover tests"""
import csv
import os
import sys
import tempfile
import unittest
from datetime import timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "dialer"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import db  # noqa: E402
import funnel  # noqa: E402
from test_db import Base, utc  # noqa: E402

HEAD = ["phone_e164", "company", "state", "timezone", "gmt_offset_now", "rank"]


def write_list(rows):
    fh = tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False, newline="")
    w = csv.writer(fh)
    w.writerow(HEAD)
    w.writerows(rows)
    fh.close()
    return fh.name


OHIO = "America/New_York"


class Campaigns(Base):
    def tearDown(self):
        db.use_campaign(None)
        super().tearDown()

    def load(self, rows, name):
        cid = db.create_campaign(name)
        path = write_list(rows)
        try:
            db.import_list_csv(path, cid, name + ".csv")
        finally:
            os.unlink(path)
        return cid

    def test_each_campaign_calls_only_its_own_leads(self):
        a = self.load([["+19372040101", "Alpha Stamping", "OH", OHIO, -4, 90]], "October")
        b = self.load([["+19372040102", "Beta Forge", "OH", OHIO, -4, 10]], "November")
        db.use_campaign(b)
        lead, _ = db.checkout("pawan")
        self.assertEqual(lead["company"], "Beta Forge")         # the higher-ranked Alpha is in another campaign
        db.use_campaign(None)
        self.assertEqual({l["company"] for l in db.lead_list()}, {"Alpha Stamping"})   # Beta is open, Alpha queued
        db.use_campaign(a)
        self.assertEqual([l["company"] for l in db.lead_list()], ["Alpha Stamping"])
        lead, _ = db.checkout("pawan")                          # Beta goes back, Alpha comes out
        self.assertEqual(lead["company"], "Alpha Stamping")
        self.assertEqual(self.row("+19372040102")["status"], "NEW")

    def test_calls_and_stats_split_by_campaign(self):
        a = self.load([["+19372040101", "Alpha", "OH", OHIO, -4, 90]], "A")
        b = self.load([["+19372040102", "Beta", "OH", OHIO, -4, 10]], "B")
        db.use_campaign(a)
        self.save({"phone": "+19372040101", "company": "Alpha"}, "BOOKED", booked_for=db.iso(self.clock + timedelta(days=1)))
        db.use_campaign(b)
        self.save({"phone": "+19372040102", "company": "Beta"}, "NO_ANSWER")
        self.assertEqual(db.stats()["funnel"]["dials"], 1)
        self.assertEqual(db.stats()["funnel"]["booked"], 0)
        db.use_campaign(a)
        self.assertEqual(db.stats()["funnel"]["booked"], 1)
        self.assertEqual(len(db.bookings()), 1)
        db.use_campaign(None)
        self.assertEqual(db.stats()["funnel"]["dials"], 2)
        listed = {c["name"]: c for c in db.campaigns_list()}
        self.assertEqual((listed["A"]["dials_today"], listed["A"]["booked"], listed["B"]["dials_today"]), (1, 1, 1))

    def test_a_number_in_two_campaigns_is_called_once(self):
        a = self.load([["+19372040101", "Alpha", "OH", OHIO, -4, 90]], "A")
        b = self.load([["+19372040101", "Alpha", "OH", OHIO, -4, 90]], "B")
        db.use_campaign(a)
        self.save({"phone": "+19372040101", "company": "Alpha"}, "NO_ANSWER")
        db.use_campaign(b)
        lead, reason = db.checkout("pawan")
        self.assertIsNone(lead)                                 # same-day redial blocked whichever campaign asks
        self.assertTrue(reason)

    def test_first_boot_turns_each_old_list_into_a_campaign(self):
        with db.connect() as con:
            con.execute("DELETE FROM campaigns")
            con.execute("DELETE FROM campaign_leads")
            con.execute("INSERT INTO leads (phone, company, source_file, created_at) VALUES ('+19372040111','Old','ppap_list_2026-09-21.csv','2026-09-21')")
            con.execute("INSERT INTO dispositions (phone, disposition, at) VALUES ('+19372040111','NO_ANSWER','2026-09-21 15:00:00')")
        db.init()
        listed = db.campaigns_list()
        self.assertEqual([c["name"] for c in listed], ["Ppap list 2026 09 21"])
        with db.connect() as con:
            self.assertEqual(con.execute("SELECT campaign_id FROM dispositions").fetchone()[0], listed[0]["id"])

    def test_delete_restore_and_reimport(self):
        a = self.load([["+19372040101", "Alpha", "OH", OHIO, -4, 90], ["+19372040102", "Beta", "OH", OHIO, -4, 10]], "A")
        db.use_campaign(a)
        self.assertEqual(db.delete_leads(["+19372040101"]), 1)
        self.assertEqual([l["company"] for l in db.lead_list()], ["Beta"])
        self.assertEqual(db.lead_list("Alpha"), [])                   # search doesn't find it either
        path = write_list([["+19372040101", "Alpha", "OH", OHIO, -4, 90]])
        try:
            db.import_list_csv(path, a)
        finally:
            os.unlink(path)
        self.assertEqual(self.row("+19372040101")["status"], "DELETED")   # a re-upload doesn't bring it back
        db.restore_leads(["+19372040101"])
        self.assertEqual(self.row("+19372040101")["status"], "NEW")

    def test_remove_from_one_campaign_keeps_it_in_the_other(self):
        a = self.load([["+19372040101", "Alpha", "OH", OHIO, -4, 90]], "A")
        b = self.load([["+19372040101", "Alpha", "OH", OHIO, -4, 90]], "B")
        db.remove_from_campaign(["+19372040101"], a)
        db.use_campaign(a)
        self.assertEqual(db.lead_list(), [])
        db.use_campaign(b)
        self.assertEqual(len(db.lead_list()), 1)
        db.remove_from_campaign(["+19372040101"], b)              # in no campaign now: deleted
        self.assertEqual(self.row("+19372040101")["status"], "DELETED")

    def test_delete_campaign_keeps_shared_leads_and_history(self):
        a = self.load([["+19372040101", "Alpha", "OH", OHIO, -4, 90], ["+19372040102", "Shared", "OH", OHIO, -4, 10]], "A")
        self.load([["+19372040102", "Shared", "OH", OHIO, -4, 10]], "B")
        db.use_campaign(a)
        self.save({"phone": "+19372040101", "company": "Alpha"}, "NO_ANSWER")
        db.use_campaign(None)
        self.assertEqual(db.delete_campaign(a), 1)
        self.assertEqual(self.row("+19372040101")["status"], "DELETED")
        self.assertEqual(self.row("+19372040102")["status"], "NEW")
        self.assertEqual(db.stats()["funnel"]["dials"], 1)        # the call still counts
        self.assertEqual([c["name"] for c in db.campaigns_list()], ["B"])

    def test_target_pace_and_estimate(self):
        a = self.load([[f"+1937204{i:04d}", f"Shop {i}", "OH", OHIO, -4, 50] for i in range(200)], "A")
        db.update_campaign(a, {"daily_target": 40})
        db.use_campaign(a)
        start = self.clock
        for i in range(10):                                   # 10 dials over the first hour
            self.clock = start + timedelta(minutes=6 * i)
            self.save({"phone": f"+1937204{i:04d}", "company": f"Shop {i}"}, "NO_ANSWER")
        self.clock = start + timedelta(hours=1)
        t = db.target_status()
        self.assertEqual((t["target"], t["done"], t["left"]), (40, 10, 30))
        self.assertAlmostEqual(t["per_hour"], 10.0, places=0)
        self.assertEqual(t["verdict"], "on_track")             # 3 more hours lands well inside the ET day
        self.assertEqual(t["untouched"], 190)
        self.assertEqual(t["days_to_first_touch"], 5)

    def test_analytics_series_by_day_week_month(self):
        a = self.load([["+19372040101", "Alpha", "OH", OHIO, -4, 90], ["+19372040102", "Beta", "OH", OHIO, -4, 10]], "A")
        db.use_campaign(a)
        self.save({"phone": "+19372040101", "company": "Alpha"}, "NO_ANSWER")
        self.clock = utc(23, "14:00")
        self.save({"phone": "+19372040102", "company": "Beta"}, "BOOKED", booked_for=db.iso(self.clock + timedelta(days=2)))
        days = db.analytics("day", 7)
        self.assertEqual(len(days["series"]), 7)
        self.assertEqual([s["dials"] for s in days["series"]][-2:], [1, 1])
        self.assertEqual(days["series"][-1]["booked"], 1)
        weeks = db.analytics("week", 4)
        self.assertEqual(weeks["series"][-1]["dials"], 2)
        self.assertEqual(weeks["series"][-1]["start"], "2026-09-21")      # Monday
        months = db.analytics("month", 3)
        self.assertEqual([m["start"] for m in months["series"]], ["2026-07-01", "2026-08-01", "2026-09-01"])

    def test_recording_ids_are_stored_with_the_call(self):
        self.lead("+19372040101", "Alpha", OHIO, -4)
        self.save({"phone": "+19372040101", "company": "Alpha"}, "PITCHED_NO",
                  call_sid="CA" + "a" * 32, recording_sid="RE" + "b" * 32)
        self.assertEqual(db.history("+19372040101")[0]["recording_sid"], "RE" + "b" * 32)
        self.assertEqual(db.calls_today()[0]["recording_sid"], "RE" + "b" * 32)


class MonthRange(unittest.TestCase):
    def test_month_starts_on_the_first_in_the_stats_zone(self):
        start = funnel.range_start("month", utc(25, "03:00"), "America/New_York")   # still the 24th in New York
        self.assertEqual(start, utc(1, "04:00"))


if __name__ == "__main__":
    unittest.main()
