import unittest

from http_server import serve_http


class HttpServerTests(unittest.TestCase):
    def test_serves_on_loopback_with_bounded_threads(self):
        calls = []
        app = object()

        class Server:
            def run(self):
                calls.append("run")

        def fake_create_server(*args, **kwargs):
            calls.append((args, kwargs))
            return Server()

        serve_http(app, 3000, create_server=fake_create_server)

        self.assertEqual(
            calls,
            [((app,), {"host": "127.0.0.1", "port": 3000, "threads": 4}), "run"],
        )

    def test_announces_after_bind_and_before_run(self):
        calls = []

        class Server:
            def run(self):
                calls.append("run")

        def fake_create_server(*args, **kwargs):
            calls.append("bound")
            return Server()

        serve_http(
            object(), 3000,
            create_server=fake_create_server,
            on_ready=lambda: calls.append("ready"),
        )

        self.assertEqual(calls, ["bound", "ready", "run"])

    def test_does_not_announce_when_bind_fails(self):
        calls = []

        def fail_bind(*args, **kwargs):
            raise OSError("address in use")

        with self.assertRaises(OSError):
            serve_http(
                object(), 3000,
                create_server=fail_bind,
                on_ready=lambda: calls.append("ready"),
            )

        self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
