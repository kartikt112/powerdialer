"""Queue behaviour against a real SQLite file with a frozen clock.  python -m unittest discover tests"""
import csv
import os
import shutil
import sys
import tempfile
import unittest
from datetime import datetime, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "dialer"))
import db  # noqa: E402
import policy  # noqa: E402

OUTCOMES = [
    dict(key="BOOKED", kind="final", connect=True, dm=True, pitched=True, resonated=True, offered=True, booked=True),
    dict(key="CALLBACK", kind="callback", connect=True, dm=True, pitched=True),
    dict(key="RESONATED_NO", kind="retry", connect=True, dm=True, pitched=True, resonated=True, offered=True),
    dict(key="PITCHED_NO", kind="final", connect=True, dm=True, pitched=True),
    dict(key="GATEKEEPER", kind="retry", connect=True),
    dict(key="VOICEMAIL", kind="retry"), dict(key="NO_ANSWER", kind="retry"),
    dict(key="BAD_NUMBER", kind="final"), dict(key="DNC", kind="dnc", connect=True),
]
POOL = [{"number": "+19375550001", "area_code": "937", "state": "OH", "warmup_start": "2026-01-01"},
        {"number": "+12535550002", "area_code": "253", "state": "WA", "warmup_start": "2026-01-01"}]

# Tuesday 22 Sep 2026. EDT is UTC-4, PDT is UTC-7.
def utc(day, hhmm):
    h, m = hhmm.split(":")
    return datetime(2026, 9, day, int(h), int(m))


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        db.DATA_DIR = self.tmp
        db.DB_PATH = os.path.join(self.tmp, "dialer.db")
        db.configure(OUTCOMES, "America/New_York", {})
        db.configure_policy(pool=POOL, enforce_windows=True)
        db._rr_cursor = 0
        db.init()
        self.clock = utc(22, "13:00")                      # 9:00am ET, 6:00am PT
        self._now = db.now
        db.now = lambda: self.clock

    def tearDown(self):
        db.now = self._now
        shutil.rmtree(self.tmp, ignore_errors=True)

    def lead(self, phone, company, tz, offset, rank=50, state="OH", **extra):
        cols = dict(phone=phone, company=company, tz_name=tz, tz_offset=offset, rank=rank, state=state,
                    created_at=db.iso(self.clock), **extra)
        with db.connect() as con:
            con.execute(f"INSERT INTO leads ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})", list(cols.values()))

    def save(self, lead, key, **extra):
        return db.disposition(lead["phone"], lead["company"], key, "", "pawan", 30,
                              extra=dict(number_used=lead.get("_caller_id", ""), **extra))

    def row(self, phone):
        with db.connect() as con:
            return dict(con.execute("SELECT * FROM leads WHERE phone=?", (phone,)).fetchone())


