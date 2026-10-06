"""reply-timeout — per-chat reply timeout arming for Feishu DM sessions (P0).

Behavior (P0 scope: Feishu DM sessions only; group chats and cron-delivered
turns are out of scope by design):

1. After each final assistant reply (``post_llm_call``) in a Feishu DM session,
   a short-lived worker thread runs ONE stateless structured intent call via
   ``ctx.llm`` (never enters session context / state.db as conversation): the
   model outputs an integer "minutes to wait for the user", 0 = don't wait.
2. minutes > 0 -> arm a per-session-key timer and (configurable, default on)
   announce 「【回复超时 N min 后触发】」 to the chat via ``hermes send``.
3. Any real inbound user message before expiry cancels the pending timer and
   resets the chain counter (``pre_llm_call``). Self-injected
   ``<system_reminder>`` turns and ``[Cron delivery:`` mirrors are NOT user
   messages and never reset anything.
4. On expiry the plugin injects one user-role message
   ``<system_reminder>用户在 N min 后未做回复</system_reminder>`` into the
   session via ``ctx.inject_message`` (stateful: enters context and state.db,
   triggers a full agent turn — the agent decides what to do with full
   context).
5. The reminder-triggered turn may arm again (the chain), capped at
   ``max_chain`` (default 3); a real user message resets the counter.

Timers survive gateway restarts (unexpired ones re-arm with remaining time,
expired ones are dropped). All state.db access is read-only (``mode=ro``).
"""

from __future__ import annotations

import logging
import os
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger("plugins.reply-timeout")

REMINDER_PREFIX = "<system_reminder>"
CRON_MIRROR_PREFIX = "[Cron delivery:"
REMINDER_TEMPLATE = "<system_reminder>用户在 {minutes} min 后未做回复</system_reminder>"
ANNOUNCE_TEMPLATE = "【回复超时 {minutes} min 后触发】"

INTENT_SCHEMA = {
    "type": "object",
    "properties": {
        "minutes": {"type": "integer", "minimum": 0, "maximum": 120},
    },
    "required": ["minutes"],
    "additionalProperties": False,
}

INTENT_INSTRUCTIONS = (
    "你是「回复超时」意图分类器：判断助手这条最终回复发出后，是否需要等待用户回复。"
    "只输出一个整数分钟数：0＝不需要等（信息已交付、闲聊收尾、纯通知、无需用户输入）；"
    "1–120＝值得等用户回复的预计分钟数。参考：等待确认/二选一 5–20；等待决策/答复 15–45；"
    "任务汇报后等反馈或等用户回来 30–120；简短追问 5–15。拿不准输出 0。"
)

_CLIP = 1200  # chars per field in the intent payload

_FEISHU_DOMAIN_MAP = {"feishu": "https://open.feishu.cn", "lark": "https://open.larksuite.com"}
_TOKEN_CACHE: dict = {"token": None, "expires_at": 0.0}


