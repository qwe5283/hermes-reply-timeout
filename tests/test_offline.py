"""Offline unit tests for reply-timeout (no LLM call, no real send, no gateway).

Run: python3 tests/test_offline.py  (from the plugin dir)
"""

import sys
import time
import types
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import reply_timeout as rt  # noqa: E402


class FakeState:
    def __init__(self):
        self.data = {}

    def get(self, key, default=None):
        return self.data.get(key, default)

    def set(self, key, value):
        self.data[key] = value


class FakeLlmResult:
    def __init__(self, parsed, text=""):
        self.parsed = parsed
        self.text = text


class FakeLlm:
    """Stands in for ctx.llm: returns a fixed minutes value, or raises."""

    def __init__(self, minutes=0, raise_on_override=False, exc=None):
        self.minutes = minutes
        self.raise_on_override = raise_on_override
        self.exc = exc
        self.calls = []

    def complete_structured(self, provider=None, model=None, **kwargs):
        self.calls.append({"provider": provider, "model": model})
        if self.exc:
            raise self.exc
        if (provider or model) and self.raise_on_override:
            raise PermissionError(
                "Plugin 'reply-timeout' cannot override the model "
                "(set plugins.entries.reply-timeout.llm.allow_model_override to true to allow)."
            )
        return FakeLlmResult(parsed={"minutes": self.minutes})


class FakeCtx:
    def __init__(self):
        self.config = {"announce": False}  # never actually send in tests
        self.state = FakeState()
        self.injected = []
        self.hooks = []
        self.unloads = []
        self.llm = FakeLlm()

    def get_config(self, key, default=None):
        return self.config.get(key, default)

    def inject_message(self, content, role="user", session_key=None):
        self.injected.append((content, role, session_key))
        return True

    def register_hook(self, name, cb):
        self.hooks.append(name)

    def on_unload(self, cb):
        self.unloads.append(cb)


DM_KEY = "agent:main:feishu:dm:oc_TESTCHAT"
DM_SESSION_ID = "sess-dm-1"


def make_plugin(monkey_minutes=7):
    ctx = FakeCtx()
    plug = rt.ReplyTimeoutPlugin(ctx)
    # deterministic intent via the fake LLM (real _intent_minutes logic runs)
    ctx.llm = FakeLlm(minutes=monkey_minutes)
    # point the session-key resolver at our DM key without touching state.db
    plug._sk_cache[DM_SESSION_ID] = DM_KEY
    return ctx, plug


class TestKeyParsing(unittest.TestCase):
    def test_dm_key(self):
        self.assertEqual(rt._dm_chat_id(DM_KEY), "oc_TESTCHAT")

    def test_group_key_rejected(self):
        self.assertIsNone(rt._dm_chat_id("agent:main:feishu:group:oc_G:ou_U"))

    def test_thread_key_rejected(self):
        self.assertIsNone(rt._dm_chat_id("agent:main:feishu:dm:oc_D:omt_T"))

    def test_other_platform_rejected(self):
        self.assertIsNone(rt._dm_chat_id("agent:main:telegram:dm:12345"))

    def test_empty(self):
        self.assertIsNone(rt._dm_chat_id(None))
        self.assertIsNone(rt._dm_chat_id(""))


