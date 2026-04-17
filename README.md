# Telegram Calorie & Macro Tracker Bot

A Telegram bot that:

- reads your meal message (in any language — Russian, Ukrainian, English, etc.),
- uses OpenAI to extract ingredients with calories and macronutrients (protein, fat, carbs),
- writes ingredient names in **Ukrainian** to a Google Sheet `log` tab,
- keeps a `daily` tab in sync (totals from log, deficit vs target, macro sums).

## How calorie & macro estimation works

The bot instructs the model to follow a strict 4-step procedure for every ingredient:

1. **Extract** each distinct food component from the message.
2. **Estimate grams** — convert pieces, ml, spoons, cups, or vague portions to total grams (using standard weights like 1 egg ≈ 60 g).
3. **Look up per-100 g values** — `kcal_per_100g`, `protein_per_100g`, `fat_per_100g`, `carbs_per_100g` from standard nutrition tables.
4. **Compute** — `value = round(per_100g * grams / 100, 1)` for each metric.

As an extra safety net, the bot **recomputes** `kcal`, `protein`, `fat`, and `carbs` server-side from the model's `grams` and `*_per_100g` fields, so arithmetic errors in the model's output are corrected before they reach the sheet.

## Design: log vs daily

- **`log` is the source of truth** — every ingredient row with `kcal`, `protein`, `fat`, `carbs`.
- **`daily` is derived** — for each date, totals are the sum of the corresponding columns in `log`. `deficit = target_kcal - total_kcal`.

After each meal, the bot **recomputes and upserts** that day's row in `daily` so numbers stay current.

A **scheduled job** runs once per day (default 00:05 in `TIMEZONE`) and refreshes **yesterday and today** in `daily`. That covers late-night entries, manual edits in `log`, or edge cases where you want an end-of-day reconciliation without sending another message.

Adjust schedule with `DAILY_SYNC_HOUR` and `DAILY_SYNC_MINUTE` in `.env`.

## Sheet layout

**Tab `log`:** `logged_at`, `date`, `entry_id`, `ingredient`, `amount`, `unit`, `kcal`, `protein`, `fat`, `carbs`, `raw_input`, `note`

**Tab `daily`:** `date`, `total_kcal`, `target_kcal`, `deficit`, `total_protein`, `total_fat`, `total_carbs`

## 1) Google Cloud and Sheet

1. Create a spreadsheet with tabs named exactly `log` and `daily`.
2. In Google Cloud, enable Google Sheets API and Google Drive API.
3. Create a service account and download its JSON key as `credentials.json` in this project folder.
4. Share the spreadsheet with the service account email (Editor).

## 2) Telegram bot

