from pathlib import Path
import unittest
from unittest import mock

from agent_status import AgentStatusReporter, configured_status_channel


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


class AgentStatusReporterTests(unittest.TestCase):
    def reporter(self, client, channel="C012ABC34"):
        return AgentStatusReporter(
            client, channel, "Ned", Path("/source"), logger=mock.Mock()
        )

    @mock.patch("agent_status.socket.gethostname", return_value="ned-host.local")
    @mock.patch("agent_status.source_revision", return_value="abc1234")
    def test_ready_message_contains_only_runtime_identity(self, _revision, _hostname):
        client = FakeClient()
        self.assertTrue(self.reporter(client).ready())
        self.assertEqual(client.calls, [{
            "channel": "C012ABC34",
            "text": "🟢 Ned is online and ready · host `ned-host` · revision `abc1234`",
        }])

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
            "C012ABC34", "Ned", Path("/source"), logger=logger,
        )
        self.assertFalse(reporter.fatal())
        logger.warning.assert_called_once_with(
            "Agent status delivery failed (%s)", "RuntimeError"
        )


if __name__ == "__main__":
    unittest.main()