class TestLifecycle(unittest.TestCase):
    def test_arm_and_fire(self):
        ctx, plug = make_plugin(monkey_minutes=1)
        plug.on_post_llm_call(
            session_id=DM_SESSION_ID, platform="feishu",
            user_message="在吗", assistant_response="在，等你确认",
        )
        # worker is async; give it a moment (intent is monkeypatched-instant)
        for _ in range(100):
            if plug._live:
                break
            time.sleep(0.02)
        self.assertIn(DM_KEY, plug._live)
        rec = plug._live[DM_KEY]["rec"]
        self.assertEqual(rec["minutes"], 1)
        self.assertEqual(rec["chain"], 1)
        self.assertTrue(plug._live[DM_KEY]["timer"].is_alive(),
                        "timer thread must be running after arm")
        self.assertEqual(ctx.state.get("timers"), {DM_KEY: rec})
        # force fire
        plug._live[DM_KEY]["timer"].cancel()
        plug._fire(DM_KEY, rec)
        self.assertEqual(len(ctx.injected), 1)
        content, role, sk = ctx.injected[0]
        self.assertEqual(content, rt.REMINDER_TEMPLATE.format(minutes=1))
        self.assertEqual(role, "user")
        self.assertEqual(sk, DM_KEY)
        self.assertEqual(ctx.state.get("last_fire")[DM_KEY]["chain"], 1)
        self.assertEqual(ctx.state.get("timers"), {})

    def test_silent_and_wrong_platform_skip(self):
        ctx, plug = make_plugin()
        plug.on_post_llm_call(session_id=DM_SESSION_ID, platform="feishu",
                              user_message="x", assistant_response="[SILENT]")
        plug.on_post_llm_call(session_id=DM_SESSION_ID, platform="telegram",
                              user_message="x", assistant_response="hi")
        plug.on_post_llm_call(session_id=DM_SESSION_ID, platform="feishu",
                              user_message="x", assistant_response="  ")
        time.sleep(0.1)
        self.assertEqual(plug._live, {})

    def test_real_inbound_cancels_and_resets(self):
        ctx, plug = make_plugin()
        plug.on_post_llm_call(session_id=DM_SESSION_ID, platform="feishu",
                              user_message="q", assistant_response="a")
        for _ in range(100):
            if plug._live:
                break
            time.sleep(0.02)
        self.assertIn(DM_KEY, plug._live)
        plug.on_pre_llm_call(session_id=DM_SESSION_ID, user_message="我回来了")
        self.assertNotIn(DM_KEY, plug._live)
        self.assertEqual(ctx.state.get("timers"), {})

    def test_reminder_inbound_does_not_cancel(self):
        ctx, plug = make_plugin()
        plug.on_post_llm_call(session_id=DM_SESSION_ID, platform="feishu",
                              user_message="q", assistant_response="a")
        for _ in range(100):
            if plug._live:
                break
            time.sleep(0.02)
        plug.on_pre_llm_call(session_id=DM_SESSION_ID,
                             user_message=rt.REMINDER_TEMPLATE.format(minutes=7))
        self.assertIn(DM_KEY, plug._live)  # still armed

    def test_cron_mirror_does_not_cancel(self):
        ctx, plug = make_plugin()
        plug.on_post_llm_call(session_id=DM_SESSION_ID, platform="feishu",
                              user_message="q", assistant_response="a")
        for _ in range(100):
            if plug._live:
                break
            time.sleep(0.02)
        plug.on_pre_llm_call(session_id=DM_SESSION_ID,
                             user_message="[Cron delivery: morning-align]\n📌 晨间对齐")
        self.assertIn(DM_KEY, plug._live)

    def test_chain_cap(self):
        ctx, plug = make_plugin()
        reminder_msg = rt.REMINDER_TEMPLATE.format(minutes=5)
        # simulate three fired reminders already
        ctx.state.set("last_fire", {DM_KEY: {"chain": 3, "minutes": 5, "at": time.time()}})
        plug.on_post_llm_call(session_id=DM_SESSION_ID, platform="feishu",
                              user_message=reminder_msg, assistant_response="还在等")
        time.sleep(0.1)
        self.assertEqual(plug._live, {})  # chain 4 > max_chain 3 -> not armed

    def test_chain_increments_on_reminder_turn(self):
        ctx, plug = make_plugin()
        reminder_msg = rt.REMINDER_TEMPLATE.format(minutes=5)
        ctx.state.set("last_fire", {DM_KEY: {"chain": 2, "minutes": 5, "at": time.time()}})
        plug.on_post_llm_call(session_id=DM_SESSION_ID, platform="feishu",
                              user_message=reminder_msg, assistant_response="再等等")
        for _ in range(100):
            if plug._live:
                break
            time.sleep(0.02)
        self.assertEqual(plug._live[DM_KEY]["rec"]["chain"], 3)

    def test_rearm_replaces(self):
        ctx, plug = make_plugin()
        plug.on_post_llm_call(session_id=DM_SESSION_ID, platform="feishu",
                              user_message="q1", assistant_response="a1")
        for _ in range(100):
            if plug._live:
                break
            time.sleep(0.02)
        first_timer = plug._live[DM_KEY]["timer"]
        plug.on_post_llm_call(session_id=DM_SESSION_ID, platform="feishu",
                              user_message="q2", assistant_response="a2")
        # NOTE: _arm does pop→insert inside the lock; an unlocked reader can
        # sample the transient empty window, so poll with .get() and only
        # break on the *new* timer object.
        for _ in range(200):
            entry = plug._live.get(DM_KEY)
            if entry is not None and entry["timer"] is not first_timer:
                break
            time.sleep(0.02)
        entry = plug._live.get(DM_KEY)
        assert entry is not None, "re-arm must leave a live entry"
        self.assertIsNot(entry["timer"], first_timer)

    def test_intent_zero_not_armed(self):
        ctx, plug = make_plugin(monkey_minutes=0)
        plug.on_post_llm_call(session_id=DM_SESSION_ID, platform="feishu",
                              user_message="好的", assistant_response="不客气")
        time.sleep(0.1)
        self.assertEqual(plug._live, {})

    def test_clamp_via_real_intent(self):
        ctx, plug = make_plugin()
        ctx.llm = FakeLlm(minutes=999)
        minutes = plug._intent_minutes("q", "a")
        self.assertEqual(minutes, 120)

    def test_intent_zero_via_real_intent(self):
        ctx, plug = make_plugin()
        ctx.llm = FakeLlm(minutes=0)
        self.assertEqual(plug._intent_minutes("好的", "不客气"), 0)