1. Chat with [@BotFather](https://t.me/BotFather), create a bot, copy the token.

## 3) Environment

```bash
cp .env.example .env
```

Fill:

- `TELEGRAM_BOT_TOKEN`
- `OPENAI_API_KEY`
- `GOOGLE_SHEET_NAME` (exact spreadsheet title)
- `GOOGLE_SHEETS_CREDENTIALS_FILE` (default `credentials.json`)
- `OPENAI_MODEL` (default `gpt-4o-mini`)
- `TIMEZONE` (example: `Europe/Kyiv`)
- `DEFAULT_TARGET_KCAL` (used for new days until you set a row or use `/settarget`)
- `DAILY_SYNC_HOUR`, `DAILY_SYNC_MINUTE` (nightly refresh of `daily`)
- `WEBHOOK_URL` (Cloud Run only — your service URL, e.g. `https://calorie-bot-xxxxx-ew.a.run.app`; leave empty for polling mode)
- `WEBHOOK_SECRET` (optional — a stable secret token for validating Telegram webhook requests; if unset, a random one is generated each startup)

## 4) Running locally (polling mode)

When `WEBHOOK_URL` is empty (or unset), the bot uses long-polling — no public URL needed:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python bot.py
```

## 5) Deploying to Cloud Run (webhook mode)

Cloud Run requires webhook mode because it only handles incoming HTTP requests. This is a full walkthrough from scratch -- no prior GCP experience needed.

### 5.1) Install the gcloud CLI

Download and install from https://cloud.google.com/sdk/docs/install, then verify:

```bash
gcloud --version
```

### 5.2) Create a GCP project

1. Go to https://console.cloud.google.com
2. Click the project dropdown at the top left, then **New Project**.
3. Name it something like `calorie-bot`, click **Create**.
4. Make sure the new project is selected in the dropdown.
5. GCP offers a free trial with $300 credit -- accept it if prompted.

Then authenticate locally and set the project:

```bash
gcloud auth login
gcloud config set project YOUR_PROJECT_ID
```

Your project ID is shown on the project dashboard (e.g. `calorie-bot-123456`).

### 5.3) Enable required APIs

```bash
gcloud services enable \
  run.googleapis.com \
  cloudbuild.googleapis.com \
  artifactregistry.googleapis.com \
  sheets.googleapis.com \
  drive.googleapis.com
```

### 5.4) Create a service account for Google Sheets

If you already have a `credentials.json` from earlier, skip to 5.5.

1. Go to **IAM & Admin -> Service Accounts** in the console (https://console.cloud.google.com/iam-admin/serviceaccounts).
2. Click **Create Service Account**. Name: `sheets-bot`, click **Create and Continue**.
3. Skip the optional role grants, click **Done**.
4. Click into the new service account, go to the **Keys** tab.
5. Click **Add Key -> Create new key -> JSON -> Create**.
6. A `.json` file downloads. Rename it to `credentials.json` and place it in the project root.

Then share your Google Sheet:

1. Open `credentials.json`, copy the `client_email` value (e.g. `sheets-bot@calorie-bot-123456.iam.gserviceaccount.com`).
2. Open your Google Spreadsheet, click **Share**, paste that email, give **Editor** access.

### 5.5) Set shell variables

Pick a region close to you. For Ukraine, `europe-west1` (Belgium) or `europe-central2` (Warsaw) work well:

```bash
export REGION=europe-west1
export SERVICE_NAME=calorie-bot
```

### 5.6) Create an Artifact Registry repository

Cloud Build needs a place to store Docker images:

```bash
gcloud artifacts repositories create cloud-run-source-deploy \
  --repository-format=docker \
  --location=${REGION} \
  --description="Docker images for Cloud Run"
```

### 5.7) Build the container image

From the project directory:

```bash
cd /path/to/calorie-telegram-bot

gcloud builds submit \
  --tag ${REGION}-docker.pkg.dev/$(gcloud config get-value project)/cloud-run-source-deploy/${SERVICE_NAME}
```

This uploads your code, builds the Docker image in the cloud, and stores it. Takes 1-3 minutes the first time.

### 5.8) Deploy to Cloud Run (first deploy)

```bash
gcloud run deploy ${SERVICE_NAME} \
  --image ${REGION}-docker.pkg.dev/$(gcloud config get-value project)/cloud-run-source-deploy/${SERVICE_NAME} \
  --region ${REGION} \
  --platform managed \
  --allow-unauthenticated \
  --min-instances 1 \
  --set-env-vars "TELEGRAM_BOT_TOKEN=<your-telegram-token>" \
  --set-env-vars "OPENAI_API_KEY=<your-openai-key>" \
  --set-env-vars "GOOGLE_SHEET_NAME=<exact spreadsheet title>" \
  --set-env-vars "TIMEZONE=Europe/Kyiv" \
  --set-env-vars "DEFAULT_TARGET_KCAL=1800" \
  --set-env-vars "WEBHOOK_SECRET=<pick-any-random-string>"
```

Replace the `<...>` placeholders with your real values. **Do not set `WEBHOOK_URL` yet** -- we need the service URL first.

Notes:
- `--allow-unauthenticated` is required so Telegram can reach the webhook endpoint.
- `--min-instances 1` keeps the container always running so the nightly scheduler works. Set to `0` to save money (but nightly sync will only fire when the container is alive).

The deploy output will show:

```
Service URL: https://calorie-bot-xxxxx-ew.a.run.app
```

Copy this URL.

### 5.9) Set the webhook URL

Redeploy with `WEBHOOK_URL` pointing to the service URL you just got:

```bash
gcloud run services update ${SERVICE_NAME} \
  --region ${REGION} \
  --update-env-vars "WEBHOOK_URL=https://calorie-bot-xxxxx-ew.a.run.app"
```

This triggers a new revision. Once live (a few seconds), the bot calls `setWebhook` on Telegram and starts receiving messages.

### 5.10) Verify it works

1. Open your Telegram bot and send `/start`. You should get the help message.
2. Send a meal, e.g. `2 яйця, тост з маслом`. It should reply with calories and macros and log to the sheet.
3. If something is wrong, check logs:

```bash
gcloud run services logs read ${SERVICE_NAME} --region ${REGION} --limit 50
```

Or in the browser: **Cloud Run -> calorie-bot -> Logs** tab.

### Updating the bot after code changes

Rebuild and redeploy (env vars are preserved):

```bash
gcloud builds submit \
  --tag ${REGION}-docker.pkg.dev/$(gcloud config get-value project)/cloud-run-source-deploy/${SERVICE_NAME}

gcloud run deploy ${SERVICE_NAME} \
  --image ${REGION}-docker.pkg.dev/$(gcloud config get-value project)/cloud-run-source-deploy/${SERVICE_NAME} \
  --region ${REGION}
```

### Credentials file

The `credentials.json` file is baked into the image via `COPY . .`. For production, consider using [Secret Manager](https://cloud.google.com/run/docs/configuring/secrets) to mount it as a volume instead.

### Cost expectations

- **Cloud Run free tier**: 2 million requests/month, 360,000 GB-seconds of memory, 180,000 vCPU-seconds. A personal calorie bot stays well within this.
- **With `--min-instances 1`**: the always-on container uses ~0.5 GB memory, costing roughly $5-10/month after free tier. Set `--min-instances 0` to avoid this.
- **Cloud Build**: 120 free build-minutes/day.
- **Artifact Registry**: 500 MB free storage.

### Troubleshooting

- **"Permission denied" on deploy** -- run `gcloud auth login` again and check the project is correct.
- **Bot does not reply** -- check logs. Common issues: wrong `TELEGRAM_BOT_TOKEN`, missing `WEBHOOK_URL`, or `credentials.json` not in the image.
- **Sheets errors** -- make sure you shared the spreadsheet with the service account email and `GOOGLE_SHEET_NAME` matches exactly (including case).
- **Cold start latency** -- first message after idle takes 3-5 seconds. Use `--min-instances 1` to avoid this.

## Commands

- **Send meal text** — e.g. `2 яйця, тост з маслом, кава з молоком` (rows go to `log`, then `daily` updates). Works in any language; ingredient names in the sheet are always Ukrainian.
- `/today` — total kcal, target, deficit, and macros (P/F/C) for today.
- `/settarget <kcal>` — set calorie target for today (`/settarget 2200 2026-04-14` for a specific date).

## Notes

- Calorie and macro estimates come from the model; treat as approximate.
- The bot overwrites header row 1 if it does not match the expected layout (see above). If upgrading from an older version with fewer columns, insert the new columns (`protein`, `fat`, `carbs`) in the existing sheet or start fresh.
