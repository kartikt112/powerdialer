"""Metrics: attribution to the original dial date, rates, exports."""
import csv
import io
from datetime import timedelta

from _base import CFG, DbCase
from test_calls import row
from test_intake import collect
import db
import funnel
import policy


class Funnel(DbCase):
    def setUp(self):
        super().setUp()
        leads = collect([row(1, "Alpha Gauges", "98200 11111"), row(2, "Beta Forge", "98200 22222"),
                         row(3, "Gamma Tools", "020 2711 3333")])[0]
        db.import_leads(leads, "test.csv", CFG)
        self.now = db.now()
        self.three_days_ago = self.now - timedelta(days=3)

    def call(self, lead_id, outcome, when, **extra):
        lead = db.lead_payload(lead_id, CFG)
        p = {"lead_id": lead_id, "phone_id": lead["phones"][0]["id"], "outcome": outcome,
             "dialed_at": db.iso(when), "connected_at": db.iso(when + timedelta(seconds=20)),
             "ended_at": db.iso(when + timedelta(seconds=140)), "script_version": "v1"}
        p.update(extra)
        return db.save_call(p, CFG, "pawan")

    def test_wins_count_on_the_day_of_the_original_dial(self):
        self.call(1, "SAMPLE_REQ", self.three_days_ago, whatsapp="98200 11111", objections=["PRICE_ASK"])
        card = db.lead_payload(1, CFG)["samples"][0]["id"]
        for stage in ("received", "delivered", "feedback", "quote"):
            db.sample_stage(card, stage, {"feedback": "good", "turnaround_min": 30})
        db.sample_stage(card, "won", {"deal_value_inr": "100000", "drawings_committed": "20000"})
        self.call(2, "NO_ANSWER", self.now)

        today = funnel.stats(CFG, "today")["counts"]
        self.assertEqual((today["dials"], today["samples_asked"], today["won"], today["inr_won"]), (1, 0, 0, 0))
        everything = funnel.stats(CFG, "all")["counts"]
        self.assertEqual((everything["dials"], everything["samples_asked"], everything["samples_delivered"],
                          everything["won"], everything["inr_won"], everything["drawings_committed"]),
                         (2, 1, 1, 1, 100000, 20000))

        rows = funnel.tracker_rows(CFG, "all")
        day = policy.to_ist(self.three_days_ago).strftime("%Y-%m-%d")
        first = next(r for r in rows if r[0] == day)
        #          Date Calls Pitched Resonations Booked Delivered Sales Sales ₹
        self.assertEqual(first[:8], [day, 1, 1, 1, 1, 1, 1, 100000])
        self.assertIn("top objection: asked the price", first[8])
        last = rows[-1]
        self.assertEqual(last[1:8], [1, 0, 0, 0, 0, 0, 0])

    def test_tracker_csv_header_matches_the_imperium_sheet(self):
        self.call(1, "DEMO_BOOKED", self.now, demo_at=policy.to_ist(self.now + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M"))
        text = funnel.tracker_csv(CFG, "all")
        self.assertTrue(text.startswith("﻿"))
        rows = list(csv.reader(io.StringIO(text.lstrip("﻿"))))
        self.assertEqual(rows[0], ["Date", "Calls", "DM's Pitched", "Resonations", "Call Booked", "Sales Calls Done",
                                   "Sales", "Sales ₹", "Notes"])
        self.assertEqual(rows[1][4], "1")                         # a demo counts as Call Booked

    def test_calls_csv_has_every_call(self):
        self.call(1, "NO_ANSWER", self.now, continued=True)
        self.call(2, "GATEKEEPER", self.now)
        rows = list(csv.reader(io.StringIO(funnel.calls_csv(CFG, "all").lstrip("﻿"))))
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[1][11], "yes")                      # tried the next number
        self.assertEqual(rows[2][12], "Reception blocked or transferred nowhere")

    def test_rates_and_tones(self):
        for i in range(10):
            self.call(3, "NO_ANSWER" if i < 6 else "GATEKEEPER", self.now - timedelta(minutes=i))
        s = funnel.stats(CFG, "today")
        connect = next(r for r in s["rates"] if r["key"] == "connect_rate")
        self.assertEqual((connect["num"], connect["den"], connect["tone"]), (4, 10, "good"))
        sample = next(r for r in s["rates"] if r["key"] == "sample_rate")
        self.assertTrue(sample["star"])
        self.assertEqual(sample["tone"], "bad")
        dm = next(r for r in s["rates"] if r["key"] == "dm_reach")
        self.assertEqual(dm["tone"], "none")                      # only 4 connects: too few to judge

    def test_positive_conversations_are_unique_leads(self):
        when = self.now + timedelta(hours=2)
        cb = policy.to_ist(when).strftime("%Y-%m-%dT%H:%M")
        self.call(1, "CALLBACK", self.now, callback_at=cb)
        self.call(1, "INTERESTED_NO", self.now)
        self.call(2, "PITCHED_NO", self.now)
        self.assertEqual(funnel.stats(CFG, "today")["counts"]["positive"], 1)

    def test_breakdowns_and_best_hour(self):
        self.call(1, "GATEKEEPER", self.now)
        self.call(3, "NO_ANSWER", self.now)
        s = funnel.stats(CFG, "today")
        kinds = {r["k"]: r for r in s["breakdowns"]["number_kind"]["rows"]}
        self.assertEqual((kinds["mobile"]["connects"], kinds["landline"]["connects"]), (1, 0))
        self.assertEqual(s["best_hours"][0]["k"], policy.to_ist(self.now).hour)
        self.assertEqual(s["breakdowns"]["script_version"]["rows"][0]["k"], "v1")

    def test_week_starts_on_monday_ist(self):
        start, _ = funnel.range_bounds("week", self.now)
        self.assertEqual(policy.to_ist(start).weekday(), 0)
        self.assertEqual(policy.to_ist(start).hour, 0)
