"""Saving calls: outcome rules, the sample pipeline, DNC and undo."""
from datetime import timedelta

from _base import CFG, DbCase
from test_intake import collect
import db
import policy


def row(n, company, phone, tier="A", flags=""):
    return [n, tier, company, "Pune", "MH", "Precision machining", phone, "", "Rahul Shah (Quality Head)",
            "CMM reports", "200", "", flags, ""]


def ist_input(dt_utc):
    return policy.to_ist(dt_utc).strftime("%Y-%m-%dT%H:%M")


class Calls(DbCase):
    def setUp(self):
        super().setUp()
        leads = collect([row(1, "Alpha Gauges", "98200 11111 / 020 2711 2222"), row(2, "Beta Forge", "98200 33333")])[0]
        db.import_leads(leads, "test.csv", CFG)
        with db.connect() as con:
            self.ids = {r["company"]: r["id"] for r in con.execute("SELECT id, company FROM leads")}
        self.alpha = db.lead_payload(self.ids["Alpha Gauges"], CFG)

    def save(self, outcome, lead=None, **extra):
        lead = lead or self.alpha
        p = {"lead_id": lead["id"], "phone_id": lead["phones"][0]["id"], "outcome": outcome,
             "dialed_at": db.iso(db.now() - timedelta(minutes=3)), "connected_at": db.iso(db.now() - timedelta(minutes=2)),
             "ended_at": db.iso(db.now())}
        p.update(extra)
        return db.save_call(p, CFG, "pawan")

    def lead(self, company="Alpha Gauges"):
        return db.lead_payload(self.ids[company], CFG)

    # -- outcome rules -----------------------------------------------------

    def test_sample_needs_whatsapp_or_email(self):
        with self.assertRaises(db.SaveError):
            self.save("SAMPLE_REQ")
        with self.assertRaises(db.SaveError):
            self.save("SAMPLE_REQ", whatsapp="12345")
        cid = self.save("SAMPLE_REQ", whatsapp="98200 44444", objections=["PRICE_ASK"])
        lead = self.lead()
        self.assertEqual(lead["status"], "PIPELINE")
        self.assertEqual(lead["whatsapp"], "+919820044444")
        self.assertEqual(len(lead["samples"]), 1)
        self.assertEqual(lead["samples"][0]["stage"], "asked")
        self.assertEqual(lead["samples"][0]["origin_call_id"], cid)
        with db.connect() as con:
            c = con.execute("SELECT * FROM calls WHERE id=?", (cid,)).fetchone()
        self.assertEqual((c["pickup"], c["dm"], c["pitched"], c["interested"], c["sample_asked"]), (1, 1, 1, 1, 1))
        self.assertEqual(c["talk_seconds"], 120)

    def test_sample_accepts_an_email_instead(self):
        self.save("SAMPLE_REQ", email="rahul@alpha.in")
        self.assertEqual(self.lead()["email"], "rahul@alpha.in")

    def test_demo_needs_a_future_time(self):
        with self.assertRaises(db.SaveError):
            self.save("DEMO_BOOKED")
        with self.assertRaises(db.SaveError):
            self.save("DEMO_BOOKED", demo_at=ist_input(db.now() - timedelta(hours=1)))
        self.save("DEMO_BOOKED", demo_at=ist_input(db.now() + timedelta(days=1)))
        card = self.lead()["samples"][0]
        self.assertEqual((card["kind"], card["stage"]), ("demo", "demo"))

    def test_callback_sets_the_next_action(self):
        when = db.now() + timedelta(hours=3)
        self.save("CALLBACK", callback_at=ist_input(when))
        lead = self.lead()
        self.assertEqual(lead["next_action_type"], "callback")
        self.assertEqual(lead["next_action_at"][:16], db.iso(when)[:16])

    def test_try_next_number_stays_in_the_same_attempt(self):
        self.save("NO_ANSWER", continued=True)
        self.assertEqual(self.lead()["attempts"], 0)
        second = dict(self.alpha, phones=self.alpha["phones"][1:])
        self.save("NO_ANSWER", lead=second)
        lead = self.lead()
        self.assertEqual(lead["attempts"], 1)
        with db.connect() as con:
            attempts = [r["attempt_no"] for r in con.execute("SELECT attempt_no FROM calls ORDER BY id")]
        self.assertEqual(attempts, [1, 1])
        self.assertTrue(all(p["tried_today"] for p in lead["phones"]))

    def test_continue_only_after_no_answer(self):
        with self.assertRaises(db.SaveError):
            self.save("GATEKEEPER", continued=True)

    def test_bad_number_marks_the_number_and_keeps_the_other(self):
        self.save("BAD_NUMBER")
        lead = self.lead()
        self.assertTrue(lead["phones"][0]["bad"])
        self.assertEqual(lead["status"], "NEW")
        beta = self.lead("Beta Forge")
        self.save("BAD_NUMBER", lead=beta)
        self.assertEqual(self.lead("Beta Forge")["status"], "DONE")

    def test_dm_mobile_becomes_the_first_number(self):
        self.save("DM_NOT_IN", dm_name="Rahul Shah", dm_mobile="99200 55555")
        lead = self.lead()
        self.assertEqual(lead["phones"][0]["e164"], "+919920055555")
        self.assertEqual(lead["phones"][0]["origin"], "dm")

    # -- DNC ---------------------------------------------------------------

    def test_dnc_is_permanent_at_dial_and_import(self):
        self.save("DNC", notes="asked us not to call")
        lead = self.lead()
        self.assertEqual(lead["status"], "DNC")
        self.assertTrue(lead["dial_block"])
        self.assertTrue(all(p["blocked"] for p in lead["phones"]))
        # The same company again, even under a new number, stays blocked.
        leads = collect([row(1, "Alpha Gauges", "98200 99999")])[0]
        report = db.import_leads(leads, "again.csv", CFG)
        self.assertIn("Alpha Gauges", report["dnc"])
        self.assertEqual(self.lead()["status"], "DNC")
        # A different company that shares a blocked number is caught too.
        leads = collect([row(3, "Gamma Tools", "98200 11111")])[0]
        db.import_leads(leads, "again.csv", CFG)
        with db.connect() as con:
            self.assertEqual(con.execute("SELECT status FROM leads WHERE company='Gamma Tools'").fetchone()[0], "DNC")
        self.assertNotIn(self.ids["Alpha Gauges"], [l["id"] for _g, _r, l in db.queue(db.now(), CFG)])
        _id, error = db.add_phone(self.ids["Beta Forge"], "98200 11111")
        self.assertIn("do-not-call", error)
        _id, error = db.add_referral(None, {"company": "Alpha Gauges Pvt Ltd", "phone": "98200 77777"}, CFG, "pawan")
        self.assertTrue(error)

    # -- undo --------------------------------------------------------------

    def test_undo_puts_everything_back(self):
        cid = self.save("SAMPLE_REQ", whatsapp="98200 44444", dm_mobile="99200 55555")
        lead_id, _notes = db.undo(cid, "pawan", CFG)
        lead = self.lead()
        self.assertEqual(lead_id, lead["id"])
        self.assertEqual((lead["status"], lead["attempts"], lead["samples"]), ("NEW", 0, []))
        self.assertNotIn("+919920055555", [p["e164"] for p in lead["phones"]])
        cid = self.save("DNC")
        db.undo(cid, "pawan", CFG)
        self.assertFalse(self.lead()["is_dnc"])


