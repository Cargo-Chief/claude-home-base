import threading
import unittest

from session_lifecycle import oldest_evictable_session


class FakeSession:
    def __init__(self, last_activity, *, active=False):
        self.last_activity = last_activity
        self.turn_lock = threading.Lock()
        if active:
            self.turn_lock.acquire()


class SessionLifecycleTests(unittest.TestCase):
    def test_selects_oldest_unlocked_session(self):
        sessions = {
            "new-idle": FakeSession(30),
            "old-active": FakeSession(10, active=True),
            "old-idle": FakeSession(20),
        }
        self.assertEqual("old-idle", oldest_evictable_session(sessions))

    def test_refuses_to_select_when_every_session_is_active(self):
        sessions = {
            "one": FakeSession(10, active=True),
            "two": FakeSession(20, active=True),
        }
        self.assertIsNone(oldest_evictable_session(sessions))


if __name__ == "__main__":
    unittest.main()
