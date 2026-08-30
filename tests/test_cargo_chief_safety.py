import json
from pathlib import Path
import subprocess
import tempfile
import unittest

from cargo_chief_safety import (
    AuthorityPolicy,
    GENERATED_MARKER,
    RoomPolicy,
    SafetyError,
    build_claude_command,
    preflight_room,
    resolve_room_policy,
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

    def test_dm_override_is_resolved_for_current_sender(self):
        config = {
            "models": self.models,
            "channels": {"D1": self.entry(model="claude-sonnet-5")},
            "dm_users": {"U1": {"model": "claude-opus-4-8[1m]"}},
        }
        self.assertEqual(resolve_room_policy(config, "D1", "U1").model, "claude-opus-4-8[1m]")


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
            self.policy, initial_prompt="context", model_prompt="room prompt", session_id="session-1"
        )
        self.assertIn("auto", command)
        self.assertNotIn("bypassPermissions", command)
        self.assertEqual("claude-opus-4-8[1m]", command[command.index("--model") + 1])
        appended = command[command.index("--append-system-prompt") + 1]
        self.assertEqual("autonomous\n\nroom prompt", appended)
        self.assertEqual(["--resume", "session-1"], command[-2:])


if __name__ == "__main__":
    unittest.main()
