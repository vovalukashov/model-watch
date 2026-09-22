#!/usr/bin/env python3
"""
model-watch — git-scraping для каталогов моделей.

Что делает за один прогон:
  1. читает sources.json;
  2. скачивает каждый источник (JSON или текст);
  3. нормализует его и вытаскивает список model id;
  4. пишет снимок в data/<name>.json и data/<name>.ids.txt;
  5. сравнивает ids с последним коммитом (git diff) и шлёт
     добавленные/удалённые id в Telegram (или в stdout, если токена нет).

Коммит и push делает GitHub Actions после скрипта (см. .github/workflows/watch.yml).
Зависимостей нет — только стандартная библиотека Python 3.10+.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
SOURCES = ROOT / "sources.json"

USER_AGENT = "model-watch/1.0 (+https://github.com/; git-scraping bot)"
TIMEOUT = 30
TG_LIMIT = 3900  # лимит sendMessage — 4096 символов, оставляем запас

ENV_RE = re.compile(r"\$\{(\w+)\}")

DETAIL_LIMIT = 6      # сколько добавленных id расписывать подробно
DETAIL_BUDGET = 220   # символов на одну строку с деталями

# поля, ради которых в каталог и лезут: цена, контекст, дата
INTEREST_RE = re.compile(r"(?i)(cost|price|limit|context|window|tokens|created|release)")


# ---------- helpers ---------------------------------------------------------

def expand_env(value: str) -> str | None:
    """Подставляет ${VAR} из окружения. Если переменной нет — возвращает None."""
    missing = False

    def repl(m: re.Match) -> str:
        nonlocal missing
        v = os.environ.get(m.group(1), "")
        if not v:
            missing = True
        return v

    out = ENV_RE.sub(repl, value)
    return None if missing else out


def fetch(url: str, headers: dict[str, str]) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, **headers})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        return resp.read()


def walk_values(obj, key: str):
    """Рекурсивно собирает значения всех полей с именем key."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k == key and isinstance(v, (str, int, float)):
                yield str(v)
            else:
                yield from walk_values(v, key)
    elif isinstance(obj, list):
        for item in obj:
            yield from walk_values(item, key)


def extract_ids(raw: bytes, src: dict) -> tuple[str, list[str], object]:
    """
    Возвращает (нормализованный текст снимка, отсортированный список id,
    разобранный JSON или None для текстовых источников).

    Режимы извлечения (поле "extract"):
      regex     — все совпадения "pattern" в тексте;
      key       — значения всех полей "key" (по умолчанию "id") в JSON;
      top_keys  — ключи верхнего уровня JSON-объекта.
    """
    kind = src.get("kind", "json")
    mode = src.get("extract", "regex" if kind == "text" else "key")

    if kind == "json":
        parsed = json.loads(raw.decode("utf-8"))
        text = json.dumps(parsed, ensure_ascii=False, indent=1, sort_keys=True)
    else:
        parsed = None
        text = raw.decode("utf-8", errors="replace")

    if mode == "regex":
        pattern = re.compile(src["pattern"])
        found = [m.group(1) if m.groups() else m.group(0) for m in pattern.finditer(text)]
    elif mode == "key":
        if parsed is None:
            raise ValueError("extract=key требует kind=json")
        found = list(walk_values(parsed, src.get("key", "id")))
    elif mode == "top_keys":
        if not isinstance(parsed, dict):
            raise ValueError("extract=top_keys требует JSON-объект на верхнем уровне")
        found = list(parsed.keys())
    else:
        raise ValueError(f"неизвестный extract: {mode}")

    only = src.get("only")
    if only:
        only_re = re.compile(only)
        found = [f for f in found if only_re.search(f)]

    ids = sorted(set(found))
    return text, ids, parsed


def walk_dicts(obj):
    """Рекурсивно обходит все словари внутри разобранного JSON."""
    if isinstance(obj, dict):
        yield obj
        for v in obj.values():
            yield from walk_dicts(v)
    elif isinstance(obj, list):
        for item in obj:
            yield from walk_dicts(item)


def find_entry(parsed, src: dict, model_id: str):
    """Находит объект модели по её id. None, если источник текстовый или объекта нет."""
    if not isinstance(parsed, (dict, list)):
        return None
    if src.get("extract") == "top_keys":
        entry = parsed.get(model_id) if isinstance(parsed, dict) else None
        return entry if isinstance(entry, dict) else None
    key = src.get("key", "id")
    for d in walk_dicts(parsed):
        if str(d.get(key, "")) == model_id:
            return d
    return None


def flatten(entry: dict, prefix: str = ""):
    """Разворачивает вложенные словари в пары «a.b → значение»."""
    for k, v in entry.items():
        name = f"{prefix}{k}"
        if isinstance(v, dict):
            yield from flatten(v, f"{name}.")
        elif isinstance(v, (str, int, float, bool)):
            yield name, v


