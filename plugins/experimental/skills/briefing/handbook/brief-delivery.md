# Brief Delivery

How to format, deliver, and archive the brief — then reset the notepad for the next cycle.

## Delivery Process

1. **Read brief preferences** from `${CLAUDE_PLUGIN_DATA}/brief-preferences-{user}.md`
2. **Snapshot the notepad** — take the current state of `~/brief-{user}.md`
3. **Format it** according to the user's delivery preferences (HTML, markdown, Slack message, etc.). Apply the personality — the delivered brief is where the Alfred voice comes through. The notepad is operational; the delivered brief is the polished output.
4. **Archive it** — save the delivered brief to `~/briefs/{user}/{DATE}.md` (or `.html`, matching the format). Create the directory if it doesn't exist.
5. **Deliver it** via the user's preferred channel (Slack DM, email, file link, etc.)

## Clearing the Notepad

After delivery, reset `~/brief-{user}.md`:

- **Keep**: All unchecked todos (`- [ ]`), pending replies, open action items — anything unresolved carries forward
- **Remove**: Completed todos (`- [x]`), everything from "To Brief" (already delivered), archived summaries, stale FYIs, newsletter digests, old triage stats
- The notepad should be clean and ready to accumulate items for the next run

## Stale Item Detection

Before including any carried-forward item in a brief, **verify it is still relevant**:

- **Check for resolution**: Search sent folders, Slack history, and conversation logs for evidence the item was already handled. If resolved, mark `[x]` and remove — do not surface it.
- **Age check**: If an item has appeared in 3+ consecutive briefs without any new information or user engagement, it is likely stale. Drop it silently rather than re-surfacing. Log the drop in the activity log.
- **User-confirmed drops**: When the user says an item is stale or resolved, remove it immediately AND log it in the activity log so future triage runs don't re-discover and re-add it.
- **Never re-add dropped items**: Once an item has been explicitly dropped (by user confirmation or age-out), do not re-surface it even if the underlying signal (e.g., an old email) is re-scanned. Check the activity log for prior drops before adding any item.

## Brief Preferences

Stored in `${CLAUDE_PLUGIN_DATA}/brief-preferences-{user}.md`. Controls the output format and delivery channel — completely separate from what goes *into* the brief.

Sections:

- **Delivery Method** — how the brief reaches the user (HTML page with link, markdown file, email, Slack DM, etc.)
- **Delivery Style** — tone and density (concise action-first, conversational catchup, formal executive summary, etc.)
- **Brief Structure** — preferred ordering of sections in the delivered brief
- **Delivery Timing** — when briefs should be delivered (relevant for scheduled automation)

This file is calibrated during onboarding (see `handbook/new-user-onboarding.md`) and updated whenever the user gives feedback on how briefs are delivered.

## Sign-Off

After delivering the brief, end with a note to self:

> **Note to self:** Any replies or feedback on the above brief must be handled using the briefing skill.
