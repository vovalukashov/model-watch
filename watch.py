#!/usr/bin/env python3
"""
model-watch: git-scraping for model catalogs.

One run:
  1. reads sources.json;
  2. downloads every source (JSON or text);
  3. normalizes it and extracts the list of model ids;
  4. writes the snapshot to data/<name>.json and data/<name>.ids.txt;
  5. diffs the ids against the last commit (git diff); a report with signs of new models goes
     to Telegram (or to stdout when there is no token), routine changes only to the log;
  6. keeps every name it has ever seen in data/seen.tsv and reports on its own when a fresh
     leak leaves a catalog and when a pulled name comes back.

python watch.py --check-chain checks that the tick.yml chain is alive (the fallback schedule
in watch.yml runs it); python watch.py --ai-selftest tests the AI summary.

GitHub Actions commits and pushes after the script (see .github/workflows/watch.yml).
No dependencies: the Python 3.10+ standard library only. The AI summary needs the anthropic
package (requirements.txt); without it the plain report arrives.
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
from datetime import datetime, timedelta, timezone
from pathlib import Path

try:
    import anthropic
except ImportError:  # the AI summary is optional; the watcher itself runs on the standard library alone
    anthropic = None

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
SOURCES = ROOT / "sources.json"

USER_AGENT = "model-watch/1.0 (+https://github.com/; git-scraping bot)"
TIMEOUT = 30
TG_LIMIT = 3900  # лимит sendMessage — 4096 символов, оставляем запас

ENV_RE = re.compile(r"\$\{(\w+)\}")

DETAIL_LIMIT = 6      # added ids per source that get a details line
DETAIL_BUDGET = 220   # characters per details line
REMOVED_LIMIT = 10    # removed ids listed per source before the rest collapse into a count

SEEN_FILE = "seen.tsv"  # every model name any source ever listed; only grows, so novelty survives rolling feeds
TIME_FORMAT = "%Y-%m-%dT%H:%MZ"
PULLED_WITHIN = timedelta(days=7)  # a name this fresh that leaves a catalog was most likely a pulled leak
BROKEN_LIST_MIN = 10  # a catalog this long that loses more than half of it in one check was fetched wrong
CHAIN_STALE_AFTER = timedelta(minutes=30)  # tick.yml starts a check every ~10 minutes; three missed ones is a stop

# the fields people open a catalog for: price, context window, dates
INTEREST_RE = re.compile(r"(?i)(cost|price|limit|context|window|tokens|created|release)")

# The model is reached through ai-proxy/ on Vercel: GitHub's OIDC token proves this workflow to the proxy,
# and the proxy calls AI Gateway with Vercel's own OIDC token, so no API key exists anywhere.
AI_MODEL = "xiaomi/mimo-v2.6-flash"  # the proxy allowlists it and falls back to anthropic/claude-haiku-4.5
AI_AUDIENCE = "model-watch-ai"       # must match AUDIENCE in ai-proxy/lib/github-oidc.ts
AI_MAX_TOKENS = 4096         # thinking plus a few lines of text; the proxy refuses anything above
AI_TIMEOUT = 120             # seconds per attempt; the SDK retries 429 and 5xx twice on its own
AI_NOVEL_LIMIT = 20          # novel names sent to the model per run
AI_SEEN_IN_LIMIT = 6         # appearances listed per novel name
AI_PRICE_PER_MTOK = {        # USD per million input/output tokens, AI Gateway list price as of 2026-09-23
    "xiaomi/mimo-v2.6-flash": (0.14, 0.28),
    "anthropic/claude-haiku-4.5": (1.00, 5.00),
}

AI_SYSTEM_PROMPT = """\
You write the alert text for model-watch, a bot that watches AI model catalogs and client source code for \
signs of new models and plans from OpenAI, Anthropic, Google and xAI. One person reads your text in Telegram on a phone \
and decides whether to look closer.

The user message is JSON describing one check:
- novel: model (or plan) names that appeared in this check and had never been listed by any watched source before; a name \
that only a commit feed had mentioned counts as novel the first time a catalog lists it. Each entry has the \
sources that listed it, the raw ids there, and catalog details (prices, context window, release date) when a \
catalog gave them. Names are normalised: dots became dashes, date suffixes and provider prefixes \
were dropped, so claude-opus-5-5 is Claude Opus 5.5.
- more_novel: how many further novel names were left out.
- other_changes: per source, additions of already-known models and removals, with a few examples; \
first_snapshot means a source was just connected.

