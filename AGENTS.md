# Project: Calorie Telegram Bot

## What it does

A multi-user Telegram bot for daily calorie and macronutrient tracking. Each user connects their own Google Sheet via Telegram. The user sends a meal description (in any language — typically Russian or Ukrainian), and the bot:

1. Calls OpenAI to extract ingredients with calories and macros (protein, fat, carbs).
2. Writes ingredient rows to a Google Sheet `log` tab.
3. Recomputes and upserts a daily summary row in a `daily` tab.
4. Replies with the logged values and today's running totals.

## Architecture

Single-file Python app (`bot.py`) with no framework beyond `python-telegram-bot`. Deployed to Google Cloud Run using webhooks.

```
Telegram --webhook--> Cloud Run (bot.py) ---> OpenAI API (extraction)
                                          \-> Google Sheets API (per-user logging)
                                          \-> Firestore (per-user config persistence)
```

- **Polling mode**: used for local development (when `WEBHOOK_URL` is unset).
- **Webhook mode**: used on Cloud Run (when `WEBHOOK_URL` is set). Listens on `0.0.0.0:$PORT`.
- **Per-user config**: each Telegram user uploads their own Google service account JSON and sets a spreadsheet title. Stored in Firestore (or filesystem for local dev) so configs survive cold starts.

## Key files

- `bot.py` — entire bot logic: OpenAI prompt, extraction/recompute, Sheets I/O, Telegram handlers, per-user config backend.
- `requirements.txt` — Python deps. Note `python-telegram-bot[webhooks]` (the `[webhooks]` extra is required for Cloud Run).
- `Dockerfile` — Cloud Run container image. Runs as non-root `appuser`.
- `.env.example` — all supported env vars with descriptions.
- `.gcloudignore` — controls what `gcloud builds submit` uploads.
- `.dockerignore` — controls what Docker `COPY . .` includes.

## Code structure in bot.py

- `SYSTEM_PROMPT` — the OpenAI prompt. Ingredient names must be Ukrainian (Cyrillic). Uses a 4-step procedure: extract → estimate grams → look up per-100g values → compute.
- `IngredientLog` dataclass — per-ingredient row: ingredient, amount, unit, kcal, protein, fat, carbs, note.
- `UserConfigBackend` protocol + `FilesystemUserConfigStore` / `FirestoreUserConfigStore` — per-user config storage. `_create_config_backend()` factory picks backend from env / auto-detection.
- `UserSpreadsheet` class — Google Sheets client (log + daily worksheets) for one user, constructed from their stored service account JSON.
- `CalorieBot` class — stateful singleton holding OpenAI client + config backend. Key methods:
  - `extract_ingredients()` — calls OpenAI, parses JSON, **recomputes** kcal/protein/fat/carbs server-side from `grams` and `*_per_100g` fields (overrides model arithmetic).
  - `get_user_spreadsheet()` — loads user config, returns cached `UserSpreadsheet` or creates one.
  - `save_service_account_json()` / `set_sheet_name()` — credential + sheet onboarding.
- Telegram handlers: `start`, `help_cmd`, `setup_cmd`, `status_cmd`, `today`, `set_target`, `set_sheet`, `handle_credentials_document`, `handle_message`.
- `main()` — starts webhook or polling based on `WEBHOOK_URL`.

## Sheet layout

**log** (12 columns): `logged_at`, `date`, `entry_id`, `ingredient`, `amount`, `unit`, `kcal`, `protein`, `fat`, `carbs`, `raw_input`, `note`

**daily** (7 columns): `date`, `total_kcal`, `target_kcal`, `deficit`, `total_protein`, `total_fat`, `total_carbs`

The `daily` sheet is fully derived from `log` — recomputed on every meal log, `/today`, and `/settarget`.

## Environment variables

See `.env.example`. Critical ones:
- `TELEGRAM_BOT_TOKEN`, `OPENAI_API_KEY` — required secrets.
- `USER_CONFIG_BACKEND` — `firestore` or `filesystem` (auto-detects GCP if empty).
- `FIRESTORE_COLLECTION` — Firestore collection name (default `user_configs`).
- `WEBHOOK_URL` — Cloud Run service URL. Empty = polling mode.
- `WEBHOOK_SECRET` — validates Telegram webhook requests.
- `TIMEZONE` — default timezone for new users (e.g. `Europe/Kyiv`).

## Deployment

Deployed to **Google Cloud Run** (free tier). See README section 5 for the full guide. Key points:
- `gcloud builds submit` builds the image (respects `.gcloudignore`, not `.gitignore`).
- `gcloud run deploy` deploys it. Env vars persist across deploys.
- Scales to zero when idle (no cost). Cold starts take 3-5 seconds.
- Cloud Run service account needs `roles/datastore.user` for Firestore access.
- Per-user config (Google service account JSON + sheet title) is stored in Firestore, so it survives cold starts and redeployments.

## Conventions

- All code is in a single `bot.py` — keep it that way unless complexity demands splitting.
- Ingredient names in the sheet are always Ukrainian regardless of input language.
- The bot recomputes nutrition values server-side from `grams` and `*_per_100g` to catch model arithmetic errors.
- No background jobs or cron — everything is triggered by user messages.
