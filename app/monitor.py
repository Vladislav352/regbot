"""Еженедельный мониторинг регуляторных изменений -> дайджест в Telegram.

Запуск (launchd, понедельник 08:30): python3 -m app.monitor
Ручной прогон без отправки:          python3 -m app.monitor --dry-run
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import re
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

from . import db, llm
from .tg import Bot, TgError

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "data" / "config.json"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("monitor")

# RSS/новостные источники. Держать короткими и официальными.
SOURCES = [
    ("alta_laws", "https://www.alta.ru/rss/laws_news/"),
    ("alta_news", "https://www.alta.ru/rss/news/"),
    ("rzn_news", "https://roszdravnadzor.gov.ru/news/rss"),
]

# фильтр релевантности по заголовку
KEYWORDS = re.compile(
    r"медицинск|медиздели|косметик|парфюмер|009/2011|пластыр|"
    r"регистрац|декларац|сертифика|росздрав|росаккредитац|"
    r"маркировк|честн\w* знак|тн вэд|ндс|таможен|еаэс|ввоз|импорт",
    re.IGNORECASE,
)

DIGEST_PROMPT = """Ты — регуляторный ассистент компании (пластыри/патчи,
мази, гели; медизделия и косметика; импорт из Китая; продажи на Ozon/WB).

Ниже — свежие новости из мониторинга. Составь короткий дайджест для продуктолога:
1. Отбери ТОЛЬКО то, что реально касается компании (регистрация медизделий,
   ТР ТС 009/2011, декларации, маркировка Честный знак, ТН ВЭД/пошлины/НДС на
   пластыри и косметику, правила маркетплейсов). Остальное молча выброси.
   Если по заголовку неясно — открой новость по ссылке (WebFetch) и проверь,
   касаются ли перечни/изменения наших категорий.
2. По каждой отобранной новости: одна строка сути + почему это важно для нас + ссылка.
3. Если важного нет — так и напиши одной строкой: «За неделю значимых изменений
   не замечено».
Формат: обычный текст, без markdown-заголовков. Начни с «📰 Регуляторный дайджест».

Новости:
{items}"""


def fetch_rss(url: str) -> list[dict]:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (regbot)"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        raw = resp.read()
    items = []
    try:
        root = ET.fromstring(raw)
        for it in root.iter("item"):
            title = (it.findtext("title") or "").strip()
            link = (it.findtext("link") or "").strip()
            if title and link:
                items.append({"title": title, "url": link})
    except ET.ParseError:
        # кривой XML (РЗН грешит) — грубый разбор регэкспом
        text = raw.decode("utf-8", errors="replace")
        for m in re.finditer(
                r"<item>.*?<title>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</title>"
                r".*?<link>(.*?)</link>", text, re.S):
            title, link = m.group(1).strip(), m.group(2).strip()
            if title and link:
                items.append({"title": title, "url": link})
    return items


def collect() -> list[dict]:
    con = db.connect()
    fresh = []
    for source, url in SOURCES:
        try:
            items = fetch_rss(url)
        except Exception as e:  # источник лежит — не валим весь прогон
            log.warning("источник %s недоступен: %s", source, e)
            continue
        for it in items:
            if not KEYWORDS.search(it["title"]):
                continue
            guid = hashlib.sha256(it["url"].encode()).hexdigest()[:24]
            try:
                con.execute(
                    "INSERT INTO monitor_items(source, guid, title, url) VALUES(?,?,?,?)",
                    (source, guid, it["title"], it["url"]))
                fresh.append(it)
            except Exception:
                pass  # уже видели (UNIQUE guid)
        con.commit()
    con.close()
    return fresh


def build_digest(items: list[dict]) -> str:
    listing = "\n".join(f"- {it['title']} — {it['url']}" for it in items[:60])
    text, engine = llm.complete(DIGEST_PROMPT.format(items=listing), web=True)
    log.info("дайджест собран движком %s из %d новостей", engine, len(items))
    return text


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    db.init()
    fresh = collect()
    log.info("новых релевантных новостей: %d", len(fresh))

    if not fresh:
        digest = ("📰 Регуляторный дайджест\nЗа неделю новых релевантных "
                  "публикаций в отслеживаемых источниках не появилось.")
    else:
        digest = build_digest(fresh)

    if args.dry_run:
        print(digest)
        return

    cfg = json.loads(CONFIG_PATH.read_text())
    bot = Bot(cfg["token"])
    con = db.connect()
    sent_to = []
    for tg_id in db.allowed_user_ids(con):
        try:
            bot.send_message(tg_id, digest)
            sent_to.append(tg_id)
        except TgError as e:
            log.warning("не доставлено %s: %s", tg_id, e)
    con.execute("INSERT INTO digests(text, items, sent_to) VALUES(?,?,?)",
                (digest, len(fresh), json.dumps(sent_to)))
    con.execute("UPDATE monitor_items SET digested=1 WHERE digested=0")
    con.commit()
    con.close()
    log.info("дайджест отправлен: %s", sent_to)


if __name__ == "__main__":
    main()