class TestIntentFallback(unittest.TestCase):
    def test_override_denied_falls_back_to_session_model(self):
        ctx, plug = make_plugin()
        ctx.config["intent_model"] = "some-cheap-model"
        ctx.llm = FakeLlm(minutes=10, raise_on_override=True)
        minutes = plug._intent_minutes("q", "a")
        self.assertEqual(minutes, 10)
        self.assertEqual(len(ctx.llm.calls), 2)  # denied, then fallback
        self.assertIsNone(ctx.llm.calls[1]["model"])  # session model on retry

    def test_llm_failure_returns_zero(self):
        ctx, plug = make_plugin()
        ctx.llm = FakeLlm(exc=RuntimeError("provider down"))
        self.assertEqual(plug._intent_minutes("q", "a"), 0)

    def test_unparsable_output_returns_zero(self):
        ctx, plug = make_plugin()
        ctx.llm = FakeLlm(minutes=7)
        ctx.llm.complete_structured = lambda **kw: FakeLlmResult(parsed=None, text="huh")
        self.assertEqual(plug._intent_minutes("q", "a"), 0)


class TestRestore(unittest.TestCase):
    def test_restore_unexpired_and_drop_expired(self):
        ctx, plug = make_plugin()
        now = time.time()
        ctx.state.set("timers", {
            DM_KEY: {"session_key": DM_KEY, "chat_id": "oc_TESTCHAT", "minutes": 30,
                     "chain": 2, "armed_at": now - 60, "expires_at": now + 1500},
            "agent:main:feishu:dm:oc_OLD": {"session_key": "agent:main:feishu:dm:oc_OLD",
                     "chat_id": "oc_OLD", "minutes": 5, "chain": 1,
                     "armed_at": now - 999, "expires_at": now - 60},
        })
        plug.restore_timers()
        self.assertIn(DM_KEY, plug._live)
        rec = plug._live[DM_KEY]["rec"]
        self.assertEqual(rec["chain"], 2)
        self.assertTrue(1 <= rec["minutes"] <= 25, rec["minutes"])  # ~25 min left
        timers = ctx.state.get("timers")
        self.assertIn(DM_KEY, timers)
        self.assertNotIn("agent:main:feishu:dm:oc_OLD", timers)

    def test_cancel_all(self):
        ctx, plug = make_plugin()
        plug.on_post_llm_call(session_id=DM_SESSION_ID, platform="feishu",
                              user_message="q", assistant_response="a")
        for _ in range(100):
            if plug._live:
                break
            time.sleep(0.02)
        plug.cancel_all()
        self.assertEqual(plug._live, {})
        self.assertEqual(ctx.state.get("timers"), {})


class TestRegister(unittest.TestCase):
    def test_register_wires_hooks(self):
        rt._MODULE_SINGLETON["registered"] = False
        ctx = FakeCtx()
        rt.register(ctx)
        self.assertIn("post_llm_call", ctx.hooks)
        self.assertIn("pre_llm_call", ctx.hooks)
        self.assertEqual(len(ctx.unloads), 1)

    def test_second_register_is_a_noop(self):
        rt._MODULE_SINGLETON["registered"] = False
        ctx = FakeCtx()
        rt.register(ctx)
        n_hooks, n_unloads = len(ctx.hooks), len(ctx.unloads)
        rt.register(ctx)  # duplicate load, no intervening unload
        self.assertEqual(len(ctx.hooks), n_hooks)
        self.assertEqual(len(ctx.unloads), n_unloads)

    def test_unload_resets_singleton(self):
        rt._MODULE_SINGLETON["registered"] = False
        ctx = FakeCtx()
        rt.register(ctx)
        ctx.unloads[0]()  # cancel_all
        self.assertFalse(rt._MODULE_SINGLETON["registered"])
        ctx2 = FakeCtx()
        rt.register(ctx2)  # clean re-register after unload
        self.assertEqual(len(ctx2.hooks), 2)