class Pipeline(DbCase):
    def setUp(self):
        super().setUp()
        leads = collect([row(1, "Alpha Gauges", "98200 11111")])[0]
        db.import_leads(leads, "test.csv", CFG)
        lead = db.lead_payload(1, CFG)
        db.save_call({"lead_id": 1, "phone_id": lead["phones"][0]["id"], "outcome": "SAMPLE_REQ", "whatsapp": "98200 11111"}, CFG, "pawan")
        self.card = db.lead_payload(1, CFG)["samples"][0]["id"]

    def stage(self, stage, **f):
        return db.sample_stage(self.card, stage, f)

    def get(self):
        return db.lead_payload(1, CFG)["samples"][0]

    def test_full_run_to_won(self):
        self.assertIsNone(self.stage("received"))
        self.assertIsNone(self.stage("delivered", turnaround_min="42"))
        self.assertEqual(self.get()["turnaround_min"], 42)
        self.assertIsNotNone(self.stage("feedback"))                 # good or issues is required
        self.assertIsNone(self.stage("feedback", feedback="good"))
        self.assertIsNone(self.stage("quote"))
        self.assertIsNotNone(self.stage("won"))                      # a win needs a value
        self.assertIsNone(self.stage("won", deal_value_inr="1,00,000", drawings_committed="20000"))
        card = self.get()
        self.assertEqual((card["stage"], card["deal_value_inr"], card["drawings_committed"]), ("won", 100000, 20000))
        for k in ("asked_at", "received_at", "delivered_at", "feedback_at", "quote_sent_at", "won_at"):
            self.assertTrue(card[k], k)
        self.assertEqual(db.lead_payload(1, CFG)["status"], "DONE")

    def test_turnaround_is_worked_out_when_left_blank(self):
        self.stage("received")
        self.assertIsNone(self.stage("delivered"))
        self.assertEqual(self.get()["turnaround_min"], 0)

    def test_lost_closes_the_lead(self):
        self.assertIsNone(self.stage("lost", lost_reason="went quiet"))
        self.assertEqual(db.lead_payload(1, CFG)["status"], "DONE")
        self.assertEqual(self.get()["lost_reason"], "went quiet")

    def test_unknown_stage(self):
        self.assertIsNotNone(self.stage("shipped"))
