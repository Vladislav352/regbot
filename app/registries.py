"""Проверки реестров: РУ Росздравнадзора и декларации/СГР ФСА.

Codex CLI сейчас используется без веб-инструментов, поэтому живую проверку
реестров не запускаем: отдаём кэш или прямые ссылки для ручной проверки.
Боту запрещено выдумывать содержимое реестров.
"""
from __future__ import annotations

from . import db

LINKS = {
    "rzn": "https://roszdravnadzor.gov.ru/services/misearch",
    "fsa": "https://pub.fsa.gov.ru/rds/declaration",
    "fsa_sgr": "https://pub.fsa.gov.ru/sgr",
}


def _check(registry: str, query: str) -> dict:
    query = query.strip()
    con = db.connect()
    try:
        cached = db.cache_get(con, registry, query.lower())
        if cached:
            cached["from_cache"] = True
            return cached
        return {"status": "unavailable", "results": [],
                "note": "автопроверка через Claude отключена; проверь первоисточник вручную"}
    finally:
        con.close()


def check_ru(query: str) -> dict:
    return _check("rzn", query)


def check_declaration(query: str) -> dict:
    return _check("fsa", query)


def format_result(kind: str, query: str, data: dict) -> str:
    """Человекочитаемый ответ для Telegram."""
    title = "Реестр медизделий Росздравнадзора" if kind == "rzn" \
        else "Реестр деклараций Росаккредитации"
    link = LINKS["rzn"] if kind == "rzn" else LINKS["fsa"]
    head = f"🔎 {title}\nЗапрос: {query}\n"
    cache_mark = " (из кэша, <24ч)" if data.get("from_cache") else ""

    if data.get("status") == "unavailable":
        return (head + "\n⚠️ Реестр сейчас недоступен для автопроверки. "
                f"Проверь вручную: {link}\n"
                + (f"Примечание: {data['note']}" if data.get("note") else ""))
    if data.get("status") == "not_found" or not data.get("results"):
        return (head + f"\nНичего не найдено{cache_mark}. Это НЕ гарантия отсутствия "
                f"записи — проверь варианты написания вручную: {link}")

    lines = [head + f"\nНайдено{cache_mark}:"]
    for r in data["results"][:5]:
        if kind == "rzn":
            lines.append(f"• {r.get('name', '?')}\n  РУ {r.get('ru_number', '?')} "
                         f"от {r.get('ru_date', '?')}, {r.get('manufacturer', '?')} "
                         f"— {r.get('state', '?')}")
        else:
            lines.append(f"• {r.get('number', '?')}\n  {r.get('product', '?')} / "
                         f"{r.get('applicant', '?')} — {r.get('state', '?')}"
                         + (f", до {r['valid_till']}" if r.get("valid_till") else ""))
    if data.get("note"):
        lines.append(f"\nПримечание: {data['note']}")
    lines.append(f"\nПервоисточник: {data.get('checked_url') or link}")
    return "\n".join(lines)