class TestNoAnnounceRecursion(unittest.TestCase):
    def test_restore_does_not_announce(self):
        ctx, plug = make_plugin()
        ctx.config["announce"] = True
        sent = []
        plug._announce = lambda chat_id, minutes: sent.append((chat_id, minutes))
        plug.on_post_llm_call(session_id=DM_SESSION_ID, platform="feishu",
                              user_message="q", assistant_response="a")
        for _ in range(100):
            if plug._live:
                break
            time.sleep(0.02)
        self.assertEqual(len(sent), 1)  # the initial arm announced once
        plug.restore_timers()  # restart path: re-arm silently
        self.assertEqual(len(sent), 1)  # ...and did NOT announce again

    def test_gateway_detection(self):
        old_argv = sys.argv
        try:
            sys.argv = ["hermes", "gateway", "run"]
            self.assertTrue(rt._looks_like_gateway())
            sys.argv = ["hermes", "send", "--to", "feishu:oc_X", "msg"]
            self.assertFalse(rt._looks_like_gateway())
            sys.argv = ["hermes", "chat", "-q", "hi"]
            self.assertFalse(rt._looks_like_gateway())
        finally:
            sys.argv = old_argv


class TestRealExpiry(unittest.TestCase):
    """One test that lets REAL time pass and requires the timer to actually
    fire (the 10-06 P0 blocker: Timer was created but never .start()ed, and
    every other test masked it by calling _fire manually)."""

    def test_timer_fires_and_injects_after_interval(self):
        ctx, plug = make_plugin()
        plug._arm(DM_KEY, "oc_TESTCHAT", minutes=0.05, chain=1, announce=False)  # ~3s
        self.assertTrue(plug._live[DM_KEY]["timer"].is_alive())
        deadline = time.time() + 10
        while time.time() < deadline:
            if ctx.injected:
                break
            time.sleep(0.05)
        self.assertEqual(len(ctx.injected), 1)
        content, role, sk = ctx.injected[0]
        self.assertEqual(content, rt.REMINDER_TEMPLATE.format(minutes=0))
        self.assertEqual(role, "user")
        self.assertEqual(sk, DM_KEY)
        self.assertNotIn(DM_KEY, plug._live)
        self.assertEqual(ctx.state.get("last_fire")[DM_KEY]["chain"], 1)


class TestAnnounce(unittest.TestCase):
    def test_direct_api_preferred_and_subprocess_skipped(self):
        ctx, plug = make_plugin()
        sent = []
        rt._feishu_send_text = lambda chat_id, text: sent.append((chat_id, text)) or True
        plug._announce("oc_X", 45)
        self.assertEqual(sent, [("oc_X", "【回复超时 45 min 后触发】")])

    def test_subprocess_fallback_on_api_failure(self):
        ctx, plug = make_plugin()
        rt._feishu_send_text = lambda chat_id, text: False
        calls = []
        import subprocess as _sp

        orig_run = _sp.run
        _sp.run = lambda *a, **kw: calls.append(a) or orig_run(["true"], capture_output=True, text=True)
        try:
            plug._announce("oc_X", 45)
        finally:
            _sp.run = orig_run
        self.assertEqual(len(calls), 1)


class TestTimerStarts(unittest.TestCase):
    """Regression: armed timers must actually be STARTED (10-06 P0 — Timer was
    created but .start() was never called, so nothing ever fired on time)."""

    def test_armed_timer_is_alive(self):
        ctx, plug = make_plugin()
        plug.on_post_llm_call(session_id=DM_SESSION_ID, platform="feishu",
                              user_message="q", assistant_response="a")
        for _ in range(100):
            if plug._live:
                break
            time.sleep(0.02)
        timer = plug._live[DM_KEY]["timer"]
        self.assertIsNotNone(timer)
        self.assertTrue(timer.is_alive(), "armed Timer must be started (alive while waiting)")
        timer.cancel()

    def test_short_timer_fires_end_to_end(self):
        import threading as _th

        ctx, plug = make_plugin()
        real_timer = _th.Timer

        class FastTimer(real_timer):
            def __init__(self, interval, function, args=None, kwargs=None):
                super().__init__(min(interval, 0.2), function, args=args, kwargs=kwargs)

        rt.threading.Timer = FastTimer
        try:
            plug.on_post_llm_call(session_id=DM_SESSION_ID, platform="feishu",
                                  user_message="q", assistant_response="a")
            deadline = time.time() + 5
            while time.time() < deadline and not ctx.injected:
                time.sleep(0.05)
        finally:
            rt.threading.Timer = real_timer
        self.assertEqual(len(ctx.injected), 1, "timer must fire and inject the reminder")
        self.assertNotIn(DM_KEY, plug._live)
        last_fire = ctx.state.get("last_fire") or {}
        self.assertEqual(last_fire[DM_KEY]["chain"], 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
