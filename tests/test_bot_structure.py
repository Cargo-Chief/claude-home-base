import ast
import inspect
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).parents[1]))

from cargo_chief_safety import format_audit_metadata


class BotTurnStructureTest(unittest.TestCase):
    def test_owner_timeout_does_not_override_delegate_timeout(self):
        tree = ast.parse(
            (Path(__file__).parents[1] / "bot.py").read_text(encoding="utf-8")
        )
        values = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if (
                        isinstance(target, ast.Subscript)
                        and isinstance(target.slice, ast.Constant)
                        and target.slice.value == "CARGO_CHIEF_DELEGATE_TIMEOUT"
                    ):
                        values.append(ast.unparse(node.value))
            elif isinstance(node, ast.Dict):
                for key, value in zip(node.keys, node.values):
                    if isinstance(key, ast.Constant) and key.value == "CARGO_CHIEF_DELEGATE_TIMEOUT":
                        values.append(ast.unparse(value))

        self.assertEqual(["str(DELEGATE_TIMEOUT)", "str(DELEGATE_TIMEOUT)"], values)

    def test_turn_hard_limit_accommodates_delegate_and_owner_windows(self):
        tree = ast.parse(
            (Path(__file__).parents[1] / "bot.py").read_text(encoding="utf-8")
        )
        assignments = {
            target.id: ast.unparse(node.value)
            for node in ast.walk(tree)
            if isinstance(node, ast.Assign)
            for target in node.targets
            if isinstance(target, ast.Name)
        }

        self.assertEqual(
            "max(4 * CLAUDE_TIMEOUT, DELEGATE_TIMEOUT + CLAUDE_TIMEOUT)",
            assignments["MAX_TURN_RUNTIME"],
        )

    def test_normal_claude_turn_is_sibling_of_steering_branch(self):
        tree = ast.parse(
            (Path(__file__).parents[1] / "bot.py").read_text(encoding="utf-8")
        )
        function = next(
            node for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name == "process_message_async"
        )
        parents = {}
        for node in ast.walk(function):
            for child in ast.iter_child_nodes(node):
                parents[child] = node
        steering = next(
            node for node in ast.walk(function)
            if isinstance(node, ast.If)
            and "session.turn_lock.locked()" in ast.unparse(node.test)
        )
        normal = next(
            node for node in ast.walk(function)
            if isinstance(node, ast.If) and ast.unparse(node.test) == "session"
        )

        self.assertIs(parents[steering], parents[normal])
        self.assertIsInstance(normal.body[0], ast.With)
        turn_body = normal.body[0].body
        calls = {node.func.id for statement in turn_body for node in ast.walk(statement)
                 if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)}
        self.assertIn("_send_to_claude", calls)
        self.assertIn("wait_for_turn_completion", calls)


    def test_audit_calls_match_the_formatter_signature(self):
        # A budget-command audit row was calling format_audit_metadata with
        # action/used/limit/unit and no message_length, so every approver
        # command raised TypeError after posting its Slack confirmation --
        # outside the handler's except, and with no test covering the call.
        # Delegation rows belong on append_delegation_audit instead.
        accepted = set(inspect.signature(format_audit_metadata).parameters)
        tree = ast.parse(
            (Path(__file__).parents[1] / "bot.py").read_text(encoding="utf-8")
        )
        offenders = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
            if name != "format_audit_metadata":
                continue
            supplied = {kw.arg for kw in node.keywords if kw.arg is not None}
            unknown = supplied - accepted
            if unknown or "message_length" not in supplied:
                offenders.append((node.lineno, sorted(unknown)))

        self.assertEqual([], offenders)

if __name__ == "__main__":
    unittest.main()
