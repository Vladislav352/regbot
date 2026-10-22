"""РегБот — Telegram-ассистент продуктолога по регуляторике.

Запуск: python3 -m app.bot  (из каталога regbot, токен — в data/config.json)
"""
from __future__ import annotations

import json
import logging
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import db, registries, search
from .tg import Bot, TgError

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "data" / "config.json"
LOG_PATH = ROOT / "logs" / "bot.log"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.FileHandler(LOG_PATH, encoding="utf-8"),
              logging.StreamHandler()],
)
log = logging.getLogger("regbot")

HELP_TEXT = """Я — регуляторный ассистент компании. Что умею:

📚 Просто напиши вопрос — отвечу по базе знаний с указанием источников.
Темы: регистрация медизделий (РУ, классы риска, ПП 1684, ЕАЭС), косметика
(ТР ТС 009/2011, декларации, СГР), таможня и ввоз из Китая (ТН ВЭД, НДС,
Честный знак), формулировки для карточек и упаковки.

🔎 Проверки реестров:
/ru <название | номер РУ | производитель> — ссылка/кэш по реестру медизделий Росздравнадзора
/decl <номер | ИНН | продукция> — ссылка/кэш по реестру деклараций Росаккредитации

Примеры:
/ru пластырь согревающий
/decl 7700000000

⚠️ Я справочник, а не юрист: перед решениями сверяйся с первоисточником —
ссылки даю в каждом ответе."""


def load_config() -> dict:
    if not CONFIG_PATH.exists():
        sys.exit(f"Нет конфига {CONFIG_PATH}. Создай JSON: "
                 '{"token": "...", "admin_ids": [123]}')
    cfg = json.loads(CONFIG_PATH.read_text())
    if not cfg.get("token"):
        sys.exit("В data/config.json не заполнен token")
    return cfg


