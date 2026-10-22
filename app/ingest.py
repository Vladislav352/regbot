"""Загрузка корпуса (corpus/) в SQLite: documents + chunks + FTS.

Запуск:  python3 -m app.ingest          # залить всё новое
         python3 -m app.ingest --reset  # пересобрать с нуля
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

from . import db

ROOT = Path(__file__).resolve().parent.parent
CORPUS = ROOT / "corpus"

MAX_CHUNK = 2600   # символов
OVERLAP = 200

# документы, которые быстро устаревают (ставки, перечни, сроки)
VOLATILE_HINTS = ("688", "1042", "маркировк", "тн вэд", "ставк", "перечень",
                  "честн", "2214", "переходн")


def split_chunks(text: str) -> list[tuple[str, str]]:
    """Режем по заголовкам (markdown/«Статья N»/«N.») и склеиваем до MAX_CHUNK.
    Возвращает [(heading, chunk_text)]."""
    lines = text.splitlines()
    blocks: list[tuple[str, list[str]]] = []
    heading = ""
    buf: list[str] = []
    head_re = re.compile(
        r"^(#{1,4}\s+.+|Статья\s+\d+|Глава\s+[IVXLC\d]+|Раздел\s+[IVXLC\d]+|"
        r"Приложение\s+(№\s*)?\d+|\d+\.\s+[А-ЯA-Z].{3,80})$"
    )
    for line in lines:
        if head_re.match(line.strip()):
            if buf:
                blocks.append((heading, buf))
            heading = line.strip().lstrip("# ")
            buf = []
        else:
            buf.append(line)
    if buf:
        blocks.append((heading, buf))

    chunks: list[tuple[str, str]] = []
    for head, blines in blocks:
        blob = "\n".join(blines).strip()
        if not blob and not head:
            continue
        body = (head + "\n" + blob).strip() if head else blob
        while len(body) > MAX_CHUNK:
            cut = body.rfind("\n", MAX_CHUNK - 600, MAX_CHUNK)
            if cut == -1:
                cut = MAX_CHUNK
            chunks.append((head, body[:cut].strip()))
            body = body[max(0, cut - OVERLAP):]
        if body.strip():
            chunks.append((head, body.strip()))
    return [(h, c) for h, c in chunks if len(c) > 40]


def read_text(path: Path) -> str | None:
    if path.suffix.lower() in (".txt", ".md"):
        return path.read_text(encoding="utf-8", errors="replace")
    if path.suffix.lower() == ".pdf":
        try:
            import subprocess
            out = subprocess.run(["pdftotext", "-enc", "UTF-8", str(path), "-"],
                                 capture_output=True, text=True, timeout=120)
            if out.returncode == 0 and out.stdout.strip():
                return out.stdout
        except (OSError, subprocess.TimeoutExpired):
            pass
        print(f"  ! не смог извлечь текст из PDF: {path.name}", file=sys.stderr)
    return None


def load_manifest(folder: Path) -> dict[str, dict]:
    merged: dict[str, dict] = {}
    for mf in sorted(folder.glob("_manifest*.json")):
        try:
            items = json.loads(mf.read_text(encoding="utf-8"))
        except ValueError:
            continue
        for it in items:
            if isinstance(it, dict) and "file" in it:
                merged[Path(it["file"]).name] = it
    return merged


def ingest_folder(con, folder: Path, category: str) -> int:
    if not folder.exists():
        return 0
    manifest = load_manifest(folder)
    count = 0
    for path in sorted(folder.iterdir()):
        if path.name.startswith("_") or path.name.startswith("."):
            continue
        if path.suffix.lower() not in (".txt", ".md", ".pdf"):
            continue
        rel = str(path.relative_to(ROOT))
        exists = con.execute("SELECT id FROM documents WHERE file=?", (rel,)).fetchone()
        if exists:
            continue
        text = read_text(path)
        if not text or len(text.strip()) < 100:
            continue
        meta = manifest.get(path.name, {})
        title = meta.get("title") or path.stem.replace("_", " ").replace("-", " ")
        volatile = int(any(h in (title + rel).lower() for h in VOLATILE_HINTS))
        cur = con.execute(
            "INSERT INTO documents(file, title, doc_type, number, doc_date, "
            "edition_date, source_url, category, volatile) VALUES(?,?,?,?,?,?,?,?,?)",
            (rel, title, meta.get("doc_type"), meta.get("number"),
             meta.get("date"), meta.get("edition_date"), meta.get("source_url"),
             category, volatile),
        )
        doc_id = cur.lastrowid
        for seq, (head, chunk) in enumerate(split_chunks(text)):
            con.execute(
                "INSERT INTO chunks(doc_id, seq, heading, text) VALUES(?,?,?,?)",
                (doc_id, seq, head, chunk),
            )
        con.commit()
        count += 1
        print(f"  + [{category}] {title[:80]}")
    return count


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--reset", action="store_true")
    args = ap.parse_args()

    db.init()
    con = db.connect()
    if args.reset:
        con.execute("DELETE FROM chunks")
        con.execute("DELETE FROM documents")
        con.commit()

    total = 0
    total += ingest_folder(con, CORPUS / "reviews", "review")
    total += ingest_folder(con, CORPUS / "raw" / "med", "med")
    total += ingest_folder(con, CORPUS / "raw" / "cosmetics", "cosmetics")
    total += ingest_folder(con, CORPUS / "raw" / "customs", "customs")

    docs = con.execute("SELECT COUNT(*) c FROM documents").fetchone()["c"]
    chunks = con.execute("SELECT COUNT(*) c FROM chunks").fetchone()["c"]
    print(f"Готово: +{total} новых документов; всего {docs} документов, {chunks} чанков.")
    con.close()


if __name__ == "__main__":
    main()
