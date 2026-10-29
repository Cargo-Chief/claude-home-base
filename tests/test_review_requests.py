import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).parents[1]))

from cargo_chief_safety import REVIEW_ONLY_DENY_RULES, RoomPolicy, SafetyError
from review_requests import (
    REVIEW_POST_SCRIPT,
    REVIEW_DONE_MARKER,
    BUSY_REPLY,
    CAP_REPLY,
    CAP_STORE_REPLY,
    CapStoreError,
    ERROR_REPLY,
    RecentMentions,
    RequestThrottle,
    THROTTLE_REPLY,
    WRAPPER_REPLY,
    CONFIG_REPLY,
    HUMAN,
    IGNORE,
    LOOKUP_FAILED_REPLY,
    NOT_OPEN_REPLY,
    OWN_PR_REPLY,
    PeerRequestError,
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
    request_peer_review,
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

    def test_repo_case_variants_share_one_key(self):
        self.assertEqual(PR, single_pr_url("review https://github.com/cargo-chief/C4/pull/0123"))

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

    def test_unreadable_record_fails_closed_instead_of_resetting(self):
        for content in (b"{not json", b"[1, 2]", b"\xff\xfe"):
            with self.subTest(content=content):
                self.store.path.write_bytes(content)
                with self.assertRaises(CapStoreError):
                    self.store.exhausted(PR, 3)
                with self.assertRaises(CapStoreError):
                    self.store.admit(PR, 3)
                self.assertEqual(content, self.store.path.read_bytes())


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
        task = build_review_task(PR, Path("/root"))
        self.assertIn(f"cc-code-review high {PR}` without `--comment`", task)
        self.assertIn("untrusted", task)
        self.assertIn(f"line containing only `{REVIEW_DONE_MARKER}`", task)

    def test_task_posts_through_review_post_with_a_verdict(self):
        task = build_review_task(PR, Path("/root"))
        post = f"python3 /root/docs/scripts/ops/with_github_app_token.py -- python3 {REVIEW_POST_SCRIPT}"
        self.assertTrue(REVIEW_POST_SCRIPT.is_absolute())
        self.assertIn(f"`{post} submit {PR} --review", task)
        self.assertIn(f"`{post} threads {PR}`", task)
        for verdict in ("request_changes", "approve", "comment"):
            self.assertIn(f"`{verdict}`", task)
        self.assertIn("--resolve <id>", task)
        self.assertIn("Bash(*pulls/*/reviews*)", REVIEW_ONLY_DENY_RULES)
        self.assertIn("Bash(*graphql*)", REVIEW_ONLY_DENY_RULES)




def policy(review_only=True):
    return RoomPolicy(
        root=Path("/root"), permission_mode="auto", overlay=Path("/root/agent-kit/modes/review-only.md"),
        escalation_channel="DAPPROVER", backend="claude", model="claude-opus-5-5", effort="high",
        role="eng", fallback_backend="codex", fallback_model="gpt-6-sol", fallback_effort="high",
        review_only=review_only,
    )


class ThrottleTest(unittest.TestCase):
    def test_concurrent_admissions_are_bounded(self):
        throttle = RequestThrottle(max_concurrent=2, clock=lambda: 0)
        self.assertTrue(throttle.try_begin("U1"))
        self.assertTrue(throttle.try_begin("U2"))
        self.assertFalse(throttle.try_begin("U3"))
        throttle.end("U1")
        self.assertTrue(throttle.try_begin("U3"))

    def test_a_requester_holds_one_slot_at_a_time(self):
        throttle = RequestThrottle(max_concurrent=2, clock=lambda: 0)
        self.assertTrue(throttle.try_begin("U1"))
        self.assertFalse(throttle.try_begin("U1"))
        self.assertTrue(throttle.try_begin("U2"))
        throttle.end("U1")
        self.assertTrue(throttle.try_begin("U1"))

    def test_expired_requesters_are_pruned(self):
        now = [0.0]
        throttle = RequestThrottle(window=10, clock=lambda: now[0])
        self.assertTrue(throttle.try_begin("U1"))
        throttle.end("U1")
        now[0] = 11
        self.assertTrue(throttle.try_begin("U2"))
        self.assertEqual({"U2"}, set(throttle._requests))

    def test_per_requester_rate_rolls(self):
        now = [0.0]
        throttle = RequestThrottle(per_requester=2, window=3600, clock=lambda: now[0])
        for _ in range(2):
            self.assertTrue(throttle.try_begin("U1"))
            throttle.end("U1")
        self.assertFalse(throttle.try_begin("U1"))
        self.assertTrue(throttle.try_begin("U2"))
        throttle.end("U2")
        now[0] = 3601
        self.assertTrue(throttle.try_begin("U1"))


class RecentMentionsTest(unittest.TestCase):
    def test_same_channel_and_ts_is_handled_once_per_window(self):
        now = [0.0]
        recent = RecentMentions(window=3600, clock=lambda: now[0])
        self.assertTrue(recent.first_delivery("C1", "1.1"))
        self.assertFalse(recent.first_delivery("C1", "1.1"))
        self.assertTrue(recent.first_delivery("C2", "1.1"))
        now[0] = 3601
        self.assertTrue(recent.first_delivery("C1", "1.1"))


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
        self.policy = policy()
        self.wrapper_ok = True
        self.turn_starts = True
        self.authorized = {"UKIRK"}
        self.control_handled = False
        self.claims = ThreadClaims()
        self.throttle = RequestThrottle(clock=lambda: 0)
        self.caps = ReviewCapStore(Path(self.temp.name) / "caps.json")
        self.ts = 0
        self.handler = ReviewRequestHandler(
            bot_user_id="ULISA",
            caps=self.caps,
            claims=self.claims,
            throttle=self.throttle,
            recent=RecentMentions(),
            load_config=lambda: self.config,
            post=lambda channel, thread, text: self.posts.append((channel, thread, text)),
            audit=self.audits.append,
            is_authorized=lambda user: user in self.authorized,
            run_control_command=self.control,
            turn_running=lambda thread: self.running,
            provider_unavailable=lambda thread: self.provider_down,
            room_policy=self.room_policy,
            lookup=self.lookup,
            start_turn=self.start_turn,
            spawn=lambda target, *args: target(*args),
            wrapper_ready=lambda root: self.wrapper_ok,
        )

    def tearDown(self):
        self.temp.cleanup()

    def control(self, event):
        self.controls.append(event["user"])
        return self.control_handled

    def room_policy(self, channel, user):
        if self.room_error:
            raise self.room_error
        return self.policy

    def lookup(self, pr, root):
        self.lookups.append(pr)
        if self.lookup_error:
            raise self.lookup_error
        return self.info

    def start_turn(self, admission):
        # Stands in for process_message_async: the turn starts (or does not) and returns.
        self.assertIs(self.policy, admission.policy)
        if self.turn_starts and admission.start():
            self.turns.append((admission.event["user"], admission.pr))

    def mention(self, user="USTRANGER", text=f"<@ULISA> review {PR}", **extra):
        self.ts += 1
        event = {"user": user, "channel": "CREVIEWS", "ts": f"{self.ts}.1", "text": text}
        event.update(extra)
        self.handler.intake(event)
        return event

    def outcomes(self):
        return [line.rsplit("OUTCOME:", 1)[1] for line in self.audits]

    def replies(self):
        return [post[2] for post in self.posts]

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

    def test_redelivered_mention_is_handled_once(self):
        event = self.mention()
        self.handler.intake(dict(event))
        self.assertEqual(1, len(self.turns))
        self.assertEqual(["accepted"], self.outcomes())

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
        self.assertEqual([USAGE_HINT, USAGE_HINT], self.replies())
        self.assertEqual(["refused_no_pr"] * 3, self.outcomes())
        self.assertEqual([], self.turns)

    def test_invalid_config_fails_closed(self):
        self.config = {"peer_review": {"peer_bot_user_ids": []}}
        self.mention()
        self.mention(user="UNED", bot_id="BNED")
        self.assertEqual([CONFIG_REPLY], self.replies())
        self.assertEqual(["refused_config"], self.outcomes())
        self.assertEqual([], self.turns)

    def test_room_that_is_not_review_only_is_refused(self):
        self.room_error = SafetyError("not review-only")
        self.mention()
        self.policy = policy(review_only=False)
        self.room_error = None
        self.mention()
        self.assertEqual(["refused_room"] * 2, self.outcomes())
        self.assertEqual([], self.turns)

    def test_outdated_wrapper_is_refused_before_any_lookup(self):
        self.wrapper_ok = False
        self.mention()
        self.assertEqual([WRAPPER_REPLY], self.replies())
        self.assertEqual(["refused_wrapper"], self.outcomes())
        self.assertEqual([], self.lookups)

    def test_own_pr_is_refused(self):
        self.info = PullRequestInfo("app/lisa-cargochief", "OPEN")
        self.mention(user="UNED", bot_id="BNED")
        self.assertEqual([OWN_PR_REPLY], self.replies())
        self.assertEqual(["refused_own_pr"], self.outcomes())
        self.assertEqual([], self.turns)

    def test_closed_or_merged_pr_is_refused(self):
        for state in ("CLOSED", "MERGED"):
            self.info = PullRequestInfo("kfranzenCC", state)
            self.mention()
        self.assertEqual([NOT_OPEN_REPLY] * 2, self.replies())
        self.assertEqual(["refused_state"] * 2, self.outcomes())

    def test_lookup_failure_fails_closed(self):
        self.lookup_error = ReviewGuardError("PR lookup exited 1")
        self.mention()
        self.assertEqual([LOOKUP_FAILED_REPLY], self.replies())
        self.assertEqual(["refused_lookup"], self.outcomes())
        self.assertEqual([], self.turns)

    def test_cap_gives_one_notice_then_silence_without_lookups(self):
        for _ in range(5):
            self.mention()
        self.assertEqual(2, len(self.turns))
        self.assertEqual([CAP_REPLY], self.replies())
        self.assertEqual(["accepted", "accepted", "refused_cap", "refused_cap", "refused_cap"],
                         self.outcomes())
        self.assertEqual(2, len(self.lookups))

    def test_repo_case_variants_share_the_cap(self):
        self.mention(text="review https://github.com/Cargo-Chief/C4/pull/123")
        self.mention(text="review https://github.com/cargo-chief/c4/pull/123")
        self.mention(text=f"review {PR}")
        self.assertEqual(2, len(self.turns))
        self.assertEqual(["accepted", "accepted", "refused_cap"], self.outcomes())

    def test_cap_is_not_charged_when_the_turn_does_not_start(self):
        self.turn_starts = False
        for _ in range(3):
            self.mention()
        self.assertEqual(["refused_error"] * 3, self.outcomes())
        self.assertEqual([ERROR_REPLY] * 3, self.replies())
        self.assertFalse(self.caps.exhausted(PR, 2))

    def test_unreadable_cap_record_refuses(self):
        self.caps.path.write_text("{not json")
        self.mention()
        self.assertEqual([CAP_STORE_REPLY], self.replies())
        self.assertEqual(["refused_cap_store"], self.outcomes())
        self.assertEqual([], self.lookups)

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

    def test_throttle_refuses_and_counts_failed_lookups(self):
        self.lookup_error = ReviewGuardError("PR lookup exited 1")
        for _ in range(10):
            self.mention()
        self.lookup_error = None
        self.mention()
        self.assertEqual(["refused_lookup"] * 10 + ["refused_throttle"], self.outcomes())
        self.assertEqual(THROTTLE_REPLY, self.replies()[-1])
        self.mention(user="UOTHERHUMAN")
        self.assertEqual("accepted", self.outcomes()[-1])

    def test_concurrent_admissions_are_limited(self):
        self.assertTrue(self.throttle.try_begin("UA"))
        self.assertTrue(self.throttle.try_begin("UB"))
        self.mention()
        self.assertEqual(["refused_throttle"], self.outcomes())
        self.assertTrue(self.claims.claim("1.1"), "a throttled request releases its claim")

    def test_admission_slot_is_released_after_the_guards(self):
        self.mention()
        self.mention(thread_ts="0.8")
        self.mention(thread_ts="0.7")
        self.assertEqual(["accepted", "accepted", "refused_cap"], self.outcomes())

    def test_provider_fallback_refuses_reviews(self):
        self.provider_down = True
        self.mention()
        self.assertEqual([PROVIDER_REPLY], self.replies())
        self.assertEqual(["refused_provider"], self.outcomes())


if __name__ == "__main__":
    unittest.main()


class PeerRequestTest(unittest.TestCase):
    def setUp(self):
        self.posts = []
        self.audits = []
        self.lookups = []
        self.info = PullRequestInfo("app/lisa-cargochief", "OPEN")
        room = {
            "root": "/workspace", "permission_mode": "auto",
            "autonomous_overlay": "agent-kit/modes/review-only.md",
            "private_escalation_channel": "DAPPROVER", "backend": "claude",
            "model": "claude-opus-5-5", "effort": "high",
            "fallback": {"backend": "codex", "model": "gpt-6-sol", "effort": "high"},
            "review_only": True,
        }
        self.config = {
            "models": ["claude-opus-5-5"],
            "channels": {"CREVIEWS": room, "CWORK": {**room, "review_only": False}},
            "peer_review": {"self_github_login": "lisa-cargochief[bot]", "peer_bot_user_ids": ["UNED"]},
        }

    def lookup(self, pr, root):
        self.lookups.append((pr, root))
        return self.info

    def request(self, text=PR, env=None):
        return request_peer_review(
            text, config=self.config, env=env or {"CLAUDE_CHANNEL_ID": "CWORK"}, lookup=self.lookup,
            post=lambda channel, message: self.posts.append((channel, message)),
            audit=self.audits.append,
        )

    def refused(self, outcome, **kwargs):
        with self.assertRaises(PeerRequestError) as caught:
            self.request(**kwargs)
        self.assertEqual(outcome, caught.exception.outcome)
        self.assertEqual([], self.posts)
        self.assertTrue(self.audits[-1].endswith(f"OUTCOME:{outcome}"), self.audits[-1])

    def test_happy_path_posts_the_fixed_mention_in_the_review_room(self):
        self.assertEqual("CREVIEWS", self.request(f"<{PR}|c4#123>"))
        self.assertEqual([("CREVIEWS", f"<@UNED> please review {PR}")], self.posts)
        self.assertEqual([(PR, Path("/workspace"))], self.lookups)
        self.assertEqual([f"REVIEW_REQUEST_SENT | PR:{PR} | OUTCOME:sent"], self.audits)

    def test_needs_exactly_one_pr_url(self):
        self.refused("refused_no_pr", text="please review my PR")
        self.refused("refused_no_pr", text=f"{PR} https://github.com/Cargo-Chief/c4/pull/9")
        self.assertIn("PR:none", self.audits[0])

    def test_a_review_turn_cannot_request_reviews(self):
        self.refused("refused_review_turn", env={"CLAUDE_CHANNEL_ID": "CWORK", "CARGO_CHIEF_REVIEW_ONLY": "1"})
        self.refused("refused_review_turn", env={"CLAUDE_CHANNEL_ID": "CREVIEWS"})
        self.assertEqual([], self.lookups)

    def test_only_this_agents_own_pr(self):
        self.info = PullRequestInfo("kfranzenCC", "OPEN")
        self.refused("refused_not_own_pr")

    def test_closed_pr_and_failed_lookup_are_refused(self):
        self.info = PullRequestInfo("lisa-cargochief[bot]", "MERGED")
        self.refused("refused_state")

        def failing(pr, root):
            raise ReviewGuardError("PR lookup exited 1")

        self.lookup = failing
        self.refused("refused_lookup")

    def test_needs_exactly_one_peer(self):
        self.config["peer_review"]["peer_bot_user_ids"] = []
        self.refused("refused_peer")
        self.config["peer_review"]["peer_bot_user_ids"] = ["UNED", "UOTHER"]
        self.refused("refused_peer")

    def test_needs_exactly_one_review_room(self):
        self.config["channels"]["CREVIEWS2"] = dict(self.config["channels"]["CREVIEWS"])
        self.refused("refused_room")
        del self.config["channels"]["CREVIEWS2"]
        self.config["channels"]["CREVIEWS"]["review_only"] = False
        self.refused("refused_room")

    def test_invalid_peer_config_is_refused(self):
        del self.config["peer_review"]["self_github_login"]
        self.refused("refused_config")

    def test_sender_honours_the_per_pr_budget(self):
        from review_requests import ReviewCapStore
        with tempfile.TemporaryDirectory() as temp:
            sent = ReviewCapStore(Path(temp) / "sent.json")
            def send():
                return request_peer_review(
                    PR, config=self.config, env={"CLAUDE_CHANNEL_ID": "CWORK"}, lookup=self.lookup,
                    post=lambda channel, message: self.posts.append((channel, message)),
                    audit=self.audits.append, sent=sent,
                )
            for _ in range(3):
                send()
            with self.assertRaisesRegex(PeerRequestError, "used its review requests"):
                send()
        self.assertEqual(3, len(self.posts))
        self.assertIn("OUTCOME:refused_sent_cap", self.audits[-1])

    def test_a_failed_post_is_audited_and_refused(self):
        def boom(channel, message):
            raise RuntimeError("not_in_channel")
        with self.assertRaisesRegex(PeerRequestError, "Slack refused"):
            request_peer_review(
                PR, config=self.config, env={"CLAUDE_CHANNEL_ID": "CWORK"}, lookup=self.lookup,
                post=boom, audit=self.audits.append,
            )
        self.assertIn("OUTCOME:refused_post", self.audits[-1])