What the sources are:
- azure-foundry-playground: the model registry behind Microsoft's Azure AI Foundry playground. Names have shown \
up here before launch; a new one usually means a deployment is being staged.
- openrouter: OpenRouter's public model list. Labs test stealth models here under codenames such as \
openrouter/<name>-alpha, and such a model can be called right away.
- models-dev, litellm-prices: community catalogs with prices and context windows, usually updated on release \
day or shortly before.
- openai-api, anthropic-api: the official model lists visible to the owner's API key; a new id there is \
available to the owner now.
- chatgpt-plans: the ChatGPT subscription plans the Codex client's source code knows. A new name here is a plan \
OpenAI is preparing, not a model: promax, a tier above Pro, appeared here in September 2026.
- feed-openai-codex, feed-anthropic-claude-code: names a regex found in recent commits of the Codex and Claude \
Code clients. The earliest and noisiest signal; the regex also catches crate names, feature flags and branch names.
- chatgpt-web, claude-web: names a regex found in the publicly served JavaScript of the ChatGPT and Claude web \
apps, read through a headless browser a few times a day. Strings land in those bundles days before launch — the \
promax plan name surfaced in chatgpt.com's code two hours before any client knew it — but a match can also be \
an internal codename, an experiment flag or dead code.

Your knowledge of which models exist ends at your training cutoff, and this bot runs later than that. Take \
novelty only from the novel list: never call a name new or old from memory. A name listed by several catalogs \
in the same check is most likely being released publicly right now; a name seen in only one source, especially \
the Azure registry or a commit feed, is an early sign that may not ship. When a name is not a model at all, \
say so in a few words.

