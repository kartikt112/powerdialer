"""The Imperium funnel: counts, rates, attribution, sheet.  python -m unittest discover tests"""
import json
import os
import sys
import unittest
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "dialer"))
import funnel  # noqa: E402

NOW = datetime(2026, 9, 25, 18, 0, 0)          # a Friday, 2pm Eastern
TZ = "America/New_York"


def call(key, at, phone="+15550000001", **kw):
    flags = {
        "BOOKED": dict(pickup=1, dm=1, pitched=1, resonated=1, offered=1, booked=1),
        "CALLBACK": dict(pickup=1, dm=1, pitched=1),
        "RESONATED_NO": dict(pickup=1, dm=1, pitched=1, resonated=1, offered=1),
        "PITCHED_NO": dict(pickup=1, dm=1, pitched=1),
        "DM_NO_PITCH": dict(pickup=1, dm=1),
        "GATEKEEPER": dict(pickup=1),
    }.get(key, {})
    return dict({"disposition": key, "at": at, "phone": phone, "duration": 60}, **flags, **kw)


class Flags(unittest.TestCase):
    def test_outcome_definitions_drive_flags(self):
        booked = dict(connect=True, dm=True, pitched=True, resonated=True, offered=True, booked=True)
        self.assertEqual(funnel.flags_for(booked),
                         dict(pickup=1, dm=1, pitched=1, resonated=1, offered=1, booked=1))

    def test_offered_tick_only_counts_on_a_pickup(self):
        self.assertEqual(funnel.flags_for(dict(connect=True, dm=True, pitched=True), True)["offered"], 1)
        self.assertEqual(funnel.flags_for(dict(connect=False), True)["offered"], 0)


class Summary(unittest.TestCase):
    def setUp(self):
        day = "2026-09-25 "
        self.rows = (
            [call("NO_ANSWER", day + "13:%02d:00" % i, phone="+1555100%04d" % i) for i in range(40)]
            + [call("GATEKEEPER", day + "14:%02d:00" % i, phone="+1555200%04d" % i) for i in range(4)]
            + [call("PITCHED_NO", day + "15:0%d:00" % i, phone="+1555300%04d" % i) for i in range(3)]
            + [call("RESONATED_NO", day + "15:20:00", phone="+15554000001"),
               call("RESONATED_NO", day + "15:25:00", phone="+15554000001"),     # same lead twice
               call("BOOKED", day + "15:40:00", phone="+15554000002", booked_for="2026-09-28 15:00:00")]
        )

    def test_counts_and_rates(self):
        c = funnel.summarise(self.rows, NOW)
        self.assertEqual((c["dials"], c["pickups"], c["pitched"], c["resonated"], c["offered"], c["booked"]),
                         (50, 10, 6, 3, 3, 1))
        r = c["rates"]
        self.assertAlmostEqual(r["pr"], 6 / 50)
        self.assertAlmostEqual(r["rr"], 3 / 6)
        self.assertAlmostEqual(r["abr"], 1 / 50)
        self.assertAlmostEqual(r["pickup"], 10 / 50)
        self.assertAlmostEqual(r["dm_reach"], 6 / 10)
        self.assertAlmostEqual(r["offer"], 3 / 3)
        self.assertIsNone(r["sur"])                 # the booking has not come due yet
        self.assertIsNone(r["scr"])

    def test_conversations_are_unique_leads(self):
        self.assertEqual(funnel.summarise(self.rows, NOW)["conversations"], 2)

    def test_skips_are_not_dials(self):
        self.assertEqual(funnel.summarise([call("SKIP", "2026-09-25 13:00:00")], NOW)["dials"], 0)

    def test_empty_is_safe(self):
        c = funnel.summarise([], NOW)
        self.assertEqual(c["dials"], 0)
        self.assertTrue(all(v is None for v in c["rates"].values()))


class FollowThrough(unittest.TestCase):
    def test_show_rate_ignores_future_and_rescheduled(self):
        rows = [
            call("BOOKED", "2026-09-21 14:00:00", booked_for="2026-09-22 15:00:00", show_status="SHOWED", sale=1, sale_amount=4500),
            call("BOOKED", "2026-09-21 15:00:00", booked_for="2026-09-23 15:00:00", show_status="NO_SHOW"),
            call("BOOKED", "2026-09-22 15:00:00", booked_for="2026-09-24 15:00:00"),                 # due, unmarked
            call("BOOKED", "2026-09-22 16:00:00", booked_for="2026-09-24 16:00:00", show_status="RESCHEDULED"),
            call("BOOKED", "2026-09-25 15:00:00", booked_for="2026-09-29 15:00:00"),                 # future
        ]
        c = funnel.summarise(rows, NOW)
        self.assertEqual((c["booked"], c["due_bookings"], c["showed"], c["no_shows"], c["sales"]), (5, 3, 1, 1, 1))
        self.assertAlmostEqual(c["rates"]["sur"], 1 / 3)
        self.assertAlmostEqual(c["rates"]["scr"], 1.0)
        self.assertEqual(c["sales_amount"], 4500)