class Rotation(Base):
    def test_queue_follows_the_prospects_clock_east_to_west(self):
        self.lead("+19372040101", "Ohio Shop", "America/New_York", -4, rank=10)
        self.lead("+12532040102", "Washington Shop", "America/Los_Angeles", -7, rank=99, state="WA")
        status = db.window_status()                        # the cockpit's "now dialing" line
        self.assertEqual((status["tier"], status["zones"], status["eligible"]), ("power", [{"label": "ET", "eligible": 1}], 1))
        lead, _ = db.checkout("pawan")                     # 9am ET: only Ohio is awake, despite the lower rank
        self.assertEqual(lead["company"], "Ohio Shop")
        self.assertEqual((lead["_caller_id"], lead["_caller_reason"]), ("+19375550001", "area code match"))
        db.release(lead["phone"])

        self.clock = utc(22, "16:00")                      # noon ET (lunch), 9am PT
        lead, _ = db.checkout("pawan")
        self.assertEqual(lead["company"], "Washington Shop")
        self.assertEqual(lead["_caller_id"], "+12535550002")

    def test_power_windows_beat_secondary_windows(self):
        self.lead("+19372040101", "ET secondary", "America/New_York", -4, rank=99)      # 10:30am ET
        self.lead("+13122040103", "CT power", "America/Chicago", -5, rank=10, state="IL")  # 9:30am CT
        self.clock = utc(22, "14:30")
        self.assertEqual(db.checkout("pawan")[0]["company"], "CT power")
        self.assertEqual(db.window_status()["tier"], "secondary")    # CT lead is now checked out

    def test_nothing_is_dialed_cold_at_lunch_or_on_a_weekend(self):
        self.lead("+19372040101", "Ohio Shop", "America/New_York", -4)
        for clock in (utc(22, "16:00"), utc(26, "14:00")):                # noon Tue, 10am Sat
            self.clock = clock
            lead, reason = db.checkout("pawan")
            self.assertIsNone(lead)
            self.assertIn("calling window", reason)
        status = db.window_status()
        self.assertFalse(status["open"])
        self.assertEqual((status["next_zone"], status["next_tier"]), ("ET", "power"))

    def test_a_requested_callback_may_ring_at_lunch_but_not_after_six(self):
        self.lead("+19372040101", "Ohio Shop", "America/New_York", -4, callback_at="2026-09-22 15:00:00")
        self.clock = utc(22, "16:10")                                     # 12:10pm ET
        self.assertEqual(db.checkout("pawan")[0]["company"], "Ohio Shop")
        db.release("+19372040101")
        self.clock = utc(22, "22:30")                                     # 6:30pm ET
        self.assertIsNone(db.checkout("pawan")[0])


class Retry(Base):
    def test_no_answer_waits_for_another_day_and_the_other_half(self):
        self.lead("+19372040101", "Ohio Shop", "America/New_York", -4)
        lead, _ = db.checkout("pawan")                                    # Tue 9:00am ET
        self.save(lead, "NO_ANSWER")
        r = self.row(lead["phone"])
        self.assertEqual((r["attempts"], r["next_half"], r["status"]), (1, "pm", "NEW"))
        self.assertEqual(r["next_attempt_at"], "2026-09-24 17:30:00")     # Thu 1:30pm ET, in UTC

        self.clock = utc(22, "20:00")                                     # same day, 4pm ET: blocked
        self.assertIsNone(db.checkout("pawan")[0])
        _, reason = db.checkout_specific(lead["phone"], "pawan")
        self.assertIn("Already tried today", reason)
        self.clock = utc(25, "13:00")                                     # Fri 9am ET: due, but wrong half
        self.assertIsNone(db.checkout("pawan")[0])
        self.clock = utc(25, "20:00")                                     # Fri 4pm ET
        self.assertEqual(db.checkout("pawan")[0]["company"], "Ohio Shop")

    def test_six_tries_then_exhausted_and_exported_for_email(self):
        self.lead("+19372040101", "Ohio Shop", "America/New_York", -4, email="dale@example.com")
        for n in range(1, 7):
            r = self.row("+19372040101")
            if r["next_attempt_at"]:
                self.clock = datetime.fromisoformat(r["next_attempt_at"]) + timedelta(minutes=10)
            lead, reason = db.checkout("pawan")
            self.assertIsNotNone(lead, (n, reason))
            self.save(lead, "VOICEMAIL" if policy.voicemail_allowed(n) else "NO_ANSWER")
        r = self.row("+19372040101")
        self.assertEqual((r["status"], r["last_disposition"], r["attempts"], r["tags"]), ("EXHAUSTED", "EXHAUSTED", 6, "email_only"))
        with open(os.path.join(self.tmp, "out", "exhausted_for_email.csv")) as fh:
            rows = list(csv.DictReader(fh))
        self.assertEqual([(x["company"], x["email"]) for x in rows], [("Ohio Shop", "dale@example.com")])
        with db.connect() as con:                                          # the log keeps what really happened
            self.assertEqual(con.execute("SELECT disposition FROM dispositions ORDER BY id DESC LIMIT 1").fetchone()[0], "NO_ANSWER")
        self.assertIsNone(db.checkout("pawan")[0])