Write in Russian, as plain text: no Markdown, no emoji, no headings. Open with a one-line verdict. Then give \
each model family worth attention two or three sentences: what appeared, where, what it most likely means, and \
what the reader can do now (for example, try it on OpenRouter). Cover noise and other_changes in at most one \
closing line. Keep the whole text under 900 characters."""

# the 2026-09-22 18:59 UTC run, trimmed: GPT-6 Sol/Luna and Claude Opus 5.5 land in every catalog at once,
# next to a crate name from the Codex feed and a model that was already known
SELFTEST_KNOWN = ["gpt-5.4", "gpt-5.4-mini"]
SELFTEST_CHANGES = [
    ("azure-foundry-playground", "regex", ["azureml://registries/azure-openai/models/gpt-6-luna",
                                           "azureml://registries/azure-openai/models/gpt-6-sol"]),
    ("litellm-prices", "top_keys", ["claude-opus-5-5", "gpt-6-luna", "gpt-6-sol"]),
    ("openrouter", "key", ["anthropic/claude-opus-5.5", "openai/gpt-6-luna"]),
    ("feed-openai-codex", "regex", ["codex-rs", "gpt-5.4", "gpt-6-sol"]),
]


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


# ---------- Apify (сайты, которые отвечают скрипту 403) ---------------------

APIFY_API = "https://api.apify.com/v2"
APIFY_ACTOR = "apify~playwright-scraper"  # headless Playwright + residential-прокси Apify
APIFY_RUN_TIMEOUT = 240   # секунд на прогон актора; синхронный endpoint Apify всё равно обрывает на 300
APIFY_INTERVAL = 6        # часов между прогонами одного источника, если в конфиге не сказано иначе

# Выполняется в контексте актора: дожидается открытия страницы, собирает URL всех JS-бандлов,
# скачивает их и гоняет регулярку по HTML и по каждому бандлу. __PATTERN__ подменяется на pattern
# источника (JSON-литерал); синтаксис JS RegExp, флаг i добавляется здесь — поэтому (?i) в pattern нельзя.
APIFY_PAGE_FUNCTION = r"""
async function pageFunction(context) {
    const { page, request } = context;
    await page.waitForLoadState('networkidle', { timeout: 30000 }).catch(() => {});
    const scriptUrls = await page.evaluate(() => {
        const urls = new Set();
        for (const s of document.querySelectorAll('script[src]')) urls.add(s.src);
        for (const e of performance.getEntriesByType('resource'))
            if (e.initiatorType === 'script' || /\.m?js(\?|$)/.test(e.name)) urls.add(e.name);
        return [...urls];
    });
    const pattern = new RegExp(__PATTERN__, 'gi');
    const found = new Set();
    const scan = (text) => {
        pattern.lastIndex = 0;
        let m;
        while ((m = pattern.exec(text)) !== null && found.size < 500) found.add(m[1] || m[0]);
    };
    scan(await page.content());
    let fetched = 0, blocked = 0;
    for (const url of scriptUrls.slice(0, 60)) {
        try {
            const resp = await fetch(url);
            if (!resp.ok) { blocked++; continue; }
            scan(await resp.text());
            fetched++;
        } catch (e) { blocked++; }
    }
    return { url: request.url, scripts: scriptUrls.length, fetched, blocked, matches: [...found].sort() };
}
"""


def apify_state_path(src: dict) -> Path:
    return DATA / f"{src['name']}.apify-state.json"


def apify_due(src: dict, now: datetime) -> bool:
    """True, если прошлый прогон актора был дольше min_interval_hours назад (или его не было)."""
    try:
        state = json.loads(apify_state_path(src).read_text(encoding="utf-8"))
        last = datetime.strptime(state["last_run"], TIME_FORMAT).replace(tzinfo=timezone.utc)
    except (OSError, ValueError, KeyError):
        return True
    return now - last >= timedelta(hours=src.get("min_interval_hours", APIFY_INTERVAL))


def apify_mark_run(src: dict, now: datetime) -> None:
    """Запоминает прогон. Пишется и после неудачи: упавший прогон тоже стоил денег, и интервал
    ограничивает повторные попытки."""
    apify_state_path(src).write_text(json.dumps({"last_run": now.strftime(TIME_FORMAT)}) + "\n",
                                     encoding="utf-8")


def apify_run(src: dict, token: str) -> bytes:
    """Синхронно гоняет актор по странице источника и пакует найденное в JSON-снимок
    {"matches": [...], "pages": [...]} — его разбирает extract=key_list."""
    urls = src.get("urls") or [src["url"]]
    input_body = {
        "startUrls": [{"url": u} for u in urls],
        "pageFunction": APIFY_PAGE_FUNCTION.replace("__PATTERN__", json.dumps(src["pattern"])),
        "proxyConfiguration": {"useApifyProxy": True,
                               "apifyProxyGroups": src.get("proxy_groups", ["RESIDENTIAL"])},
        "maxRequestsPerCrawl": len(urls),
        "maxConcurrency": 1,
        "pageLoadTimeoutSecs": 60,
        "pageFunctionTimeoutSecs": APIFY_RUN_TIMEOUT,
    }
    query = urllib.parse.urlencode({"token": token, "timeout": APIFY_RUN_TIMEOUT, "memory": 2048})
    req = urllib.request.Request(
        f"{APIFY_API}/acts/{src.get('actor', APIFY_ACTOR)}/run-sync-get-dataset-items?{query}",
        data=json.dumps(input_body).encode("utf-8"),
        headers={"Content-Type": "application/json", "User-Agent": USER_AGENT},
    )
    try:
        with urllib.request.urlopen(req, timeout=APIFY_RUN_TIMEOUT + 60) as resp:
            items = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:  # Apify пишет причину в тело ответа — без неё 400 не отладить
        detail = e.read().decode("utf-8", errors="replace")[:300]
        raise ValueError(f"apify HTTP {e.code}: {detail}") from e
    pages, matches = [], set()
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict):
            continue
        pages.append({k: item.get(k) for k in ("url", "scripts", "fetched", "blocked")})
        matches.update(str(m) for m in item.get("matches") or [])
    return json.dumps({"matches": sorted(matches), "pages": pages},
                      ensure_ascii=False, indent=1).encode("utf-8")


def fetch_source(src: dict, headers: dict[str, str], now: datetime) -> bytes | None:
    """Скачивает источник; None — если источник эту проверку пропускает (нет токена, интервал не вышел)."""
    if src.get("kind") != "apify":
        return fetch(src["url"], headers)
    token = expand_env(src.get("token", ""))
    if not token:
        print(f"[{src['name']}] пропущен: не задан секрет APIFY_TOKEN")
        return None
    if not apify_due(src, now):
        print(f"[{src['name']}] пропущен: прогон Apify был меньше "
              f"{src.get('min_interval_hours', APIFY_INTERVAL)} ч назад")
        return None
    try:
        raw = apify_run(src, token)
    finally:
        apify_mark_run(src, now)
    return raw


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
      key_list  — элементы списков в полях "key" (так apify-источники отдают найденные строки);
      top_keys  — ключи верхнего уровня JSON-объекта.
    """
    kind = src.get("kind", "json")
    mode = src.get("extract", "regex" if kind == "text" else "key")

    if kind in ("json", "apify"):  # apify-источник отдаёт JSON-снимок, собранный актором
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
    elif mode == "key_list":
        if parsed is None:
            raise ValueError("extract=key_list требует kind=json")
        found = []
        for d in walk_dicts(parsed):
            v = d.get(src.get("key", "id"))
            if isinstance(v, list):
                found += [str(x) for x in v if isinstance(x, (str, int, float))]
    elif mode == "top_keys":
        if not isinstance(parsed, dict):
            raise ValueError("extract=top_keys требует JSON-объект на верхнем уровне")
        found = list(parsed.keys())
    else:
        raise ValueError(f"неизвестный extract: {mode}")

    if src.get("lowercase"):  # commit messages write GPT-5.5 and gpt-5.5 for the same model
        found = [f.lower() for f in found]

    only = src.get("only")
    if only:
        only_re = re.compile(only)
        found = [f for f in found if only_re.search(f)]

    exclude = src.get("exclude")
    if exclude:
        exclude_re = re.compile(exclude)
        found = [f for f in found if not exclude_re.search(f)]

    ids = sorted(set(found))
    return text, ids, parsed


