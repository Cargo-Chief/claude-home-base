import ast
import re
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

        # Only the Codex fallback launches governed delegates; Claude sessions delegate natively,
        # and a review on Codex does not delegate.
        self.assertEqual(["str(DELEGATE_TIMEOUT)"], values)

    def _function(self, name):
        tree = ast.parse(
            (Path(__file__).parents[1] / "bot.py").read_text(encoding="utf-8")
        )
        return next(
            node for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef) and node.name == name
        )

    def test_claude_spawn_does_not_arm_the_delegation_guard(self):
        from cargo_chief_safety import LAUNCHER_ENV_KEYS

        spawn = self._function("_spawn_claude_process")
        literals = {
            node.value for node in ast.walk(spawn)
            if isinstance(node, ast.Constant) and isinstance(node.value, str)
        }
        self.assertFalse(literals & set(LAUNCHER_ENV_KEYS))
        self.assertIn("claude_owner_env(os.environ)", ast.unparse(spawn))
        self.assertNotIn("{**os.environ}", ast.unparse(spawn))

    def test_fallback_queue_note_is_only_for_a_persons_own_message_to_the_bot(self):
        source = ast.unparse(self._function("process_message_async"))
        self.assertIn(
            "notify = addressed_to_bot(event_type=event.get('type', ''), is_dm=is_dm) and "
            "(not (review_notice or handoff_from or forward))",
            source,
        )
        self.assertIn("hold_thread_turn(_openai_turn_lock(thread_ts), queue_notice)", source)

    def _flat(self, name):
        return re.sub(r"\s+", " ", ast.unparse(self._function(name)))

    def test_a_claude_work_turn_takes_the_thread_lock_before_any_session_and_before_it_runs(self):
        source = self._flat("process_message_async")
        take = "if not review: held_thread_lock = _take_thread_turn(thread_ts)"
        self.assertIn(take, source)
        self.assertLess(source.index("_turn_admission.acquire("), source.index(take))
        self.assertLess(source.index(take), source.index("session = _get_or_create_live_session("))
        race = "if not review and held_thread_lock is None: held_thread_lock = _take_thread_turn(thread_ts)"
        self.assertIn(race, source)
        self.assertLess(source.index(race), source.index("with session.turn_lock:"))
        helper = self._flat("_take_thread_turn")
        self.assertIn("if not lock.acquire(blocking=False): raise _RerouteAfterTurn()", helper)
        self.assertIn("if _thread_uses_openai(thread_ts): lock.release() raise _RerouteAfterTurn()", helper)

    def test_a_claude_turn_takes_no_work_slot_while_another_turn_holds_the_thread(self):
        source = self._flat("process_message_async")
        check = "if not review and _thread_turn_running(thread_ts): raise _RerouteAfterTurn()"
        self.assertIn(check, source)
        self.assertLess(source.index(check), source.index("_turn_admission.acquire("))

    def test_the_thread_lock_is_released_first_in_finally(self):
        function = self._function("process_message_async")
        outer = next(node for node in ast.walk(function) if isinstance(node, ast.Try) and node.finalbody
                     and "held_thread_lock" in ast.unparse(node.finalbody[0]))
        self.assertEqual(
            "if held_thread_lock is not None:\n    held_thread_lock.release()", ast.unparse(outer.finalbody[0])
        )

    def test_a_codex_turn_rechecks_the_provider_after_waiting_and_a_notice_never_moves(self):
        source = self._flat("process_message_async")
        hold = "with hold_thread_turn(_openai_turn_lock(thread_ts), queue_notice):"
        recheck = "if not _thread_uses_openai(thread_ts): if review_notice: raise _NoLiveSession() raise _RerouteAfterTurn()"
        self.assertIn(recheck, source)
        self.assertLess(source.index(hold), source.index(recheck))
        self.assertIn("if review_notice and use_openai != notice_on_codex:", source)

    def test_limit_replays_inside_a_claude_turn_reuse_the_held_lock(self):
        source = self._flat("process_message_async")
        self.assertEqual(
            2, source.count("with contextlib.nullcontext() if held_thread_lock else _openai_turn_lock(thread_ts):")
        )

    def test_a_rerouted_message_routes_again_inside_the_function(self):
        function = self._function("process_message_async")
        loop = next(node for node in function.body if isinstance(node, ast.While))
        body = re.sub(r"\s+", " ", ast.unparse(loop))
        self.assertIn("if reroute: _wait_for_thread_turn(thread_ts, queue_notice)", body)
        self.assertIn("continue break", body)
        # The one-time work (route claim, attachments, envelope) happens before the loop, once.
        before = re.sub(r"\s+", " ", ast.unparse(ast.Module(body=function.body[:function.body.index(loop)], type_ignores=[])))
        for once in ("_reply_routes.claim(", "download_slack_files(event)", "build_authority_envelope("):
            self.assertIn(once, before)
            self.assertNotIn(once, body)
        self.assertIn("if review_notice and session is not None and (not reroute):", body)
        self.assertIn("if woken is not None and session is not woken:", body)
        self.assertIn("if not reroute and woken is not None and (woken is not session): woken.waking = False", body)

    def test_the_wait_notes_only_a_codex_turn_and_only_once(self):
        self.assertIn(
            "if queue_notice is not None and _codex_turn_running(thread_ts):", self._flat("_wait_for_thread_turn")
        )
        self.assertIn("if not noted.is_set(): noted.set()", self._flat("process_message_async"))

    def test_a_provider_switch_says_when_a_turn_is_still_running(self):
        source = ast.unparse(self._function("_maybe_provider_command"))
        self.assertIn("if action != 'status' and _thread_turn_running(thread_ts):", source)

    def test_claude_stop_paths_stop_the_whole_process_tree(self):
        for name in ("_stop_unread_process", "_interrupt_session", "_stop_background_delegates"):
            source = ast.unparse(self._function(name))
            self.assertIn("stop_process_tree(session.proc)", source, name)
            self.assertNotIn(".terminate()", source, name)
            self.assertNotIn(".kill()", source, name)

    def test_a_killing_stop_is_never_steered_into(self):
        for name in ("_interrupt_session", "_stop_background_delegates"):
            source = ast.unparse(self._function(name))
            self.assertIn("session.stop_done = threading.Event()", source, name)
            self.assertIn("session.stop_done.set()", source, name)
        routing = ast.unparse(self._function("process_message_async"))
        self.assertIn("session.stop_done.wait(timeout=STOP_WAIT_SECONDS)", routing)
        creation = ast.unparse(self._function("_get_or_create_live_session"))
        self.assertIn("not session.stop_done", creation)

    def test_eviction_and_idle_cleanup_close_the_whole_tree(self):
        for name in ("_get_or_create_live_session", "_cleanup_idle_sessions"):
            source = ast.unparse(self._function(name))
            self.assertIn("close_process_tree(", source, name)
            self.assertNotIn(".kill()", source, name)

    def test_a_review_session_uses_its_own_slot_when_deciding_to_evict(self):
        creation = ast.unparse(self._function("_get_or_create_live_session"))
        self.assertIn("needs_eviction(work, max_work_sessions=MAX_LIVE_SESSIONS, new_is_review=fresh)", creation)
        self.assertIn("if not live.waking and (evict_waiting or ts not in waiting)", creation)
        self.assertIn("oldest_evictable_session(candidates, protected=waiting)", creation)
        process = ast.unparse(self._function("process_message_async"))
        self.assertIn("evict_waiting=bool(forward or handoff_from)", process)
        self.assertIn("raise _SessionsFull()", creation)
        self.assertNotIn("live session cap reached", creation)
        self.assertNotIn("len(_live_sessions) >= MAX_LIVE_SESSIONS", creation)
        self.assertNotIn("waiting_threads", creation.split("with _live_sessions_lock:")[0], "read only when evicting")

    def test_a_queued_review_is_bounded_rechecked_and_always_frees_its_slot(self):
        process = ast.unparse(self._function("process_message_async"))
        self.assertLess(process.index("_review_slots.acquire("), process.index("_turn_admission.acquire("))
        self.assertIn("review_admission.refuse('refused_queue', REVIEW_QUEUE_FULL_REPLY)", process)
        self.assertIn("slot == 'queued' and (not _review_still_runnable(", process)
        self.assertIn("_turn_admission.acquire(None if review else", process)
        self.assertIn("if review_slot_held:\n                _review_slots.release()", process)

    def test_a_woken_thread_is_unevictable_until_its_turn_runs(self):
        notice = ast.unparse(self._function("_start_review_notice"))
        self.assertLess(notice.index("session.waking = True"), notice.index("process_message_async("))
        process = ast.unparse(self._function("process_message_async"))
        self.assertIn("if review_notice and session is not None and (not reroute):\n                session.waking = False", process)

    def test_owner_inactivity_waits_on_the_busy_check_and_logs_the_limit(self):
        source = ast.unparse(self._function("process_message_async"))
        self.assertIn("delegate_active=lambda: _owner_turn_busy(session)", source)
        self.assertIn("LIMIT:%s", source)
        self.assertLess(source.index("timeout_limit ="), source.index("reactions_remove"),
                        "the limit is read before any Slack call delays it")
        reader = ast.unparse(self._function("_reader_loop"))
        self.assertIn("session.pending_tools.observe(data)", reader)
        self.assertIn("session.background_agents.observe(data)", reader)
        self.assertIn("_pause_review_turn(session)", reader)
        self.assertIn("text_blocks = _take_review_marker(session, text_blocks)", reader)
        self.assertIn("if session.review and session.background_agents.running():",
                      ast.unparse(self._function("_owner_turn_busy")))
        stop = ast.unparse(self._function("_maybe_stop_from_message"))
        self.assertIn("outcome = _stop_review(session)", stop)
        self.assertIn("not (review and session.stop_done)", source)

    def test_slack_posts_skip_previews_and_redelivered_events(self):
        source = (Path(__file__).parents[1] / "bot.py").read_text(encoding="utf-8")
        self.assertIn("slack_client = QuietWebClient(", source)
        self.assertNotIn(" WebClient(token=", source)
        middleware = ast.unparse(self._function("_drop_redelivered_events"))
        self.assertIn("_recent_events.first_delivery", middleware)

    def test_claude_reader_never_waits_on_the_launcher_marker(self):
        reader = ast.unparse(self._function("_reader_loop"))
        self.assertNotIn("delegate_verification_file", reader)
        self.assertNotIn("delegation_verification_status", reader)

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
            "max(RUNTIME_POLICY.max_turn_runtime, DELEGATE_TIMEOUT + CLAUDE_TIMEOUT)",
            assignments["MAX_TURN_RUNTIME"],
        )

    def test_output_after_a_turn_is_announced_once_without_touching_the_turn(self):
        reader = ast.unparse(self._function("_reader_loop"))
        self.assertIn(
            "if not session.turn_lock.locked() and (not session.after_turn_noticed):", reader
        )
        notice = ast.unparse(self._function("_notice_output_after_turn"))
        self.assertIn("session.after_turn_noticed = True", notice)
        self.assertNotIn("reporter", notice)
        process = ast.unparse(self._function("process_message_async"))
        self.assertIn("session.after_turn_noticed = False", process)

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
        # A non-review turn first takes the thread's turn lock; the turn itself is the With.
        turn = next(statement for statement in normal.body if isinstance(statement, ast.With))
        self.assertIn("session.turn_lock", ast.unparse(turn.items[0]))
        turn_body = turn.body
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

    def test_claude_turns_report_through_the_turn_reporter(self):
        reader = ast.unparse(self._function("_reader_loop"))
        self.assertIn("session.reporter.text(text)", reader)
        self.assertIn("session.reporter.tool_used()", reader)
        self.assertIn(
            "session.reporter.finish(post_reply=not session.private_escalation_pending)",
            ast.unparse(self._function("_finish_turn")),
        )
        self.assertIn("session.reporter.finish(state='stopped')", reader)
        self.assertIn("_live_sessions.get(session.thread_ts) is session", reader)
        process = ast.unparse(self._function("process_message_async"))
        self.assertIn("session.reporter.begin()", process)
        self.assertIn("session.reporter.finish(state='stopped')", process)
        self.assertIn("session.reporter.mark_stopped()", ast.unparse(self._function("_maybe_stop_from_message")))

    def test_reader_keeps_on_text_filters_for_skip_limit_and_attachments(self):
        reader = ast.unparse(self._function("_reader_loop"))
        self.assertIn("text.strip() == 'SKIP' or text in session.limit_notices or _ATTACH_PATTERN.search(text)", reader)
        self.assertIn("data.get('message', {}).get('model') == SYNTHETIC_MODEL", reader)

    def test_on_text_treats_only_the_cli_notice_as_a_limit(self):
        process = ast.unparse(self._function("process_message_async"))
        self.assertIn("session is not None and text_block in session.limit_notices", process)
        self.assertNotIn("is_claude_limit_notice(text_block)", process)

    def test_heartbeat_uses_the_guarded_pass(self):
        heartbeat = ast.unparse(self._function("_reporter_heartbeat"))
        self.assertIn("tick_all(", heartbeat)
        self.assertIn("except Exception", heartbeat)

    def test_review_only_rooms_divert_mentions_before_authorization(self):
        mention = ast.unparse(self._function("handle_mention"))
        self.assertLess(mention.index("is_review_only_room("), mention.index("_channel_allowed("))
        self.assertLess(mention.index("_review_requests.intake(event)"), mention.index("is_authorized(user_id)"))

    def test_review_turns_never_touch_reply_routes(self):
        process = ast.unparse(self._function("process_message_async"))
        self.assertIn("elif not authority.allows(user_id):", process)
        self.assertLess(process.index("if not review and (not handoff_from) and (not review_notice):"), process.index("_reply_routes.claim("))
        self.assertNotIn("_reply_routes.add", process)
        self.assertLess(process.index("raise _ReviewBusy()"), process.index("_send_to_claude(session, text)"))

    def test_a_review_notice_turn_is_the_peer_bot_with_no_routes_or_handoff(self):
        process = ast.unparse(self._function("process_message_async"))
        notice = process.index("elif review_notice:")
        self.assertLess(notice, process.index("elif not authority.allows(user_id):"))
        self.assertIn("if user_id not in peer_ids:", process[notice:notice + 300])
        self.assertIn("review_notice=review_notice['pr'] if review_notice else None", process)
        self.assertEqual(2, process.count("from_handoff=bool(handoff_from or review_notice)"))
        self.assertLess(process.index("if review_notice and use_openai and (_get_openai_session(thread_ts) is None):"),
                        process.index("_run_openai_fallback("))
        self.assertIn("process_user = review_notice['room_user'] if review_notice else user_id", process)
        # Both notice runs of Codex are resume-only, as the thread's person.
        self.assertEqual(2, process.count("resume_only="))
        self.assertIn("resume_only=bool(review_notice)", process)
        self.assertIn("elif fallback_requested and review_notice:", process)
        self.assertLess(process.index("elif fallback_requested and review_notice:"),
                        process.index("elif fallback_requested:"))
        self.assertEqual(2, process.count("record_user=not forward"))
        fallback = ast.unparse(self._function("_run_openai_fallback"))
        self.assertLess(fallback.index("raise _NoLiveSession()"), fallback.index("build_codex_command("))
        self.assertIn("if resume_only and (not _codex_resumes_as(thread_ts, channel, user_id, session_id)):", fallback)
        self.assertLess(fallback.index("session_id = saved_session_id if saved_cwd == cwd.resolve() else None"),
                        fallback.index("if resume_only"))
        self.assertIn("user_id=user_id if record_user else None", fallback)
        self.assertIn("existing_only=bool(review_notice)", process)
        self.assertIn("except _NoLiveSession:", process)
        factory = ast.unparse(self._function("_get_or_create_live_session"))
        self.assertLess(factory.index("raise _NoLiveSession()"), factory.index("_spawn_claude_process("))
        start = ast.unparse(self._function("_start_review_notice"))
        self.assertLess(start.index("session = _live_session_for(thread, channel)"), start.index("chat_postMessage("))
        self.assertIn("restore(f'failed_{type(exc).__name__}')", start)
        self.assertIn("room_user = codex_user or session.user_id", start)
        self.assertIn("review_notice={**review_notice, 'room_user': room_user}", start)
        self.assertLess(start.index("session = _live_session_for(thread, channel)"),
                        start.index("codex_user = _codex_notice_user(thread, channel, requested_at)"))
        self.assertIn("review_notice = {'pr': pr, 'refused': refused}", start)

    def test_escalation_and_forwarding_refuse_review_only_rooms_server_side(self):
        main = ast.unparse(self._function("main"))
        escalate = main.index("if args.escalate:")
        self.assertIn("review_route_refusal(_load_model_config(), os.environ)", main[escalate:escalate + 200])
        self.assertLess(main.index("if args.forward_to:"), main.index("if args.send:"))
        endpoint = ast.unparse(self._function("register_forward_endpoint"))
        self.assertLess(endpoint.index("forward_refusal("), endpoint.index("_add_forward("))

    def test_request_review_cli_takes_only_the_url(self):
        main = ast.unparse(self._function("main"))
        self.assertLess(main.index("if args.request_review is not None:"), main.index("if args.send:"))
        self.assertIn("request_peer_review(args.request_review, config=_load_model_config(), env=os.environ", main)

    def test_review_lookup_uses_the_downscoped_env(self):
        lookup = ast.unparse(self._function("_lookup_review_pr"))
        self.assertIn("review_turn_env(claude_owner_env(os.environ))", lookup)

    def test_review_spawn_passes_self_and_requester_logins(self):
        spawn = ast.unparse(self._function("_spawn_claude_process"))
        self.assertIn("self_login=peer.self_github_login", spawn)
        self.assertIn("requester_login=peer.github_logins.get(user_id, '')", spawn)
        self.assertIn("pr=review_pr", spawn)
        self.assertIn("review_base=review_plan.base if review_plan else ''", spawn)
        main = ast.unparse(self._function("main"))
        self.assertIn("lookup=lambda pr, root: _lookup_review_pr(pr, root, include_reviews=False)", main)
        process = ast.unparse(self._function("process_message_async"))
        self.assertIn("review_pr=review_admission.pr if review else ''", process)
        self.assertIn("build_review_task(review_admission.pr, room_policy.root, review_admission.plan)", process)
        self.assertIn("review_plan=review_admission.plan if review else None", process)
        self.assertIn("mode=review_plan.mode if review_plan else ''", spawn)
        self.assertIn("pass_no=review_plan.pass_no if review_plan else 0", spawn)

if __name__ == "__main__":
    unittest.main()
