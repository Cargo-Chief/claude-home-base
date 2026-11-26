import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from daily_status import (  # noqa: E402
    UNAVAILABLE_TEXT,
    WITHHELD_TEXT,
    StatusError,
    build_post,
    configured_team_status,
    post_request_is_exact,
    validate_run_dir,
)


def _draft(**sections):
    base = {key: [] for key in ("yesterday", "finished", "working_on", "blocked", "waiting_on_operator")}
    base.update(sections)
    return {"schema": "daily-status/v1", "date": "2026-11-26", "covers": "Wed Nov 25", "sections": base}


class DailyStatusTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.run = self.root / "work" / "daily-status" / "2026-11-26"
        self.run.mkdir(parents=True)

    def tearDown(self):
        self.tmp.cleanup()

    def _write(self, draft, verdict="clear", sha=None):
        raw = json.dumps(draft).encode()
        (self.run / "draft.json").write_bytes(raw)
        review = {
            "schema": "daily-status-review/v1",
            "draft_sha256": sha or hashlib.sha256(raw).hexdigest(),
            "verdict": verdict,
        }
        (self.run / "review.json").write_text(json.dumps(review))

    def test_cleared_draft_renders_with_mention_and_labelled_links(self):
        self._write(_draft(
            finished=[{"text": "Lambda images readable <!channel>", "links": [
                "https://github.com/Cargo-Chief/mercury/pull/1240",
                "https://cargochief.atlassian.net/browse/CN-9745",
            ]}],
            waiting_on_operator=[{"text": "Kit PR needs approval", "links": [
                "https://cargochief.slack.com/archives/C0BTN8L6G7P/p1764000000123456",
            ]}],
        ))
        outcome, text = build_post(self.run, "U8PP6NP8D")
        self.assertEqual(outcome, "posted")
        self.assertIn("<https://github.com/Cargo-Chief/mercury/pull/1240|mercury#1240>", text)
        self.assertIn("<https://cargochief.atlassian.net/browse/CN-9745|CN-9745>", text)
        self.assertIn("*Waiting on <@U8PP6NP8D>*", text)
        self.assertIn("&lt;!channel&gt;", text, "model text cannot broadcast")
        self.assertIn("*Blocked*\n• nothing", text)

    def test_no_mention_when_nothing_waits(self):
        self._write(_draft())
        outcome, text = build_post(self.run, "U8PP6NP8D")
        self.assertEqual(outcome, "posted")
        self.assertNotIn("<@", text)

    def test_missing_draft_is_unavailable(self):
        self.assertEqual(build_post(self.run, ""), ("unavailable", UNAVAILABLE_TEXT))

    def test_withheld_verdict_missing_review_or_edited_draft_withhold(self):
        self._write(_draft(), verdict="withhold")
        self.assertEqual(build_post(self.run, ""), ("withheld", WITHHELD_TEXT))
        self._write(_draft(), sha="0" * 64)
        self.assertEqual(build_post(self.run, "")[0], "withheld")
        self._write(_draft())
        (self.run / "review.json").unlink()
        self.assertEqual(build_post(self.run, "")[0], "withheld")

    def test_invalid_drafts_withhold(self):
        bad = [
            _draft(finished=[{"text": "x", "links": ["https://evil.example/x"]}]),
            _draft(finished=[{"text": "two\nlines", "links": []}]),
            _draft(finished=[{"text": "see https://evil.example/x", "links": []}]),
            _draft(finished=[{"text": "see www.evil.example", "links": []}]),
            _draft(finished=[{"text": "x" * 241, "links": []}]),
            _draft(finished=[{"text": "x", "links": [], "extra": 1}]),
            _draft(finished=[{"text": "x"}] * 11),
            {**_draft(), "date": "2026-11-25"},
            {**_draft(), "sections": {"yesterday": []}},
        ]
        for draft in bad:
            with self.subTest(draft=draft):
                self._write(draft)
                self.assertEqual(build_post(self.run, "")[0], "withheld")

    def test_run_dir_must_be_dated_dir_under_work(self):
        self.assertEqual(validate_run_dir(str(self.run), self.root), self.run)
        for raw in ("work/daily-status/2026-11-26", str(self.root / "work"), str(self.root / "work" / "daily-status" / "x")):
            with self.subTest(raw=raw), self.assertRaises(StatusError):
                validate_run_dir(raw, self.root)
        link = self.root / "work" / "daily-status" / "2026-11-27"
        link.symlink_to(self.run)
        with self.assertRaises(StatusError):
            validate_run_dir(str(link), self.root)

    def test_config_and_exact_args(self):
        self.assertEqual(configured_team_status({"TEAM_STATUS_CHANNEL_ID": "C8T4GCLMV"}).channel, "C8T4GCLMV")
        for env in ({}, {"TEAM_STATUS_CHANNEL_ID": "#team_status"},
                    {"TEAM_STATUS_CHANNEL_ID": "C1", "TEAM_STATUS_MENTION_USER_ID": "@kirk"}):
            with self.subTest(env=env), self.assertRaises(StatusError):
                configured_team_status(env)
        self.assertTrue(post_request_is_exact(["--post-daily-status", "/x"]))
        self.assertFalse(post_request_is_exact(["--post-daily-status", "/x", "--channel"]))
        self.assertFalse(post_request_is_exact(["--channel", "C1", "--post-daily-status", "/x"]))


if __name__ == "__main__":
    unittest.main()
