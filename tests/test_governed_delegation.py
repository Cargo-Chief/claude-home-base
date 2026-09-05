import contextlib
import fcntl
import io
import json
import math
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from codex_delegation import CodexDelegateResult

from governed_delegation import (
    _acquire_delegate_lock,
    _append_audit,
    _normalized_charge,
    _provider_call_limit,
    BUDGET_UNIT,
    CODEX_GENERATION_FACTOR,
    DEFAULT_TOKEN_BUDGET,
    LEGACY_BUDGET_UNIT,
    MAX_CODEX_GENERATION_FACTOR,
    MIN_CODEX_GENERATION_FACTOR,
    SUPERSEDED_BUDGET_UNITS,
    DelegateResult,
    DelegationError,
    ROUTES,
    USAGE_RECEIPT_SCHEMA,
    budget_status,
    cleanup_stale_delegate_pid,
    codex_generation_factor,
    consume_allocation_exhaustion,
    consume_budget_exhaustion,
    delegate_timeout_from_env,
    delegation_audit_path,
    delegation_verification_status,
    discard_unverifiable_verification,
    governed_delegate_active,
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
        self.assertEqual(("verification reset", None), parse_budget_command(
            "delegation verification reset"
        ))
        self.assertEqual(("verification reset", None), parse_budget_command(
            "<@U123> delegate verification reset"
        ))
        self.assertIsNone(parse_budget_command("delegation verification status"))
        self.assertIsNone(parse_budget_command("please reset the delegate verification"))
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

    def test_budget_update_failure_preserves_the_last_complete_state(self):
        path = self.work / "budget.json"
        update_budget(path, add_tokens=12)

        with patch("governed_delegation.os.replace", side_effect=OSError("stopped")):
            with self.assertRaisesRegex(DelegationError, "could not be persisted"):
                update_budget(path, add_tokens=5)

        self.assertEqual(12, budget_status(path)["used"])
        self.assertEqual([], list(self.work.glob(".budget.json.*.writing")))

    def test_empty_existing_budget_fails_closed_until_named_reset(self):
        path = self.work / "budget.json"
        path.touch()

        with self.assertRaisesRegex(DelegationError, "state is invalid"):
            budget_status(path)

        self.assertEqual(0, update_budget(path, reset=True)["used"])
        self.assertEqual(BUDGET_UNIT, budget_status(path)["unit"])

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

    def test_concurrent_budget_updates_share_the_stable_sidecar_lock(self):
        path = self.work / "budget.json"
        initialize_budget(path)
        errors = []

        def add_one():
            try:
                update_budget(path, add_tokens=1)
            except Exception as exc:  # pragma: no cover - collected for the assertion
                errors.append(exc)

        workers = [threading.Thread(target=add_one) for _ in range(20)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join()

        self.assertEqual([], errors)
        self.assertEqual(20, budget_status(path)["used"])
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

    def test_delegate_timeout_is_independent_and_bounded(self):
        self.assertEqual(1_800, delegate_timeout_from_env({}))
        self.assertEqual(
            1_200,
            delegate_timeout_from_env({"CARGO_CHIEF_DELEGATE_TIMEOUT": "1200"}),
        )
        for value in ("not-a-number", "0", "-1", "1801"):
            with self.subTest(value=value):
                with self.assertRaises(DelegationError):
                    delegate_timeout_from_env({"CARGO_CHIEF_DELEGATE_TIMEOUT": value})

    def test_invalid_delegate_timeout_does_not_consume_request(self):
        request = self.work / "delegation-request.json"
        request.write_text(json.dumps({
            "tier": "bounded", "prompt": "private brief", "mutation": False,
            "budget_unit": BUDGET_UNIT, "planned_tokens": 45_000,
        }))
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
            "CARGO_CHIEF_DELEGATE_TIMEOUT": "invalid",
            "CLAUDE_THREAD_TS": "T1",
            "CLAUDE_CHANNEL_ID": "C1",
            "CARGO_CHIEF_CURRENT_USER": "U1",
        }

        with self.assertRaisesRegex(DelegationError, "must be an integer"):
            launch_from_environment(env)

        self.assertTrue(request.is_file())

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

    def test_delegate_launch_tolerates_transient_activity_probe_lock(self):
        lock = (self.work / "delegate.pid").with_suffix(".lock")
        holder = lock.open("a", encoding="utf-8")
        contender = lock.open("a", encoding="utf-8")
        fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)

        def release_probe():
            threading.Event().wait(0.02)
            fcntl.flock(holder, fcntl.LOCK_UN)
            holder.close()

        releaser = threading.Thread(target=release_probe)
        releaser.start()
        try:
            _acquire_delegate_lock(contender, wait_seconds=0.2)
            fcntl.flock(contender, fcntl.LOCK_UN)
        finally:
            contender.close()
            releaser.join(timeout=1)
        self.assertFalse(releaser.is_alive())

    def test_held_delegate_lock_reports_active(self):
        pid = self.work / "delegate.pid"
        lock = pid.with_suffix(".lock")
        with lock.open("a", encoding="utf-8") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertTrue(governed_delegate_active(pid))

    def test_free_delegate_lock_reports_inactive(self):
        self.assertFalse(governed_delegate_active(self.work / "delegate.pid"))

    def test_stale_pid_cleanup_respects_delegate_lock(self):
        pid = self.work / "delegate.pid"
        pid.write_text("123\n", encoding="utf-8")
        lock = pid.with_suffix(".lock")
        with lock.open("a", encoding="utf-8") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertFalse(cleanup_stale_delegate_pid(pid))
        self.assertTrue(pid.exists())
        self.assertTrue(cleanup_stale_delegate_pid(pid))
        self.assertFalse(pid.exists())

    def test_stale_pid_cleanup_refuses_symlink(self):
        target = self.work / "target"
        target.write_text("123\n", encoding="utf-8")
        pid = self.work / "delegate.pid"
        pid.symlink_to(target)

        self.assertFalse(cleanup_stale_delegate_pid(pid))
        self.assertTrue(pid.is_symlink())

    def test_delegate_helpers_refuse_symlinked_lock(self):
        target = self.work / "real.lock"
        target.touch()
        pid = self.work / "delegate.pid"
        pid.with_suffix(".lock").symlink_to(target)

        self.assertFalse(governed_delegate_active(pid))
        self.assertFalse(cleanup_stale_delegate_pid(pid))

    def test_stale_pid_cleanup_ignores_missing_marker(self):
        self.assertFalse(cleanup_stale_delegate_pid(self.work / "delegate.pid"))

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
            "CARGO_CHIEF_DELEGATE_TIMEOUT": "1200",
            "CLAUDE_THREAD_TS": "T1",
            "CLAUDE_CHANNEL_ID": "C1",
            "CARGO_CHIEF_CURRENT_USER": "U1",
        }
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(0, launch_from_environment(env))
        self.assertEqual("delegate evidence\n", output.getvalue())
        self.assertEqual(54, budget_status(budget)["used"])
        audit = (self.work / "audit.log").read_text()
        self.assertIn("MODEL:gpt-5.6-sol", audit)
        self.assertIn("BUDGET_UNIT:generation_tokens_v2", audit)
        self.assertIn("BUDGET_TOKENS:14", audit)
        self.assertIn("PROVIDER_TOKENS:35", audit)
        self.assertIn("RAW_TOKENS:3500", audit)
        self.assertNotIn("private brief", audit)
        self.assertNotIn("delegate evidence", audit)
        self.assertTrue(run.call_args.kwargs["read_only"])
        self.assertEqual(112_500, run.call_args.kwargs["token_limit"])
        self.assertEqual(1_200, run.call_args.kwargs["timeout"])
        self.assertIn("app-server", run.call_args.args[0])
        self.assertTrue((self.work / "verification.json").is_file())
        verify_output = io.StringIO()
        with contextlib.redirect_stdout(verify_output):
            self.assertEqual(0, verify_from_environment(env))
        self.assertEqual({
            "schema_version": USAGE_RECEIPT_SCHEMA,
            "budget_unit": BUDGET_UNIT,
            "actual_tokens": 14,
        }, {
            key: value
            for key, value in json.loads(verify_output.getvalue()).items()
            if key != "request_id"
        })
        self.assertRegex(json.loads(verify_output.getvalue())["request_id"], r"^[0-9a-f]{64}$")
        self.assertFalse((self.work / "verification.json").exists())
        self.assertIn("OWNER_VERIFY_TOOLS:1", (self.work / "audit.log").read_text())

    @patch("governed_delegation.run_codex_delegate")
    def test_launch_refuses_allocation_that_exceeds_remaining_budget(self, run):
        request = self.work / "delegation-request.json"
        request.write_text(json.dumps({
            "tier": "bounded", "prompt": "work", "mutation": False,
            "budget_unit": BUDGET_UNIT, "planned_tokens": 45_000,
        }))
        budget = self.work / "budget.json"
        update_budget(budget, limit=50_000, add_tokens=10_000)
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

        with self.assertRaisesRegex(DelegationError, "does not fit"):
            launch_from_environment(env)

        run.assert_not_called()
        self.assertFalse(request.exists())

    def test_verification_refuses_a_pending_marker_without_typed_usage(self):
        marker = self.work / "verification.json"
        # On the current unit, so the refusal under test is the missing typed
        # usage rather than the unit mismatch checked ahead of it.
        marker.write_text(
            json.dumps({"status": "pending", "budget_unit": BUDGET_UNIT}) + "\n",
            encoding="utf-8",
        )
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

    def _verify_environment(self, marker):
        return {
            "CARGO_CHIEF_DELEGATE_VERIFICATION_FILE": str(marker),
            "CARGO_CHIEF_AUDIT_LOG": str(self.work / "audit.log"),
            "CARGO_CHIEF_CURRENT_USER": "U1",
            "CLAUDE_CHANNEL_ID": "C1",
            "CLAUDE_THREAD_TS": "T1",
        }

    # Every unit a marker can carry that this launcher cannot verify: the
    # superseded generation unit, an unrecognized one, the raw-token unit, a
    # non-string, and — for a marker written before the unit key existed — no
    # key at all.
    UNVERIFIABLE_UNITS = (
        SUPERSEDED_BUDGET_UNITS[0], "generation_tokens_v99", LEGACY_BUDGET_UNIT, 7, None,
    )

    def _unverifiable_marker(self, unit):
        marker = self.work / "verification.json"
        metadata = {
            "status": "pending", "tier": "bounded", "model": "gpt-5.6-sol",
            "tokens": 40_000, "provider_tokens": 100_000, "raw_tokens": 1_125_414,
            "generation_factor": CODEX_GENERATION_FACTOR, "request_id": "a" * 64,
        }
        if unit is not None:
            metadata["budget_unit"] = unit
        marker.write_text(json.dumps(metadata) + "\n", encoding="utf-8")
        return marker

    def test_verification_refuses_every_non_current_unit_without_consuming(self):
        # Verification runs inside the owner's turn and bot.py withholds that
        # turn's assistant text while the marker exists, so consuming the marker
        # here would unmute the same turn and release delegate text whose spend
        # was never verified.
        for unit in self.UNVERIFIABLE_UNITS:
            with self.subTest(unit=unit):
                marker = self._unverifiable_marker(unit)
                output = io.StringIO()

                with contextlib.redirect_stdout(output):
                    with self.assertRaises(DelegationError) as raised:
                        verify_from_environment(self._verify_environment(marker))

                message = str(raised.exception)
                self.assertIn(BUDGET_UNIT, message)
                self.assertIn("delegation verification reset", message)
                # No receipt is emitted for spend that cannot be verified.
                self.assertEqual("", output.getvalue())
                self.assertTrue(marker.is_file())

    def test_approver_discard_clears_every_unverifiable_marker(self):
        audit = self.work / "audit.log"
        for unit in self.UNVERIFIABLE_UNITS:
            with self.subTest(unit=unit):
                marker = self._unverifiable_marker(unit)

                discarded = discard_unverifiable_verification(
                    marker, audit, user="U1", channel="C1", thread="T1",
                )

                self.assertFalse(marker.exists())
                self.assertEqual("pending", discarded["status"])
                self.assertEqual(40_000, discarded["tokens"])
                self.assertEqual(100_000, discarded["provider_tokens"])
                with self.assertRaisesRegex(DelegationError, "no delegation verification"):
                    discard_unverifiable_verification(
                        marker, audit, user="U1", channel="C1", thread="T1",
                    )
        # The marker is the only place provider_tokens lives, so the discard
        # must not be the one terminal transition that leaves no audit trace.
        log = audit.read_text(encoding="utf-8")
        self.assertEqual(len(self.UNVERIFIABLE_UNITS), log.count("STATUS:verification_discarded"))
        self.assertIn("PROVIDER_TOKENS:100000", log)
        self.assertIn("BUDGET_TOKENS:40000", log)
        self.assertIn("RAW_TOKENS:1125414", log)
        self.assertIn("REQUEST_ID:" + "a" * 64, log)
        self.assertIn("MARKER_STATUS:pending", log)

    def test_approver_discard_never_clears_a_current_unit_marker(self):
        # A marker on the current unit is verifiable by running the verification
        # stage, so discarding it would bypass a live gate rather than recover
        # from a dead one.
        audit = self.work / "audit.log"
        marker = self._unverifiable_marker(BUDGET_UNIT)

        with self.assertRaises(DelegationError) as raised:
            discard_unverifiable_verification(
                marker, audit, user="U1", channel="C1", thread="T1",
            )

        self.assertIn(BUDGET_UNIT, str(raised.exception))
        self.assertIn("cannot be discarded", str(raised.exception))
        self.assertTrue(marker.is_file())
        self.assertFalse(audit.exists())
        # It is verifiable, which is exactly why it is not discardable.
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(0, verify_from_environment(self._verify_environment(marker)))
        self.assertFalse(marker.exists())

    def test_approver_discard_keeps_the_audit_row_content_free(self):
        audit = self.work / "audit.log"
        marker = self.work / "verification.json"
        marker.write_text(json.dumps({
            "status": "pending", "tier": "the delegate prompt text",
            "model": "another prompt fragment", "tokens": "not a number",
            "request_id": "private brief",
        }) + "\n", encoding="utf-8")

        discarded = discard_unverifiable_verification(
            marker, audit, user="U1", channel="C1", thread="T1",
        )

        self.assertNotIn("tokens", discarded)
        log = audit.read_text(encoding="utf-8")
        self.assertNotIn("prompt", log)
        self.assertNotIn("private brief", log)
        self.assertIn("BUDGET_TOKENS:unrecorded", log)
        self.assertIn("STATUS:verification_discarded", log)

    @patch("governed_delegation.run_codex_delegate")
    def test_stage_allocation_exhaustion_preserves_thread_for_owner(self, run):
        # The runner only reports exhaustion once observed tokens reach the
        # scaled call limit, so the mock must overshoot that limit, not 45_000.
        expected_limit = math.ceil(45_000 * CODEX_GENERATION_FACTOR)
        provider_tokens = expected_limit + 500
        expected_charge = math.ceil(provider_tokens / CODEX_GENERATION_FACTOR)
        run.return_value = CodexDelegateResult(
            tokens=provider_tokens, raw_tokens=1_125_414, budget_exhausted=True,
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

        self.assertEqual(expected_limit, run.call_args.kwargs["token_limit"])
        self.assertLessEqual(expected_limit, provider_tokens)
        self.assertEqual(expected_charge, budget_status(budget)["used"])
        self.assertLess(expected_charge, DEFAULT_TOKEN_BUDGET)
        marker = self.work / "verification.json"
        self.assertEqual("allocation_exhausted", json.loads(marker.read_text())["status"])
        self.assertIn("STATUS:allocation_exhausted", (self.work / "audit.log").read_text())
        with self.assertRaisesRegex(DelegationError, "no thread-budget reset is required"):
            verify_from_environment(env)
        self.assertTrue(marker.exists())

    @patch("governed_delegation.run_codex_delegate")
    def test_over_budget_return_is_withheld(self, run):
        run.return_value = CodexDelegateResult(
            tokens=25, budget_exhausted=True,
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

    def _launch_environment(self, request, budget, *, provider="openai", extra=None):
        values = {
            "CARGO_CHIEF_ROOT": str(self.root),
            "CARGO_CHIEF_DELEGATION_REQUEST_FILE": str(request),
            "CARGO_CHIEF_IMPLEMENTATION_CLAIM_FILE": str(self.work / "claim.txt"),
            "CARGO_CHIEF_DELEGATION_BUDGET_FILE": str(budget),
            "CARGO_CHIEF_DELEGATE_PID_FILE": str(self.work / "pid"),
            "CARGO_CHIEF_DELEGATE_VERIFICATION_FILE": str(self.work / "verification.json"),
            "CARGO_CHIEF_AUDIT_LOG": str(self.work / "audit.log"),
            "CARGO_CHIEF_OWNER_PROVIDER": provider,
            "CARGO_CHIEF_OWNER_MODEL": (
                "gpt-5.6-sol" if provider == "openai" else "claude-opus-5[1m]"
            ),
            "CARGO_CHIEF_OWNER_EFFORT": "high",
            "CLAUDE_THREAD_TS": "T1", "CLAUDE_CHANNEL_ID": "C1",
            "CARGO_CHIEF_CURRENT_USER": "U1",
        }
        values.update(extra or {})
        return values

    def _bounded_request(self, planned_tokens=45_000):
        request = self.work / "delegation-request.json"
        request.write_text(json.dumps({
            "tier": "bounded", "prompt": "private brief", "mutation": False,
            "budget_unit": BUDGET_UNIT, "planned_tokens": planned_tokens,
        }))
        return request

    def test_generation_factor_default_and_bounds(self):
        # The constant is re-measured and raised through the PR gate, so every
        # numeric expectation is derived from it; only the non-numeric and
        # non-finite rejections are deliberate literals.
        self.assertEqual(CODEX_GENERATION_FACTOR, codex_generation_factor({}))
        narrowed = (MIN_CODEX_GENERATION_FACTOR + MAX_CODEX_GENERATION_FACTOR) / 2
        self.assertEqual(
            narrowed,
            codex_generation_factor(
                {"CARGO_CHIEF_CODEX_GENERATION_FACTOR": str(narrowed)}
            ),
        )
        rejected = (
            "",
            "not-a-number",
            "nan",
            "inf",
            str(MIN_CODEX_GENERATION_FACTOR - 0.1),
            str(MAX_CODEX_GENERATION_FACTOR + 0.1),
            str(-MAX_CODEX_GENERATION_FACTOR),
        )
        for value in rejected:
            with self.subTest(value=value):
                with self.assertRaises(DelegationError):
                    codex_generation_factor(
                        {"CARGO_CHIEF_CODEX_GENERATION_FACTOR": value}
                    )

    def test_generation_factor_constant_is_pinned(self):
        # The one place the constant's value is asserted. Every other
        # expectation derives from it, so a re-measurement is a deliberate,
        # reviewed two-line change here rather than a cascade of misleading
        # failures — and cannot be a silent one-line diff that doubles every
        # Codex ceiling and halves its charge against an approver-gated budget.
        # Update this assertion together with the constant.
        self.assertEqual(2.5, CODEX_GENERATION_FACTOR)

    def test_normalization_helpers_refuse_an_out_of_range_factor(self):
        # The helpers take a bare float now, so the bounds that used to hold by
        # construction have to be re-checked. A 0.0 reaching the charge raises
        # ZeroDivisionError, which main()'s DelegationError handler does not
        # catch: the launcher would exit with a traceback after the delegate had
        # spent tokens and before update_budget, so the spend is never charged.
        rejected = (
            0.0, 0, -1.0, MAX_CODEX_GENERATION_FACTOR + 0.1,
            float("inf"), float("nan"), "2.5", None, True,
        )
        for factor in rejected:
            with self.subTest(factor=factor):
                with self.assertRaises(DelegationError):
                    _provider_call_limit(45_000, "openai", factor)
                with self.assertRaises(DelegationError):
                    _normalized_charge(100_000, "openai", factor)
                # The claude path is not exempt: it shares the guard.
                with self.assertRaises(DelegationError):
                    _normalized_charge(100_000, "claude", factor)

    def test_generation_factor_override_can_only_narrow_the_grant(self):
        # The maximum equals the default, so no environment override can raise
        # the effective per-call ceiling or shrink the charge below the default.
        self.assertEqual(MIN_CODEX_GENERATION_FACTOR, 1.0)
        self.assertEqual(MAX_CODEX_GENERATION_FACTOR, CODEX_GENERATION_FACTOR)
        floor = codex_generation_factor({
            "CARGO_CHIEF_CODEX_GENERATION_FACTOR": str(MIN_CODEX_GENERATION_FACTOR),
        })
        self.assertEqual(
            math.ceil(45_000 * MIN_CODEX_GENERATION_FACTOR),
            _provider_call_limit(45_000, "openai", floor),
        )
        self.assertEqual(
            math.ceil(100_000 / MIN_CODEX_GENERATION_FACTOR),
            _normalized_charge(100_000, "openai", floor),
        )
        self.assertLessEqual(
            _provider_call_limit(45_000, "openai", floor),
            _provider_call_limit(45_000, "openai", CODEX_GENERATION_FACTOR),
        )
        with self.assertRaises(DelegationError) as raised:
            codex_generation_factor({
                "CARGO_CHIEF_CODEX_GENERATION_FACTOR": str(
                    MAX_CODEX_GENERATION_FACTOR + 0.1
                ),
            })
        message = str(raised.exception)
        self.assertIn(str(MIN_CODEX_GENERATION_FACTOR), message)
        self.assertIn(str(MAX_CODEX_GENERATION_FACTOR), message)

    def test_call_limit_and_charge_are_provider_normalized(self):
        factor = codex_generation_factor({})
        self.assertEqual(
            math.ceil(45_000 * factor), _provider_call_limit(45_000, "openai", factor)
        )
        self.assertEqual(45_000, _provider_call_limit(45_000, "claude", factor))
        self.assertEqual(
            math.ceil(100_000 / factor), _normalized_charge(100_000, "openai", factor)
        )
        self.assertEqual(100_000, _normalized_charge(100_000, "claude", factor))
        # Nonzero generated spend is never rounded down to a free call, and only
        # zero spend is free. Expressed as an exact ceil-of-division so that an
        # implementation returning `tokens` unchanged cannot satisfy it.
        for tokens in (1, 2):
            with self.subTest(tokens=tokens):
                self.assertEqual(
                    math.ceil(tokens / factor),
                    _normalized_charge(tokens, "openai", factor),
                )
                self.assertGreaterEqual(
                    _normalized_charge(tokens, "openai", factor), 1
                )
        self.assertEqual(0, _normalized_charge(0, "openai", factor))

    @patch("governed_delegation.run_codex_delegate")
    def test_codex_generation_tokens_are_charged_normalized(self, run):
        provider_tokens = 100_000
        expected_limit = math.ceil(45_000 * CODEX_GENERATION_FACTOR)
        expected_charge = math.ceil(provider_tokens / CODEX_GENERATION_FACTOR)
        run.return_value = CodexDelegateResult(
            texts=["delegate evidence"], tokens=provider_tokens, raw_tokens=1_125_414,
        )
        request = self._bounded_request()
        budget = self.work / "budget.json"
        values = self._launch_environment(request, budget)

        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(0, launch_from_environment(values))

        self.assertEqual(expected_limit, run.call_args.kwargs["token_limit"])
        self.assertEqual(expected_charge, budget_status(budget)["used"])
        marker = json.loads((self.work / "verification.json").read_text())
        self.assertEqual(expected_charge, marker["tokens"])
        self.assertEqual(provider_tokens, marker["provider_tokens"])
        self.assertEqual(1_125_414, marker["raw_tokens"])
        self.assertEqual(CODEX_GENERATION_FACTOR, marker["generation_factor"])
        audit = (self.work / "audit.log").read_text()
        self.assertIn(f"BUDGET_TOKENS:{expected_charge}", audit)
        self.assertIn(f"PROVIDER_TOKENS:{provider_tokens}", audit)
        self.assertIn(f"GENERATION_FACTOR:{CODEX_GENERATION_FACTOR}", audit)

    @patch("governed_delegation.run_codex_delegate")
    def test_narrowed_generation_factor_governs_limit_charge_marker_and_audit(self, run):
        # The launcher must apply one validated factor everywhere. A parallel
        # recomputation passes every test that leaves the variable unset or sets
        # an invalid value, because both leave the default in force on all four
        # surfaces; only a valid *narrowed* override separates them.
        narrowed = (MIN_CODEX_GENERATION_FACTOR + MAX_CODEX_GENERATION_FACTOR) / 2
        self.assertNotEqual(CODEX_GENERATION_FACTOR, narrowed)
        provider_tokens = 100_000
        run.return_value = CodexDelegateResult(
            texts=["delegate evidence"], tokens=provider_tokens, raw_tokens=1_125_414,
        )
        request = self._bounded_request()
        budget = self.work / "budget.json"
        values = self._launch_environment(request, budget, extra={
            "CARGO_CHIEF_CODEX_GENERATION_FACTOR": str(narrowed),
        })

        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(0, launch_from_environment(values))

        self.assertEqual(
            math.ceil(45_000 * narrowed), run.call_args.kwargs["token_limit"]
        )
        self.assertEqual(
            math.ceil(provider_tokens / narrowed), budget_status(budget)["used"]
        )
        marker = json.loads((self.work / "verification.json").read_text())
        self.assertEqual(narrowed, marker["generation_factor"])
        self.assertEqual(math.ceil(provider_tokens / narrowed), marker["tokens"])
        audit = (self.work / "audit.log").read_text()
        self.assertIn(f"GENERATION_FACTOR:{narrowed}", audit)
        self.assertIn(f"BUDGET_TOKENS:{math.ceil(provider_tokens / narrowed)}", audit)

    @patch("governed_delegation.run_claude_delegate")
    def test_claude_provider_budget_is_unnormalized(self, run):
        # A real Claude delegate that reached the 45,000 call limit would report
        # budget_exhausted, so the mocked count stays below it: this test asserts
        # a successful launch, which is only reachable under the limit.
        provider_tokens = 30_000
        run.return_value = DelegateResult(
            text="delegate evidence", tokens=provider_tokens, raw_tokens=1_125_414,
        )
        request = self._bounded_request()
        budget = self.work / "budget.json"
        values = self._launch_environment(request, budget, provider="claude")

        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(0, launch_from_environment(values))

        self.assertEqual(45_000, run.call_args.kwargs["token_limit"])
        self.assertEqual(provider_tokens, budget_status(budget)["used"])
        marker = json.loads((self.work / "verification.json").read_text())
        self.assertEqual(provider_tokens, marker["tokens"])
        self.assertEqual(provider_tokens, marker["provider_tokens"])
        self.assertEqual(1.0, marker["generation_factor"])
        audit = (self.work / "audit.log").read_text()
        self.assertIn(f"BUDGET_TOKENS:{provider_tokens}", audit)
        self.assertIn(f"PROVIDER_TOKENS:{provider_tokens}", audit)
        self.assertIn("GENERATION_FACTOR:1.0", audit)

    @patch("governed_delegation.run_codex_delegate")
    def test_invalid_generation_factor_refuses_before_spending(self, run):
        request = self._bounded_request()
        budget = self.work / "budget.json"
        values = self._launch_environment(request, budget, extra={
            "CARGO_CHIEF_CODEX_GENERATION_FACTOR": "8",
        })

        with self.assertRaisesRegex(DelegationError, "CODEX_GENERATION_FACTOR"):
            launch_from_environment(values)

        run.assert_not_called()
        self.assertEqual(0, budget_status(budget)["used"])

    @patch("governed_delegation.run_codex_delegate")
    @patch("governed_delegation.run_claude_delegate")
    def test_invalid_generation_factor_does_not_consume_the_one_shot_files(
        self, claude, codex
    ):
        # A purely environmental misconfiguration must not destroy the one-shot
        # request or implementation claim, and validating it only on the openai
        # path would make that depend on which provider the thread is on.
        for provider in ("openai", "claude"):
            with self.subTest(provider=provider):
                request = self._bounded_request()
                claim = self.work / "claim.txt"
                claim.write_text("/nonexistent/plan.md\n", encoding="utf-8")
                budget = self.work / "budget.json"
                values = self._launch_environment(
                    request, budget, provider=provider, extra={
                        "CARGO_CHIEF_CODEX_GENERATION_FACTOR": str(
                            MAX_CODEX_GENERATION_FACTOR + 0.1
                        ),
                    },
                )

                with self.assertRaisesRegex(
                    DelegationError, "CODEX_GENERATION_FACTOR"
                ):
                    launch_from_environment(values)

                self.assertTrue(request.is_file())
                self.assertTrue(claim.is_file())
                self.assertEqual(0, budget_status(budget)["used"])
        claude.assert_not_called()
        codex.assert_not_called()

    @patch("governed_delegation.run_codex_delegate")
    def test_superseded_generation_unit_requires_named_approver_reset(self, run):
        budget = self.work / "budget.json"
        budget.write_text(
            json.dumps({
                "limit": 250_000, "used": 45_000, "unit": "generation_tokens_v1",
            }) + "\n",
            encoding="utf-8",
        )
        self.assertEqual("generation_tokens_v1", budget_status(budget)["unit"])
        with self.assertRaises(DelegationError) as raised:
            update_budget(budget, add_tokens=1)
        # Both refusals must name the actual superseded unit; the previous text
        # called a generation-token file "legacy raw-token" accounting.
        self.assertIn("generation_tokens_v1", str(raised.exception))
        self.assertNotIn("raw-token", str(raised.exception))

        request = self._bounded_request()
        values = self._launch_environment(request, budget)
        with self.assertRaises(DelegationError) as launched:
            launch_from_environment(values)
        self.assertIn("generation_tokens_v1", str(launched.exception))
        self.assertNotIn("raw-token", str(launched.exception))
        self.assertIn("must be reset by a named approver", str(launched.exception))
        # This reset preserves an approver-set limit, so it must not carry the
        # raw-token warning about returning the limit to the default.
        self.assertIn("keeps the current limit", str(launched.exception))
        run.assert_not_called()

        reset = update_budget(budget, reset=True)
        self.assertEqual({"limit": 250_000, "used": 0, "unit": BUDGET_UNIT}, reset)

    def test_legacy_raw_token_budget_refusal_names_its_own_unit(self):
        budget = self.work / "budget.json"
        budget.write_text(
            json.dumps({"limit": 250_000, "used": 45_000}) + "\n", encoding="utf-8",
        )
        self.assertEqual(LEGACY_BUDGET_UNIT, budget_status(budget)["unit"])

        with self.assertRaises(DelegationError) as raised:
            update_budget(budget, add_tokens=1)

        message = str(raised.exception)
        self.assertIn(LEGACY_BUDGET_UNIT, message)
        self.assertIn("must be reset by a named approver", message)
        # An approver who ran `delegation budget set` must be told that this
        # reset, unlike the superseded-generation-unit reset, drops the ceiling.
        self.assertIn(str(DEFAULT_TOKEN_BUDGET), message)
        self.assertIn("default", message)

    def test_unit_migration_preserves_an_approver_set_limit(self):
        budget = self.work / "budget.json"
        budget.write_text(
            json.dumps({
                "limit": 500_000, "used": 120_000, "unit": "generation_tokens_v1",
            }) + "\n",
            encoding="utf-8",
        )

        reset = update_budget(budget, reset=True)

        # `used` cannot be reinterpreted across units and is discarded; the
        # approver-set limit is a separate decision and must survive.
        self.assertEqual({"limit": 500_000, "used": 0, "unit": BUDGET_UNIT}, reset)
        self.assertEqual(500_000, budget_status(budget)["limit"])

    def test_request_declaring_a_superseded_unit_names_the_skew(self):
        request = self.work / "delegation-request.json"
        request.write_text(json.dumps({
            "tier": "bounded", "prompt": "work", "mutation": False,
            "budget_unit": SUPERSEDED_BUDGET_UNITS[0], "planned_tokens": 45_000,
        }))

        with self.assertRaises(DelegationError) as raised:
            load_request(request)

        message = str(raised.exception)
        self.assertIn(SUPERSEDED_BUDGET_UNITS[0], message)
        self.assertIn(BUDGET_UNIT, message)
        self.assertIn("agent-kit", message)
        self.assertNotIn("budget contract is invalid", message)
        self.assertFalse(request.exists())

    @patch("governed_delegation.run_codex_delegate")
    def test_codex_overshoot_charges_normalized_spend_and_withholds(self, run):
        provider_tokens = math.ceil(45_000 * CODEX_GENERATION_FACTOR)
        expected_charge = math.ceil(provider_tokens / CODEX_GENERATION_FACTOR)
        run.return_value = CodexDelegateResult(
            texts=["partial evidence"], tokens=provider_tokens, raw_tokens=1_125_414,
            budget_exhausted=True,
        )
        request = self._bounded_request()
        budget = self.work / "budget.json"
        values = self._launch_environment(request, budget)

        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            with self.assertRaisesRegex(DelegationError, "return withheld"):
                launch_from_environment(values)

        self.assertEqual("", output.getvalue())
        self.assertEqual(expected_charge, budget_status(budget)["used"])
        marker = json.loads((self.work / "verification.json").read_text())
        self.assertEqual("allocation_exhausted", marker["status"])
        self.assertEqual(expected_charge, marker["tokens"])
        self.assertEqual(provider_tokens, marker["provider_tokens"])


if __name__ == "__main__":
    unittest.main()
