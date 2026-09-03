import unittest
from unittest import mock
from types import SimpleNamespace

from agent_status import (
    AgentStatusReporter,
    configured_status_channel,
    reboot_request_has_conflict,
)


class FakeClient:
    def __init__(self, error=None):
        self.calls = []
        self.error = error

    def chat_postMessage(self, **kwargs):
        if self.error:
            raise self.error
        self.calls.append(kwargs)


class AgentStatusConfigurationTests(unittest.TestCase):
    def test_omitted_channel_disables_reporting(self):
        self.assertEqual(configured_status_channel({}), "")

    def test_accepts_channel_id(self):
        self.assertEqual(
            configured_status_channel({"AGENT_STATUS_CHANNEL_ID": "C012ABC34"}),
            "C012ABC34",
        )

    def test_accepts_legacy_private_channel_id(self):
        self.assertEqual(
            configured_status_channel({"AGENT_STATUS_CHANNEL_ID": "G012ABC34"}),
            "G012ABC34",
        )

    def test_rejects_channel_name_or_dm(self):
        for value in ("agent-status", "#agent-status", "D012ABC34", "C1;unsafe"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                configured_status_channel({"AGENT_STATUS_CHANNEL_ID": value})

    def test_reboot_mode_rejects_other_bot_operations(self):
        args = SimpleNamespace(
            request_reboot_status=True,
            channel=["C123", "sensitive detail"],
        )
        self.assertTrue(reboot_request_has_conflict(args))

    def test_bare_reboot_mode_has_no_conflict(self):
        args = SimpleNamespace(request_reboot_status=True)
        self.assertFalse(reboot_request_has_conflict(args))


class AgentStatusReporterTests(unittest.TestCase):
    def reporter(self, client, channel="C012ABC34"):
        return AgentStatusReporter(
            client, channel, "Ned", logger=mock.Mock()
        )

    def test_ready_message_contains_only_agent_identity(self):
        client = FakeClient()
        self.assertTrue(self.reporter(client).ready())
        self.assertEqual(client.calls, [{
            "channel": "C012ABC34",
            "text": "🟢 Ned is online and ready.",
        }])

    def test_unsafe_display_name_is_not_posted(self):
        client = FakeClient()
        reporter = AgentStatusReporter(
            client, "C012ABC34", "<!channel> customer"
        )
        reporter.ready()
        self.assertEqual(client.calls[0]["text"], "🟢 Agent is online and ready.")

    @mock.patch("agent_status.threading.Thread")
    def test_ready_async_uses_daemon_thread(self, thread):
        client = FakeClient()
        self.reporter(client).ready_async()
        thread.assert_called_once_with(
            target=mock.ANY,
            daemon=True,
        )
        thread.return_value.start.assert_called_once_with()

    def test_disabled_reporter_makes_no_slack_call(self):
        client = FakeClient()
        self.assertFalse(self.reporter(client, channel="").ready())
        self.assertEqual(client.calls, [])

    def test_fatal_message_has_no_exception_or_task_payload(self):
        client = FakeClient()
        self.assertTrue(self.reporter(client).fatal())
        text = client.calls[0]["text"]
        self.assertIn("fatal runtime error", text)
        self.assertIn("No task or customer details", text)
        self.assertNotIn("traceback", text.lower())

    def test_reboot_message_is_fixed_and_has_no_free_form_input(self):
        client = FakeClient()
        self.assertTrue(self.reporter(client).reboot_needed())
        self.assertEqual(
            client.calls[0]["text"],
            "🟡 Ned needs an operator reboot. No task or customer details were included.",
        )

    def test_delivery_failure_is_nonfatal_and_logs_only_exception_class(self):
        logger = mock.Mock()
        reporter = AgentStatusReporter(
            FakeClient(RuntimeError("secret request body")),
            "C012ABC34", "Ned", logger=logger,
        )
        self.assertFalse(reporter.fatal())
        logger.warning.assert_called_once_with(
            "Agent status delivery failed (%s)", "RuntimeError"
        )


if __name__ == "__main__":
    unittest.main()
