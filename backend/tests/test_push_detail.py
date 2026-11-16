"""The detail of a notification: the message itself with its lines, a card in full, and a group item by item."""
import unittest

from coconut.push_relay import BODY_LIMIT, body_text, decision_text, group_lines, notification_text


class PushDetailTests(unittest.TestCase):
    def test_a_long_message_keeps_its_lines_and_list_items(self):
        text = "**Two new letters** arrived today:\n\n- Item 101 — Inland Revenue\n- Item 102 — Utah Tax\n\nBoth scans saved."
        self.assertEqual(body_text(text),
                         "Two new letters arrived today:\n\n• Item 101 — Inland Revenue\n• Item 102 — Utah Tax\n\nBoth scans saved.")
        self.assertEqual(notification_text(text), "Two new letters arrived today: Item 101 — Inland Revenue Item 102 — Utah Tax Both scans saved.")

    def test_the_body_holds_far_more_than_one_line(self):
        self.assertEqual(len(body_text("word " * 400)), BODY_LIMIT)

    def test_a_card_shows_its_question_reason_risk_and_choices(self):
        text = decision_text({"title": "Merge PR #42?", "context": "It lets long labels wrap.", "risk": "Low.",
                              "options": [{"label": "Merge"}, {"label": "Leave open"}]})
        self.assertEqual(text, "Merge PR #42?\n\nIt lets long labels wrap.\n\nRisk: Low.\n\nChoices: Merge · Leave open")

    def test_a_group_lists_each_item_and_the_count_of_the_rest(self):
        lines = [f"Agent {n}: done" for n in range(8)]
        self.assertEqual(group_lines(lines, 8).split("\n"), lines[-6:] + ["and 2 more"])
        self.assertEqual(group_lines(lines[:2], 2).split("\n"), lines[:2])


if __name__ == "__main__":
    unittest.main()