class RegBot:
    def __init__(self, cfg: dict):
        self.bot = Bot(cfg["token"])
        self.admin_ids: set[int] = set(cfg.get("admin_ids", []))
        self.pool = ThreadPoolExecutor(max_workers=2)
        self.busy: set[int] = set()          # chat_id с запросом в работе
        self.busy_lock = threading.Lock()
        con = db.connect()
        for aid in self.admin_ids:
            db.upsert_user(con, aid, None, None, allowed=True, role="admin")
        con.close()

    # --- доступ -------------------------------------------------------------

    def _allowed(self, tg_id: int) -> bool:
        con = db.connect()
        try:
            return tg_id in self.admin_ids or db.is_allowed(con, tg_id)
        finally:
            con.close()

    # --- обработка входящих ---------------------------------------------------

    def handle_update(self, upd: dict) -> None:
        if "callback_query" in upd:
            self.handle_callback(upd["callback_query"])
            return
        msg = upd.get("message")
        if not msg or "text" not in msg:
            return
        chat_id = msg["chat"]["id"]
        user = msg.get("from", {})
        text = msg["text"].strip()

        con = db.connect()
        db.upsert_user(con, user.get("id", chat_id), user.get("username"),
                       f"{user.get('first_name', '')} {user.get('last_name', '')}".strip())
        con.close()

        if not self._allowed(user.get("id", chat_id)):
            self.bot.send_message(
                chat_id,
                "Это внутренний бот компании. Доступ выдаёт администратор — "
                f"передай ему свой Telegram ID: {user.get('id', chat_id)}")
            log.info("отказ в доступе: %s (@%s)", user.get("id"), user.get("username"))
            return

        con = db.connect()
        db.log_message(con, user.get("id", chat_id), "in", text)
        con.close()

        if text.startswith("/start") or text.startswith("/help"):
            self.bot.send_message(chat_id, HELP_TEXT)
            return
        if text.startswith("/ru"):
            self._spawn(chat_id, self._do_registry, chat_id, "rzn", text[3:].strip())
            return
        if text.startswith("/decl"):
            self._spawn(chat_id, self._do_registry, chat_id, "fsa", text[5:].strip())
            return
        if text.startswith("/adduser"):
            self._cmd_adduser(chat_id, user.get("id"), text)
            return
        if text.startswith("/users"):
            self._cmd_users(chat_id, user.get("id"))
            return
        if text.startswith("/stats"):
            self._cmd_stats(chat_id, user.get("id"))
            return
        if text.startswith("/"):
            self.bot.send_message(chat_id, "Не знаю такой команды. /help")
            return
        # обычный вопрос -> RAG
        self._spawn(chat_id, self._do_answer, chat_id, user.get("id", chat_id), text)

    def _spawn(self, chat_id: int, fn, *args) -> None:
        with self.busy_lock:
            if chat_id in self.busy:
                self.bot.send_message(
                    chat_id, "Ещё думаю над прошлым вопросом — секунду 🙌")
                return
            self.busy.add(chat_id)

        def run():
            try:
                fn(*args)
            except Exception:
                log.exception("ошибка обработчика")
                try:
                    self.bot.send_message(
                        chat_id, "Что-то сломалось при обработке. Попробуй ещё раз; "
                                 "если повторится — скажи администратору.")
                except TgError:
                    pass
            finally:
                with self.busy_lock:
                    self.busy.discard(chat_id)

        self.pool.submit(run)

    # --- сценарии -------------------------------------------------------------

    def _do_answer(self, chat_id: int, tg_id: int, question: str) -> None:
        placeholder = self.bot.send_message(chat_id, "🔍 Ищу в базе и думаю (обычно до минуты)…")
        self.bot.send_chat_action(chat_id)
        t0 = time.time()
        text, meta = search.answer(question)
        meta["sec"] = round(time.time() - t0, 1)

        con = db.connect()
        out_id = db.log_message(con, tg_id, "out", text, meta)
        con.close()

        self.bot.edit_message(chat_id, placeholder.get("message_id", 0), "✅ Готово:")
        self.bot.send_message(chat_id, text, reply_markup={
            "inline_keyboard": [[
                {"text": "👍 Точно", "callback_data": f"fb:up:{out_id}"},
                {"text": "👎 Мимо", "callback_data": f"fb:down:{out_id}"},
            ]]
        })
        log.info("ответ за %ss, движок=%s, чанков=%s",
                 meta["sec"], meta.get("engine"), meta.get("chunks"))

    def _do_registry(self, chat_id: int, kind: str, query: str) -> None:
        if not query:
            example = "/ru пластырь согревающий" if kind == "rzn" else "/decl 7700000000"
            self.bot.send_message(chat_id, f"Добавь запрос, например: {example}")
            return
        self.bot.send_message(chat_id, "🌐 Проверяю кэш реестра и первоисточник…")
        self.bot.send_chat_action(chat_id)
        data = registries.check_ru(query) if kind == "rzn" \
            else registries.check_declaration(query)
        self.bot.send_message(chat_id, registries.format_result(kind, query, data))

    # --- админ ------------------------------------------------------------------

    def _cmd_adduser(self, chat_id: int, tg_id: int, text: str) -> None:
        if tg_id not in self.admin_ids:
            self.bot.send_message(chat_id, "Команда только для администратора.")
            return
        parts = text.split(maxsplit=2)
        if len(parts) < 2 or not parts[1].lstrip("-").isdigit():
            self.bot.send_message(chat_id, "Формат: /adduser <tg_id> [имя]")
            return
        new_id = int(parts[1])
        name = parts[2] if len(parts) > 2 else None
        con = db.connect()
        db.upsert_user(con, new_id, None, name, allowed=True)
        con.close()
        self.bot.send_message(chat_id, f"Готово: {new_id} ({name or 'без имени'}) допущен.")

    def _cmd_users(self, chat_id: int, tg_id: int) -> None:
        if tg_id not in self.admin_ids:
            return
        con = db.connect()
        rows = con.execute(
            "SELECT tg_id, username, full_name, role, is_allowed FROM users").fetchall()
        con.close()
        lines = [f"{'✅' if r['is_allowed'] else '⛔'} {r['tg_id']} "
                 f"@{r['username'] or '—'} {r['full_name'] or ''} [{r['role']}]"
                 for r in rows]
        self.bot.send_message(chat_id, "Пользователи:\n" + "\n".join(lines))

    def _cmd_stats(self, chat_id: int, tg_id: int) -> None:
        if tg_id not in self.admin_ids:
            return
        con = db.connect()
        docs = con.execute("SELECT COUNT(*) c FROM documents").fetchone()["c"]
        chunks = con.execute("SELECT COUNT(*) c FROM chunks").fetchone()["c"]
        msgs = con.execute("SELECT COUNT(*) c FROM messages WHERE direction='in'").fetchone()["c"]
        fb = con.execute(
            "SELECT verdict, COUNT(*) c FROM feedback GROUP BY verdict").fetchall()
        con.close()
        fb_str = ", ".join(f"{r['verdict']}: {r['c']}" for r in fb) or "нет"
        self.bot.send_message(
            chat_id,
            f"📊 База: {docs} документов, {chunks} чанков.\n"
            f"Вопросов получено: {msgs}.\nОценки: {fb_str}.")

    def handle_callback(self, cq: dict) -> None:
        data = cq.get("data", "")
        if data.startswith("fb:"):
            _, verdict, msg_id = data.split(":", 2)
            con = db.connect()
            db.add_feedback(con, int(msg_id), cq["from"]["id"], verdict)
            con.close()
            self.bot.answer_callback(cq["id"],
                                     "Спасибо!" if verdict == "up" else
                                     "Записал — разберёмся, что не так.")

    # --- главный цикл ------------------------------------------------------------

    def run(self) -> None:
        me = self.bot.get_me()
        log.info("бот @%s запущен", me.get("username"))
        while True:
            try:
                for upd in self.bot.get_updates():
                    self.handle_update(upd)
            except TgError as e:
                log.warning("сбой поллинга: %s — пауза 10с", e)
                time.sleep(10)
            except Exception:
                log.exception("неожиданная ошибка цикла — пауза 15с")
                time.sleep(15)


def main() -> None:
    db.init()
    RegBot(load_config()).run()


if __name__ == "__main__":
    main()