class Caps(Base):
    def test_hard_stop_when_every_caller_id_is_at_150(self):
        self.lead("+19372040101", "Ohio Shop", "America/New_York", -4)
        with db.connect() as con:
            for number in ("+19375550001", "+12535550002"):
                con.executemany("INSERT INTO dispositions (phone, disposition, at, number_used) VALUES (?,?,?,?)",
                                [(f"+1555{i:07d}", "NO_ANSWER", db.iso(self.clock), number) for i in range(150)])
        lead, reason = db.checkout("pawan")
        self.assertIsNone(lead)
        self.assertIn("daily dial cap", reason)

    def test_auto_park_after_a_bad_week(self):
        self.lead("+19372040101", "Ohio Shop", "America/New_York", -4)
        with db.connect() as con:
            con.executemany("INSERT INTO dispositions (phone, disposition, at, number_used, pickup) VALUES (?,?,?,?,?)",
                            [(f"+1555{i:07d}", "NO_ANSWER", db.iso(self.clock - timedelta(days=2)), "+19375550001", int(i < 10)) for i in range(120)])
        lead, _ = db.checkout("pawan")
        self.assertEqual(lead["_caller_id"], "+19375550001")
        self.save(lead, "NO_ANSWER")                                       # 10 pickups in 121 dials: parked
        health = {n["number"]: n for n in db.numbers_health()}
        self.assertTrue(health["+19375550001"]["parked"])
        self.assertIn("auto", health["+19375550001"]["park_reason"])
        self.lead("+19372040109", "Second Ohio Shop", "America/New_York", -4)
        self.assertEqual(db.checkout("pawan")[0]["_caller_id"], "+12535550002")   # falls through to the next number

    def test_mobiles_are_blocked_unless_allowed(self):
        self.lead("+19372040101", "Mobile Only Shop", "America/New_York", -4, is_mobile=1)
        self.assertIsNone(db.checkout("pawan")[0])
        self.assertIn("mobile", db.checkout_specific("+19372040101", "pawan")[1])
        db.configure_policy(pool=POOL, allow_mobile=True)
        self.assertIsNotNone(db.checkout("pawan")[0])


class NationalDnc(Base):
    def test_scrub_list_is_checked_again_at_dial_time(self):
        scrub = os.path.join(self.tmp, "national_dnc.txt")
        with open(scrub, "w") as fh:
            fh.write("937-204-0101\n")
        db.configure_policy(pool=POOL, scrub_file=scrub)
        self.lead("+19372040101", "Scrubbed Shop", "America/New_York", -4, rank=99)
        self.lead("+19372040102", "Clean Shop", "America/New_York", -4, rank=10)
        lead, _ = db.checkout("pawan")
        self.assertEqual(lead["company"], "Clean Shop")                    # the better-ranked lead was skipped
        self.assertEqual(self.row("+19372040101")["status"], "DNC")
        self.assertTrue(db.is_dnc("+19372040101"))
        self.assertIn("do-not-call", db.checkout_specific("+19372040101", "pawan")[1])    # now on the internal list too
        with open(scrub, "a") as fh:
            fh.write("(937) 204-0177\n")
        os.utime(scrub, (1, 2_000_000_000))
        self.assertIn("national do-not-call", db.checkout_specific("+19372040177", "pawan")[1])  # typed by hand

    def test_a_newer_scrub_file_is_picked_up_without_a_restart(self):
        scrub = os.path.join(self.tmp, "national_dnc.txt")
        open(scrub, "w").close()
        db.configure_policy(pool=POOL, scrub_file=scrub)
        self.assertEqual(db.scrub_numbers(), set())
        with open(scrub, "w") as fh:
            fh.write("9372040101\n")
        os.utime(scrub, (1, 2_000_000_000))
        self.assertEqual(db.scrub_numbers(), {"+19372040101"})


