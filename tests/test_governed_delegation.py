import contextlib
import fcntl
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from governed_delegation import (
    _append_audit,
    DEFAULT_TOKEN_BUDGET,
    DelegationError,
    ROUTES,
    budget_status,
    delegation_audit_path,
    delegation_verification_status,
    launch_from_environment,
    load_request,
    run_claude_delegate,
    update_budget,
    validate_implementation_plan,
    verify_from_environment,
)
from openai_fallback import CodexTurnResult


PLAN = """---
readiness: implementation-ready
---
# Plan

## Blocking Product Questions
None.

## 1. Data Flow Diagram
source -> destination

## 2. Affected Components Inventory
one component

## 6. Testing Requirements
tests defined

## 7. Dependency and deployment order
1. dependency

## 8. Rollback Plan
revert

## Required authorizations
Status: clear
"""


class GovernedDelegationTest(unittest.TestCase):

    def test_delegation_audit_is_workspace_writable_and_private(self):
        path = delegation_audit_path(self.root)
        self.assertEqual(
            self.root.resolve() / "work" / "home-base" / "delegation-audit.log",
            path,
        )
        _append_audit(path, {"STATUS": "completed"})
        self.assertIn("STATUS:completed", path.read_text(encoding="utf-8"))
        self.assertEqual(0o600, path.stat().st_mode & 0o777)

    def test_delegation_verification_status_distinguishes_budget_exhaustion(self):
        marker = self.work / "verification.json"
        self.assertIsNone(delegation_verification_status(marker))
        marker.write_text('{"status":"pending"}\n', encoding="utf-8")
        self.assertEqual("pending", delegation_verification_status(marker))
        marker.write_text('{"status":"budget_exhausted"}\n', encoding="utf-8")
        self.assertEqual("budget_exhausted", delegation_verification_status(marker))
        marker.write_text("not-json\n", encoding="utf-8")
        self.assertEqual("invalid", delegation_verification_status(marker))

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / "docs" / ".git").mkdir(parents=True)
        self.work = self.root / "work" / "home-base" / "thread"
        self.work.mkdir(parents=True)
        self.docs_worktree = self.root / "worktrees" / "gate" / "docs"
        (self.docs_worktree / "plans").mkdir(parents=True)
        self.plan = self.docs_worktree / "plans" / "plan.md"
        self.plan.write_text(PLAN)

    def tearDown(self):
        self.temp.cleanup()

    def _claim(self, text=None):
        claim = self.work / "implementation-claim.txt"
        claim.write_text(text if text is not None else str(self.plan))
        return claim

    @patch("governed_delegation.subprocess.run")
    def test_validates_and_consumes_implementation_plan(self, run):
        run.return_value.stdout = str(self.root / "docs" / ".git") + "\n"
        claim = self._claim()
        self.assertEqual(self.plan.resolve(), validate_implementation_plan(self.root, claim))
        self.assertFalse(claim.exists())

    @patch("governed_delegation.subprocess.run")
    def test_refuses_incomplete_plan_and_consumes_claim(self, run):
        run.return_value.stdout = str(self.root / "docs" / ".git") + "\n"
        self.plan.write_text("---\nreadiness: draft\n---\n")
        claim = self._claim()
        with self.assertRaisesRegex(DelegationError, "not implementation-ready"):
            validate_implementation_plan(self.root, claim)
        self.assertFalse(claim.exists())

    def test_request_is_exact_and_one_shot(self):
        request = self.work / "delegation-request.json"
        request.write_text(json.dumps({"tier": "bounded", "prompt": "check it", "mutation": False}))
        self.assertEqual("bounded", load_request(request).tier)
        self.assertFalse(request.exists())

        request.write_text(json.dumps({"tier": "explore", "prompt": "x", "mutation": True}))
        with self.assertRaisesRegex(DelegationError, "must remain read-only"):
            load_request(request)
        self.assertFalse(request.exists())

        request.write_text(json.dumps({"tier": "bounded", "prompt": "x", "mutation": False, "extra": True}))
        with self.assertRaisesRegex(DelegationError, "only tier, prompt, and mutation"):
            load_request(request)
        self.assertFalse(request.exists())

    def test_budget_persists_reset_and_limit(self):
        path = self.work / "budget.json"
        self.assertEqual(
            {"limit": DEFAULT_TOKEN_BUDGET, "used": 0}, budget_status(path)
        )
        self.assertEqual(12, update_budget(path, add_tokens=12)["used"])
        self.assertEqual(99, update_budget(path, limit=99)["limit"])
        self.assertEqual(0, update_budget(path, reset=True)["used"])

    def test_exact_provider_routes(self):
        self.assertEqual(("claude-opus-5[1m]", "medium"), ROUTES["implementation"]["claude"])
        self.assertEqual(("gpt-5.6-sol", "medium"), ROUTES["implementation"]["openai"])
        self.assertEqual(("claude-sonnet-5", "high"), ROUTES["mechanical"]["claude"])
        self.assertEqual(("gpt-5.6-terra", "high"), ROUTES["mechanical"]["openai"])
        self.assertEqual(("claude-haiku-4-5-20251001", "medium"), ROUTES["explore"]["claude"])
        self.assertEqual(("gpt-5.6-luna", "medium"), ROUTES["explore"]["openai"])

    @patch("governed_delegation.subprocess.Popen")
    def test_claude_prompt_uses_stdin_and_usage_is_metered(self, popen):
        process = popen.return_value
        process.returncode = 0
        process.communicate.return_value = (
            json.dumps({"result": "evidence", "usage": {"input_tokens": 8, "output_tokens": 2}}),
            "ignored stderr",
        )
        seen = []
        result = run_claude_delegate(
            ["claude", "-p", "--output-format", "json"], "private prompt",
            cwd=str(self.work), env={}, timeout=10,
            on_process=lambda value: seen.append(value),
        )
        self.assertEqual("evidence", result.text)
        self.assertEqual(10, result.tokens)
        process.communicate.assert_called_once_with("private prompt", timeout=10)
        self.assertNotIn("private prompt", popen.call_args.args[0])
        self.assertEqual([process, None], seen)

    def test_refuses_concurrent_delegate_before_consuming_request(self):
        request = self.work / "delegation-request.json"
        request.write_text(json.dumps({"tier": "bounded", "prompt": "work", "mutation": False}))
        pid = self.work / "delegate.pid"
        lock = pid.with_suffix(".lock")
        lock.touch()
        env = {"CARGO_CHIEF_DELEGATE_PID_FILE": str(pid)}
        with lock.open("a") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaisesRegex(DelegationError, "already active"):
                launch_from_environment(env)
        self.assertTrue(request.exists())

    @patch("governed_delegation.run_codex_turn")
    def test_openai_launch_records_budget_and_content_free_audit(self, run):
        run.return_value = CodexTurnResult(
            session_id="S", texts=["delegate evidence"],
            usage={"input_tokens": 30, "output_tokens": 5},
        )
        request = self.work / "delegation-request.json"
        request.write_text(json.dumps({"tier": "bounded", "prompt": "private brief", "mutation": False}))
        env = {
            "CARGO_CHIEF_ROOT": str(self.root),
            "CARGO_CHIEF_DELEGATION_REQUEST_FILE": str(request),
            "CARGO_CHIEF_IMPLEMENTATION_CLAIM_FILE": str(self.work / "claim.txt"),
            "CARGO_CHIEF_DELEGATION_BUDGET_FILE": str(self.work / "budget.json"),
            "CARGO_CHIEF_DELEGATE_PID_FILE": str(self.work / "pid"),
            "CARGO_CHIEF_DELEGATE_VERIFICATION_FILE": str(self.work / "verification.json"),
            "CARGO_CHIEF_AUDIT_LOG": str(self.work / "audit.log"),
            "CARGO_CHIEF_OWNER_PROVIDER": "openai",
            "CARGO_CHIEF_OWNER_MODEL": "gpt-5.6-sol",
            "CARGO_CHIEF_OWNER_EFFORT": "high",
            "CLAUDE_THREAD_TS": "T1",
            "CLAUDE_CHANNEL_ID": "C1",
            "CARGO_CHIEF_CURRENT_USER": "U1",
        }
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(0, launch_from_environment(env))
        self.assertEqual("delegate evidence\n", output.getvalue())
        self.assertEqual(35, budget_status(self.work / "budget.json")["used"])
        audit = (self.work / "audit.log").read_text()
        self.assertIn("MODEL:gpt-5.6-sol", audit)
        self.assertIn("TOKENS:35", audit)
        self.assertNotIn("private brief", audit)
        self.assertNotIn("delegate evidence", audit)
        self.assertIn("--sandbox", run.call_args.args[0])
        self.assertIn("read-only", run.call_args.args[0])
        self.assertTrue((self.work / "verification.json").is_file())
        verify_output = io.StringIO()
        with contextlib.redirect_stdout(verify_output):
            self.assertEqual(0, verify_from_environment(env))
        self.assertEqual("VERIFICATION_RECORDED\n", verify_output.getvalue())
        self.assertFalse((self.work / "verification.json").exists())
        self.assertIn("OWNER_VERIFY_TOOLS:1", (self.work / "audit.log").read_text())

    @patch("governed_delegation.run_codex_turn")
    def test_over_budget_return_is_withheld(self, run):
        run.return_value = CodexTurnResult(
            texts=["must not surface"], usage={"total_tokens": 10}
        )
        budget = self.work / "budget.json"
        update_budget(budget, limit=10)
        request = self.work / "delegation-request.json"
        request.write_text(json.dumps({"tier": "bounded", "prompt": "work", "mutation": False}))
        env = {
            "CARGO_CHIEF_ROOT": str(self.root),
            "CARGO_CHIEF_DELEGATION_REQUEST_FILE": str(request),
            "CARGO_CHIEF_IMPLEMENTATION_CLAIM_FILE": str(self.work / "claim.txt"),
            "CARGO_CHIEF_DELEGATION_BUDGET_FILE": str(budget),
            "CARGO_CHIEF_DELEGATE_PID_FILE": str(self.work / "pid"),
            "CARGO_CHIEF_DELEGATE_VERIFICATION_FILE": str(self.work / "verification.json"),
            "CARGO_CHIEF_AUDIT_LOG": str(self.work / "audit.log"),
            "CARGO_CHIEF_OWNER_PROVIDER": "openai",
            "CARGO_CHIEF_OWNER_MODEL": "gpt-5.6-sol",
            "CARGO_CHIEF_OWNER_EFFORT": "high",
            "CLAUDE_THREAD_TS": "T1", "CLAUDE_CHANNEL_ID": "C1",
            "CARGO_CHIEF_CURRENT_USER": "U1",
        }
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            with self.assertRaisesRegex(DelegationError, "return withheld"):
                launch_from_environment(env)
        self.assertEqual("", output.getvalue())
        marker = self.work / "verification.json"
        self.assertEqual("budget_exhausted", json.loads(marker.read_text())["status"])
        with self.assertRaisesRegex(DelegationError, "reset by a named approver"):
            verify_from_environment(env)
        self.assertTrue(marker.exists())


if __name__ == "__main__":
    unittest.main()
