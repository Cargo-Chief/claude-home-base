import ast
from pathlib import Path
import unittest


class BotTurnStructureTest(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