class Sheet(unittest.TestCase):
    def test_columns_match_the_imperium_tracker_exactly(self):
        self.assertEqual(funnel.SHEET_COLUMNS, ["Date", "Calls", "DM's Pitched", "Resonations", "Call Booked",
                                                "Sales Calls Done", "Sales", "Sales $", "Notes"])

    def test_sale_is_credited_to_the_dial_date_not_the_meeting_date(self):
        rows = [
            call("NO_ANSWER", "2026-09-21 14:00:00"),
            call("BOOKED", "2026-09-21 15:00:00", booked_for="2026-09-24 15:00:00",
                 show_status="SHOWED", sale=1, sale_amount=4500,
                 objections=json.dumps(["SEND_EMAIL", "BUSY"])),
            call("NO_ANSWER", "2026-09-24 15:00:00"),          # the day the meeting actually happened
        ]
        sheet = funnel.daily_sheet(rows, TZ, {"SEND_EMAIL": "Send me an email", "BUSY": "Busy"}, NOW)
        self.assertEqual(sheet[0][:8], ["2026-09-21", 2, 1, 1, 1, 1, 1, 4500])
        self.assertEqual(sheet[1][:8], ["2026-09-24", 1, 0, 0, 0, 0, 0, 0])
        self.assertIn("Send me an email (1)", sheet[0][8])

    def test_day_is_the_prospects_day_not_utc(self):
        # 00:30 UTC on the 26th is still 8:30pm Eastern on the 25th.
        self.assertEqual(str(funnel.local_date("2026-09-26 00:30:00", TZ)), "2026-09-25")
        self.assertEqual(funnel.range_start("today", datetime(2026, 9, 26, 0, 30), TZ), datetime(2026, 9, 25, 4, 0))
        self.assertEqual(funnel.range_start("week", NOW, TZ), datetime(2026, 9, 21, 4, 0))   # Monday
        self.assertIsNone(funnel.range_start("all", NOW, TZ))


class Breakdown(unittest.TestCase):
    def test_per_script_version(self):
        rows = [call("BOOKED", "2026-09-25 14:00:00", script_version="v1", booked_for="2026-09-29 15:00:00"),
                call("NO_ANSWER", "2026-09-25 14:05:00", script_version="v1"),
                call("PITCHED_NO", "2026-09-25 14:10:00", script_version="v2"),
                call("NO_ANSWER", "2026-09-25 14:15:00", script_version="v2")]
        got = {g["script_version"]: g for g in funnel.by_script(rows, NOW)}
        self.assertAlmostEqual(got["v1"]["rates"]["abr"], 0.5)
        self.assertAlmostEqual(got["v2"]["rates"]["abr"], 0.0)
        self.assertAlmostEqual(got["v2"]["rates"]["pr"], 0.5)

    def test_top_objections(self):
        rows = [call("PITCHED_NO", "2026-09-25 14:00:00", objections=json.dumps(["BUSY", "SEND_EMAIL"])),
                call("PITCHED_NO", "2026-09-25 14:01:00", objections=json.dumps(["BUSY", "OTHER:his nephew does it"])),
                call("PITCHED_NO", "2026-09-25 14:02:00", objections=json.dumps(["BUSY"]))]
        top = funnel.top_objections(rows, {"BUSY": "Busy"})
        self.assertEqual((top[0]["label"], top[0]["count"]), ("Busy", 3))
        self.assertEqual(len(top), 2)


class Grades(unittest.TestCase):
    def test_grading_against_targets(self):
        self.assertEqual(funnel.grade(0.025, 0.02, 100), "good")
        self.assertEqual(funnel.grade(0.016, 0.02, 100), "warn")
        self.assertEqual(funnel.grade(0.01, 0.02, 100), "bad")
        self.assertEqual(funnel.grade(0.0, 0.02, 5), "none")       # too few dials to judge
        self.assertEqual(funnel.grade(None, 0.02, 100), "none")


if __name__ == "__main__":
    unittest.main()
