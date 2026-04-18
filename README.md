# Telegram Calorie & Macro Tracker Bot

A Telegram bot for daily calorie and macronutrient tracking. Send a meal in any language and the bot will extract ingredients, estimate calories and macros, and store everything in Firestore. Zero setup for end users -- just message the bot.

## Commands

| Command | Description |
|---|---|
| _meal text_ | Log a meal, e.g. `2 eggs, toast with butter, coffee`. Works in any language. |
| `/today` | Today's meals breakdown and totals. |
| `/day -1` or `/day 2026-04-15` | View meals and totals for any day (offset or date). |
| `/settarget <kcal>` | Set calorie target for today. Add a date for another day: `/settarget 2200 2026-04-14`. |
| `/help` | Show available commands. |

## How estimation works

The bot instructs the model to follow a strict 4-step procedure for every ingredient:

1. **Extract** each distinct food component from the message.
2. **Estimate grams** -- convert pieces, ml, spoons, cups, or vague portions to total grams (using standard weights like 1 egg = 60 g).
3. **Look up per-100 g values** -- `kcal_per_100g`, `protein_per_100g`, `fat_per_100g`, `carbs_per_100g` from standard nutrition tables.
4. **Compute** -- `value = round(per_100g * grams / 100, 1)` for each metric.

As a safety net, the bot **recomputes** all values server-side from the model's `grams` and `*_per_100g` fields, so arithmetic errors in the model output are corrected before storage.

## Data model (Firestore)

All data is stored in Firestore under a single collection (default `users`):

```
users/{user_id}                   -- user prefs (target_kcal, timezone)
users/{user_id}/log/{auto_id}     -- one doc per ingredient row
users/{user_id}/daily/{date_str}  -- one doc per day (derived totals)
```

- **`log`** is the source of truth -- every ingredient with kcal, protein, fat, carbs.
- **`daily`** is derived -- recomputed from `log` on every meal log, `/today`, and `/settarget`.

## Running locally

```bash
cp .env.example .env   # fill in TELEGRAM_BOT_TOKEN, OPENAI_API_KEY
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# Option A: use the Firestore emulator
export FIRESTORE_EMULATOR_HOST=localhost:8080
gcloud emulators firestore start --host-port=localhost:8080

# Option B: use a real Firestore database
export GOOGLE_CLOUD_PROJECT=your-project-id

python bot.py
```

When `WEBHOOK_URL` is empty, the bot uses long-polling -- no public URL needed.

## Environment variables

See `.env.example` for the full list. Key ones:

| Variable | Required | Default | Description |
|---|---|---|---|
| `TELEGRAM_BOT_TOKEN` | yes | -- | Telegram bot token from @BotFather |
| `OPENAI_API_KEY` | yes | -- | OpenAI API key |
| `OPENAI_MODEL` | no | `gpt-4o-mini` | Model for nutrition extraction |
| `TIMEZONE` | no | `UTC` | Default timezone for new users |
| `DEFAULT_TARGET_KCAL` | no | `2000` | Default daily calorie target |
| `FIRESTORE_COLLECTION` | no | `users` | Top-level Firestore collection |
| `WEBHOOK_URL` | no | -- | Cloud Run service URL. Empty = polling mode. |
| `WEBHOOK_SECRET` | no | random | Secret for validating Telegram webhooks |

## Architecture

```
Telegram --> bot.py --> OpenAI API (nutrition extraction)
                    \-> Firestore (per-user calorie data)
```

- **Polling mode** for local development (`WEBHOOK_URL` empty).
- **Webhook mode** for Cloud Run (`WEBHOOK_URL` set).

## Notes

- Calorie and macro estimates come from the model; treat as approximate.
- Ingredient names are always stored in Ukrainian regardless of input language.
- OpenAI usage is billed to whoever deploys the bot.