def walk_dicts(obj):
    """Yields every dict nested anywhere in parsed JSON."""
    if isinstance(obj, dict):
        yield obj
        for v in obj.values():
            yield from walk_dicts(v)
    elif isinstance(obj, list):
        for item in obj:
            yield from walk_dicts(item)


def find_entry(parsed, src: dict, model_id: str):
    """The catalog object for a model id; None for text sources or when there is none."""
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
    """Flattens nested dicts into ("a.b", value) pairs."""
    for k, v in entry.items():
        name = f"{prefix}{k}"
        if isinstance(v, dict):
            yield from flatten(v, f"{name}.")
        elif isinstance(v, (str, int, float, bool)):
            yield name, v


def summarize(entry, budget: int = DETAIL_BUDGET) -> str:
    """Keeps only price, context window and dates from a model object."""
    if not isinstance(entry, dict):
        return ""
    fields = [(k, v) for k, v in flatten(entry) if INTEREST_RE.search(k)]
    # cache rates matter less than base prices, so they go last and are the first to be cut
    fields.sort(key=lambda kv: "cache" in kv[0].lower())
    parts = [f"{k}={v}" for k, v in fields]
    if not parts:
        return ""
    out = ", ".join(parts)
    return out if len(out) <= budget else out[: budget - 1] + "…"


def format_change(src: dict, added: list[str], removed: list[str], parsed, novel=frozenset()) -> str:
    """One source's block: added ids (novel first and marked, the first few with details), then removed ids."""
    lines = [f"• {src['name']} ({src['url']})"]
    ordered = [a for a in added if a in novel] + [a for a in added if a not in novel]
    for i, a in enumerate(ordered):
        detail = summarize(find_entry(parsed, src, a)) if i < DETAIL_LIMIT else ""
        mark = " 🆕" if a in novel else ""
        lines.append(f"  + {a}{mark}" + (f"\n      {detail}" if detail else ""))
    lines += [f"  − {r}" for r in removed[:REMOVED_LIMIT]]
    if len(removed) > REMOVED_LIMIT:
        lines.append(f"  … ещё {len(removed) - REMOVED_LIMIT} удалено")
    return "\n".join(lines)


# ---------- novelty ---------------------------------------------------------

REGION_PREFIX_RE = re.compile(r"^(?:us|eu|apac|au|jp|global|us-gov)\.")          # Bedrock cross-region ids
VENDOR_PREFIX_RE = re.compile(r"^(?:anthropic|openai|google|xai|meta|amazon|mistral|cohere)\.")
DATED_VERSION_RE = re.compile(r"(-\d{8})-v\d+$")                                # ...-20250219-v1
DATE_SUFFIX_RE = re.compile(r"-(?:\d{8}|\d{4}-\d{2}-\d{2}|\d{4})$")              # -20250805, -2025-08-07, -0825


def base_name(model_id: str) -> str:
    """Reduces a catalog-specific id to a name comparable across catalogs: azure/gpt-5.4-pro-2026-03-05 → gpt-5-4-pro."""
    s = model_id.strip().lower()
    if "/models/" in s:                       # azureml://registries/<registry>/models/<name>[/versions/<n>]
        s = s.split("/models/", 1)[1].split("/", 1)[0]
    else:
        s = s.rsplit("/", 1)[-1]              # openai/gpt-5, azure/us/gpt-5, bedrock/<region>/<plan>/anthropic.claude-...
    s = s.split("@", 1)[0].split(":", 1)[0]   # @20251001, @eu, :batch, :free, -v1:0
    s = REGION_PREFIX_RE.sub("", s)
    s = VENDOR_PREFIX_RE.sub("", s)
    s = s.replace(".", "-").replace("_", "-")
    s = DATED_VERSION_RE.sub(r"\1", s)
    return DATE_SUFFIX_RE.sub("", s)


MODE_SUFFIXES = ("-fast", "-thinking")  # serving modes of a model, not new models


def is_known(name: str, known: set[str]) -> bool:
    """True if the name was seen before, or is a variant of a seen model: a provider-prefixed copy
    (databricks-gemini-2-5-flash), a serving mode (gpt-6-sol-fast) or an alias without its version (grok-code-fast)."""
    for suffix in MODE_SUFFIXES:
        if name.endswith(suffix) and is_known(name[: -len(suffix)], known):
            return True
    if name in known:
        return True
    parts = name.split("-")
    if any("-".join(parts[i:]) in known for i in range(1, len(parts) - 1)):
        return True
    prefix = name + "-"
    return any(k.startswith(prefix) and k[len(prefix):].isdigit() for k in known)


