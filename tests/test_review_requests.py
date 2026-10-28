import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).parents[1]))

from cargo_chief_safety import SafetyError
from review_requests import (
    BUSY_REPLY,
    CAP_REPLY,
    CONFIG_REPLY,
    HUMAN,
    IGNORE,
    LOOKUP_FAILED_REPLY,
    NOT_OPEN_REPLY,
    OWN_PR_REPLY,
    PEER_BOT,
    PROVIDER_REPLY,
    USAGE_HINT,
    PullRequestInfo,
    ReviewCapStore,
    ReviewGuardError,
    ReviewRequestHandler,
    ThreadClaims,
    build_review_task,
    classify_sender,
    format_review_audit,
    lookup_pr,
    normalize_login,
    pr_urls,
    single_pr_url,
)

PR = "https://github.com/Cargo-Chief/c4/pull/123"


class PrUrlTest(unittest.TestCase):
    def test_plain_link(self):
        self.assertEqual(PR, single_pr_url(f"<@ULISA> please review {PR}"))

    def test_slack_angle_wrapping(self):
        self.assertEqual(PR, single_pr_url(f"<@ULISA> review <{PR}>"))

    def test_slack_label_wrapping_counts_once(self):
        self.assertEqual(PR, single_pr_url(f"review <{PR}|{PR}>"))
        self.assertEqual(PR, single_pr_url(f"review <{PR}|c4#123>"))

    def test_trailing_path_and_punctuation_normalize(self):
        self.assertEqual(PR, single_pr_url(f"review {PR}/files."))

    def test_no_link(self):
        self.assertIsNone(single_pr_url("<@ULISA> please review my PR"))

    def test_two_links_are_refused(self):
        text = f"review {PR} and https://github.com/Cargo-Chief/booking-agent/pull/9"
        self.assertEqual(2, len(pr_urls(text)))
        self.assertIsNone(single_pr_url(text))

    def test_other_org_and_lookalikes_do_not_match(self):
        for text in (
            "review https://github.com/other-org/c4/pull/1",
            "review https://github.com/Cargo-Chief-evil/c4/pull/1",
            "review https://github.com.evil.test/Cargo-Chief/c4/pull/1",
            "review https://evil.test/?u=https://github.com/Cargo-Chief/c4/pull/1",
            "review https://github.com/Cargo-Chief/c4/issues/1",
        ):
            with self.subTest(text=text):
                self.assertIsNone(single_pr_url(text))


class SenderTest(unittest.TestCase):
    def classify(self, event):
        return classify_sender(event, bot_user_id="ULISA", peer_bot_ids={"UNED"})

    def test_any_human_member(self):
        self.assertEqual(HUMAN, self.classify({"user": "USTRANGER"}))

    def test_listed_peer_bot(self):
        self.assertEqual(PEER_BOT, self.classify({"user": "UNED", "bot_id": "BNED"}))

    def test_unlisted_bot_is_ignored(self):
        self.assertEqual(IGNORE, self.classify({"user": "UOTHER", "bot_id": "BOTHER"}))
        self.assertEqual(IGNORE, self.classify({"user": "UOTHER", "subtype": "bot_message"}))

    def test_self_and_missing_sender_are_ignored(self):
        self.assertEqual(IGNORE, self.classify({"user": "ULISA", "bot_id": "BLISA"}))
        self.assertEqual(IGNORE, self.classify({"user": "ULISA"}))
        self.assertEqual(IGNORE, self.classify({"bot_id": "BNED"}))


class LoginTest(unittest.TestCase):
    def test_app_and_bot_spellings_match(self):
        self.assertEqual(normalize_login("app/lisa-cargochief"), normalize_login("lisa-cargochief[bot]"))
        self.assertNotEqual(normalize_login("kfranzenCC"), normalize_login("lisa-cargochief[bot]"))


class LookupTest(unittest.TestCase):
    def run_lookup(self, result=None, error=None):
        calls = []

        def runner(command, **kwargs):
            calls.append((command, kwargs))
            if error:
                raise error
            return result

        info = lookup_pr(
            PR, root=Path("/root"), child_env={"CARGO_CHIEF_REVIEW_ONLY": "1"}, python="/py",
            runner=runner,
        )
        return info, calls

    def test_reads_author_and_state_through_the_app_wrapper(self):
        result = subprocess.CompletedProcess([], 0, json.dumps({
            "author": {"login": "app/ned-cargochief", "is_bot": True}, "state": "open",
        }), "")
        info, calls = self.run_lookup(result)
        self.assertEqual(PullRequestInfo("app/ned-cargochief", "OPEN"), info)
        command, kwargs = calls[0]
        self.assertEqual(
            ["/py", "/root/docs/scripts/ops/with_github_app_token.py", "--",
             "gh", "pr", "view", PR, "--json", "author,state"],
            command,
        )
        self.assertEqual({"CARGO_CHIEF_REVIEW_ONLY": "1"}, kwargs["env"])
        self.assertEqual(60, kwargs["timeout"])

    def test_failures_fail_closed(self):
        for result, error in (
            (subprocess.CompletedProcess([], 1, "", "GitHub App authentication failed"), None),
            (subprocess.CompletedProcess([], 0, "not json", ""), None),
            (subprocess.CompletedProcess([], 0, json.dumps({"state": "OPEN"}), ""), None),
            (None, subprocess.TimeoutExpired("gh", 60)),
            (None, OSError("missing")),
        ):
            with self.subTest(result=result, error=error):
                with self.assertRaises(ReviewGuardError):
                    self.run_lookup(result, error)


class CapStoreTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.now = 1000.0
        self.store = ReviewCapStore(Path(self.temp.name) / "review-requests.json", clock=lambda: self.now)

    def tearDown(self):
        self.temp.cleanup()

    def test_cap_sends_one_notice_then_silence(self):
        self.assertEqual(["accepted"] * 3, [self.store.admit(PR, 3) for _ in range(3)])
        self.assertTrue(self.store.exhausted(PR, 3))
        self.assertEqual("notice", self.store.admit(PR, 3))
        self.assertEqual("silent", self.store.admit(PR, 3))
        self.assertFalse(self.store.exhausted("https://github.com/Cargo-Chief/c4/pull/124", 3))

    def test_window_rolls_and_persists_privately(self):
        for _ in range(3):
            self.store.admit(PR, 3)
        self.store.admit(PR, 3)
        reloaded = ReviewCapStore(self.store.path, clock=lambda: self.now)
        self.assertEqual("silent", reloaded.admit(PR, 3))
        self.assertEqual(0o600, self.store.path.stat().st_mode & 0o777)
        self.now += 24 * 3600 + 1
        self.assertFalse(self.store.exhausted(PR, 3))
        self.assertEqual("accepted", self.store.admit(PR, 3))

    def test_corrupt_file_starts_empty(self):
        self.store.path.write_text("{not json")
        self.assertEqual("accepted", self.store.admit(PR, 3))


class AuditAndTaskTest(unittest.TestCase):
    def test_audit_line_carries_no_message_text(self):
        line = format_review_audit(user="U1", channel="C1", thread="1.2", pr=PR, outcome="accepted")
        self.assertEqual(
            f"REVIEW_REQUEST | USER:U1 | CHANNEL:C1 | THREAD:1.2 | PR:{PR} | OUTCOME:accepted", line,
        )
        self.assertIn("PR:none", format_review_audit(
            user="U1", channel="C1", thread="1.2", pr=None, outcome="refused_no_pr",
        ))

    def test_task_is_fixed_and_names_the_pr(self):
        task = build_review_task(PR)
        self.assertIn(f"cc-code-review high --comment {PR}", task)
        self.assertIn("untrusted", task)


class HandlerTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.config = {"peer_review": {
            "self_github_login": "lisa-cargochief[bot]", "peer_bot_user_ids": ["UNED"],
            "max_reviews_per_pr": 2,
        }}
        self.posts = []
        self.audits = []
        self.turns = []
        self.controls = []
        self.lookups = []
        self.info = PullRequestInfo("kfranzenCC", "OPEN")
        self.lookup_error = None
        self.running = False
        self.provider_down = False
        self.room_error = None
        self.authorized = {"UKIRK"}
        self.control_handled = False
        self.claims = ThreadClaims()
        self.handler = ReviewRequestHandler(
            bot_user_id="ULISA",
            caps=ReviewCapStore(Path(self.temp.name) / "caps.json"),
            claims=self.claims,
            load_config=lambda: self.config,
            post=lambda channel, thread, text: self.posts.append((channel, thread, text)),
            audit=self.audits.append,
            is_authorized=lambda user: user in self.authorized,
            run_control_command=self.control,
            turn_running=lambda thread: self.running,
            provider_unavailable=lambda thread: self.provider_down,
            room_root=self.room_root,
            lookup=self.lookup,
            start_turn=lambda event, pr: self.turns.append((event["user"], pr)),
            spawn=lambda target, *args: target(*args),
        )

    def tearDown(self):
        self.temp.cleanup()

    def control(self, event):
        self.controls.append(event["user"])
        return self.control_handled

    def room_root(self, channel, user):
        if self.room_error:
            raise self.room_error
        return Path("/root")

    def lookup(self, pr, root):
        self.lookups.append(pr)
        if self.lookup_error:
            raise self.lookup_error
        return self.info

    def mention(self, user="USTRANGER", text=f"<@ULISA> review {PR}", **extra):
        event = {"user": user, "channel": "CREVIEWS", "ts": "1.1", "text": text}
        event.update(extra)
        self.handler.intake(event)

    def outcomes(self):
        return [line.rsplit("OUTCOME:", 1)[1] for line in self.audits]

    def test_unauthorized_human_is_accepted(self):
        self.mention()
        self.assertEqual([("USTRANGER", PR)], self.turns)
        self.assertEqual(["accepted"], self.outcomes())
        self.assertEqual([], self.controls)

    def test_listed_peer_bot_is_accepted(self):
        self.mention(user="UNED", bot_id="BNED")
        self.assertEqual([("UNED", PR)], self.turns)

    def test_unlisted_bot_and_self_are_silently_ignored(self):
        self.mention(user="UOTHER", bot_id="BOTHER")
        self.mention(user="ULISA", bot_id="BLISA")
        self.assertEqual(([], [], []), (self.turns, self.posts, self.audits))

    def test_control_commands_only_for_authorized_humans(self):
        self.control_handled = True
        self.mention(user="USTRANGER", text="<@ULISA> stop")
        self.mention(user="UNED", bot_id="BNED", text="<@ULISA> stop")
        self.assertEqual([], self.controls)
        self.mention(user="UKIRK", text="<@ULISA> stop")
        self.assertEqual(["UKIRK"], self.controls)

    def test_missing_link_hints_humans_only(self):
        self.mention(text="<@ULISA> review please")
        self.mention(user="UNED", bot_id="BNED", text="<@ULISA> review please")
        self.mention(text=f"<@ULISA> {PR} and https://github.com/Cargo-Chief/c4/pull/9")
        self.assertEqual([USAGE_HINT, USAGE_HINT], [post[2] for post in self.posts])
        self.assertEqual(["refused_no_pr"] * 3, self.outcomes())
        self.assertEqual([], self.turns)

    def test_invalid_config_fails_closed(self):
        self.config = {"peer_review": {"peer_bot_user_ids": []}}
        self.mention()
        self.mention(user="UNED", bot_id="BNED")
        self.assertEqual([CONFIG_REPLY], [post[2] for post in self.posts])
        self.assertEqual(["refused_config"], self.outcomes())
        self.assertEqual([], self.turns)

    def test_room_that_is_not_review_only_is_refused(self):
        self.room_error = SafetyError("not review-only")
        self.mention()
        self.assertEqual(["refused_room"], self.outcomes())
        self.assertEqual([], self.turns)

    def test_own_pr_is_refused(self):
        self.info = PullRequestInfo("app/lisa-cargochief", "OPEN")
        self.mention(user="UNED", bot_id="BNED")
        self.assertEqual([OWN_PR_REPLY], [post[2] for post in self.posts])
        self.assertEqual(["refused_own_pr"], self.outcomes())
        self.assertEqual([], self.turns)

    def test_closed_or_merged_pr_is_refused(self):
        for state in ("CLOSED", "MERGED"):
            self.info = PullRequestInfo("kfranzenCC", state)
            self.mention()
        self.assertEqual([NOT_OPEN_REPLY] * 2, [post[2] for post in self.posts])
        self.assertEqual(["refused_state"] * 2, self.outcomes())

    def test_lookup_failure_fails_closed(self):
        self.lookup_error = ReviewGuardError("PR lookup exited 1")
        self.mention()
        self.assertEqual([LOOKUP_FAILED_REPLY], [post[2] for post in self.posts])
        self.assertEqual(["refused_lookup"], self.outcomes())
        self.assertEqual([], self.turns)

    def test_cap_gives_one_notice_then_silence_without_lookups(self):
        for _ in range(5):
            self.mention()
        self.assertEqual(2, len(self.turns))
        self.assertEqual([CAP_REPLY], [post[2] for post in self.posts])
        self.assertEqual(["accepted", "accepted", "refused_cap", "refused_cap", "refused_cap"],
                         self.outcomes())
        self.assertEqual(2, len(self.lookups))

    def test_busy_thread_is_refused_and_never_piped(self):
        self.running = True
        self.mention(thread_ts="0.9")
        self.assertEqual([("CREVIEWS", "0.9", BUSY_REPLY)], self.posts)
        self.assertEqual(["refused_busy"], self.outcomes())
        self.assertEqual([], self.turns)

    def test_claimed_thread_is_busy_until_released(self):
        self.assertTrue(self.claims.claim("0.9"))
        self.mention(thread_ts="0.9")
        self.assertEqual(["refused_busy"], self.outcomes())
        self.claims.release("0.9")
        self.mention(thread_ts="0.9")
        self.assertEqual(["refused_busy", "accepted"], self.outcomes())
        self.assertTrue(self.claims.claim("0.9"), "the admission released its claim")

    def test_provider_fallback_refuses_reviews(self):
        self.provider_down = True
        self.mention()
        self.assertEqual([PROVIDER_REPLY], [post[2] for post in self.posts])
        self.assertEqual(["refused_provider"], self.outcomes())


if __name__ == "__main__":
    unittest.main()
