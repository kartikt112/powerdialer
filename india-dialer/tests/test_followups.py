"""The Follow-ups rail: who is owed a nudge, and when a nudge clears it."""
from datetime import timedelta

from _base import CFG, DbCase
from test_calls import row
from test_intake import collect
import db


class Followups(DbCase):
    def setUp(self):
        super().setUp()
        db.import_leads(collect([row(1, "Alpha Gauges", "98200 11111"), row(2, "Beta Forge", "98200 22222"),
                                 row(3, "Gamma Tools", "98200 33333")])[0], "t.csv", CFG)
        self.now = db.now()

    def save(self, lead_id, outcome, when, **extra):
        lead = db.lead_payload(lead_id, CFG)
        p = {"lead_id": lead_id, "phone_id": lead["phones"][0]["id"], "outcome": outcome, "dialed_at": db.iso(when)}
        p.update(extra)
        return db.save_call(p, CFG, "pawan")

    def kinds(self, at):
        return sorted((x["company"], x["kind"], x["template"]) for x in db.followups_due(CFG, at))

    def test_sample_not_in_after_24_hours(self):
        self.save(1, "SAMPLE_REQ", self.now, whatsapp="98200 11111")
        self.assertEqual(self.kinds(self.now + timedelta(hours=23)), [])
        self.assertEqual(self.kinds(self.now + timedelta(hours=25)), [("Alpha Gauges", "sample_asked", "sample_request")])

    def test_delivered_without_feedback_after_48_hours(self):
        self.save(1, "SAMPLE_REQ", self.now, whatsapp="98200 11111")
        card = db.lead_payload(1, CFG)["samples"][0]["id"]
        db.sample_stage(card, "received", {})
        db.sample_stage(card, "delivered", {"turnaround_min": 20})
        later = db.now() + timedelta(hours=49)
        self.assertEqual(self.kinds(later), [("Alpha Gauges", "delivered", "sample_delivered")])
        db.sample_stage(card, "feedback", {"feedback": "good"})
        self.assertEqual(self.kinds(later), [])

    def test_interested_with_no_step_after_3_days(self):
        self.save(2, "INTERESTED_NO", self.now - timedelta(days=4))
        self.assertEqual(self.kinds(self.now), [("Beta Forge", "interested", "after_call_intro")])

    def test_a_nudge_clears_it(self):
        self.save(2, "INTERESTED_NO", self.now - timedelta(days=4))
        db.log_followup(2, "whatsapp", "after_call_intro", "919820022222", "pawan")
        self.assertEqual(self.kinds(self.now), [])
        with db.connect() as con:
            self.assertEqual(con.execute("SELECT channel, template FROM followups").fetchone()[:], ("whatsapp", "after_call_intro"))

    def test_exhausted_without_a_dm_goes_to_written_only(self):
        t = self.now - timedelta(days=20)
        for _ in range(5):
            self.save(3, "NO_ANSWER", t)
            t += timedelta(days=3)
        written = db.whatsapp_linkedin_list()
        self.assertEqual([x["company"] for x in written], ["Gamma Tools"])
        self.assertEqual(written[0]["mobile"], "+919820033333")
