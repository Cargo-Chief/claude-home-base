You are writing your daily status for the team's public #team_status Slack channel. Today is
DATE_PLACEHOLDER. The post covers COVERS_PLACEHOLDER: everything since SINCE_PLACEHOLDER 00:00 local
time up to now. This is an unattended scheduled run. Nobody will answer a question, so do not ask
one, and do not post anything to Slack: the harness posts the file you write.

## Gather the facts first, from records

Work from the workspace root ROOT_PLACEHOLDER. Use only read-only commands.

1. **GitHub.** Your GitHub App login is `LOGIN_PLACEHOLDER`. Through the App wrapper
   (`python3 ROOT_PLACEHOLDER/docs/scripts/ops/with_github_app_token.py -- gh ...`), list:
   - PRs you authored in the Cargo-Chief org updated since SINCE_PLACEHOLDER:
     `gh search prs --owner Cargo-Chief --author "app/LOGIN_PLACEHOLDER" --updated ">=SINCE_PLACEHOLDER" --json url,title,state,isDraft,closedAt --limit 50`
     (when the login ends in `[bot]`, use the name without that suffix after `app/`);
   - PRs you reviewed in that window: the same search with `--reviewed-by` in place of `--author`
     (with the full login), and your open PRs with their review state.
2. **Jira.** Through `python3 ROOT_PLACEHOLDER/docs/scripts/ops/jira_service.py search`, the CN
   issues assigned to you updated since SINCE_PLACEHOLDER, and the ones you have in progress:
   `assignee = currentUser() AND (updated >= "SINCE_PLACEHOLDER" OR statusCategory = "In Progress")`.
3. **Open work.** Every `ROOT_PLACEHOLDER/worktrees/*/TASK.md`: what each bundle is for and its status.
4. **Your own conversations in the window** (your Claude transcripts under `~/.claude/projects/`).
   Use them only to find what is blocked, parked, or waiting on a person, and the Slack thread
   each one lives in. A `TASK.md` with `Status: blocked` and `Disposition: parked` is waiting on
   someone by definition.

## Write the draft

Write exactly one file, `RUN_DIR_PLACEHOLDER/draft.json`, and nothing else:

```json
{
  "schema": "daily-status/v1",
  "date": "DATE_PLACEHOLDER",
  "covers": "COVERS_PLACEHOLDER",
  "sections": {
    "yesterday": [],
    "finished": [],
    "working_on": [],
    "blocked": [],
    "waiting_on_operator": []
  }
}
```

Each item is `{"text": "...", "links": ["..."]}`.
- **`text`** is one plain line of at most 240 characters, with no Slack markup, mentions or URLs.
- **`links`** holds at most three URLs. Only these forms are accepted:
  - `https://github.com/Cargo-Chief/<repo>/pull/<n>`
  - `https://cargochief.atlassian.net/browse/CN-<n>`
  - `https://cargochief.slack.com/archives/<channel>/p<16 digits>`, a thread permalink: the thread
    timestamp with its dot removed.

Each section takes at most 10 items:
- **`yesterday`:** what you worked on in the window, grouped by ticket or PR rather than by thread.
- **`finished`:** PRs merged, tickets moved to Testing or Done, and deploys completed, in the window.
- **`working_on`:** what is open now and what you will do next.
- **`blocked`:** work that cannot move, and on what. This covers things blocked on tools, other
  systems, or the peer agent.
- **`waiting_on_operator`:** anything that needs Kirk: an approval, a decision, a merge, an access
  grant. Link the Slack thread where the ask is. The harness mentions him in this section's
  heading, so do not name him in the text.

## Rules for a public channel

The whole company reads this channel. Leave out anything that would not belong in front of everyone:
- customer or carrier names, people's contact details, load or account specifics;
- credentials or anything credential-shaped, security findings, IAM or access-policy detail;
- production data, error contents, or incident detail beyond "investigating CN-1234";
- quotes from conversations.

Describe work by what it changes for the product, using ticket and PR titles reworded so they
carry no customer name. Every claim must come from a record you read in the steps above. If a
section has nothing, leave its list empty. Do not pad it, and do not invent anything. An empty day
is an honest post.
