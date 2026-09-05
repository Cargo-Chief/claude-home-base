import threading
import unittest

from session_lifecycle import (
    TurnAdmission,
    oldest_evictable_session,
    owner_stream_event_is_activity,
    stop_timed_out_session,
    wait_for_turn_completion,
)


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


class FakeDoneEvent:
    def __init__(
        self, clock, *, complete_on_wait=None, set_on_wait=None, after_wait=None,
    ):
        self.clock = clock
        self.complete_on_wait = complete_on_wait
        self.set_on_wait = set_on_wait
        self.after_wait = after_wait
        self.waits = 0

    def wait(self, timeout):
        self.clock.now += timeout
        self.waits += 1
        if self.after_wait:
            self.after_wait(self.waits)
        return self.waits == self.complete_on_wait

    def is_set(self):
        return (
            self.waits == self.complete_on_wait
            or (self.set_on_wait is not None and self.waits >= self.set_on_wait)
        )


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

    def test_turn_times_out_after_configured_inactivity(self):
        clock = FakeClock()
        done = FakeDoneEvent(clock)

        self.assertFalse(wait_for_turn_completion(
            done,
            inactivity_timeout=3,
            activity_at=lambda: 0,
            delegate_active=lambda: False,
            clock=clock,
            poll_interval=1,
        ))
        self.assertEqual(3, done.waits)

    def test_owner_output_resets_turn_inactivity_deadline(self):
        clock = FakeClock()
        activity = [0.0]

        def record_activity(wait_number):
            if wait_number == 2:
                activity[0] = clock.now

        done = FakeDoneEvent(
            clock, complete_on_wait=4, after_wait=record_activity,
        )

        self.assertTrue(wait_for_turn_completion(
            done,
            inactivity_timeout=3,
            activity_at=lambda: activity[0],
            delegate_active=lambda: False,
            clock=clock,
            poll_interval=1,
        ))

    def test_live_delegate_suspends_owner_inactivity_timeout(self):
        clock = FakeClock()
        done = FakeDoneEvent(clock, complete_on_wait=5)

        self.assertTrue(wait_for_turn_completion(
            done,
            inactivity_timeout=2,
            activity_at=lambda: 0,
            delegate_active=lambda: clock.now < 4,
            clock=clock,
            poll_interval=1,
        ))

    def test_delegate_completion_starts_a_fresh_owner_inactivity_window(self):
        clock = FakeClock()
        done = FakeDoneEvent(clock)

        self.assertFalse(wait_for_turn_completion(
            done,
            inactivity_timeout=2,
            activity_at=lambda: 0,
            delegate_active=lambda: clock.now < 3,
            clock=clock,
            poll_interval=1,
        ))
        self.assertEqual(5, done.waits)

    def test_hard_limit_stops_continuously_active_turn(self):
        clock = FakeClock()
        done = FakeDoneEvent(clock)

        self.assertFalse(wait_for_turn_completion(
            done,
            inactivity_timeout=2,
            activity_at=lambda: clock.now,
            delegate_active=lambda: True,
            clock=clock,
            poll_interval=1,
            max_duration=5,
        ))
        self.assertEqual(5, done.waits)

    def test_completion_at_deadline_wins_boundary_race(self):
        clock = FakeClock()
        done = FakeDoneEvent(clock, set_on_wait=3)

        self.assertTrue(wait_for_turn_completion(
            done,
            inactivity_timeout=3,
            activity_at=lambda: 0,
            delegate_active=lambda: False,
            clock=clock,
            poll_interval=1,
        ))

    def test_inbound_user_text_is_not_owner_activity(self):
        self.assertFalse(owner_stream_event_is_activity({
            "type": "user", "message": {"content": "steer"},
        }))

    def test_tool_result_and_assistant_output_are_owner_activity(self):
        self.assertTrue(owner_stream_event_is_activity({
            "type": "user",
            "message": {"content": [{"type": "tool_result", "content": "done"}]},
        }))
        self.assertTrue(owner_stream_event_is_activity({"type": "assistant"}))


if __name__ == "__main__":
    unittest.main()