def _read_secret(name: str) -> Optional[str]:
    """Env var first, then the profile home's ``.env`` (feishu-history's recipe)."""
    value = os.environ.get(name)
    if value:
        return value
    home = os.environ.get("HERMES_HOME") or str(Path.home() / ".hermes")
    try:
        for line in (Path(home) / ".env").read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith(name + "="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    except Exception:
        pass
    return None


def _feishu_base() -> str:
    domain = (_read_secret("FEISHU_DOMAIN") or "feishu").strip().lower()
    return _FEISHU_DOMAIN_MAP.get(domain, f"https://{domain}")


def _http_post_json(url: str, payload: dict, headers: dict, timeout: float) -> dict:
    """Single-shot JSON POST (stdlib; monkeypatched in tests). Returns parsed dict."""
    import json as _json
    import urllib.request

    req = urllib.request.Request(
        url, data=_json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", **headers}, method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return _json.loads(resp.read().decode("utf-8"))


def _feishu_tenant_token() -> Optional[str]:
    now = time.time()
    if _TOKEN_CACHE["token"] and now < _TOKEN_CACHE["expires_at"]:
        return _TOKEN_CACHE["token"]
    app_id, app_secret = _read_secret("FEISHU_APP_ID"), _read_secret("FEISHU_APP_SECRET")
    if not app_id or not app_secret:
        return None
    try:
        data = _http_post_json(
            f"{_feishu_base()}/open-apis/auth/v3/tenant_access_token/internal",
            {"app_id": app_id, "app_secret": app_secret}, {}, timeout=10,
        )
    except Exception as exc:
        logger.warning("feishu token fetch failed: %s", exc)
        return None
    token = (data or {}).get("tenant_access_token")
    if not token:
        logger.warning("feishu token fetch bad response: %r", data)
        return None
    # ~2h validity; keep a margin (feishu-history's rule)
    _TOKEN_CACHE.update(token=token, expires_at=now + 90 * 60)
    return token


def _feishu_send_text(chat_id: str, text: str) -> bool:
    token = _feishu_tenant_token()
    if not token:
        return False
    try:
        data = _http_post_json(
            f"{_feishu_base()}/open-apis/im/v1/messages?receive_id_type=chat_id",
            {"receive_id": chat_id, "msg_type": "text",
             "content": __import__("json").dumps({"text": text})},
            {"Authorization": f"Bearer {token}"}, timeout=10,
        )
    except Exception as exc:
        logger.warning("feishu send failed: %s", exc)
        return False
    if (data or {}).get("code") not in (0, None):
        logger.warning("feishu send rejected: %r", data)
        return False
    return True


def _clip(text: Any, limit: int = _CLIP) -> str:
    return str(text or "").strip()[:limit]


def _state_db_path() -> Path:
    home = os.environ.get("HERMES_HOME") or str(Path.home() / ".hermes")
    return Path(home) / "state.db"


def _dm_chat_id(session_key: Optional[str]) -> Optional[str]:
    """`agent:<ns>:feishu:dm:<oc_...>` (exactly 5 segments) -> chat id, else None.

    Thread-scoped or group keys carry extra segments and are rejected; cron
    sessions have a NULL session_key upstream and never reach here.
    """
    if not session_key:
        return None
    parts = session_key.split(":")
    if len(parts) == 5 and parts[2] == "feishu" and parts[3] == "dm":
        return parts[4]
    return None


class ReplyTimeoutPlugin:
    def __init__(self, ctx: Any) -> None:
        self._ctx = ctx
        self._lock = threading.RLock()
        # session_key -> {"timer": threading.Timer, "rec": dict}
        self._live: dict[str, dict] = {}
        self._sk_cache: dict[str, Optional[str]] = {}

    # ------------------------------------------------------------------ config

    def _cfg(self, key: str, default: Any) -> Any:
        try:
            value = self._ctx.get_config(key, default)
        except Exception:
            return default
        return default if value is None else value

    def _announce_enabled(self) -> bool:
        value = self._cfg("announce", True)
        if isinstance(value, str):
            return value.strip().lower() not in ("false", "0", "no", "off")
        return bool(value)

    # ----------------------------------------------------------------- session

    def _resolve_session_key(self, session_id: Optional[str]) -> Optional[str]:
        if not session_id:
            return None
        with self._lock:
            if session_id in self._sk_cache:
                return self._sk_cache[session_id]
        try:
            # Ironclad local rule: state.db is only ever opened mode=ro.
            con = sqlite3.connect(f"file:{_state_db_path()}?mode=ro", uri=True, timeout=5)
            try:
                row = con.execute(
                    "SELECT session_key FROM sessions WHERE id = ?", (session_id,)
                ).fetchone()
            finally:
                con.close()
        except Exception as exc:  # read-only probe failed; stay silent on this turn
            logger.debug("session-key resolve failed for %s: %s", session_id, exc)
            return None
        key = row[0] if row and row[0] else None
        with self._lock:
            self._sk_cache[session_id] = key
        return key

    # ------------------------------------------------------------------- hooks

    def on_post_llm_call(
        self,
        session_id: str = "",
        platform: str = "",
        user_message: str = "",
        assistant_response: str = "",
        **kwargs: Any,
    ) -> None:
        """Turn finished with a final reply -> maybe arm a timer (off-thread)."""
        try:
            if platform != "feishu":
                return
            response = str(assistant_response or "").strip()
            if not response or response == "[SILENT]":
                return
            key = self._resolve_session_key(session_id)
            if not key:
                return
            chat_id = _dm_chat_id(key)
            if not chat_id:
                return
            threading.Thread(
                target=self._worker,
                args=(key, chat_id, user_message, response),
                daemon=True,
                name=f"reply-timeout:{chat_id}",
            ).start()
        except Exception:
            logger.exception("post_llm_call handler error")

    def on_pre_llm_call(
        self, session_id: str = "", user_message: str = "", **kwargs: Any
    ) -> None:
        """A turn is starting off an inbound message. Real user messages cancel
        the pending timer and reset the chain; our own reminders and cron
        mirrors must not."""
        try:
            msg = str(user_message or "").lstrip()
            if msg.startswith(REMINDER_PREFIX) or msg.startswith(CRON_MIRROR_PREFIX):
                return
            key = self._resolve_session_key(session_id)
            if not key or _dm_chat_id(key) is None:
                return
            with self._lock:
                entry = self._live.pop(key, None)
                if entry is not None:
                    entry["timer"].cancel()
                    self._save_timers()
                lf = dict(self._ctx.state.get("last_fire") or {})
                if lf.pop(key, None) is not None:
                    self._ctx.state.set("last_fire", lf)
            if entry is not None:
                logger.info("real inbound cancelled pending timer for %s", key)
        except Exception:
            logger.exception("pre_llm_call handler error")

    # ------------------------------------------------------------------ worker

    def _worker(self, session_key: str, chat_id: str, user_message: str, response: str) -> None:
        try:
            chain = self._next_chain(session_key, user_message)
            if chain is None:
                return
            minutes = self._intent_minutes(user_message, response)
            if minutes <= 0:
                return
            self._arm(session_key, chat_id, minutes, chain)
        except Exception:
            logger.exception("arm worker error")

    def _next_chain(self, session_key: str, user_message: str) -> Optional[int]:
        max_chain = int(self._cfg("max_chain", 3))
        turn_is_reminder = str(user_message or "").lstrip().startswith(REMINDER_PREFIX)
        if turn_is_reminder:
            fired = (self._ctx.state.get("last_fire") or {}).get(session_key) or {}
            chain = int(fired.get("chain", 0)) + 1
        else:
            chain = 1
        if chain > max_chain:
            logger.info(
                "chain cap %d reached for %s; not arming", max_chain, session_key
            )
            return None
        return chain

    def _intent_minutes(self, user_message: str, response: str) -> int:
        prov = self._cfg("intent_provider", None) or None
        mod = self._cfg("intent_model", None) or None
        payload = (
            f"[用户消息]\n{_clip(user_message)}\n\n[助手最终回复]\n{_clip(response)}"
        )
        kwargs = dict(
            instructions=INTENT_INSTRUCTIONS,
            input=[{"type": "text", "text": payload}],
            json_schema=INTENT_SCHEMA,
            schema_name="reply_timeout.minutes",
            temperature=0.0,
            max_tokens=32,
            timeout=45,
            purpose="reply-timeout.intent",
        )
        try:
            result = self._ctx.llm.complete_structured(provider=prov, model=mod, **kwargs)
        except Exception as exc:
            if (prov or mod) and "override" in str(exc).lower():
                logger.warning(
                    "intent model override denied (%s); retrying on session model", exc
                )
                try:
                    result = self._ctx.llm.complete_structured(**kwargs)
                except Exception as exc2:
                    logger.warning("intent call failed: %s", exc2)
                    return 0
            else:
                logger.warning("intent call failed: %s", exc)
                return 0
        parsed = getattr(result, "parsed", None)
        try:
            minutes = int((parsed or {}).get("minutes", 0))
        except Exception:
            logger.warning("intent output unparsable: %r", getattr(result, "text", None))
            return 0
        lo = max(0, int(self._cfg("min_minutes", 1)))
        hi = max(lo, int(self._cfg("max_minutes", 120)))
        if minutes <= 0:
            return 0
        return max(lo, min(hi, minutes))

    # ----------------------------------------------------------------- arming

    def _arm(
        self, session_key: str, chat_id: str, minutes: int, chain: int, announce: bool = True
    ) -> None:
        rec = {
            "session_key": session_key,
            "chat_id": chat_id,
            "minutes": minutes,
            "chain": chain,
            "armed_at": time.time(),
            "expires_at": time.time() + minutes * 60,
        }
        with self._lock:
            old = self._live.pop(session_key, None)
            if old is not None:
                old["timer"].cancel()
            timer = threading.Timer(minutes * 60, self._fire, args=(session_key, rec))
            timer.daemon = True
            self._live[session_key] = {"timer": timer, "rec": rec}
            self._save_timers()
        logger.info("armed %s: %d min (chain %d)", chat_id, minutes, chain)
        if announce and self._announce_enabled():
            self._announce(chat_id, minutes)

    def _fire(self, session_key: str, rec: dict) -> None:
        with self._lock:
            live = self._live.get(session_key)
            if not live or live["rec"] is not rec:  # replaced or cancelled
                return
            del self._live[session_key]
            self._save_timers()
            last_fire = dict(self._ctx.state.get("last_fire") or {})
            last_fire[session_key] = {
                "chain": int(rec.get("chain", 1)),
                "minutes": int(rec.get("minutes", 0)),
                "at": time.time(),
            }
            self._ctx.state.set("last_fire", last_fire)
        message = REMINDER_TEMPLATE.format(minutes=int(rec.get("minutes", 0)))
        try:
            ok = bool(self._ctx.inject_message(message, role="user", session_key=session_key))
        except Exception as exc:
            logger.error("reminder inject raised for %s: %s", session_key, exc)
            return
        if not ok:
            logger.error(
                "reminder inject refused for %s — set "
                "plugins.entries.reply-timeout.allow_gateway_injection: true",
                session_key,
            )
        else:
            logger.info(
                "reminder injected into %s (chain %d)", session_key, rec.get("chain", 1)
            )

    def _announce(self, chat_id: str, minutes: int) -> None:
        text = ANNOUNCE_TEMPLATE.format(minutes=minutes)
        # Preferred: direct Feishu API (fast, no subprocess). `hermes send`
        # loads the whole plugin system -> slow (observed 20s timeout) and was
        # the recursion vehicle before the gateway-gate fix.
        try:
            if _feishu_send_text(chat_id, text):
                return
        except Exception:
            logger.exception("direct feishu announce crashed")
        hermes = shutil.which("hermes") or "hermes"
        try:
            proc = subprocess.run(
                [hermes, "send", "--to", f"feishu:{chat_id}", "--quiet", text],
                timeout=20, capture_output=True, text=True,
            )
            if proc.returncode != 0:
                logger.warning(
                    "announce failed (rc=%d): %s", proc.returncode, (proc.stderr or "").strip()
                )
        except Exception as exc:
            logger.warning("announce failed: %s", exc)

    # ------------------------------------------------------------- persistence

    def _save_timers(self) -> None:
        """Persist the live timer records. Call with the lock held."""
        data = {sk: entry["rec"] for sk, entry in self._live.items()}
        try:
            self._ctx.state.set("timers", data)
        except Exception:
            logger.exception("persisting timers failed")

    def restore_timers(self) -> None:
        """After a restart: re-arm unexpired timers with remaining time, drop
        expired ones. Called once from register() — gateway processes only.
        NEVER announces: an announce here would spawn ``hermes send``, which
        itself loads plugins and would recurse (observed 2026-10-06)."""
        try:
            with self._lock:
                stored = dict(self._ctx.state.get("timers") or {})
                now = time.time()
                valid = []
                for sk, rec in stored.items():
                    chat = _dm_chat_id(sk)
                    remaining_min = (float(rec.get("expires_at", 0)) - now) / 60.0
                    if chat and remaining_min >= 0.5:
                        valid.append(
                            (sk, chat, max(1, round(remaining_min)), int(rec.get("chain", 1)))
                        )
                for entry in self._live.values():
                    entry["timer"].cancel()
                self._live.clear()
                self._ctx.state.set("timers", {})
            for sk, chat, minutes, chain in valid:
                self._arm(sk, chat, minutes, chain, announce=False)
            if valid:
                logger.info("restored %d timer(s) after restart", len(valid))
        except Exception:
            logger.exception("restore_timers failed")

    def cancel_all(self) -> None:
        with self._lock:
            for entry in self._live.values():
                entry["timer"].cancel()
            self._live.clear()
            try:
                self._ctx.state.set("timers", {})
            except Exception:
                pass
        _MODULE_SINGLETON["registered"] = False  # allow a clean re-register after unload
        logger.info("all reply-timeout timers cancelled (unload)")


def _looks_like_gateway() -> bool:
    """Best-effort: is this process the long-lived gateway?

    ``hermes send`` (and other one-shot CLI invocations) also load the plugin
    system, so register()-time side effects must not run there — restoring
    timers inside ``hermes send`` re-armed the timer and re-announced, spawning
    another ``hermes send`` (infinite recursion, observed 2026-10-06).
    """
    argv = [str(a) for a in sys.argv[:4]]
    return any("gateway" in a for a in argv)


_MODULE_SINGLETON = {"registered": False}


def register(ctx: Any) -> None:
    if _MODULE_SINGLETON["registered"]:
        # Same process loaded us twice (no intervening unload) — a second
        # instance would double-fire every hook: two intent calls, two timers,
        # two reminders. Keep exactly one live instance.
        logger.warning(
            "reply-timeout: register() called again without unload; keeping the existing instance"
        )
        return
    _MODULE_SINGLETON["registered"] = True
    plugin = ReplyTimeoutPlugin(ctx)
    ctx.register_hook("post_llm_call", plugin.on_post_llm_call)
    ctx.register_hook("pre_llm_call", plugin.on_pre_llm_call)
    ctx.on_unload(plugin.cancel_all)
    if _looks_like_gateway():
        plugin.restore_timers()
        logger.info("reply-timeout registered (P0: Feishu DM sessions, gateway)")
    else:
        logger.info(
            "reply-timeout registered (P0: Feishu DM sessions, non-gateway argv=%s; timers not restored)",
            sys.argv[:3],
        )