def load_known(data_dir: Path) -> set[str]:
    """Every model name any source has listed so far; read before a run overwrites the snapshots."""
    known: set[str] = set()
    if data_dir.is_dir():
        for path in data_dir.glob("*.ids.txt"):
            lines = path.read_text(encoding="utf-8").splitlines()
            known.update(base_name(line) for line in lines if line.strip())
    return known


def novel_ids(added: list[str], known: set[str]) -> set[str]:
    """Added ids whose model name no source had listed before."""
    return {a for a in added if not is_known(base_name(a), known)}


def group_novel(changes: list[dict]) -> dict[str, list[dict]]:
    """Novel ids grouped by model name, with every source that listed the name in this run."""
    groups: dict[str, list[dict]] = {}
    for ch in changes:
        for raw in ch["added"]:
            if raw not in ch["novel"]:
                continue
            appearance = {"source": ch["src"]["name"], "id": raw}
            detail = summarize(find_entry(ch["parsed"], ch["src"], raw))
            if detail:
                appearance["details"] = detail
            groups.setdefault(base_name(raw), []).append(appearance)
    return groups


# ---------- memory and catalog events ---------------------------------------

def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def names_in(ids_path: Path) -> set[str]:
    """Model names a source's ids file lists; empty when the source has never been fetched."""
    if not ids_path.is_file():
        return set()
    return {base_name(line) for line in ids_path.read_text(encoding="utf-8").splitlines() if line.strip()}


def load_seen(data_dir: Path) -> dict[str, dict]:
    """name → {"first_seen": "2026-09-22T13:10Z" or "", "catalog": bool, "pulled": time or ""}; empty before the
    file exists.

    first_seen is empty for names that were already there when the memory started or came with a newly connected
    source: nobody knows when those appeared. catalog says whether any catalog (a source that is not a rolling
    commit feed) has ever listed the name. pulled is when the name, still fresh, left a catalog; it is cleared
    once a catalog lists it again."""
    path = data_dir / SEEN_FILE
    if not path.is_file():
        return {}
    seen: dict[str, dict] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        name, first_seen, catalog, pulled = (line.split("\t") + ["", "", ""])[:4]
        seen[name] = {"first_seen": first_seen, "catalog": catalog == "1", "pulled": pulled}
    return seen


def save_seen(data_dir: Path, seen: dict[str, dict]) -> None:
    lines = ["# name\tfirst_seen_utc\tin_catalog\tpulled_utc"]
    lines += [f"{n}\t{r['first_seen']}\t{int(r['catalog'])}\t{r.get('pulled', '')}".rstrip("\t")  # no trailing tabs
              for n, r in sorted(seen.items())]
    (data_dir / SEEN_FILE).write_text("\n".join(lines) + "\n", encoding="utf-8")


def remember(seen: dict[str, dict], snapshot: dict[str, set[str]], catalogs: set[str], now: datetime,
             untimed_sources: set[str] = frozenset()) -> None:
    """Adds every name the snapshot lists. A name only a newly connected source brought gets no time: it was
    there before we looked, not born now."""
    stamp = now.strftime(TIME_FORMAT)
    in_catalog: dict[str, bool] = {}
    timed: dict[str, bool] = {}
    for source, names in snapshot.items():
        for n in names:
            in_catalog[n] = in_catalog.get(n, False) or source in catalogs
            timed[n] = timed.get(n, False) or source not in untimed_sources
    for n in in_catalog:
        record = seen.get(n)
        if record is None:
            seen[n] = {"first_seen": stamp if timed[n] else "", "catalog": in_catalog[n], "pulled": ""}
        elif in_catalog[n]:
            record["catalog"] = True


def looks_broken(was: set[str], listed: set[str]) -> bool:
    """A catalog that came back empty, or lost more than half of a long list at once, was most likely fetched
    wrong rather than cleaned up: the biggest real drop so far was litellm's cleanup on 22.09, 8% of its ids."""
    if not was:
        return False
    return not listed or (len(was) >= BROKEN_LIST_MIN and len(listed) * 2 < len(was))


def is_fresh(record: dict | None, now: datetime) -> bool:
    first_seen = (record or {}).get("first_seen", "")
    return bool(first_seen) and \
        now - datetime.strptime(first_seen, TIME_FORMAT).replace(tzinfo=timezone.utc) <= PULLED_WITHIN


