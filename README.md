# model-watch

Git-scraping для каталогов моделей: раз в 10 минут GitHub Actions скачивает
конфиги и списки моделей, кладёт снимок в `data/`, коммитит и присылает в Telegram
только разницу — какие model id появились и какие исчезли. Ровно так и появился
скриншот с `gpt-6-sol / gpt-6-luna / gpt-6-astra-minor`.

Для базового режима зависимостей нет: Python 3.10+ и git. AI-сводке нужен пакет `anthropic` —
он в `requirements.txt`, Actions ставит его сам.

## Запуск за 10 минут

1. Создай **публичный** репозиторий и залей файлы. Публичный — потому что в нём
   минуты Actions бесплатны без ограничений; ничего секретного в репозитории нет,
   ключи живут в Secrets. Быстрее всего через gh CLI из папки проекта:

   ```bash
   git init && git add . && git commit -m "init"
   gh repo create model-watch --public --source=. --push
   ```
2. Заведи бота у @BotFather, напиши ему любое сообщение и возьми `chat.id`
   из `https://api.telegram.org/bot<TOKEN>/getUpdates`
   (для канала — добавь бота админом и используй id канала вида `-100…`).
3. Секреты — через веб (Settings → Secrets and variables → Actions) или одной
   командой на каждый:

   ```bash
   gh secret set TELEGRAM_BOT_TOKEN
   gh secret set TELEGRAM_CHAT_ID
   gh secret set OPENAI_API_KEY      # по желанию
   gh secret set ANTHROPIC_API_KEY   # по желанию: официальный список моделей и AI-сводка
   ```

   С ключами провайдеров начнут отслеживаться и официальные `/v1/models`;
   без них эти два источника просто пропускаются.
4. Первый запуск руками: `gh workflow run model-watch` (или вкладка Actions →
   `model-watch` → Run workflow). Он сделает базовый снимок, закоммитит его
   в `data/` и пришлёт короткое «первый снимок, N id». Дальше — по крону.

Локально: `python watch.py` — без токена печатает отчёт в консоль.

## Как добавить источник

Один объект в `sources.json`:

```json
{
  "name": "vertex-catalog",
  "url": "https://…/models.json",
  "kind": "json",              // json | text
  "extract": "key",            // regex | key | top_keys
  "key": "id",                 // для extract=key
  "pattern": "…",              // для extract=regex; если есть группа — берётся группа 1
  "only": "(?i)gpt|claude",    // необязательный фильтр по извлечённым id
  "headers": { "Authorization": "Bearer ${SOME_SECRET}" },
  "keep_full": true,           // хранить ли полный нормализованный снимок в data/<name>.json
  "alert_removed": true        // false для «скользящих» лент вроде Atom-фидов коммитов
}
```

Если в `headers` есть `${VAR}`, а переменной нет в окружении — источник пропускается без ошибки.

## AI-сводка

Каждое добавленное id скрипт сводит к имени модели (`azure/gpt-5.4-pro-2026-03-05` → `gpt-5-4-pro`)
и сверяет со всем, что источники показывали раньше. Имя, которого не было нигде, помечается 🆕.

- Есть новые имена и задан `ANTHROPIC_API_KEY` — Claude (`claude-opus-5`) пишет короткую сводку
  по-русски: что появилось, где и что это скорее всего значит. Сводка идёт первой, сырой дифф ниже.
- Есть новые имена, ключа нет — новые имена перечислены в заголовке.
- Новых имён нет (копии известных моделей у перепродавцов, режимы вроде `-fast`, удаления) —
  сообщение приходит без звука, модель не вызывается.

Токены и цена каждого вызова видны в логе шага «Fetch, extract, diff, notify» строкой `[ai] …`.
Проверить связку ключ → Claude → Telegram: Actions → model-watch → Run workflow → галочка `ai_selftest`.

## Оговорки

- `PlaygroundConfig.json` у Azure — внутренний файл фронтенда, он может переехать
  или начать отдавать 403. Тогда просто заменишь URL: скрипт при ошибке
  скачивания пропускает источник, остальные работают.
- Cron у GitHub Actions не точный: в часы пик запуск задерживается на 5–15 минут.
  Минимальный интервал — 5 минут. В публичном репозитории минуты не тарифицируются,
  в приватном на GitHub Free квота 2 000 минут в месяц (интервал 10 минут её превысит).
- Если в репозитории 60 дней нет активности, GitHub отключает расписание;
  ручной запуск включает его обратно.
- Строка в конфиге доказывает, что модель готовят к раздаче, а не дату и не характеристики.

## Что смотреть руками (или через запланированные прогоны по иксу)

Аккаунты в иксе, которые дали первые сигналы в этом цикле:
- `@kimmonismus` (Chubby) — 6 сентября первым написал про подготовку GPT-6 Sol.
- `@lyraxana` — 20 сентября выложил кодовое имя `claude-wafer-eap` и дату для Opus 5.5.

Аккаунты, которые исторически копают код ChatGPT, claude.ai и клиентов
(проверь, что живы и не сменили ники):
- `@btibor91` (Tibor Blaho) — фичи и модели в коде веб-приложения ChatGPT.
- `@testingcatalog` — ежедневные находки в клиентах и бандлах.
- `@apples_jimmy`, `@koltregaskes`, `@scaling01`, `@legit_rumors` — слухи и агрегация.
- `@steph_palazzolo`, `@amir` — репортёры The Information, у которых чаще всего
  первыми появляются инсайды от людей, а не от кода.

Репозитории (кнопка Watch → Custom → Releases + Commits или Atom-фид
`https://github.com/<owner>/<repo>/commits/main.atom`):
- `openai/codex`, `openai/openai-python` — там всплывали `gpt-6-astra` и имена Sol/Terra/Luna.
- `anthropics/claude-code`, `anthropics/anthropic-sdk-python` — новые model id и строки `*-eap`.
- `github/docs`, путь `content/copilot/reference/ai-models/` — Copilot добавляет модели в документацию заранее.
- `BerriAI/litellm` — файл цен и контекстов (уже в `sources.json`).

Форумы и рынки:
- linux.do, раздел «前沿快讯» — китайское сообщество, которое нашло сегодняшний Azure-конфиг.
- r/singularity, r/OpenAI, r/ClaudeAI — быстрые пересказы и скриншоты роутинга на новые чекпойнты.
- Polymarket — рынки вида «Will OpenAI/Anthropic release X by date»; проценты в
  постах вроде «Opus 5.5 (99%)» берут оттуда.
