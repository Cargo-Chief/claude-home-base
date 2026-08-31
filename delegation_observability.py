"""Content-free audit metadata for Claude Code subagent delegations."""

from __future__ import annotations

from dataclasses import dataclass

from cargo_chief_safety import APPROVED_DELEGATES


@dataclass
class DelegationRecord:
    agent: str
    expected_model: str
    effort: str
    served_model: str = "unknown"
    status: str = "started"
    total_tokens: int = 0
    tool_uses: int = 0
    duration_ms: int = 0
    owner_verification_tools: int = 0


class DelegationTracker:
    """Track only routing and numeric lifecycle fields; never retain content."""

    def __init__(self) -> None:
        self.records: dict[str, DelegationRecord] = {}
        self.completed: set[str] = set()

    def observe(self, event: dict) -> None:
        event_type = event.get("type")
        parent = event.get("parent_tool_use_id")

        if event_type == "assistant" and parent:
            record = self.records.get(parent)
            model = event.get("message", {}).get("model")
            if record and isinstance(model, str) and model:
                record.served_model = model
            return

        if event_type == "system" and event.get("subtype") == "task_started":
            tool_id = event.get("tool_use_id")
            agent = event.get("subagent_type")
            if isinstance(tool_id, str) and isinstance(agent, str):
                definition = APPROVED_DELEGATES.get(agent, {})
                self.records[tool_id] = DelegationRecord(
                    agent=agent,
                    expected_model=str(definition.get("model", "unapproved")),
                    effort=str(definition.get("effort", "unapproved")),
                )
            return

        if event_type == "system" and event.get("subtype") == "task_notification":
            tool_id = event.get("tool_use_id")
            record = self.records.get(tool_id)
            if not record:
                return
            usage = event.get("usage") or {}
            record.status = str(event.get("status") or "unknown")
            record.total_tokens = _safe_nonnegative_int(usage.get("total_tokens"))
            record.tool_uses = _safe_nonnegative_int(usage.get("tool_uses"))
            record.duration_ms = _safe_nonnegative_int(usage.get("duration_ms"))
            self.completed.add(tool_id)
            return

        if event_type == "assistant" and not parent and self.completed:
            content = event.get("message", {}).get("content") or []
            tool_count = sum(
                1 for block in content
                if isinstance(block, dict) and block.get("type") == "tool_use"
                and block.get("name") != "Agent"
            )
            if tool_count:
                for tool_id in self.completed:
                    self.records[tool_id].owner_verification_tools += tool_count

    def finish_turn(self) -> list[DelegationRecord]:
        finished_ids = [tool_id for tool_id in self.records if tool_id in self.completed]
        result = [self.records.pop(tool_id) for tool_id in finished_ids]
        self.completed.difference_update(finished_ids)
        return result


def _safe_nonnegative_int(value: object) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def format_delegation_audit(
    record: DelegationRecord,
    *,
    user: str,
    channel: str,
    thread: str,
) -> str:
    model_match = record.served_model in {
        record.expected_model,
        record.expected_model.removesuffix("[1m]"),
    }
    return " | ".join([
        "DELEGATION",
        f"USER:{user}",
        f"CHANNEL:{channel}",
        f"THREAD:{thread}",
        f"AGENT:{record.agent}",
        f"MODEL:{record.expected_model}",
        f"SERVED_MODEL:{record.served_model}",
        f"MODEL_MATCH:{str(model_match).lower()}",
        f"EFFORT:{record.effort}",
        f"STATUS:{record.status}",
        f"TOKENS:{record.total_tokens}",
        f"TOOL_USES:{record.tool_uses}",
        f"DURATION:{record.duration_ms / 1000:.1f}s",
        f"OWNER_VERIFY_TOOLS:{record.owner_verification_tools}",
    ])