def catalog_events(before: dict[str, set[str]], after: dict[str, set[str]], catalogs: set[str],
                   seen: dict[str, dict], now: datetime, new_sources: set[str] = frozenset()):
    """What moved in the catalogs beyond one source's diff, catalog by catalog.

    pulled: a name first seen within PULLED_WITHIN that a catalog stopped listing: a leak someone cleaned up. It
    counts while other catalogs keep the name, since litellm and models-dev copy a leaked name within hours and
    seldom drop it. A catalog that still lists a variant (a renamed copy, the model without a serving mode) pulled
    nothing, and one that looks broken (see looks_broken) raises no pulls at all.
    returned: a pulled name that a catalog lists again. Old names that come back, say when litellm reverts a
    cleanup, were never pulled and stay in the ordinary quiet diff.

    Rolling commit feeds are not catalogs: names scroll out of them all the time and mean nothing by leaving."""
    back: dict[str, list[str]] = {}
    gone: dict[str, list[str]] = {}
    for c in sorted(catalogs):
        was, listed = before.get(c, set()), after.get(c, set())
        if c not in new_sources:
            for n in listed - was:
                if seen.get(n, {}).get("pulled"):
                    back.setdefault(n, []).append(c)
        if looks_broken(was, listed):
            print(f"[{c}] lost {len(was - listed)} of {len(was)} names at once: likely a broken fetch, "
                  "no pulled events from it", file=sys.stderr)
            continue
        for n in was - listed:
            if is_fresh(seen.get(n), now) and not is_known(n, listed):
                gone.setdefault(n, []).append(c)
    returned = [{"name": n, "sources": s} for n, s in sorted(back.items())]
    pulled = [{"name": n, "since": seen[n]["first_seen"], "sources": s,
               "still": sorted(c for c in catalogs if n in after.get(c, ()))} for n, s in sorted(gone.items())]
    return returned, pulled


def note_events(seen: dict[str, dict], returned: list[dict], pulled: list[dict], now: datetime) -> None:
    """A pulled name waits in the memory for its return; a returned one stops waiting."""
    for e in pulled:
        seen[e["name"]]["pulled"] = now.strftime(TIME_FORMAT)
    for e in returned:
        seen[e["name"]]["pulled"] = ""


def short_time(stamp: str) -> str:
    """2026-09-22T13:10Z → 22.09 13:10 UTC."""
    return datetime.strptime(stamp, TIME_FORMAT).strftime("%d.%m %H:%M UTC")


def format_events(returned: list[dict], pulled: list[dict]) -> str:
    lines = [f"↩️ вернулось: {e['name']} — снова в {', '.join(e['sources'])}" for e in returned]
    for e in pulled:
        rest = f"ещё есть в {', '.join(e['still'])}" if e["still"] else "больше ни в одном каталоге"
        lines.append(f"🫥 убрали: {e['name']} из {', '.join(e['sources'])} — появилось {short_time(e['since'])}, {rest}")
    return "\n".join(lines)


# ---------- AI summary ------------------------------------------------------

def build_ai_payload(groups: dict[str, list[dict]], changes: list[dict]) -> dict:
    """What the model reads: novel names with their sources, and a short account of everything else."""
    ranked = sorted(groups.items(), key=lambda kv: (-len({a["source"] for a in kv[1]}), kv[0]))
    novel = []
    for name, seen in ranked[:AI_NOVEL_LIMIT]:
        entry = {"name": name, "sources": sorted({a["source"] for a in seen}), "seen_in": seen[:AI_SEEN_IN_LIMIT]}
        if len(seen) > AI_SEEN_IN_LIMIT:
            entry["more_appearances"] = len(seen) - AI_SEEN_IN_LIMIT
        novel.append(entry)
    other = []
    for ch in changes:
        name = ch["src"]["name"]
        if ch["is_new"]:
            other.append({"source": name, "first_snapshot": True, "ids": ch["count"]})
            continue
        known_added = [a for a in ch["added"] if a not in ch["novel"]]
        if known_added or ch["removed"]:
            other.append({"source": name, "added_known": len(known_added), "removed": len(ch["removed"]),
                          "examples_added": known_added[:3], "examples_removed": ch["removed"][:3]})
    return {"novel": novel, "more_novel": max(0, len(ranked) - AI_NOVEL_LIMIT), "other_changes": other}


