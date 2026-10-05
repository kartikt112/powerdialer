"""IST windows, holidays, retries and queue priority."""
import unittest
from datetime import datetime, timedelta

from _base import CFG, DbCase
from test_calls import row
from test_intake import collect
import db
import policy

CAL = CFG["calling"]
RETRY = CFG["retry"]


def ist(s):
    return datetime.strptime(s, "%Y-%m-%d %H:%M")


class Windows(unittest.TestCase):
    # 2026-10-01 is a Thursday.
    def test_tiers_through_a_weekday(self):
        expect = {"08:59": "closed", "09:00": "off", "09:59": "off", "10:00": "power", "12:59": "power",
                  "13:00": "lunch", "14:29": "lunch", "14:30": "power", "17:29": "power", "17:30": "soft",
                  "18:29": "soft", "18:30": "off", "20:59": "off", "21:00": "closed", "23:30": "closed"}
        for hm, tier in expect.items():
            self.assertEqual(policy.tier(ist("2026-10-01 " + hm), CAL), tier, hm)

    def test_hard_limit_is_nine_to_nine(self):
        self.assertFalse(policy.legal(ist("2026-10-01 08:59"), CAL))
        self.assertTrue(policy.legal(ist("2026-10-01 09:00"), CAL))
        self.assertTrue(policy.legal(ist("2026-10-01 20:59"), CAL))
        self.assertFalse(policy.legal(ist("2026-10-01 21:00"), CAL))

    def test_sunday_is_closed_and_saturday_is_flagged(self):
        self.assertEqual(policy.tier(ist("2026-10-04 11:00"), CAL), "closed")          # Sunday
        self.assertEqual(policy.tier(ist("2026-10-03 11:00"), CAL), "power")           # Saturday
        kind, note = policy.day_kind(ist("2026-10-03 11:00"), CAL)
        self.assertEqual(kind, "saturday")
        self.assertIn("half day", note)

    def test_holidays_are_closed(self):
        for day, name in (("2026-10-02", "Gandhi Jayanti"), ("2026-10-20", "Dussehra"), ("2026-11-08", "Diwali"),
                          ("2026-11-24", "Guru Nanak Jayanti"), ("2026-12-25", "Christmas")):
            self.assertEqual(policy.tier(ist(day + " 11:00"), CAL), "closed", day)
            self.assertEqual(policy.day_kind(ist(day + " 11:00"), CAL), ("holiday", name))
        self.assertEqual(policy.tier(ist("2026-10-21 11:00"), CAL), "power")

    def test_next_open_skips_lunch_sunday_and_holidays(self):
        self.assertEqual(policy.next_cold_open(ist("2026-10-01 13:10"), CAL), ist("2026-10-01 14:30"))
        self.assertEqual(policy.next_cold_open(ist("2026-10-01 19:00"), CAL), ist("2026-10-03 10:00"))  # Fri is Gandhi Jayanti
        self.assertEqual(policy.next_cold_open(ist("2026-10-03 19:00"), CAL), ist("2026-10-05 10:00"))  # over Sunday
        status = policy.window_status(ist("2026-10-01 13:10"), CAL)
        self.assertEqual((status["tier"], status["next_open"]), ("lunch", "2:30 pm"))


class Retries(unittest.TestCase):
    def test_never_the_same_day_and_halves_alternate(self):
        when, half = policy.next_attempt(ist("2026-10-05 10:30"), 1, RETRY, CAL)       # Monday morning
        self.assertEqual((when, half), (ist("2026-10-06 14:30"), "pm"))
        when, half = policy.next_attempt(ist("2026-10-06 15:00"), 2, RETRY, CAL)
        self.assertEqual((when, half), (ist("2026-10-08 10:00"), "am"))

    def test_sundays_and_holidays_are_skipped(self):
        when, _ = policy.next_attempt(ist("2026-10-03 11:00"), 1, RETRY, CAL)          # Saturday -> Sunday -> Monday
        self.assertEqual(when.date(), ist("2026-10-05 00:00").date())
        when, _ = policy.next_attempt(ist("2026-10-01 11:00"), 1, RETRY, CAL)          # Thursday -> holiday -> Saturday
        self.assertEqual(when.date(), ist("2026-10-03 00:00").date())

    def test_five_attempts_fit_in_about_two_weeks(self):
        t = ist("2026-10-05 10:30")
        first = t
        tries = 1
        while True:
            nxt, _ = policy.next_attempt(t, tries, RETRY, CAL)
            if nxt is None:
                break
            self.assertNotEqual(nxt.date(), t.date())
            t, tries = nxt, tries + 1
        self.assertEqual(tries, 5)
        self.assertLessEqual((t - first).days, 15)


