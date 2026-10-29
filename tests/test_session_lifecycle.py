import threading
import unittest
from unittest import mock

from session_lifecycle import (
    BackgroundAgents,
    PendingTools,
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

    def test_pending_tools_track_calls_until_their_results(self):
        tools = PendingTools()
        tools.observe({"type": "assistant", "message": {"content": [
            {"type": "text", "text": "running the suite"},
            {"type": "tool_use", "id": "t1", "name": "Bash"},
            {"type": "tool_use", "id": "t2", "name": "Read"},
        ]}})
        tools.observe({"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "t2", "content": "ok"},
        ]}})
        self.assertTrue(tools.active())
        tools.observe({"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "t1", "content": "ok"},
        ]}})
        self.assertFalse(tools.active())

    def test_pending_tools_ignore_echoes_subagent_calls_and_clear_per_turn(self):
        tools = PendingTools()
        tools.observe({"type": "user", "message": {"content": "plain inbound text"}})
        tools.observe({"type": "system", "subtype": "init"})
        tools.observe({"type": "assistant", "parent_tool_use_id": "agent-1",
                       "message": {"content": [{"type": "tool_use", "id": "inner"}]}})
        self.assertFalse(tools.active())
        tools.observe({"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "t3"}]}})
        self.assertTrue(tools.active())
        tools.clear()
        self.assertFalse(tools.active())

    def test_a_call_stops_counting_after_its_max_age(self):
        clock = FakeClock()
        tools = PendingTools(max_age=30, clock=clock)
        tools.observe({"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "hung"}]}})
        clock.now = 29
        self.assertTrue(tools.active())
        clock.now = 30
        self.assertFalse(tools.active(), "a hung or orphaned call must not hold the limit off for the whole turn")

    def test_a_quiet_tool_call_outlasts_the_inactivity_limit(self):
        clock = FakeClock()
        done = FakeDoneEvent(clock, complete_on_wait=12)
        tools = PendingTools()
        tools.observe({"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "t1"}]}})

        self.assertTrue(wait_for_turn_completion(
            done,
            inactivity_timeout=3,
            activity_at=lambda: 0,
            delegate_active=tools.active,
            clock=clock,
            poll_interval=1,
            max_duration=60,
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


def started(task_id, **extra):
    return {"type": "system", "subtype": "task_started", "task_id": task_id, "task_type": "local_agent", **extra}


def notified(task_id):
    return {"type": "system", "subtype": "task_notification", "task_id": task_id, "status": "completed"}


class BackgroundAgentsTest(unittest.TestCase):
    def test_a_background_agent_runs_until_its_notification(self):
        agents = BackgroundAgents()
        agents.observe(started("a"))
        agents.observe(started("b"))
        agents.observe({"type": "result"})
        self.assertTrue(agents.running())
        agents.observe(notified("a"))
        self.assertTrue(agents.running())
        agents.observe(notified("b"))
        self.assertFalse(agents.running())

    def test_shell_awaited_subagent_owned_and_ambient_tasks_are_ignored(self):
        agents = BackgroundAgents()
        agents.observe({"type": "system", "subtype": "task_started", "task_id": "s", "task_type": "local_bash"})
        agents.observe(started("w", awaited=True))
        agents.observe(started("o", owned_by_subagent=True))
        agents.observe(started("m", ambient=True))
        self.assertFalse(agents.running())

    def test_malformed_events_change_nothing(self):
        agents = BackgroundAgents()
        agents.observe({"type": "system", "subtype": "task_started", "task_type": "local_agent"})
        agents.observe(started(7))
        agents.observe(notified("ghost"))
        self.assertFalse(agents.running())


class BackgroundAgentsAgeTest(unittest.TestCase):
    def test_an_agent_whose_notification_never_comes_stops_counting(self):
        clock = FakeClock()
        agents = BackgroundAgents(max_age=60, clock=clock)
        agents.observe(started("a"))
        clock.now = 59
        self.assertTrue(agents.running())
        clock.now = 60
        self.assertFalse(agents.running())

if __name__ == "__main__":
    unittest.main()