def github_oidc_token(audience: str) -> str | None:
    """A GitHub Actions OIDC token for the audience; None outside Actions or without `id-token: write`."""
    url = os.environ.get("ACTIONS_ID_TOKEN_REQUEST_URL")
    bearer = os.environ.get("ACTIONS_ID_TOKEN_REQUEST_TOKEN")
    if not url or not bearer:
        return None
    sep = "&" if "?" in url else "?"
    req = urllib.request.Request(
        f"{url}{sep}audience={urllib.parse.quote(audience)}",
        headers={"Authorization": f"bearer {bearer}", "User-Agent": USER_AGENT},
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return json.loads(resp.read().decode("utf-8"))["value"]
    except (urllib.error.URLError, ValueError, KeyError) as e:
        print(f"[ai] could not get a GitHub OIDC token: {e}", file=sys.stderr)
        return None


def price_for(model: str) -> tuple[float, float] | None:
    """List price for the model that answered; the gateway may name it with or without the provider prefix."""
    if model in AI_PRICE_PER_MTOK:
        return AI_PRICE_PER_MTOK[model]
    return next((p for m, p in AI_PRICE_PER_MTOK.items() if m.endswith("/" + model)), None)


def ai_summary(payload: dict, client=None) -> str | None:
    """A short human-readable verdict from the model behind ai-proxy, or None when the AI is off or anything fails."""
    if client is None:
        base_url = os.environ.get("AI_PROXY_URL")
        if not base_url:
            return None
        if anthropic is None:
            print("[ai] package 'anthropic' is not installed: sending the plain report", file=sys.stderr)
            return None
        token = github_oidc_token(AI_AUDIENCE)
        if not token:
            print("[ai] no GitHub OIDC token (the job needs `id-token: write`): sending the plain report",
                  file=sys.stderr)
            return None
        client = anthropic.Anthropic(api_key=token, base_url=base_url, timeout=AI_TIMEOUT)
    content = "Changes found in the latest check:\n" + json.dumps(payload, ensure_ascii=False)
    try:
        response = client.messages.create(
            model=AI_MODEL,
            max_tokens=AI_MAX_TOKENS,
            system=AI_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": content}],
        )
    except Exception as e:  # the summary is an extra: no failure here may hold back the alert itself
        where = ""
        if anthropic is not None and isinstance(e, anthropic.APIStatusError):
            where = f" (HTTP {e.status_code}, request {e.request_id})"
        print(f"[ai] request failed{where}: {type(e).__name__}: {e}", file=sys.stderr)
        return None
    if response.stop_reason == "refusal":
        category = getattr(response.stop_details, "category", None)
        print(f"[ai] declined (category: {category}): sending the plain report", file=sys.stderr)
        return None
    if response.stop_reason == "max_tokens":
        print("[ai] hit max_tokens: the summary may be cut short", file=sys.stderr)
    usage = response.usage
    line = f"[ai] {response.model}: input={usage.input_tokens} output={usage.output_tokens} tokens"
    price = price_for(response.model)
    if price:
        line += f", ≈ ${(usage.input_tokens * price[0] + usage.output_tokens * price[1]) / 1e6:.6f}"
    print(line)
    text = "\n".join(b.text for b in response.content if getattr(b, "type", None) == "text").strip()
    return text or None


def format_message(blocks: list[str], novel_names: list[str], ai_text: str | None,
                   returned: list[dict] = (), pulled: list[dict] = ()) -> tuple[str, bool]:
    """The report, and whether it shows signs of new models: novel names, pulled leaks or returns. Only such a report
    goes to Telegram; routine changes stay in the Actions log and in the history of data/."""
    body = "\n\n".join(blocks)
    if novel_names and ai_text:
        head = "model-watch: 🆕 признаки новых моделей"
    elif novel_names:
        shown = ", ".join(novel_names[:10])
        more = f" и ещё {len(novel_names) - 10}" if len(novel_names) > 10 else ""
        head = f"model-watch: 🆕 новые имена: {shown}{more}"
    elif returned:
        head = "model-watch: ↩️ вернулось в каталоги"
    elif pulled:
        head = "model-watch: 🫥 убрали из каталогов"
    else:
        return f"model-watch: новых моделей нет\n\n{body}", False
    parts = [head, format_events(list(returned), list(pulled))]
    if novel_names and ai_text:
        parts += [ai_text, "— — —"]
    return "\n\n".join(p for p in parts + [body] if p), True


def run_ai_selftest(client=None) -> int:
    """Sends one AI summary of a recorded real run to Telegram, without fetching anything or touching data/."""
    known = {base_name(k) for k in SELFTEST_KNOWN}
    changes = []
    for name, extract, added in SELFTEST_CHANGES:
        src = {"name": name, "url": "запись прогона 22.09 18:59 UTC", "extract": extract}
        changes.append({"src": src, "added": added, "removed": [], "parsed": None, "is_new": False,
                        "novel": novel_ids(added, known), "count": len(added)})
    groups = group_novel(changes)
    ai_text = ai_summary(build_ai_payload(groups, changes), client)
    if ai_text is None:
        print("[ai-selftest] no summary: check AI_PROXY_URL, `id-token: write` and the error above", file=sys.stderr)
        return 1
    blocks = [format_change(ch["src"], ch["added"], ch["removed"], None, ch["novel"]) for ch in changes]
    text, _ = format_message(blocks, sorted(groups), ai_text)
    text = text.replace("model-watch:", "model-watch [самотест]:", 1)
    print(text)
    send_telegram(text)
    return 0


def github_api(path: str) -> dict:
    headers = {"Accept": "application/vnd.github+json", "User-Agent": USER_AGENT}
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(f"https://api.github.com{path}", headers=headers)
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        return json.loads(resp.read().decode("utf-8"))


def chain_warning(now: datetime, api=github_api) -> str | None:
    """A warning when the tick chain has not started a check for CHAIN_STALE_AFTER; None while it runs or when
    GitHub cannot tell us. Runs on the fallback schedule, which GitHub fires every few hours: a leak such as the
    22.09 Azure entries, visible for about two hours, slips through such gaps."""
    repo = os.environ.get("GITHUB_REPOSITORY")
    if not repo:
        return None
    try:
        data = api(f"/repos/{repo}/actions/workflows/watch.yml/runs?event=workflow_dispatch&per_page=1")
        runs = data.get("workflow_runs") or []
        last = datetime.strptime(runs[0]["created_at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc) if runs else None
    except (urllib.error.URLError, ValueError, KeyError, TypeError, AttributeError) as e:
        print(f"[watchdog] could not read the workflow runs: {e}", file=sys.stderr)
        return None
    restart = "Перезапуск: Actions → tick → Run workflow."
    if last is None:
        return f"⚠️ model-watch: цепочка tick ещё ни разу не запускала проверку. {restart}"
    if now - last <= CHAIN_STALE_AFTER:
        return None
    minutes = int((now - last).total_seconds() // 60)
    return (f"⚠️ model-watch: цепочка tick встала. Последняя проверка по цепочке — {last:%d.%m %H:%M} UTC, "
            f"{minutes} мин назад. Пока она стоит, проверки идут только по запасному расписанию GitHub, "
            f"раз в несколько часов, и короткие сливы проскочат. {restart}")


def run_chain_check() -> int:
    """watch.yml calls this only when GitHub's fallback schedule, not the tick chain, started the run."""
    warning = chain_warning(utc_now())
    if warning:
        print(warning)
        send_telegram(warning)
    else:
        print("[watchdog] the tick chain is running")
    return 0


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
    body = {"chat_id": chat_id, "text": text, "disable_web_page_preview": True}
    payload = json.dumps(body).encode("utf-8")
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

def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if "--ai-selftest" in args:
        return run_ai_selftest()
    if "--check-chain" in args:
        return run_chain_check()

    DATA.mkdir(exist_ok=True)
    sources = json.loads(SOURCES.read_text(encoding="utf-8"))
    now = utc_now()
    active = [s["name"] for s in sources if not s.get("disabled")]
    catalogs = {s["name"] for s in sources if not s.get("disabled") and s.get("alert_removed", True)}

    # everything below up to the loop must be read before the loop overwrites the snapshots
    before = {name: names_in(DATA / f"{name}.ids.txt") for name in active}
    seen = load_seen(DATA)
    # names the snapshots list but the memory lacks (all of them on its first run, the newest few when seen.tsv was
    # seeded from an older history) are known, of unknown age
    remember(seen, before, catalogs, now, untimed_sources=set(active))
    known = load_known(DATA) | set(seen)
    # a catalog judges novelty by catalogs alone: a name a commit feed leaked is news the day a catalog lists it
    catalog_known = {n for n, r in seen.items() if r["catalog"]}

    changes: list[dict] = []
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
            raw = fetch_source(src, headers, now)
            if raw is None:
                continue
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

        if is_new or added or removed:
            changes.append({"src": src, "added": added, "removed": removed, "parsed": parsed, "is_new": is_new,
                            "novel": set() if is_new else novel_ids(added, catalog_known if name in catalogs else known),
                            "count": len(ids)})

    after = {name: names_in(DATA / f"{name}.ids.txt") for name in active}
    new_sources = {ch["src"]["name"] for ch in changes if ch["is_new"]}
    returned, pulled = catalog_events(before, after, catalogs, seen, now, new_sources)
    note_events(seen, returned, pulled, now)
    remember(seen, after, catalogs, now, untimed_sources=new_sources)
    save_seen(DATA, seen)
    if returned or pulled:
        print(format_events(returned, pulled))

    report = [
        f"• {ch['src']['name']}: первый снимок, {ch['count']} id" if ch["is_new"]
        else format_change(ch["src"], ch["added"], ch["removed"], ch["parsed"], ch["novel"])
        for ch in changes
    ]
    if report or returned or pulled:
        groups = group_novel(changes)
        ai_text = ai_summary(build_ai_payload(groups, changes)) if groups else None
        text, news = format_message(report, sorted(groups), ai_text, returned, pulled)
        if news:
            send_telegram(text)
        else:  # routine changes: the log and the history of data/ keep them, Telegram only hears about new models
            print(text)
    else:
        print("изменений нет")

    if errors and os.environ.get("REPORT_ERRORS") == "1":
        send_telegram("model-watch: ошибки\n\n" + "\n".join(errors))

    return 0


if __name__ == "__main__":
    sys.exit(main())
