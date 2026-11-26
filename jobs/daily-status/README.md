# Daily status

Each weekday at 6:00 AM local time, this job posts the agent's status to a team channel:
- what it did on the previous working day (Monday's post covers Friday to Sunday);
- what it finished;
- what it is working on now;
- what is blocked;
- what is waiting on the operator, with the operator @-mentioned.

## How it works

1. `daily-status.sh` runs as the agent's own principal, from the agent's home-base clone. It works
   out the window and creates `work/daily-status/<date>/`.
   It writes `threads.json` (`daily_status.py threads`), which lists the agent's live Slack thread
   workspaces with their permalinks, bundles and `TASK.md` status lines. Transcripts carry no thread
   ID, so this is how blocked and waiting items get a link to their thread. The renderer marks any
   such item that has no thread link.
2. **Author pass.** `claude -p` gathers facts from records:
   - GitHub, through the App wrapper;
   - Jira, through `jira_service.py`;
   - the agent's bundle `TASK.md` files;
   - the agent's own transcripts, used only to find blocked or waiting items.

   It writes `draft.json` (`daily-status/v1`): five sections of one-line items, each with at most
   three links.
3. **Review pass.** A second `claude -p` checks the draft against the rules for a public channel. It
   writes `review.json` with the draft's SHA-256, which the script computes and passes in.
4. **Post.** `bot.py --post-daily-status <run dir>` posts exactly one message:
   - the rendered status, when the draft validates and the verdict is `clear` for that exact hash;
   - otherwise a fixed withheld notice, or a fixed unavailable notice when there is no draft.

   It writes a `posted` marker, so a launchd re-fire does not post twice.

Claude never posts to Slack. The harness renders every heading, mention and link. Links must be a
Cargo-Chief PR, a CN ticket or a Slack thread permalink. Item text is escaped, so it cannot mention
anyone or broadcast. A thread session that runs `--post-daily-status` is refused.

## Configuration

Add both keys to the bot's secret env file (`~/.config/cargo-chief/home-base.env`), and invite the
app to the channel:

```bash
TEAM_STATUS_CHANNEL_ID=C...        # the status channel's ID
TEAM_STATUS_MENTION_USER_ID=U...   # who "waiting on" items mention
```

## Install (headless service principal)

Render `com.cargo-chief.daily-status.daemon.plist` by replacing:
- `YOUR_USERNAME` (it is also in the label, so each agent gets its own job);
- `YOUR_GROUP`;
- `CARGO_CHIEF_ROOT_PLACEHOLDER`.

Then install it as a root-owned LaunchDaemon, as the diary job does:

```bash
plutil -lint /path/to/rendered.plist
sudo install -o root -g wheel -m 644 /path/to/rendered.plist \
  /Library/LaunchDaemons/com.cargo-chief.daily-status.<user>.plist
sudo launchctl bootstrap system /Library/LaunchDaemons/com.cargo-chief.daily-status.<user>.plist
```

Preflight, as the agent: `jobs/daily-status/daily-status.sh --dry-run` prints `STATUS_DRY_RUN_OK`
with the window it would cover. It checks the files, tools and harness credential without running a
model or posting.

For a live run, use `sudo launchctl kickstart system/com.cargo-chief.daily-status.<user>`. It posts
today's status, unless a status for today was already posted. The log is
`~/.local/state/cargo-chief/home-base/logs/daily-status.log` (mode 0600), beside the service logs,
so the operator account that reads those reads it too. It records process status and, for a failed
model pass, the last line of that pass's output, which is normally the CLI's error. Each pass's full
output is in the run directory (`author.out`, `review.out`). When a pass stopped on a Claude usage
limit, the post says so instead of the generic unavailable line. The job runs on Claude only; it
has no Codex fallback.
