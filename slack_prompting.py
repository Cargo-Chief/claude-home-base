"""Pure Slack prompt builders that can be tested without bot credentials."""

from pathlib import Path
import shlex


def contains_private_escalation_action(
    content: list, target: Path, transport_python: Path, transport_script: Path
) -> bool:
    """Detect the one-shot file write or the exact no-argument escalation command."""
    expected = target.expanduser().resolve()
    expected_python = transport_python.expanduser().resolve()
    expected_script = transport_script.expanduser().resolve()
    for block in content:
        if not isinstance(block, dict) or block.get("type") != "tool_use":
            continue
        name = block.get("name")
        tool_input = block.get("input") or {}
        if name in {"Write", "Edit", "MultiEdit"}:
            value = tool_input.get("file_path")
            if value and Path(str(value)).expanduser().resolve() == expected:
                return True
        if name == "Bash":
            try:
                tokens = shlex.split(str(tool_input.get("command") or ""))
            except ValueError:
                continue
            if (
                len(tokens) == 3
                and Path(tokens[0]).expanduser().resolve() == expected_python
                and Path(tokens[1]).expanduser().resolve() == expected_script
                and tokens[2] == "--escalate"
            ):
                return True
    return False


def needs_relevance_prefix(
    *,
    event_type: str,
    is_dm: bool,
    has_existing_session: bool,
    has_live_process: bool,
    show_reminder: bool,
) -> bool:
    """Return whether a message needs model-based shared-space relevance filtering.

    Slack has already established that an ``app_mention`` directly addresses this
    bot. Sending that event through a second, model-based relevance decision is
    redundant and can leak the filter's internal ``SKIP`` deliberation to Slack.
    """
    if is_dm or event_type == "app_mention":
        return False
    return (
        (not has_existing_session and not has_live_process)
        or show_reminder
    )


def relevance_prefix(channel_name: str, bot_display_name: str, *, reminder: bool = False) -> str:
    """Build the shared-space relevance instruction for the configured bot name."""
    if reminder:
        return (
            f"[Reminder: #{channel_name} is a shared, multi-person space.] "
            f"Only respond if you're directly addressed by name '{bot_display_name}', tagged, or "
            "actively part of this exchange. In all other cases respond with exactly "
            '"SKIP" and nothing else — that suppresses the message in Slack.\n\n'
        )

    return (
        f"A new message in #{channel_name}. "
        f"Only respond if you're directly addressed by your name '{bot_display_name}' or tagged "
        "or if you're already part of the conversation thread. "
        'Respond with exactly "SKIP" in ALL other cases. '
        "Don't say 'Skipping this as it's not relevant' or 'Nothing for me to do here' "
        'or anything like it. Just be precise and say exactly "SKIP" — '
        "because that will result in the harness not showing your msg "
        "at all in Slack, which is the correct behaviour.\n\n"
    )
