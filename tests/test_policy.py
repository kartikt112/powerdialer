"""Calling windows, retry cadence, voicemail policy, caller-ID picker.  python -m unittest discover tests"""
import os
import sys
import unittest
from datetime import date, datetime, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "dialer"))
import policy  # noqa: E402

MON, TUE, WED, THU, FRI, SAT, SUN = (datetime(2026, 9, 21 + i) for i in range(7))


def at(day, hhmm):
    h, m = hhmm.split(":")
    return day.replace(hour=int(h), minute=int(m))


class Windows(unittest.TestCase):
    def test_tiers_through_the_day(self):
        want = {"07:59": None, "08:00": None, "08:15": "power", "10:14": "power", "10:15": "secondary",
                "11:29": "secondary", "11:30": None, "12:30": None, "13:29": None, "13:30": "secondary",
                "15:44": "secondary", "15:45": "power", "17:44": "power", "17:45": None, "18:00": None}
        for clock, tier in want.items():
            self.assertEqual(policy.tier(at(TUE, clock)), tier, clock)

    def test_never_weekends(self):
        for day in (SAT, SUN):
            self.assertIsNone(policy.tier(at(day, "09:00")))
            self.assertFalse(policy.in_hard(at(day, "09:00")))

    def test_hard_limits_cover_lunch_for_requested_callbacks(self):
        self.assertTrue(policy.in_hard(at(TUE, "12:15")))      # a callback they asked for
        self.assertFalse(policy.in_hard(at(TUE, "18:00")))
        self.assertFalse(policy.in_hard(at(TUE, "07:59")))

    def test_next_open_skips_lunch_and_the_weekend(self):
        self.assertEqual(policy.next_open(at(TUE, "11:40")), at(TUE, "13:30"))
        self.assertEqual(policy.next_open(at(FRI, "17:50")), at(FRI + timedelta(days=3), "08:15"))

    def test_zone_labels(self):
        self.assertEqual(policy.zone_label("America/Chicago", -5), "CT")     # CDT is -5, still Central
        self.assertEqual(policy.zone_label("America/Detroit", -4), "ET")
        self.assertEqual(policy.zone_label("", -8), "PT")


class RetryCadence(unittest.TestCase):
    def walk(self, first):
        """Follow a lead through every retry; returns the list of attempt datetimes."""
        attempts, when = [first], first
        for made in range(1, 7):
            nxt, want_half = policy.next_attempt(when, made)
            if nxt is None:
                break
            self.assertEqual(policy.half(nxt), want_half)
            attempts.append(nxt)
            when = nxt
        return attempts

    def test_no_same_day_redial(self):
        for start in (at(MON, "08:20"), at(WED, "16:00"), at(FRI, "09:00")):
            seq = self.walk(start)
            for a, b in zip(seq, seq[1:]):
                self.assertGreater(b.date(), a.date())

    def test_alternates_morning_and_afternoon(self):
        seq = self.walk(at(MON, "08:20"))
        self.assertEqual([policy.half(x) for x in seq], ["am", "pm", "am", "pm", "am", "pm"])
        seq = self.walk(at(MON, "16:30"))
        self.assertEqual([policy.half(x) for x in seq], ["pm", "am", "pm", "am", "pm", "am"])

    def test_never_the_same_weekday_twice_in_a_row(self):
        for start in (at(MON, "08:20"), at(TUE, "16:00"), at(THU, "09:30"), at(FRI, "14:00")):
            seq = self.walk(start)
            for a, b in zip(seq, seq[1:]):
                self.assertNotEqual(a.weekday(), b.weekday(), (a, b))

    def test_only_weekdays_and_inside_a_window(self):
        for start in (at(MON, "08:20"), at(THU, "16:00"), at(FRI, "09:00")):
            for when in self.walk(start)[1:]:
                self.assertLess(when.weekday(), 5)
                self.assertIsNotNone(policy.tier(when), when)

    def test_six_attempts_over_about_three_weeks_then_exhausted(self):
        seq = self.walk(at(MON, "08:20"))
        self.assertEqual(len(seq), 6)
        span = (seq[-1].date() - seq[0].date()).days
        self.assertTrue(18 <= span <= 25, span)
        self.assertEqual(policy.next_attempt(seq[-1], 6), (None, None))
        self.assertTrue(policy.exhausted(6))
        self.assertFalse(policy.exhausted(5))

    def test_voicemail_only_on_attempts_1_3_5(self):
        self.assertEqual([n for n in range(1, 8) if policy.voicemail_allowed(n)], [1, 3, 5])


POOL = [
    {"number": "+19375550001", "area_code": "937", "state": "OH"},
    {"number": "+16145550002", "area_code": "614", "state": "OH"},
    {"number": "+12145550003", "area_code": "214", "state": "TX"},
]
TODAY = date(2026, 9, 22)


