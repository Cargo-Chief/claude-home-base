"""Pure Slack prompt builders that can be tested without bot credentials."""


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
