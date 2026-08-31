import json
from pathlib import Path
import subprocess
import tempfile
import unittest

from cargo_chief_safety import (
    AuthorityPolicy,
    GENERATED_MARKER,
    RoomPolicy,
    RuntimePolicy,
    SafetyError,
    build_claude_command,
    find_workspace_root,
    format_audit_metadata,
    preflight_room,
    resolve_room_policy,
    validate_secret_env_path,
    write_private_json,
)


class AuthorityPolicyTest(unittest.TestCase):
    def test_refuses_empty_authorized_users(self):
        with self.assertRaisesRegex(SafetyError, "AUTHORIZED_USERS"):
            AuthorityPolicy.from_env({"CARGO_CHIEF_APPROVERS": "U1"})

    def test_refuses_empty_approvers(self):
        with self.assertRaisesRegex(SafetyError, "CARGO_CHIEF_APPROVERS"):
            AuthorityPolicy.from_env({"AUTHORIZED_USERS": "U1"})

    def test_approver_must_be_authorized(self):
        with self.assertRaisesRegex(SafetyError, "also be authorized"):
            AuthorityPolicy.from_env({
                "AUTHORIZED_USERS": "U1", "CARGO_CHIEF_APPROVERS": "U2",
            })

    def test_checks_current_sender(self):
        policy = AuthorityPolicy.from_env({
            "AUTHORIZED_USERS": "U1,U2", "CARGO_CHIEF_APPROVERS": "U2",
        })
        self.assertTrue(policy.allows("U1"))
        self.assertFalse(policy.can_approve("U1"))
        self.assertTrue(policy.can_approve("U2"))
        self.assertFalse(policy.allows("U3"))


class RoomPolicyTest(unittest.TestCase):
    models = ["claude-opus-4-8[1m]", "claude-sonnet-5"]

    def config(self, channel, **extra):
        value = {"models": self.models, "channels": {"C1": channel}}
        value.update(extra)
        return value

    def entry(self, **overrides):
        value = {
            "root": "/workspace",
            "permission_mode": "auto",
            "autonomous_overlay": "agent-kit/modes/autonomous.md",
            "private_escalation_channel": "DAPPROVER",
            "backend": "claude",
            "model": "claude-opus-4-8[1m]",
            "effort": "high",
        }
        value.update(overrides)
        return value

    def test_requires_complete_room_entry(self):
        with self.assertRaisesRegex(SafetyError, "missing"):
            resolve_room_policy(self.config({"model": "claude-x"}), "C1", "U1")

    def test_rejects_bypass_permissions_and_codex(self):
        with self.assertRaisesRegex(SafetyError, "permission_mode"):
            resolve_room_policy(
                self.config(self.entry(permission_mode="bypassPermissions")), "C1", "U1"
            )
        with self.assertRaisesRegex(SafetyError, "not allowed"):
            resolve_room_policy(
                self.config(self.entry(backend="codex")), "C1", "U1"
            )

    def test_rejects_model_outside_allowlist(self):
        with self.assertRaisesRegex(SafetyError, "not allowlisted"):
            resolve_room_policy(self.config(self.entry(model="claude-unknown")), "C1", "U1")

    def test_rejects_invalid_private_escalation_channel(self):
        with self.assertRaisesRegex(SafetyError, "invalid private escalation channel"):
            resolve_room_policy(
                self.config(self.entry(private_escalation_channel="D1; unsafe")),
                "C1",
                "U1",
            )

    def test_dm_override_is_resolved_for_current_sender(self):
        config = {
            "models": self.models,
            "channels": {"D1": self.entry(model="claude-sonnet-5")},
            "dm_users": {"U1": {"model": "claude-opus-4-8[1m]"}},
        }
        self.assertEqual(resolve_room_policy(config, "D1", "U1").model, "claude-opus-4-8[1m]")


class RuntimePolicyTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.source = self.base / "workspace" / "claude-home-base"
        self.source.mkdir(parents=True)
        (self.base / "workspace" / "agent-kit").mkdir()
        (self.base / "workspace" / "docs").mkdir()
        self.workspace = (self.base / "workspace").resolve()
        self.source = self.source.resolve()
        self.runtime = self.base / "runtime"

    def tearDown(self):
        self.temp.cleanup()

    def env(self, **overrides):
        value = {
            "CARGO_CHIEF_RUNTIME_DIR": str(self.runtime),
            "CLAUDE_TIMEOUT": "600",
            "MAX_LIVE_SESSIONS": "1",
            "ENABLE_FILE_TRANSFER": "false",
            "ENABLE_TRANSCRIPT_SEARCH": "false",
        }
        value.update(overrides)
        return value

    def test_prepares_private_runtime_tree(self):
        policy = RuntimePolicy.from_env(
            self.env(), source_dir=self.source, workspace_root=self.workspace, home=self.base
        )
        policy.prepare()
        for directory in (policy.root, policy.log_dir, policy.state_dir, policy.temp_dir):
            self.assertTrue(directory.is_dir())
            self.assertEqual(0o700, directory.stat().st_mode & 0o777)

    def test_requires_private_external_credential_file(self):
        credentials = self.base / "credentials.env"
        credentials.write_text("PLACEHOLDER=value\n")
        credentials.chmod(0o600)
        self.assertEqual(
            credentials,
            validate_secret_env_path(credentials, workspace_root=self.workspace),
        )
        credentials.chmod(0o644)
        with self.assertRaisesRegex(SafetyError, "mode 600"):
            validate_secret_env_path(credentials, workspace_root=self.workspace)
        inside = self.source / "secrets.env"
        inside.write_text("PLACEHOLDER=value\n")
        inside.chmod(0o600)
        with self.assertRaisesRegex(SafetyError, "outside the Cargo Chief workspace"):
            validate_secret_env_path(inside, workspace_root=self.workspace)

    def test_finds_workspace_from_canonical_and_worktree_checkouts(self):
        self.assertEqual(self.workspace, find_workspace_root(self.source))
        worktree_source = self.workspace / "worktrees/task/claude-home-base"
        worktree_source.mkdir(parents=True)
        self.assertEqual(self.workspace, find_workspace_root(worktree_source))

    def test_rejects_runtime_inside_checkout(self):
        env = self.env(CARGO_CHIEF_RUNTIME_DIR=str(self.source / "runtime"))
        with self.assertRaisesRegex(SafetyError, "outside the Cargo Chief workspace"):
            RuntimePolicy.from_env(
                env, source_dir=self.source, workspace_root=self.workspace, home=self.base
            )

    def test_rejects_long_timeout_or_parallel_sessions(self):
        with self.assertRaisesRegex(SafetyError, "CLAUDE_TIMEOUT"):
            RuntimePolicy.from_env(self.env(CLAUDE_TIMEOUT="901"), source_dir=self.source, workspace_root=self.workspace, home=self.base)
        with self.assertRaisesRegex(SafetyError, "MAX_LIVE_SESSIONS"):
            RuntimePolicy.from_env(self.env(MAX_LIVE_SESSIONS="2"), source_dir=self.source, workspace_root=self.workspace, home=self.base)

    def test_rejects_file_transfer_and_transcript_search(self):
        with self.assertRaisesRegex(SafetyError, "ENABLE_FILE_TRANSFER"):
            RuntimePolicy.from_env(self.env(ENABLE_FILE_TRANSFER="true"), source_dir=self.source, workspace_root=self.workspace, home=self.base)
        with self.assertRaisesRegex(SafetyError, "ENABLE_TRANSCRIPT_SEARCH"):
            RuntimePolicy.from_env(self.env(ENABLE_TRANSCRIPT_SEARCH="true"), source_dir=self.source, workspace_root=self.workspace, home=self.base)

    def test_cleans_only_old_regular_temp_files(self):
        policy = RuntimePolicy.from_env(self.env(), source_dir=self.source, workspace_root=self.workspace, home=self.base)
        policy.prepare()
        old = policy.temp_dir / "old.stderr"
        recent = policy.temp_dir / "recent.stderr"
        old.write_text("old")
        recent.write_text("recent")
        old.touch()
        recent.touch()
        old_time = 100.0
        import os
        os.utime(old, (old_time, old_time))
        removed = policy.cleanup_temp(older_than_seconds=50, now=200.0)
        self.assertEqual(1, removed)
        self.assertFalse(old.exists())
        self.assertTrue(recent.exists())

    def test_state_json_is_private(self):
        policy = RuntimePolicy.from_env(self.env(), source_dir=self.source, workspace_root=self.workspace, home=self.base)
        policy.prepare()
        state = policy.state_dir / "sessions.json"
        write_private_json(state, {"thread": "session"})
        self.assertEqual(0o600, state.stat().st_mode & 0o777)
        self.assertEqual({"thread": "session"}, json.loads(state.read_text()))

    def test_audit_formatter_accepts_metadata_not_content(self):
        record = format_audit_metadata(
            "INTERACTION", user="U1", channel="C1", thread="T1",
            message_length=17, response_length=29, session_id="S1", duration=1.25,
        )
        self.assertEqual(
            "INTERACTION | USER:U1 | CHANNEL:C1 | THREAD:T1 | MSG_LEN:17 "
            "| RESP_LEN:29 | SESSION:S1 | DURATION:1.2s",
            record,
        )


class PreflightTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.kit = self.root / "agent-kit"
        self.docs = self.root / "docs"
        for repo in (self.kit, self.docs):
            repo.mkdir()
            subprocess.run(["git", "init", "-b", "master", str(repo)], check=True, capture_output=True)
            subprocess.run(["git", "-C", str(repo), "config", "user.email", "test@example.com"], check=True)
            subprocess.run(["git", "-C", str(repo), "config", "user.name", "Test"], check=True)
        (self.kit / "roles" / "eng").mkdir(parents=True)
        (self.kit / "modes").mkdir()
        (self.kit / "AGENTS.base.md").write_text("base\n")
        (self.kit / "roles" / "eng" / "ROLE.md").write_text("role: eng\n")
        self.overlay = self.kit / "modes" / "autonomous.md"
        self.overlay.write_text("autonomous\n")
        (self.docs / "README.md").write_text("docs fixture\n")
        (self.root / "AGENTS.md").write_text(
            f"base\n\nrole: eng\n\n{GENERATED_MARKER}\n"
        )
        (self.root / "CLAUDE.md").symlink_to("AGENTS.md")
        for repo in (self.kit, self.docs):
            subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
            subprocess.run(["git", "-C", str(repo), "commit", "-m", "fixture"], check=True, capture_output=True)
        self.policy = RoomPolicy(
            root=self.root, permission_mode="auto", overlay=self.overlay,
            escalation_channel="D1", backend="claude", model="claude-opus-4-8[1m]",
            effort="high", role="eng",
        )

    def tearDown(self):
        self.temp.cleanup()

    def test_accepts_clean_released_inputs(self):
        warnings = preflight_room(self.policy)
        self.assertEqual(2, len(warnings))  # fixtures intentionally have no origin/master refs

    def test_rejects_dirty_clone(self):
        (self.docs / "dirty.txt").write_text("dirty")
        with self.assertRaisesRegex(SafetyError, "docs clone is not clean"):
            preflight_room(self.policy)

    def test_rejects_parked_shared_clone_branch(self):
        subprocess.run(["git", "-C", str(self.kit), "switch", "-c", "feature"], check=True, capture_output=True)
        with self.assertRaisesRegex(SafetyError, "expected master"):
            preflight_room(self.policy)

    def test_rejects_stale_generated_instructions(self):
        (self.root / "AGENTS.md").write_text(f"old\n{GENERATED_MARKER}\n")
        with self.assertRaisesRegex(SafetyError, "AGENTS.md is stale"):
            preflight_room(self.policy)

    def test_rejects_overlay_replaced_by_untracked_file(self):
        subprocess.run(["git", "-C", str(self.kit), "rm", "modes/autonomous.md"], check=True, capture_output=True)
        self.overlay.parent.mkdir(exist_ok=True)
        self.overlay.write_text("replacement\n")
        with self.assertRaisesRegex(SafetyError, "agent-kit clone is not clean"):
            preflight_room(self.policy)

    def test_rejects_symlinked_overlay(self):
        target = self.root / "overlay-copy.md"
        target.write_text("copy\n")
        self.overlay.unlink()
        self.overlay.symlink_to(target)
        with self.assertRaisesRegex(SafetyError, "agent-kit clone is not clean"):
            preflight_room(self.policy)

    def test_rejects_real_claude_file(self):
        (self.root / "CLAUDE.md").unlink()
        (self.root / "CLAUDE.md").write_text("AGENTS.md\n")
        with self.assertRaisesRegex(SafetyError, "symlink"):
            preflight_room(self.policy)

    def test_command_is_explicit_and_never_bypasses_permissions(self):
        command = build_claude_command(
            self.policy,
            initial_prompt="context",
            transport_python=Path("/venv/bin/python"),
            transport_script=Path("/home-base/bot.py"),
            escalation_message_file=Path("/workspace/work/escalation.txt"),
            model_prompt="room prompt",
            session_id="session-1",
        )
        self.assertIn("auto", command)
        self.assertNotIn("bypassPermissions", command)
        self.assertEqual("claude-opus-4-8[1m]", command[command.index("--model") + 1])
        appended = command[command.index("--append-system-prompt") + 1]
        self.assertTrue(appended.startswith("autonomous\n\nCargo Chief harness routing:"))
        self.assertIn("private escalation route is Slack channel D1", appended)
        self.assertIn(
            "/venv/bin/python /home-base/bot.py --escalate",
            appended,
        )
        self.assertIn("/workspace/work/escalation.txt", appended)
        self.assertNotIn("--channel D1", appended)
        self.assertTrue(appended.endswith("\n\nroom prompt"))
        self.assertEqual(["--resume", "session-1"], command[-2:])


if __name__ == "__main__":
    unittest.main()