class CallerId(unittest.TestCase):
    def pick(self, phone, state, used=None, parked=(), cursor=0, pool=POOL):
        return policy.pick_caller_id(phone, state, pool, used or {}, TODAY, parked, cursor)

    def test_area_code_match_wins(self):
        entry, why, _ = self.pick("+19372040101", "OH")
        self.assertEqual((entry["number"], why), ("+19375550001", "area code match"))

    def test_same_state_when_no_area_code_match(self):
        entry, why, _ = self.pick("+14402040101", "OH")           # 440 is Ohio, not in the pool
        self.assertEqual((entry["state"], why), ("OH", "same state"))
        entry, why, _ = self.pick("+19372040101", "OH", used={"+19375550001": 150})
        self.assertEqual((entry["number"], why), ("+16145550002", "same state"))   # 937 is capped

    def test_round_robin_otherwise(self):
        seen, cursor = [], 0
        for _ in range(6):
            entry, why, cursor = self.pick("+12532040101", "WA", cursor=cursor)
            self.assertEqual(why, "round robin")
            seen.append(entry["number"])
        self.assertEqual(seen, [e["number"] for e in POOL] * 2)

    def test_hard_stop_at_150(self):
        full = {e["number"]: 150 for e in POOL}
        entry, why, _ = self.pick("+19372040101", "OH", used=full)
        self.assertIsNone(entry)
        self.assertIn("daily dial cap", why)
        entry, _, _ = self.pick("+19372040101", "OH", used=dict(full, **{"+12145550003": 149}))
        self.assertEqual(entry["number"], "+12145550003")

    def test_warm_up_cap_is_30_a_day_for_21_days(self):
        fresh = dict(POOL[0], warmup_start=TODAY - timedelta(days=5))
        warm = dict(POOL[0], warmup_start=TODAY - timedelta(days=21))
        self.assertEqual(policy.daily_cap(fresh, TODAY), 30)
        self.assertEqual(policy.daily_cap(warm, TODAY), 150)
        self.assertEqual(policy.daily_cap(POOL[0], TODAY), 150)     # no start date: treated as warm
        entry, why, _ = policy.pick_caller_id("+19372040101", "OH", [fresh, POOL[1]], {"+19375550001": 30}, TODAY)
        self.assertEqual((entry["number"], why), ("+16145550002", "same state"))

    def test_parked_numbers_are_never_offered(self):
        entry, why, _ = self.pick("+19372040101", "OH", parked={"+19375550001"})
        self.assertEqual(entry["number"], "+16145550002")
        entry, why, _ = self.pick("+19372040101", "OH", parked={e["number"] for e in POOL})
        self.assertIsNone(entry)
        self.assertIn("parked", why)

    def test_auto_park_under_15_percent_over_100_dials(self):
        self.assertTrue(policy.should_park(100, 14))
        self.assertFalse(policy.should_park(100, 15))
        self.assertFalse(policy.should_park(99, 0))                  # not enough dials to judge
        self.assertTrue(policy.spam_suspect(50, 9))
        self.assertFalse(policy.spam_suspect(49, 0))

    def test_spoken_number_for_the_voicemail(self):
        self.assertEqual(policy.spoken("+19893751429"), "989, 375, 1429")


class Compliance(unittest.TestCase):
    def test_windows_can_never_reach_outside_tcpa_hours(self):
        ok = dict(policy.DEFAULT_WINDOWS)
        self.assertEqual(policy.check_windows(ok), ok)
        for bad in ({"hard": ["07:30", "18:00"]}, {"hard": ["08:00", "21:30"]},
                    {"power": [["07:45", "10:15"]]}, {"secondary": [["17:00", "19:00"]]}, {"hard": ["18:00", "08:00"]}):
            with self.assertRaises(ValueError):
                policy.check_windows(dict(ok, **bad))

    def test_scrub_file_takes_any_format(self):
        import tempfile
        with tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False) as fh:
            fh.write("phone,name\n(937) 204-0101,x\n1-814-204-0102,y\n+12532040103\n9375550199\nnot a number 12345\n")
        got = policy.load_number_file(fh.name)
        os.unlink(fh.name)
        self.assertEqual(got, {"+19372040101", "+18142040102", "+12532040103", "+19375550199"})


class Session(unittest.TestCase):
    def test_time_and_a_half_eta(self):
        self.assertEqual(policy.eta_seconds(60, 1800, 40), int(60 * 45 * 1.5))     # 45s a dial so far
        self.assertEqual(policy.eta_seconds(100, 0, 0), int(100 * 45 * 1.5))       # no history yet: default pace
        self.assertEqual(policy.eta_seconds(0, 1800, 40), 0)


if __name__ == "__main__":
    unittest.main()
