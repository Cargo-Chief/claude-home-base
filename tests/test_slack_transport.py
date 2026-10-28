from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).parents[1]))

from slack_transport import NoUnfurlMixin, RecentEvents


class FakeClient:
    def chat_postMessage(self, **kwargs):
        return kwargs


class QuietClient(NoUnfurlMixin, FakeClient):
    pass


class SlackTransportTest(unittest.TestCase):
    def test_posts_do_not_unfurl_by_default(self):
        sent = QuietClient().chat_postMessage(channel="C1", text="see https://example.com")
        self.assertFalse(sent["unfurl_links"])
        self.assertFalse(sent["unfurl_media"])
        self.assertEqual("C1", sent["channel"])

    def test_a_caller_can_still_ask_for_a_preview(self):
        sent = QuietClient().chat_postMessage(channel="C1", text="x", unfurl_links=True)
        self.assertTrue(sent["unfurl_links"])

    def test_a_redelivered_event_is_dropped(self):
        events = RecentEvents()
        self.assertTrue(events.first_delivery("Ev1"))
        self.assertFalse(events.first_delivery("Ev1"))
        self.assertTrue(events.first_delivery("Ev2"))

    def test_requests_without_an_event_id_always_pass(self):
        events = RecentEvents()
        self.assertTrue(events.first_delivery(""))
        self.assertTrue(events.first_delivery(""))

    def test_memory_is_bounded_and_keeps_recent_ids(self):
        events = RecentEvents(capacity=2)
        events.first_delivery("Ev1")
        events.first_delivery("Ev2")
        events.first_delivery("Ev1")
        events.first_delivery("Ev3")
        self.assertFalse(events.first_delivery("Ev1"))
        self.assertTrue(events.first_delivery("Ev2"))


if __name__ == "__main__":
    unittest.main()
