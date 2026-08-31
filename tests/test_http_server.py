import unittest

from http_server import serve_http


class HttpServerTests(unittest.TestCase):
    def test_serves_on_loopback_with_bounded_threads(self):
        calls = []
        app = object()

        def fake_serve(*args, **kwargs):
            calls.append((args, kwargs))

        serve_http(app, 3000, serve=fake_serve)

        self.assertEqual(
            calls,
            [((app,), {"host": "127.0.0.1", "port": 3000, "threads": 4})],
        )


if __name__ == "__main__":
    unittest.main()
