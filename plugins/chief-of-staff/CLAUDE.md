# Chief of Staff Plugin

The operational core — the workflows a chief of staff runs for you and your team. Email, briefs, action items today; more of the recurring routines over time.

## Skills

| Skill | Use When |
|-------|----------|
| `briefing` | Process email inboxes, deliver formatted briefs, and manage action items for each team member. Use when any conversation touches briefs, email, action items, inboxes, or whats-happening questions — and always when a user states or changes a preference about how they're briefed or how their email is handled. |

## Workflows

| Workflow | Purpose |
|----------|---------|
| `briefing-triage` | Daily brief triage with a hard verification gate between the notepad and the delivered brief. Invoked by the `briefing` skill on scheduled runs, not directly. |

## Per-user data

Persistent per-user state lives in `${CLAUDE_PLUGIN_DATA}/` (resolves to `data/chief-of-staff-claude-home-base/`): `brief-preferences-{user}.md`, `inbox-preferences-{user}.md`, `inbox-portrait-{user}.md`, `inbox-log-{user}.md`.
