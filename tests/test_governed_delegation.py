import contextlib
import fcntl
import io
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from codex_delegation import CodexDelegateResult

from governed_delegation import (
    _append_audit,
    BUDGET_UNIT,
    DEFAULT_TOKEN_BUDGET,
    LEGACY_BUDGET_UNIT,
    DelegationError,
    ROUTES,
    USAGE_RECEIPT_SCHEMA,
    budget_status,
    consume_allocation_exhaustion,
    consume_budget_exhaustion,
    delegation_audit_path,
    delegation_verification_status,
    initialize_budget,
    prepare_owner_delegation_state,
    launch_from_environment,
    load_request,
    parse_budget_command,
    run_claude_delegate,
    update_budget,
    validate_implementation_plan,
    verify_from_environment,
)


class _Input:
    def __init__(self):
        self.value = ""

    def write(self, value):
        self.value += value

    def flush(self):
        pass

    def close(self):
        pass


class _StreamProcess:
    def __init__(self, lines):
        self.stdin = _Input()
        self.stdout = iter(lines)
        self.returncode = None
        self.pid = 123

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
        marker.write_text('{"status":"allocation_exhausted"}\n', encoding="utf-8")
        self.assertEqual("allocation_exhausted", delegation_verification_status(marker))
        marker.write_text("not-json\n", encoding="utf-8")
        self.assertEqual("invalid", delegation_verification_status(marker))

    def test_consumes_only_a_valid_budget_exhaustion_marker(self):
        marker = self.work / "verification.json"
        marker.write_text('{"status":"budget_exhausted"}\n', encoding="utf-8")
        self.assertTrue(consume_budget_exhaustion(marker))
        self.assertFalse(marker.exists())
        self.assertFalse(consume_budget_exhaustion(marker))

        marker.write_text('{"status":"pending"}\n', encoding="utf-8")
        self.assertFalse(consume_budget_exhaustion(marker))
        self.assertTrue(marker.exists())

        marker.write_text("not-json\n", encoding="utf-8")
        self.assertFalse(consume_budget_exhaustion(marker))
        self.assertTrue(marker.exists())

        marker.write_text('{"status":"allocation_exhausted"}\n', encoding="utf-8")
        self.assertTrue(consume_allocation_exhaustion(marker))
        self.assertFalse(marker.exists())

    def test_does_not_consume_symlinked_budget_exhaustion_marker(self):
        target = self.work / "target.json"
        target.write_text('{"status":"budget_exhausted"}\n', encoding="utf-8")
        marker = self.work / "verification.json"
        marker.symlink_to(target)

        self.assertFalse(consume_budget_exhaustion(marker))
        self.assertTrue(marker.is_symlink())
        self.assertTrue(target.exists())

    def test_parses_both_exact_budget_command_spellings(self):
        self.assertEqual(("status", None), parse_budget_command(
            "delegation budget status"
        ))
        self.assertEqual(("status", None), parse_budget_command(
            "<@U123> delegate budget status"
        ))
        self.assertEqual(("reset", None), parse_budget_command(
            "delegate budget reset"
        ))
        self.assertEqual(("set 325000", 325000), parse_budget_command(
            "delegation budget set 325000"
        ))
        self.assertIsNone(parse_budget_command(
            "delegate budget set " + "9" * 10000
        ))
        self.assertIsNone(parse_budget_command("please show the delegate budget"))

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
        request.write_text(json.dumps({
            "tier": "bounded", "prompt": "check it", "mutation": False,
            "budget_unit": BUDGET_UNIT, "planned_tokens": 25_000,
        }))
        self.assertEqual("bounded", load_request(request).tier)
        self.assertFalse(request.exists())

        request.write_text(json.dumps({
            "tier": "bounded", "prompt": "compact stage", "mutation": False,
            "budget_unit": BUDGET_UNIT, "planned_tokens": 45_000,
        }))
        parsed = load_request(request)
        self.assertEqual(BUDGET_UNIT, parsed.budget_unit)
        self.assertEqual(45_000, parsed.planned_tokens)
        self.assertFalse(request.exists())

        request.write_text(json.dumps({
            "tier": "bounded", "prompt": "missing budget contract", "mutation": False,
        }))
        with self.assertRaisesRegex(DelegationError, "unsupported keys"):
            load_request(request)
        self.assertFalse(request.exists())

        request.write_text(json.dumps({
            "tier": "explore", "prompt": "x", "mutation": True,
            "budget_unit": BUDGET_UNIT, "planned_tokens": 5_000,
        }))
        with self.assertRaisesRegex(DelegationError, "must remain read-only"):
            load_request(request)
        self.assertFalse(request.exists())

        request.write_text(json.dumps({
            "tier": "bounded", "prompt": "x", "mutation": False,
            "budget_unit": BUDGET_UNIT, "planned_tokens": 25_000, "extra": True,
        }))
        with self.assertRaisesRegex(DelegationError, "unsupported keys"):
            load_request(request)
        self.assertFalse(request.exists())

        request.write_text(json.dumps({
            "tier": "bounded", "prompt": "x", "mutation": False,
            "budget_unit": "raw_tokens", "planned_tokens": 45_000,
        }))
        with self.assertRaisesRegex(DelegationError, "budget contract is invalid"):
            load_request(request)
        self.assertFalse(request.exists())

    def test_budget_persists_reset_and_limit(self):
        path = self.work / "budget.json"
        self.assertEqual(
            {"limit": DEFAULT_TOKEN_BUDGET, "used": 0, "unit": BUDGET_UNIT},
            budget_status(path),
        )
        self.assertEqual(BUDGET_UNIT, json.loads(path.read_text())["unit"])
        self.assertEqual(12, update_budget(path, add_tokens=12)["used"])
        self.assertEqual(99, update_budget(path, limit=99)["limit"])
        reset = update_budget(path, reset=True)
        self.assertEqual(0, reset["used"])
        self.assertEqual(BUDGET_UNIT, reset["unit"])

    def test_budget_initialization_creates_only_missing_state(self):
        path = self.work / "budget.json"
        initialize_budget(path)
        self.assertEqual(
            {"limit": DEFAULT_TOKEN_BUDGET, "used": 0, "unit": BUDGET_UNIT},
            json.loads(path.read_text()),
        )

        path.write_text("malformed\n", encoding="utf-8")
        initialize_budget(path)
        self.assertEqual("malformed\n", path.read_text(encoding="utf-8"))
        reset = update_budget(path, reset=True)
        self.assertEqual(BUDGET_UNIT, reset["unit"])

    def test_budget_initialization_does_not_follow_existing_symlink(self):
        target = self.work / "target.json"
        target.write_text("unchanged\n", encoding="utf-8")
        path = self.work / "budget.json"
        path.symlink_to(target)

        initialize_budget(path)

        self.assertEqual("unchanged\n", target.read_text(encoding="utf-8"))
        with self.assertRaisesRegex(DelegationError, "state is invalid"):
            budget_status(path)

    def test_concurrent_budget_initialization_publishes_complete_state(self):
        path = self.work / "budget.json"
        errors = []

        def initialize():
            try:
                initialize_budget(path)
            except Exception as exc:  # pragma: no cover - collected for the assertion
                errors.append(exc)

        workers = [threading.Thread(target=initialize) for _ in range(20)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join()

        self.assertEqual([], errors)
        self.assertEqual(BUDGET_UNIT, json.loads(path.read_text())["unit"])
        self.assertEqual([], list(self.work.glob(".budget.json.*.writing")))

    def test_legacy_raw_budget_requires_named_approver_reset(self):
        path = self.work / "budget.json"
        path.write_text('{"limit":250000,"used":1125414}\n', encoding="utf-8")
        state = budget_status(path)
        self.assertEqual(LEGACY_BUDGET_UNIT, state["unit"])
        with self.assertRaisesRegex(DelegationError, "must be reset"):
            update_budget(path, add_tokens=1)
        with self.assertRaisesRegex(DelegationError, "must be reset"):
            update_budget(path, limit=300_000)
        reset = update_budget(path, reset=True)
        self.assertEqual(
            {"limit": 250_000, "used": 0, "unit": BUDGET_UNIT}, reset
        )
        self.assertEqual(reset, json.loads(path.read_text(encoding="utf-8")))

    def test_legacy_reset_does_not_reinterpret_a_raised_raw_token_limit(self):
        path = self.work / "budget.json"
        path.write_text('{"limit":2000000,"used":1125414}\n', encoding="utf-8")

        reset = update_budget(path, reset=True)

        self.assertEqual(
            {"limit": 250_000, "used": 0, "unit": BUDGET_UNIT}, reset
        )

    def test_owner_restart_clears_only_stage_allocation_exhaustion(self):
        budget = self.work / "budget.json"
        marker = self.work / "verification.json"
        marker.write_text('{"status":"allocation_exhausted"}\n', encoding="utf-8")

        self.assertTrue(prepare_owner_delegation_state(budget, marker))
        self.assertFalse(marker.exists())
        self.assertEqual(BUDGET_UNIT, budget_status(budget)["unit"])

        for status in ("pending", "budget_exhausted"):
            marker.write_text(json.dumps({"status": status}) + "\n", encoding="utf-8")
            self.assertFalse(prepare_owner_delegation_state(budget, marker))
            self.assertEqual(status, json.loads(marker.read_text())["status"])

    def test_exact_provider_routes(self):
        self.assertEqual(("claude-opus-5[1m]", "medium"), ROUTES["implementation"]["claude"])
        self.assertEqual(("gpt-5.6-sol", "medium"), ROUTES["implementation"]["openai"])
        self.assertEqual(("claude-sonnet-5", "high"), ROUTES["mechanical"]["claude"])
        self.assertEqual(("gpt-5.6-terra", "high"), ROUTES["mechanical"]["openai"])
        self.assertEqual(("claude-haiku-4-5-20251001", "medium"), ROUTES["explore"]["claude"])
        self.assertEqual(("gpt-5.6-luna", "medium"), ROUTES["explore"]["openai"])

    @patch("governed_delegation.subprocess.Popen")
    def test_claude_prompt_uses_stdin_and_usage_is_metered(self, popen):
        process = _StreamProcess([
            json.dumps({
                "type": "assistant",
                "message": {"id": "m1", "usage": {
                    "input_tokens": 8, "cache_read_input_tokens": 1_000, "output_tokens": 2,
                },
                            "content": [{"type": "text", "text": "evidence"}]},
            }) + "\n",
            json.dumps({
                "type": "result", "result": "evidence", "usage": {
                    "input_tokens": 8, "cache_read_input_tokens": 1_000,
                    "output_tokens": 2,
                },
            }) + "\n",
        ])
        popen.return_value = process
        seen = []
        result = run_claude_delegate(
            ["claude", "-p", "--output-format", "stream-json"], "private prompt",
            cwd=str(self.work), env={}, token_limit=100, timeout=10,
            on_process=lambda value: seen.append(value),
        )
        self.assertEqual("evidence", result.text)
        self.assertEqual(2, result.tokens)
        self.assertEqual(1_010, result.raw_tokens)
        self.assertEqual("private prompt", process.stdin.value)
        self.assertNotIn("private prompt", popen.call_args.args[0])
        self.assertEqual([process, None], seen)

    @patch("governed_delegation.subprocess.Popen")
    def test_claude_stops_and_withholds_at_incremental_limit(self, popen):
        process = _StreamProcess([
            json.dumps({
                "type": "assistant",
                "message": {"id": "m1", "usage": {"input_tokens": 8, "output_tokens": 11},
                            "content": [{"type": "tool_use", "name": "Write"}]},
            }) + "\n",
            json.dumps({"type": "result", "result": "must not surface"}) + "\n",
        ])
        popen.return_value = process
        result = run_claude_delegate(
            ["claude", "-p", "--output-format", "stream-json"], "work",
            cwd=str(self.work), env={}, token_limit=10, timeout=10,
            on_process=lambda _value: None,
        )
        self.assertTrue(result.budget_exhausted)
        self.assertEqual("", result.text)
        self.assertEqual(11, result.tokens)
        self.assertEqual(19, result.raw_tokens)
        self.assertEqual(-9, process.returncode)

    @patch("governed_delegation.subprocess.Popen")
    def test_claude_accumulates_each_message_usage_once(self, popen):
        first = {
            "type": "assistant",
            "message": {"id": "m1", "usage": {"input_tokens": 8, "output_tokens": 2},
                        "content": []},
        }
        process = _StreamProcess([
            json.dumps(first) + "\n",
            json.dumps(first) + "\n",
            json.dumps({
                "type": "assistant",
                "message": {"id": "m2", "usage": {"input_tokens": 5, "output_tokens": 1},
                            "content": []},
            }) + "\n",
            json.dumps({
                "type": "result", "result": "done",
                "usage": {"input_tokens": 13, "output_tokens": 3},
            }) + "\n",
        ])
        popen.return_value = process
        result = run_claude_delegate(
            ["claude", "-p", "--output-format", "stream-json"], "work",
            cwd=str(self.work), env={}, token_limit=20, timeout=10,
            on_process=lambda _value: None,
        )
        self.assertEqual("done", result.text)
        self.assertEqual(3, result.tokens)
        self.assertEqual(16, result.raw_tokens)

    @patch("governed_delegation.subprocess.Popen")
    def test_claude_cache_reads_do_not_exhaust_generation_budget(self, popen):
        process = _StreamProcess([
            json.dumps({
                "type": "assistant",
                "message": {"id": "m1", "usage": {
                    "input_tokens": 22,
                    "cache_creation_input_tokens": 119_658,
                    "cache_read_input_tokens": 974_531,
                    "output_tokens": 31_203,
                }, "content": []},
            }) + "\n",
            json.dumps({
                "type": "result", "result": "review evidence", "usage": {
                    "input_tokens": 22,
                    "cache_creation_input_tokens": 119_658,
                    "cache_read_input_tokens": 974_531,
                    "output_tokens": 31_203,
                },
            }) + "\n",
        ])
        popen.return_value = process

        result = run_claude_delegate(
            ["claude", "-p", "--output-format", "stream-json"], "work",
            cwd=str(self.work), env={}, token_limit=250_000, timeout=10,
            on_process=lambda _value: None,
        )

        self.assertEqual("review evidence", result.text)
        self.assertEqual(31_203, result.tokens)
        self.assertEqual(1_125_414, result.raw_tokens)
        self.assertFalse(result.budget_exhausted)

    @patch("governed_delegation.subprocess.Popen")
    def test_claude_result_usage_replaces_placeholder_assistant_usage(self, popen):
        process = _StreamProcess([
            json.dumps({
                "type": "assistant",
                "message": {"id": "m1", "usage": {
                    "input_tokens": 10, "output_tokens": 2,
                }, "content": []},
            }) + "\n",
            json.dumps({
                "type": "result", "result": "review evidence", "usage": {
                    "input_tokens": 22, "cache_read_input_tokens": 1_000,
                    "output_tokens": 31_203,
                },
            }) + "\n",
        ])
        popen.return_value = process

        result = run_claude_delegate(
            ["claude", "-p", "--output-format", "stream-json"], "work",
            cwd=str(self.work), env={}, token_limit=45_000, timeout=10,
            on_process=lambda _value: None,
        )

        self.assertEqual(31_203, result.tokens)
        self.assertEqual(32_225, result.raw_tokens)
        self.assertFalse(result.budget_exhausted)

    @patch("governed_delegation.subprocess.Popen")
    def test_claude_zero_output_preserves_raw_usage_for_audit(self, popen):
        process = _StreamProcess([
            json.dumps({
                "type": "result", "result": "", "usage": {
                    "input_tokens": 22, "cache_read_input_tokens": 1_000,
                    "output_tokens": 0,
                },
            }) + "\n",
        ])
        popen.return_value = process

        result = run_claude_delegate(
            ["claude", "-p", "--output-format", "stream-json"], "work",
            cwd=str(self.work), env={}, token_limit=45_000, timeout=10,
            on_process=lambda _value: None,
        )

        self.assertEqual(0, result.tokens)
        self.assertEqual(1_022, result.raw_tokens)
        self.assertEqual("Claude delegate returned no usage", result.error)

    def test_refuses_concurrent_delegate_before_consuming_request(self):
        request = self.work / "delegation-request.json"
        request.write_text(json.dumps({
            "tier": "bounded", "prompt": "work", "mutation": False,
            "budget_unit": BUDGET_UNIT, "planned_tokens": 25_000,
        }))
        pid = self.work / "delegate.pid"
        lock = pid.with_suffix(".lock")
        lock.touch()
        env = {"CARGO_CHIEF_DELEGATE_PID_FILE": str(pid)}
        with lock.open("a") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaisesRegex(DelegationError, "already active"):
                launch_from_environment(env)
        self.assertTrue(request.exists())

    @patch("governed_delegation.run_codex_delegate")
    def test_openai_launch_records_budget_and_content_free_audit(self, run):
        run.return_value = CodexDelegateResult(
            texts=["delegate evidence"], tokens=35, raw_tokens=3_500,
        )
        request = self.work / "delegation-request.json"
        request.write_text(json.dumps({
            "tier": "bounded", "prompt": "private brief", "mutation": False,
            "budget_unit": BUDGET_UNIT, "planned_tokens": 45_000,
        }))
        budget = self.work / "budget.json"
        update_budget(budget, add_tokens=40)
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
            "CLAUDE_THREAD_TS": "T1",
            "CLAUDE_CHANNEL_ID": "C1",
            "CARGO_CHIEF_CURRENT_USER": "U1",
        }
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(0, launch_from_environment(env))
        self.assertEqual("delegate evidence\n", output.getvalue())
        self.assertEqual(75, budget_status(budget)["used"])
        audit = (self.work / "audit.log").read_text()
        self.assertIn("MODEL:gpt-5.6-sol", audit)
        self.assertIn("BUDGET_UNIT:generation_tokens_v1", audit)
        self.assertIn("BUDGET_TOKENS:35", audit)
        self.assertIn("RAW_TOKENS:3500", audit)
        self.assertNotIn("private brief", audit)
        self.assertNotIn("delegate evidence", audit)
        self.assertTrue(run.call_args.kwargs["read_only"])
        self.assertEqual(45_000, run.call_args.kwargs["token_limit"])
        self.assertIn("app-server", run.call_args.args[0])
        self.assertTrue((self.work / "verification.json").is_file())
        verify_output = io.StringIO()
        with contextlib.redirect_stdout(verify_output):
            self.assertEqual(0, verify_from_environment(env))
        self.assertEqual({
            "schema_version": USAGE_RECEIPT_SCHEMA,
            "budget_unit": BUDGET_UNIT,
            "actual_tokens": 35,
        }, {
            key: value
            for key, value in json.loads(verify_output.getvalue()).items()
            if key != "request_id"
        })
        self.assertRegex(json.loads(verify_output.getvalue())["request_id"], r"^[0-9a-f]{64}$")
        self.assertFalse((self.work / "verification.json").exists())
        self.assertIn("OWNER_VERIFY_TOOLS:1", (self.work / "audit.log").read_text())

    def test_verification_refuses_a_pending_marker_without_typed_usage(self):
        marker = self.work / "verification.json"
        marker.write_text('{"status":"pending"}\n', encoding="utf-8")
        env = {
            "CARGO_CHIEF_DELEGATE_VERIFICATION_FILE": str(marker),
            "CARGO_CHIEF_AUDIT_LOG": str(self.work / "audit.log"),
            "CARGO_CHIEF_CURRENT_USER": "U1",
            "CLAUDE_CHANNEL_ID": "C1",
            "CLAUDE_THREAD_TS": "T1",
        }

        with self.assertRaisesRegex(DelegationError, "usage is invalid"):
            verify_from_environment(env)
        self.assertTrue(marker.exists())

    @patch("governed_delegation.run_codex_delegate")
    def test_stage_allocation_exhaustion_preserves_thread_for_owner(self, run):
        run.return_value = CodexDelegateResult(
            tokens=45_001, raw_tokens=1_125_414, budget_exhausted=True,
        )
        request = self.work / "delegation-request.json"
        request.write_text(json.dumps({
            "tier": "bounded", "prompt": "compact stage", "mutation": False,
            "budget_unit": BUDGET_UNIT, "planned_tokens": 45_000,
        }))
        budget = self.work / "budget.json"
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

        with self.assertRaisesRegex(DelegationError, "stage generation-token allocation"):
            launch_from_environment(env)

        self.assertEqual(45_000, run.call_args.kwargs["token_limit"])
        self.assertEqual(45_001, budget_status(budget)["used"])
        marker = self.work / "verification.json"
        self.assertEqual("allocation_exhausted", json.loads(marker.read_text())["status"])
        self.assertIn("STATUS:allocation_exhausted", (self.work / "audit.log").read_text())
        with self.assertRaisesRegex(DelegationError, "no thread-budget reset is required"):
            verify_from_environment(env)
        self.assertTrue(marker.exists())

    @patch("governed_delegation.run_codex_delegate")
    def test_over_budget_return_is_withheld(self, run):
        run.return_value = CodexDelegateResult(
            tokens=10, budget_exhausted=True,
        )
        budget = self.work / "budget.json"
        update_budget(budget, limit=10)
        request = self.work / "delegation-request.json"
        request.write_text(json.dumps({
            "tier": "bounded", "prompt": "work", "mutation": False,
            "budget_unit": BUDGET_UNIT, "planned_tokens": 10,
        }))
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
