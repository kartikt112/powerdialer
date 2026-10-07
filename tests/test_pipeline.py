"""Pipeline stages, follow-ups, the transcript inbox for Claude.  python -m unittest discover tests"""
import os
import sys
import unittest
from datetime import timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "dialer"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import db  # noqa: E402
import pipeline  # noqa: E402
import transcribe  # noqa: E402
from test_db import Base  # noqa: E402

P = "+19372040101"


class Pipeline(Base):
    def setUp(self):
        super().setUp()
        self.lead(P, "Ohio Shop", "America/New_York", -4)

    def stage(self):
        return self.row(P)["stage"]

    def test_outcomes_move_leads_forward_only(self):
        pipeline.after_call(P, "RESONATED_NO")
        self.assertEqual(self.stage(), "interested")
        pipeline.set_stage(P, "using", by="claude")
        pipeline.after_call(P, "RESONATED_NO")                  # a later good call doesn't move it back
        self.assertEqual(self.stage(), "using")
        pipeline.after_call(P, "BOOKED")
        self.assertEqual(self.stage(), "call_booked")
        pipeline.after_followthrough(P, "NO_SHOW")
        self.assertEqual(self.stage(), "no_show")
        pipeline.after_followthrough(P, "SHOWED", sale=True)
        self.assertEqual(self.stage(), "sold")
        pipeline.after_call(P, "DNC")                           # sold is final for automatic moves
        self.assertEqual(self.stage(), "sold")
        kinds = [e["kind"] for e in pipeline.events(P)]
        self.assertEqual(kinds.count("stage"), 5)

    def test_callback_sets_the_follow_up(self):
        pipeline.after_call(P, "CALLBACK", callback_at="2026-09-23 14:00:00")
        self.assertEqual(self.row(P)["follow_up_at"], "2026-09-23 14:00:00")

    def test_board_counts_and_due(self):
        pipeline.set_stage(P, "invite_sent")
        pipeline.set_follow_up(P, db.iso(self.clock - timedelta(hours=1)), "check they signed up")
        b = pipeline.board()
        self.assertEqual(next(s for s in b["stages"] if s["key"] == "invite_sent")["count"], 1)
        self.assertEqual(b["due"], 1)
        self.assertEqual([l["phone"] for l in pipeline.board("due")["leads"]], [P])
        self.assertEqual(pipeline.followups_due()[0]["follow_up_note"], "check they signed up")

    def test_unknown_stage_is_refused(self):
        with self.assertRaises(ValueError):
            pipeline.set_stage(P, "maybe")

    def test_transcribed_calls_wait_for_claude_then_leave_the_inbox(self):
        dispo = self.save({"phone": P, "company": "Ohio Shop"}, "RESONATED_NO", recording_sid="RE" + "a" * 32)
        with db.connect() as con:
            con.execute("UPDATE dispositions SET transcript=?, transcript_status='done' WHERE id=?",
                        ("Pawan: Hi\nThem: Send me the link, dale@ohioshop.com", dispo))
        box = pipeline.inbox()
        self.assertEqual([c["id"] for c in box["calls"]], [dispo])
        self.assertIn("dale@ohioshop.com", box["calls"][0]["transcript"])
        self.assertEqual(box["calls"][0]["lead"]["company"], "Ohio Shop")
        pipeline.update_lead(P, {"email": "dale@ohioshop.com", "dm_name": "Dale Harlan", "status": "DONE"}, "claude")
        self.assertEqual(self.row(P)["email"], "dale@ohioshop.com")
        self.assertNotEqual(self.row(P)["status"], "DONE")       # only contact fields can be set this way
        self.assertTrue(pipeline.mark_processed(dispo, "Interested, wants the trial link."))
        self.assertEqual(pipeline.inbox()["calls"], [])
        self.assertEqual(pipeline.lead_detail(P)["calls"][0]["ai_summary"], "Interested, wants the trial link.")


class Transcript(unittest.TestCase):
    def test_channels_become_names_in_time_order(self):
        result = {"results": {"utterances": [
            {"channel": 1, "start": 1.0, "transcript": "Speaking."},
            {"channel": 0, "start": 0.2, "transcript": "Hi, is Dale in?"},
            {"channel": 0, "start": 2.0, "transcript": "Dale,"}, {"channel": 0, "start": 2.4, "transcript": "Pawan here."}]}}
        self.assertEqual(transcribe.format_utterances(result),
                         "Pawan: Hi, is Dale in?\nThem: Speaking.\nPawan: Dale, Pawan here.")
        self.assertTrue(transcribe.format_utterances(result, agent_channel=1).startswith("Them: Hi, is Dale in?"))


if __name__ == "__main__":
    unittest.main()