class Undo(Base):
    def test_undo_restores_the_lead_exactly_and_drops_the_call(self):
        self.lead("+19372040101", "Ohio Shop", "America/New_York", -4, dm_name="Dale Harlan")
        lead, _ = db.checkout("pawan")
        did = self.save(lead, "RESONATED_NO", dm_name="Mike Durant", email="mike@example.com", pain="three days a package",
                        objections=["SEND_EMAIL"], offered=True)
        r = self.row(lead["phone"])
        self.assertEqual((r["dm_name"], r["email"], r["pain"], r["attempts"]), ("Mike Durant", "mike@example.com", "three days a package", 1))
        restored, notes, reason = db.undo(did, "pawan")
        self.assertIsNone(reason)
        r = self.row(lead["phone"])
        self.assertEqual((r["dm_name"], r["email"], r["pain"], r["attempts"], r["next_half"], r["status"]), ("Dale Harlan", "", "", 0, "", "OUT"))
        self.assertEqual(db.funnel_rows(), [])
        self.assertEqual(restored["_caller_id"], "+19375550001")


class Sessions(Base):
    def test_session_funnel_counts_only_its_own_calls(self):
        self.lead("+19372040101", "A", "America/New_York", -4, rank=90)
        self.lead("+19372040102", "B", "America/New_York", -4, rank=80)
        a, _ = db.checkout("pawan"); self.save(a, "NO_ANSWER")
        sid = db.session_start("pawan", "v2", 100, 90)
        b, _ = db.checkout("pawan"); self.save(b, "PITCHED_NO", session_id=sid, script_version="v2", objections=["BUSY"])
        out = db.session_end(sid, 600)
        self.assertEqual((out["funnel"]["dials"], out["funnel"]["pitched"]), (1, 1))
        self.assertEqual(out["objections"][0]["key"], "BUSY")
        self.assertEqual(out["session"]["script_version"], "v2")
        by = {g["script_version"]: g["dials"] for g in db.stats("pawan", "all")["by_script"]}
        self.assertEqual(by, {"unversioned": 1, "v2": 1})


if __name__ == "__main__":
    unittest.main()


class DialAnyway(Base):
    """The agent can choose to call a lead again: the same-day rule and the
    caller ID cap give way, do-not-call and calling hours never do."""

    def test_same_day_redial_when_the_agent_insists(self):
        self.lead("+19372040101", "Ohio Shop", "America/New_York", -4)
        lead, _ = db.checkout("pawan")
        self.save(lead, "NO_ANSWER")
        self.clock = utc(22, "15:00")
        _, reason = db.checkout_specific(lead["phone"], "pawan")
        self.assertIn("Already tried today", reason)
        again, reason = db.checkout_specific(lead["phone"], "pawan", force=True)
        self.assertIsNone(reason)
        self.assertEqual(again["company"], "Ohio Shop")

    def test_over_the_cap_when_the_agent_insists(self):
        self.lead("+19372040101", "Ohio Shop", "America/New_York", -4)
        with db.connect() as con:
            for number in ("+19375550001", "+12535550002"):
                con.executemany("INSERT INTO dispositions (phone, disposition, at, number_used) VALUES (?,?,?,?)",
                                [(f"+1555{i:07d}", "NO_ANSWER", db.iso(self.clock), number) for i in range(150)])
        _, reason = db.checkout_specific("+19372040101", "pawan")
        self.assertIn("daily dial cap", reason)
        self.assertTrue(any(k in reason for k in db.OVERRIDABLE))
        lead, reason = db.checkout_specific("+19372040101", "pawan", force=True)
        self.assertIsNone(reason)
        self.assertEqual(lead["_caller_id"], "+19375550001")          # Ohio area code still wins
        self.assertIsNone(db.checkout("pawan")[0])                    # the automatic queue stays closed

    def test_never_past_do_not_call_or_calling_hours(self):
        self.lead("+19372040101", "Ohio Shop", "America/New_York", -4)
        with db.connect() as con:
            con.execute("INSERT INTO dnc VALUES ('+19372040101','asked','x','pawan')")
        _, reason = db.checkout_specific("+19372040101", "pawan", force=True)
        self.assertIn("do-not-call", reason)
        self.assertFalse(any(k in reason for k in db.OVERRIDABLE))
        self.lead("+19372040102", "Late Shop", "America/New_York", -4)
        self.clock = utc(23, "02:00")                                 # 10pm ET
        _, reason = db.checkout_specific("+19372040102", "pawan", force=True)
        self.assertIn("Outside calling hours", reason)
