import threading
import unittest

from session_lifecycle import TurnAdmission, oldest_evictable_session, stop_timed_out_session


class FakeSession:
    def __init__(self, last_activity, *, active=False):
        self.last_activity = last_activity
        self.turn_lock = threading.Lock()
        if active:
            self.turn_lock.acquire()
        self._on_text = lambda _text: None
        self.pre_tool_text = ["late progress"]


class SessionLifecycleTests(unittest.TestCase):
    def test_turn_admission_queues_until_capacity_is_released(self):
        admission = TurnAdmission(1)
        self.assertFalse(admission.acquire())
        queued = threading.Event()
        admitted = threading.Event()
        waited = []

        def wait_for_turn():
            waited.append(admission.acquire(queued.set))
            admitted.set()
            admission.release()

        waiter = threading.Thread(target=wait_for_turn)
        waiter.start()
        self.assertTrue(queued.wait(timeout=1))
        self.assertFalse(admitted.is_set())
        admission.release()
        self.assertTrue(admitted.wait(timeout=1))
        waiter.join(timeout=1)
        self.assertFalse(waiter.is_alive())
        self.assertEqual([True], waited)

    def test_turn_admission_rejects_non_positive_limit(self):
        with self.assertRaisesRegex(ValueError, "positive"):
            TurnAdmission(0)

    def test_turn_admission_uses_all_configured_slots_before_queueing(self):
        admission = TurnAdmission(2)
        self.assertFalse(admission.acquire())
        self.assertFalse(admission.acquire())
        queued = threading.Event()
        admitted = threading.Event()

        def wait_for_turn():
            admission.acquire(queued.set)
            admitted.set()
            admission.release()

        waiter = threading.Thread(target=wait_for_turn)
        waiter.start()
        self.assertTrue(queued.wait(timeout=1))
        self.assertFalse(admitted.is_set())
        admission.release()
        self.assertTrue(admitted.wait(timeout=1))
        admission.release()
        waiter.join(timeout=1)
        self.assertFalse(waiter.is_alive())

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

    def test_timeout_silences_late_output_before_interrupt(self):
        session = FakeSession(10, active=True)
        observed = []

        def interrupt(value):
            observed.append((value._on_text, list(value.pre_tool_text)))
            return True

        self.assertTrue(stop_timed_out_session(session, interrupt))
        self.assertEqual([(None, [])], observed)


if __name__ == "__main__":
    unittest.main()