def summarize(entry, budget: int = DETAIL_BUDGET) -> str:
    """Оставляет из объекта модели только цену, контекст и даты."""
    if not isinstance(entry, dict):
        return ""
    fields = [(k, v) for k, v in flatten(entry) if INTEREST_RE.search(k)]
    # кэш-тарифы интересны реже базовых, поэтому уезжают в хвост и под обрезку
    fields.sort(key=lambda kv: "cache" in kv[0].lower())
    parts = [f"{k}={v}" for k, v in fields]
    if not parts:
        return ""
    out = ", ".join(parts)
    return out if len(out) <= budget else out[: budget - 1] + "…"


def format_change(src: dict, added: list[str], removed: list[str], parsed) -> str:
    """Блок сообщения по одному источнику: что добавилось (с деталями) и что исчезло."""
    lines = [f"• {src['name']} ({src['url']})"]
    for i, a in enumerate(added):
        detail = summarize(find_entry(parsed, src, a)) if i < DETAIL_LIMIT else ""
        lines.append(f"  + {a}" + (f"\n      {detail}" if detail else ""))
    lines += [f"  − {r}" for r in removed]
    return "\n".join(lines)


def git(*args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=ROOT, capture_output=True, text=True, check=False
    ).stdout


def diff_ids(ids_path: Path) -> tuple[list[str], list[str], bool]:
    """Возвращает (added, removed, is_new) для файла ids относительно HEAD."""
    rel = str(ids_path.relative_to(ROOT))
    status = git("status", "--porcelain", "--", rel).strip()
    if status.startswith("??"):
        return [], [], True
    out = git("diff", "--unified=0", "--", rel)
    added = [l[1:] for l in out.splitlines() if l.startswith("+") and not l.startswith("+++")]
    removed = [l[1:] for l in out.splitlines() if l.startswith("-") and not l.startswith("---")]
    return added, removed, False


def send_telegram(text: str) -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        print("\n[telegram не настроен — вывожу сообщение сюда]\n" + text)
        return
    if len(text) > TG_LIMIT:
        text = text[: TG_LIMIT - 20] + "\n…(обрезано)"
    payload = json.dumps(
        {"chat_id": chat_id, "text": text, "disable_web_page_preview": True}
    ).encode("utf-8")
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            resp.read()
    except urllib.error.URLError as e:
        print(f"[telegram] ошибка отправки: {e}", file=sys.stderr)


# ---------- main ------------------------------------------------------------

def main() -> int:
    DATA.mkdir(exist_ok=True)
    sources = json.loads(SOURCES.read_text(encoding="utf-8"))

    report: list[str] = []
    errors: list[str] = []

    for src in sources:
        name = src["name"]
        if src.get("disabled"):
            continue

        # заголовки с секретами; если секрет не задан — источник пропускаем молча
        headers: dict[str, str] = {}
        skip = False
        for k, v in src.get("headers", {}).items():
            expanded = expand_env(v)
            if expanded is None:
                skip = True
                break
            headers[k] = expanded
        if skip:
            print(f"[{name}] пропущен: не задан секрет для заголовков")
            continue

        try:
            raw = fetch(src["url"], headers)
            text, ids, parsed = extract_ids(raw, src)
        except (urllib.error.URLError, ValueError, json.JSONDecodeError, KeyError) as e:
            msg = f"[{name}] ошибка: {e}"
            print(msg, file=sys.stderr)
            errors.append(msg)
            continue

        if src.get("keep_full", True):
            (DATA / f"{name}.json").write_text(text + "\n", encoding="utf-8")
        ids_path = DATA / f"{name}.ids.txt"
        ids_path.write_text("\n".join(ids) + ("\n" if ids else ""), encoding="utf-8")

        added, removed, is_new = diff_ids(ids_path)
        if not src.get("alert_removed", True):
            removed = []  # для "скользящих" лент (Atom-фиды коммитов) исчезновение id — не событие
        print(f"[{name}] ids={len(ids)} +{len(added)} -{len(removed)}" + (" (первый снимок)" if is_new else ""))

        if is_new:
            report.append(f"• {name}: первый снимок, {len(ids)} id")
        elif added or removed:
            report.append(format_change(src, added, removed, parsed))

    if report:
        send_telegram("model-watch: изменения\n\n" + "\n\n".join(report))
    else:
        print("изменений нет")

    if errors and os.environ.get("REPORT_ERRORS") == "1":
        send_telegram("model-watch: ошибки\n\n" + "\n".join(errors))

    return 0


if __name__ == "__main__":
    sys.exit(main())
