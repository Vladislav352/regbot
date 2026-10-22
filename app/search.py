"""Поиск по базе знаний и генерация ответа с цитатами."""
from __future__ import annotations

import json
import re
import sqlite3

from . import db, llm

TOP_K = 10
MAX_CONTEXT_CHARS = 24000

QUERY_PROMPT = """Ты — поисковый модуль регуляторного ассистента компании \
(пластыри, мази, гели; медизделия и косметика; импорт из Китая; продажи на Ozon/WB).

Вопрос пользователя: «{question}»

База — SQLite FTS5 (без стемминга, русские тексты НПА). Составь 4-6 поисковых \
запросов. Правила: слова через OR, к корням слов добавляй * (например: регистрац* \
OR медицинск* OR изделий*), включай номера документов если релевантны (1684, 688, \
1042, "009/2011", 3304 и т.п.), синонимы и официальные формулировки.

Ответь СТРОГО JSON-массивом строк, без пояснений. Пример:
["регистрац* OR удостоверен* OR РУ", "1684 OR 1416", "класс* OR риск* OR НКМИ"]"""

ANSWER_PROMPT = """Ты — регуляторный ассистент компании. Пользователь — \
продуктолог (не юрист). Продукция: пластыри, патчи, мази, гели, бальзамы, спреи; \
производство в Китае; два трека вывода на рынок: медизделие (РУ Росздравнадзора) \
или косметика (ТР ТС 009/2011); продажи на Ozon/WB.

Вопрос: «{question}»

Фрагменты из базы знаний (собрана 03.09.2026):
{context}

Правила ответа:
1. Отвечай по-русски, по делу, структурно. Простым языком, термины поясняй.
2. Каждое существенное утверждение снабжай источником в формате [название документа].
3. Если фрагменты противоречат друг другу или их не хватает — скажи прямо и укажи, \
где проверить первоисточник (реестр, официальный сайт).
4. Даты, ставки, дедлайны — из фрагментов, не из головы. Если данных в фрагментах \
нет — не выдумывай.
5. В конце, если тема «живая» (ставки, перечни, сроки, маркировка), добавь одну \
строку: «⚠️ Данные из базы на 03.09.2026 — перед решением сверься с первоисточником».
6. Это справка, не юридическое заключение — не пиши длинных дисклеймеров, хватит п.5.
7. Максимум ~350 слов, если вопрос не требует большего."""


def _fts_query(con: sqlite3.Connection, q: str, limit: int) -> list[sqlite3.Row]:
    try:
        return con.execute(
            "SELECT c.id, c.doc_id, c.heading, c.text, d.title, d.edition_date, "
            "d.source_url, d.volatile, bm25(chunks_fts) AS score "
            "FROM chunks_fts JOIN chunks c ON c.id = chunks_fts.rowid "
            "JOIN documents d ON d.id = c.doc_id "
            "WHERE chunks_fts MATCH ? ORDER BY score LIMIT ?",
            (q, limit),
        ).fetchall()
    except sqlite3.OperationalError:
        return []  # кривой синтаксис MATCH — пропускаем этот запрос


def _fallback_queries(question: str) -> list[str]:
    words = [w for w in re.findall(r"[А-Яа-яA-Za-z0-9/]{3,}", question)][:8]
    if not words:
        return []
    return [" OR ".join(f"{w}*" for w in words)]


def retrieve(con: sqlite3.Connection, question: str) -> tuple[list[dict], str]:
    """Вернуть (чанки, движок_генерации_запросов)."""
    engine = "fallback"
    queries: list[str] = []
    try:
        raw, engine = llm.complete(QUERY_PROMPT.format(question=question))
        m = re.search(r"\[.*\]", raw, re.S)
        if m:
            queries = [q for q in json.loads(m.group(0)) if isinstance(q, str)][:6]
    except (llm.LLMError, ValueError):
        pass
    if not queries:
        queries = _fallback_queries(question)

    seen: dict[int, dict] = {}
    for q in queries:
        for row in _fts_query(con, q, TOP_K):
            if row["id"] not in seen:
                seen[row["id"]] = dict(row)
    ranked = sorted(seen.values(), key=lambda r: r["score"])[:TOP_K]
    return ranked, engine


def answer(question: str) -> tuple[str, dict]:
    """Полный конвейер: поиск -> ответ. Возвращает (текст, метаданные)."""
    con = db.connect()
    try:
        chunks, q_engine = retrieve(con, question)
    finally:
        con.close()

    if not chunks:
        return (
            "В базе знаний ничего не нашлось по этому вопросу. Попробуй "
            "переформулировать или спроси /help — что я умею.",
            {"engine": q_engine, "chunks": 0},
        )

    parts, used = [], []
    size = 0
    for ch in chunks:
        frag = (f"--- Источник: {ch['title']}"
                + (f" (ред. {ch['edition_date']})" if ch["edition_date"] else "")
                + (" [⚠️ волатильный]" if ch["volatile"] else "")
                + f"\n{ch['text']}")
        if size + len(frag) > MAX_CONTEXT_CHARS:
            break
        parts.append(frag)
        used.append(ch["title"])
        size += len(frag)

    text, engine = llm.complete(
        ANSWER_PROMPT.format(question=question, context="\n\n".join(parts))
    )
    return text, {"engine": engine, "chunks": len(parts), "sources": used[:10]}