class Queue(DbCase):
    def setUp(self):
        super().setUp()
        rows = [row(1, "A New", "98200 00001", tier="A"), row(2, "B New", "98200 00002", tier="B"),
                row(3, "C New", "98200 00003", tier="C"), row(4, "Callback Co", "98200 00004", tier="C"),
                row(5, "Retry Co", "98200 00005", tier="B"), row(6, "Sample Co", "98200 00006", tier="C"),
                row(7, "Held Co", "98200 00007", tier="A", flags="DEFENSE/AERO CHECK")]
        db.import_leads(collect(rows)[0], "t.csv", CFG)
        with db.connect() as con:
            self.id = {r["company"]: r["id"] for r in con.execute("SELECT id, company FROM leads")}

    def at(self, s):
        return policy.to_utc(ist(s))

    def order(self, now_utc):
        return [lead["company"] for _g, _r, lead in db.queue(now_utc, CFG)]

    def test_priority_order(self):
        now = self.at("2026-10-05 15:00")                      # Monday, afternoon power window
        with db.connect() as con:
            con.execute("UPDATE leads SET next_action_type='callback', next_action_at=? WHERE id=?",
                        (db.iso(now - timedelta(minutes=5)), self.id["Callback Co"]))
            con.execute("UPDATE leads SET attempts=1, next_action_type='retry', next_half='pm', next_action_at=? WHERE id=?",
                        (db.iso(now - timedelta(hours=1)), self.id["Retry Co"]))
            con.execute("UPDATE leads SET status='PIPELINE' WHERE id=?", (self.id["Sample Co"],))
            con.execute("INSERT INTO samples (lead_id, stage, asked_at) VALUES (?, 'asked', ?)",
                        (self.id["Sample Co"], db.iso(now - timedelta(hours=30))))
        self.assertEqual(self.order(now), ["Callback Co", "Sample Co", "A New", "Retry Co", "B New", "C New"])

    def test_callbacks_ring_outside_cold_windows_but_not_outside_hours(self):
        lunch = self.at("2026-10-05 13:30")
        with db.connect() as con:
            con.execute("UPDATE leads SET next_action_type='callback', next_action_at=? WHERE id=?",
                        (db.iso(lunch - timedelta(minutes=1)), self.id["Callback Co"]))
        self.assertEqual(self.order(lunch), ["Callback Co"])
        self.assertEqual(self.order(self.at("2026-10-05 21:30")), [])
        sunday = self.at("2026-10-11 11:00")
        with db.connect() as con:
            con.execute("UPDATE leads SET next_action_at=? WHERE id=?", (db.iso(sunday - timedelta(minutes=1)), self.id["Callback Co"]))
        self.assertEqual(self.order(sunday), ["Callback Co"])                          # Sunday: callback only

    def test_a_callback_not_yet_due_waits(self):
        now = self.at("2026-10-05 11:00")
        with db.connect() as con:
            con.execute("UPDATE leads SET next_action_type='callback', next_action_at=? WHERE id=?",
                        (db.iso(now + timedelta(minutes=30)), self.id["Callback Co"]))
        self.assertNotIn("Callback Co", self.order(now))

    def test_retry_waits_for_its_half_of_the_day(self):
        with db.connect() as con:
            con.execute("UPDATE leads SET attempts=1, next_action_type='retry', next_half='pm', next_action_at=? WHERE id=?",
                        (db.iso(self.at("2026-10-05 10:00")), self.id["Retry Co"]))
        self.assertNotIn("Retry Co", self.order(self.at("2026-10-05 11:00")))
        self.assertIn("Retry Co", self.order(self.at("2026-10-05 14:45")))

    def test_held_leads_stay_out_until_cleared(self):
        now = self.at("2026-10-05 11:00")
        self.assertNotIn("Held Co", self.order(now))
        db.clear_hold(self.id["Held Co"], "pawan")
        self.assertIn("Held Co", self.order(now))

    def test_exhausted_after_five_without_a_dm(self):
        lead_id = self.id["B New"]
        t = self.at("2026-10-05 10:30")
        for i in range(5):
            lead = db.lead_payload(lead_id, CFG)
            db.save_call({"lead_id": lead_id, "phone_id": lead["phones"][0]["id"], "outcome": "NO_ANSWER",
                          "dialed_at": db.iso(t)}, CFG, "pawan")
            t += timedelta(days=2)
        lead = db.lead_payload(lead_id, CFG)
        self.assertEqual((lead["status"], lead["list_tag"], lead["attempts"]), ("EXHAUSTED", "whatsapp_linkedin", 5))

    def test_no_same_day_redial_on_the_same_number(self):
        lead_id = self.id["A New"]
        lead = db.lead_payload(lead_id, CFG)
        db.save_call({"lead_id": lead_id, "phone_id": lead["phones"][0]["id"], "outcome": "NO_ANSWER"}, CFG, "pawan")
        lead = db.lead_payload(lead_id, CFG)
        self.assertTrue(lead["phones"][0]["blocked"])
        self.assertTrue(lead["dial_block"])
        self.assertNotIn("A New", self.order(db.now()))


class NeverLooksEmpty(DbCase):
    """Outside the windows the queue is empty, but the list must still show."""

    def setUp(self):
        super().setUp()
        db.import_leads(collect([row(1, "A New", "98200 00001"), row(2, "B Held", "98200 00002", flags="DEFENSE/AERO CHECK")])[0],
                        "list.csv", CFG)

    def test_lunch_shows_every_lead_as_waiting(self):
        lunch = policy.to_utc(ist("2026-10-05 13:30"))
        self.assertEqual(db.queue(lunch, CFG), [])
        later, total = db.waiting(lunch, CFG)
        self.assertEqual(total, 2)
        self.assertEqual({l["company"]: l["wait"] for l in later},
                         {"A New": "when cold calls open", "B Held": "held for the defence check"})
        reason = db._why_empty(lunch, CFG)
        self.assertIn("Your 2 leads are all here", reason)
        self.assertIn("2:30 pm", reason)
        self.assertNotIn("Import", reason)

    def test_totals_by_list(self):
        totals = db.list_totals()
        self.assertEqual((totals["total"], totals["open"]), (2, 2))
        self.assertEqual(totals["lists"][0]["file"], "list.csv")

    def test_only_an_empty_database_asks_for_a_list(self):
        with db.connect() as con:
            con.execute("DELETE FROM phones")
            con.execute("DELETE FROM leads")
        self.assertIn("Import a list", db._why_empty(policy.to_utc(ist("2026-10-05 11:00")), CFG))
