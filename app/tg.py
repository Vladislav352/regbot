"""Минимальный клиент Telegram Bot API на stdlib (без внешних зависимостей)."""
from __future__ import annotations

import json
import logging
import random
import socket
import time
import urllib.error
import urllib.parse
import urllib.request

API = "https://api.telegram.org"

log = logging.getLogger("tg")

# HTTP-статусы, при которых имеет смысл повторить запрос.
RETRYABLE_STATUS = {429, 500, 502, 503, 504}


class TgError(RuntimeError):
    pass


def _backoff(attempt: int) -> float:
    """Экспоненциальный бэкофф с джиттером: ~1, 2, 4, 8 ... (кап 20с)."""
    return min(2 ** attempt, 20) + random.uniform(0, 0.5)


def _retry_after(body: str) -> float | None:
    """Достать Telegram-подсказку retry_after из тела ответа (флуд-контроль)."""
    try:
        ra = (json.loads(body).get("parameters") or {}).get("retry_after")
        return min(int(ra), 30) if ra else None
    except Exception:
        return None


class Bot:
    def __init__(self, token: str):
        self.token = token
        self._offset = 0

    def _call(self, method: str, params: dict | None = None, timeout: int = 65,
              retries: int = 4) -> dict:
        url = f"{API}/bot{self.token}/{method}"
        data = json.dumps(params or {}).encode()
        last_err: TgError | None = None
        for attempt in range(retries + 1):
            req = urllib.request.Request(
                url, data=data, headers={"Content-Type": "application/json"})
            try:
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    payload = json.loads(resp.read().decode())
            except urllib.error.HTTPError as e:
                body = e.read().decode(errors="replace")[:300]
                last_err = TgError(f"{method}: HTTP {e.code} {body}")
                # 4xx (кроме 429) — логическая ошибка, ретрай не поможет.
                if e.code in RETRYABLE_STATUS and attempt < retries:
                    delay = _retry_after(body) or _backoff(attempt)
                    log.warning("%s: HTTP %s, ретрай через %.1fс (%d/%d)",
                                method, e.code, delay, attempt + 1, retries)
                    time.sleep(delay)
                    continue
                raise last_err from e
            except (urllib.error.URLError, TimeoutError, socket.timeout,
                    json.JSONDecodeError) as e:
                # Транзиентная сетевая ошибка (таймаут, обрыв, SSL, кривой JSON).
                last_err = TgError(f"{method}: {e}")
                if attempt < retries:
                    delay = _backoff(attempt)
                    log.warning("%s: %s, ретрай через %.1fс (%d/%d)",
                                method, e, delay, attempt + 1, retries)
                    time.sleep(delay)
                    continue
                raise last_err from e
            if not payload.get("ok"):
                retry_after = (payload.get("parameters") or {}).get("retry_after")
                if retry_after and attempt < retries:
                    delay = min(int(retry_after), 30)
                    log.warning("%s: flood wait %sс (%d/%d)",
                                method, delay, attempt + 1, retries)
                    time.sleep(delay)
                    continue
                raise TgError(f"{method}: {payload.get('description')}")
            return payload["result"]
        raise last_err or TgError(f"{method}: неизвестная ошибка после ретраев")

    def get_me(self) -> dict:
        return self._call("getMe", timeout=15)

    def get_updates(self, poll_timeout: int = 50) -> list[dict]:
        result = self._call("getUpdates", {
            "offset": self._offset, "timeout": poll_timeout,
            "allowed_updates": ["message", "callback_query"],
        }, timeout=poll_timeout + 15)
        if result:
            self._offset = result[-1]["update_id"] + 1
        return result

    def send_message(self, chat_id: int, text: str, *,
                     reply_markup: dict | None = None,
                     markdown: bool = False) -> dict:
        chunks = [text[i:i + 4000] for i in range(0, len(text), 4000)] or [""]
        last = {}
        for i, chunk in enumerate(chunks):
            params: dict = {"chat_id": chat_id, "text": chunk,
                            "link_preview_options": {"is_disabled": True}}
            if markdown:
                params["parse_mode"] = "Markdown"
            if reply_markup and i == len(chunks) - 1:
                params["reply_markup"] = reply_markup
            try:
                last = self._call("sendMessage", params, timeout=30)
            except TgError:
                if markdown:  # кривая разметка — ретрай без parse_mode
                    params.pop("parse_mode", None)
                    last = self._call("sendMessage", params, timeout=30)
                else:
                    raise
        return last

    def edit_message(self, chat_id: int, message_id: int, text: str) -> None:
        try:
            self._call("editMessageText", {
                "chat_id": chat_id, "message_id": message_id, "text": text[:4000],
                "link_preview_options": {"is_disabled": True},
            }, timeout=30)
        except TgError:
            pass  # не критично

    def answer_callback(self, callback_id: str, text: str = "") -> None:
        try:
            self._call("answerCallbackQuery",
                       {"callback_query_id": callback_id, "text": text}, timeout=15)
        except TgError:
            pass

    def send_chat_action(self, chat_id: int, action: str = "typing") -> None:
        try:
            self._call("sendChatAction", {"chat_id": chat_id, "action": action},
                       timeout=15)
        except TgError:
            pass
