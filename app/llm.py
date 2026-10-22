"""Маршрутизатор LLM: Codex CLI через ChatGPT-подписку.

Вызов headless:
  codex exec --skip-git-repo-check -s read-only -m gpt-5.5 "<prompt>"
"""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
import time
from pathlib import Path

STATE_PATH = Path(__file__).resolve().parent.parent / "data" / "engine_state.json"
COOLDOWN_SEC = 30 * 60
TIMEOUT_SEC = 240
CODEX_MODEL = os.environ.get("REGBOT_CODEX_MODEL", "gpt-5.5")

LIMIT_MARKERS = (
    "usage limit", "rate limit", "quota", "limit reached", "overloaded",
    "insufficient", "429", "exceeded",
)


class LLMError(RuntimeError):
    pass


def _load_state() -> dict:
    try:
        return json.loads(STATE_PATH.read_text())
    except (OSError, ValueError):
        return {}


def _save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state))


def _subprocess_env() -> dict[str, str]:
    env = os.environ.copy()
    # Боты должны использовать пользовательский ~/.codex с ChatGPT-входом,
    # а не CODEX_HOME текущего агента/службы, если он вдруг унаследован.
    env.pop("CODEX_HOME", None)
    return env


def _run(cmd: list[str], prompt: str) -> str:
    proc = subprocess.run(
        cmd, input=prompt, capture_output=True, text=True, timeout=TIMEOUT_SEC,
        env=_subprocess_env(),
    )
    out = (proc.stdout or "").strip()
    err = (proc.stderr or "").strip()
    if proc.returncode != 0 or not out:
        raise LLMError(f"{cmd[0]} rc={proc.returncode}: {err[:500] or out[:500]}")
    return out


def _call_codex(prompt: str, web: bool = False) -> str:
    # codex: веб-доступ в read-only песочнице недоступен, параметр игнорируем
    with tempfile.NamedTemporaryFile("w+", delete=False) as fh:
        out_path = fh.name
    try:
        _run(
            [
                "codex", "exec", "--skip-git-repo-check", "-s", "read-only",
                "-m", CODEX_MODEL, "-o", out_path, "-",
            ],
            prompt,
        )
        out = Path(out_path).read_text().strip()
        if not out:
            raise LLMError("codex вернул пустой финальный ответ")
        return out
    finally:
        try:
            Path(out_path).unlink()
        except OSError:
            pass


ENGINES = {"codex": _call_codex}
ORDER = ["codex"]


def _looks_like_limit(exc: Exception) -> bool:
    msg = str(exc).lower()
    return any(m in msg for m in LIMIT_MARKERS)


def complete(prompt: str, web: bool = False) -> tuple[str, str]:
    """Вернуть (ответ, имя_движка). Пробует основной движок, при лимите —
    фолбэк; исчерпанный движок помечается на COOLDOWN_SEC."""
    state = _load_state()
    now = time.time()
    order = sorted(
        ORDER,
        key=lambda e: state.get(f"cooldown_{e}", 0) > now,  # остывшие — в конец
    )
    last_exc: Exception | None = None
    for engine in order:
        if state.get(f"cooldown_{engine}", 0) > now and engine != order[-1]:
            continue
        try:
            answer = ENGINES[engine](prompt, web)
            return answer, engine
        except (LLMError, subprocess.TimeoutExpired, OSError) as exc:
            last_exc = exc
            if _looks_like_limit(exc):
                state[f"cooldown_{engine}"] = now + COOLDOWN_SEC
                _save_state(state)
            continue
    raise LLMError(f"Все движки недоступны: {last_exc}")


if __name__ == "__main__":
    ans, eng = complete("Ответь одним словом: работает ли этот вызов?")
    print(f"[{eng}] {ans}")
