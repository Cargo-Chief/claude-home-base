import json
import unittest
from unittest.mock import patch

from codex_delegation import run_codex_delegate


class _Input:
    def __init__(self):
        self.value = ""

    def write(self, value):
        self.value += value

    def flush(self):
        pass


class _Process:
    def __init__(self, events):
        self.stdin = _Input()
        self.stdout = iter(json.dumps(event) + "\n" for event in events)
        self.returncode = None
        self.pid = 456

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        if self.returncode is None:
            self.returncode = 0
        return self.returncode

    def terminate(self):
        self.returncode = -15

    def kill(self):
        self.returncode = -9


def _events(total, *, status="completed", text="evidence"):
    return [
        {"jsonrpc": "2.0", "id": 1, "result": {}},
        {"jsonrpc": "2.0", "id": 2,
         "result": {"model": "gpt-5.6-sol", "thread": {"id": "thread-1"}}},
        {"jsonrpc": "2.0", "id": 3, "result": {"turn": {"id": "turn-1"}}},
        {"jsonrpc": "2.0", "method": "item/completed",
         "params": {"item": {"type": "agentMessage", "text": text}}},
        {"jsonrpc": "2.0", "method": "thread/tokenUsage/updated",
         "params": {"tokenUsage": {"total": {"totalTokens": total}}}},
        {"jsonrpc": "2.0", "method": "turn/completed",
         "params": {"turn": {"status": status}}},
    ]


class CodexDelegationTest(unittest.TestCase):
    @patch("codex_delegation.subprocess.Popen")
    def test_completed_turn_returns_text_and_incremental_usage(self, popen):
        process = _Process(_events(40))
        popen.return_value = process
        seen = []

        result = run_codex_delegate(
            ["codex", "app-server", "--stdio"], "private prompt", cwd="/work",
            env={}, model="gpt-5.6-sol", effort="medium", read_only=True,
            token_limit=50, timeout=10, on_process=lambda value: seen.append(value),
        )

        self.assertEqual(["evidence"], result.texts)
        self.assertEqual(40, result.tokens)
        self.assertFalse(result.budget_exhausted)
        sent = [json.loads(line) for line in process.stdin.value.splitlines()]
        thread_start = next(item for item in sent if item.get("method") == "thread/start")
        self.assertEqual("read-only", thread_start["params"]["sandbox"])
        self.assertEqual("never", thread_start["params"]["approvalPolicy"])
        self.assertIn("private prompt", process.stdin.value)
        self.assertNotIn("private prompt", popen.call_args.args[0])
        self.assertEqual([process, None], seen)

    @patch("codex_delegation.subprocess.Popen")
    def test_crossing_limit_interrupts_and_withholds_text(self, popen):
        process = _Process(_events(51, status="interrupted", text="must not surface"))
        popen.return_value = process

        result = run_codex_delegate(
            ["codex", "app-server", "--stdio"], "work", cwd="/work", env={},
            model="gpt-5.6-sol", effort="medium", read_only=False,
            token_limit=50, timeout=10, on_process=lambda _value: None,
        )

        self.assertTrue(result.budget_exhausted)
        self.assertEqual([], result.texts)
        self.assertEqual(51, result.tokens)
        sent = [json.loads(line) for line in process.stdin.value.splitlines()]
        self.assertTrue(any(item.get("method") == "turn/interrupt" for item in sent))

    @patch("codex_delegation.subprocess.Popen")
    def test_non_monotonic_usage_fails_closed(self, popen):
        events = _events(40)[:-1]
        events.append({
            "jsonrpc": "2.0", "method": "thread/tokenUsage/updated",
            "params": {"tokenUsage": {"total": {"totalTokens": 39}}},
        })
        popen.return_value = _Process(events)

        result = run_codex_delegate(
            ["codex", "app-server", "--stdio"], "work", cwd="/work", env={},
            model="gpt-5.6-sol", effort="medium", read_only=True,
            token_limit=50, timeout=10, on_process=lambda _value: None,
        )

        self.assertEqual("Codex delegate usage was invalid", result.error)
        self.assertEqual([], result.texts)
        self.assertEqual(40, result.tokens)


if __name__ == "__main__":
    unittest.main()
