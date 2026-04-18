# Project: Calorie Telegram Bot

## What it does

A Telegram bot for daily calorie and macronutrient tracking. The user sends a meal description (in any language -- typically Russian or Ukrainian), and the bot:

1. Calls OpenAI to extract ingredients with calories and macros (protein, fat, carbs).
2. Writes ingredient rows to Firestore (`users/{uid}/log`).
3. Recomputes and upserts a daily summary doc (`users/{uid}/daily/{date}`).
4. Replies with the logged values and today's running totals.

No setup required for end users -- just message the bot.

## Architecture

Single-file Python app (`bot.py`) with no framework beyond `python-telegram-bot`. Deployed to Google Cloud Run using webhooks.

```
Telegram --webhook--> Cloud Run (bot.py) ---> OpenAI API (extraction)
                                          \-> Firestore (per-user data)
```

- **Polling mode**: used for local development (when `WEBHOOK_URL` is unset).
- **Webhook mode**: used on Cloud Run (when `WEBHOOK_URL` is set). Listens on `0.0.0.0:$PORT`.

## Key files

- `bot.py` -- entire bot logic: OpenAI prompt, extraction/recompute, Firestore I/O, Telegram handlers.
- `requirements.txt` -- Python deps. Note `python-telegram-bot[webhooks]` (the `[webhooks]` extra is required for Cloud Run).
- `Dockerfile` -- Cloud Run container image. Runs as non-root `appuser`.
- `.env.example` -- all supported env vars with descriptions.

## Code structure in bot.py

- `SYSTEM_PROMPT` -- the OpenAI prompt. Ingredient names must be Ukrainian (Cyrillic). Uses a 4-step procedure: extract, estimate grams, look up per-100g values, compute.
- `IngredientLog` dataclass -- per-ingredient row: ingredient, amount, unit, kcal, protein, fat, carbs, note.
- `DailyResult` dataclass -- daily summary: total_kcal, target_kcal, deficit, total_protein, total_fat, total_carbs.
- `UserStore` class -- Firestore-backed per-user data store. Key methods:
  - `append_log()` -- batch-writes ingredient docs to `users/{uid}/log`.
  - `totals_for_date()` -- queries log subcollection, sums kcal/protein/fat/carbs.
  - `upsert_daily()` -- recomputes and writes `users/{uid}/daily/{date}`.
  - `get_target_kcal()` / `set_target()` -- reads/writes user prefs.
  - `get_timezone()` -- reads user timezone preference.
- `CalorieBot` class -- singleton holding OpenAI client + UserStore. Key method:
  - `extract_ingredients()` -- calls OpenAI, parses JSON, recomputes kcal/protein/fat/carbs server-side.
- Telegram handlers: `start`, `help_cmd`, `today`, `set_target`, `handle_message`.
- `main()` -- starts webhook or polling based on `WEBHOOK_URL`.

## Data model (Firestore)

```
users/{user_id}                   -- target_kcal, timezone
users/{user_id}/log/{auto_id}     -- logged_at, date, entry_id, ingredient, amount, unit, kcal, protein, fat, carbs, raw_input, note
users/{user_id}/daily/{date_str}  -- total_kcal, target_kcal, deficit, total_protein, total_fat, total_carbs
```

The `daily` docs are fully derived from `log` -- recomputed on every meal log, `/today`, and `/settarget`.

## Environment variables

See `.env.example`. Critical ones:
- `TELEGRAM_BOT_TOKEN`, `OPENAI_API_KEY` -- required secrets.
- `FIRESTORE_COLLECTION` -- top-level collection name (default `users`).
- `WEBHOOK_URL` -- Cloud Run service URL. Empty = polling mode.
- `WEBHOOK_SECRET` -- validates Telegram webhook requests.
- `TIMEZONE` -- default timezone for new users (e.g. `Europe/Kyiv`).

## Deployment

Deployed to **Google Cloud Run** (free tier). Key points:
- `gcloud builds submit` builds the image.
- `gcloud run deploy` deploys it. Env vars persist across deploys.
- Scales to zero when idle (no cost). Cold starts take 3-5 seconds.
- Cloud Run service account needs `roles/datastore.user` for Firestore access.
- Firestore API must be enabled; database in Native mode.

## Conventions

- All code is in a single `bot.py` -- keep it that way unless complexity demands splitting.
- Ingredient names are always Ukrainian regardless of input language.
- The bot recomputes nutrition values server-side from `grams` and `*_per_100g` to catch model arithmetic errors.
- No background jobs or cron -- everything is triggered by user messages.
