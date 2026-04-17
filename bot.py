import atexit
import json
import logging
import os
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import gspread
from apscheduler.schedulers.background import BackgroundScheduler
from google.auth.exceptions import MalformedError
from dotenv import load_dotenv
from openai import OpenAI
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters


logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


SYSTEM_PROMPT = """
You are a nutrition extraction assistant.
The user describes a meal in any language (often Russian or Ukrainian).
Split it into ingredients, estimate calories and macronutrients using the procedure below, and return ONLY valid JSON.

Procedure (follow strictly for every ingredient):
1. EXTRACT each distinct food component from the message.
2. ESTIMATE GRAMS: convert the user's amount (pieces, ml, spoons, cups, servings) to total grams. Use standard weights (e.g. 1 chicken egg ≈ 60 g, 1 tbsp butter ≈ 15 g). If no amount is given, assume one typical serving and note the assumption.
3. FIND per-100g values from standard nutrition tables: kcal_per_100g, protein_per_100g, fat_per_100g, carbs_per_100g.
4. COMPUTE each value by multiplying the per-100g figure by grams / 100, rounded to 1 decimal:
   kcal = round(kcal_per_100g * grams / 100, 1)
   protein = round(protein_per_100g * grams / 100, 1)
   fat = round(fat_per_100g * grams / 100, 1)
   carbs = round(carbs_per_100g * grams / 100, 1)

JSON shape:
{
  "ingredients": [
    {
      "ingredient": "яйце куряче",
      "amount": 2,
      "unit": "шт",
      "grams": 120,
      "kcal_per_100g": 155,
      "protein_per_100g": 12.7,
      "fat_per_100g": 10.9,
      "carbs_per_100g": 0.7,
      "kcal": 186,
      "protein": 15.2,
      "fat": 13.1,
      "carbs": 0.8,
      "note": "≈60 г / шт"
    }
  ],
  "total_kcal": number,
  "total_protein": number,
  "total_fat": number,
  "total_carbs": number
}

Rules:
- "ingredient" MUST be a short, standard Ukrainian food name (nominative case, Cyrillic), regardless of what language the user wrote. E.g. "яйце куряче", "хліб житній", "масло вершкове", "куряче філе".
- All numeric fields must be numbers (not strings), rounded to 1 decimal.
- "grams" is the total estimated mass for that line.
- "kcal_per_100g", "protein_per_100g", "fat_per_100g", "carbs_per_100g" are the reference per-100g values you used.
- Each computed field must equal round(per_100g_value * grams / 100, 1).
- Each "total_*" must equal the sum of the corresponding per-ingredient values (within rounding).
- Split mixed meals into separate ingredients whenever possible.
- If amount is unknown, use 1 and unit "порція".
- If uncertain, provide conservative estimates.
- Never include explanations or markdown — return ONLY the JSON object.
""".strip()


@dataclass
class IngredientLog:
    ingredient: str
    amount: float
    unit: str
    kcal: float
    protein: float
    fat: float
    carbs: float
    note: str


class CalorieBot:
    def __init__(self) -> None:
        load_dotenv()

        self.telegram_token = self._required_env("TELEGRAM_BOT_TOKEN")
        self.openai_model = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
        self.timezone = ZoneInfo(os.getenv("TIMEZONE", "UTC"))
        self.default_target_kcal = float(os.getenv("DEFAULT_TARGET_KCAL", "2000"))

        openai_key = self._required_env("OPENAI_API_KEY")
        sheet_name = self._required_env("GOOGLE_SHEET_NAME")
        creds_path = os.getenv("GOOGLE_SHEETS_CREDENTIALS_FILE", "credentials.json")

        self.openai = OpenAI(api_key=openai_key)
        CalorieBot._validate_service_account_json(creds_path)
        try:
            self.gspread_client = gspread.service_account(filename=creds_path)
        except MalformedError as exc:
            raise RuntimeError(
                f"Invalid Google service account JSON at {creds_path!r}. "
                "Create a key under IAM & Admin → Service Accounts → Keys → Add key → JSON, "
                "not OAuth “Desktop/Web client” credentials."
            ) from exc
        spreadsheet = self.gspread_client.open(sheet_name)
        self.log_sheet = spreadsheet.worksheet("log")
        self.daily_sheet = spreadsheet.worksheet("daily")
        self._ensure_headers()

    @staticmethod
    def _required_env(key: str) -> str:
        value = os.getenv(key)
        if not value:
            raise RuntimeError(f"Missing required environment variable: {key}")
        return value

    @staticmethod
    def _validate_service_account_json(path: str) -> None:
        if not os.path.isfile(path):
            raise RuntimeError(f"Google credentials file not found: {path}")
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"Invalid JSON in Google credentials file {path!r}") from exc
        if "installed" in data or "web" in data:
            raise RuntimeError(
                f"{path!r} is OAuth client credentials (Desktop/Web app), not a service account key. "
                "Use Google Cloud: IAM & Admin → Service Accounts → select/create account → "
                "Keys → Add key → JSON. Share your spreadsheet with the `client_email` from that file."
            )
        for key in ("client_email", "private_key", "token_uri"):
            if key not in data:
                raise RuntimeError(
                    f"{path!r} is not a valid service account key (missing {key!r}). "
                    "Download JSON from Service Accounts → Keys, not from Credentials → OAuth client."
                )

    def _ensure_headers(self) -> None:
        log_headers = [
            "logged_at",
            "date",
            "entry_id",
            "ingredient",
            "amount",
            "unit",
            "kcal",
            "protein",
            "fat",
            "carbs",
            "raw_input",
            "note",
        ]
        daily_headers = [
            "date", "total_kcal", "target_kcal", "deficit",
            "total_protein", "total_fat", "total_carbs",
        ]

        if self.log_sheet.row_values(1) != log_headers:
            self.log_sheet.update("A1:L1", [log_headers])
        if self.daily_sheet.row_values(1) != daily_headers:
            self.daily_sheet.update("A1:G1", [daily_headers])

    def extract_ingredients(self, message_text: str) -> list[IngredientLog]:
        response = self.openai.chat.completions.create(
            model=self.openai_model,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": message_text},
            ],
            temperature=0.1,
        )

        content = response.choices[0].message.content
        if not content:
            raise RuntimeError("OpenAI returned empty content.")

        parsed = json.loads(content)
        ingredients = parsed.get("ingredients", [])
        if not isinstance(ingredients, list):
            raise RuntimeError("OpenAI response does not contain ingredients list.")

        logs: list[IngredientLog] = []
        for item in ingredients:
            if not isinstance(item, dict):
                continue

            grams = self._to_float(item.get("grams"))

            def _recompute(per_100g_key: str, fallback_key: str) -> float:
                per_100g = self._to_float(item.get(per_100g_key))
                if grams > 0 and per_100g > 0:
                    return round(per_100g * grams / 100.0, 1)
                return self._to_float(item.get(fallback_key))

            logs.append(
                IngredientLog(
                    ingredient=str(item.get("ingredient", "unknown")).strip() or "unknown",
                    amount=float(item.get("amount", 1)),
                    unit=str(item.get("unit", "порція")).strip() or "порція",
                    kcal=_recompute("kcal_per_100g", "kcal"),
                    protein=_recompute("protein_per_100g", "protein"),
                    fat=_recompute("fat_per_100g", "fat"),
                    carbs=_recompute("carbs_per_100g", "carbs"),
                    note=str(item.get("note", "")).strip(),
                )
            )

        if not logs:
            logs.append(
                IngredientLog(
                    ingredient="unknown",
                    amount=1.0,
                    unit="порція",
                    kcal=self._to_float(parsed.get("total_kcal")),
                    protein=self._to_float(parsed.get("total_protein")),
                    fat=self._to_float(parsed.get("total_fat")),
                    carbs=self._to_float(parsed.get("total_carbs")),
                    note="Fallback ingredient generated from totals",
                )
            )
        return logs

    def append_log_rows(self, raw_input: str, ingredients: list[IngredientLog]) -> str:
        now = datetime.now(self.timezone)
        date_str = now.strftime("%Y-%m-%d")
        logged_at = now.strftime("%Y-%m-%d %H:%M:%S")
        entry_id = uuid.uuid4().hex[:12]

        rows = []
        for ing in ingredients:
            rows.append(
                [
                    logged_at,
                    date_str,
                    entry_id,
                    ing.ingredient,
                    ing.amount,
                    ing.unit,
                    ing.kcal,
                    ing.protein,
                    ing.fat,
                    ing.carbs,
                    raw_input,
                    ing.note,
                ]
            )

        self.log_sheet.append_rows(rows, value_input_option="USER_ENTERED")
        return date_str

    @staticmethod
    def _to_float(value: Any) -> float:
        try:
            return float(value or 0)
        except (TypeError, ValueError):
            return 0.0

    @dataclass
    class _DayTotals:
        kcal: float = 0.0
        protein: float = 0.0
        fat: float = 0.0
        carbs: float = 0.0

    def totals_for_date(self, date_str: str) -> _DayTotals:
        rows = self.log_sheet.get_all_values()
        t = self._DayTotals()
        for row in rows[1:]:
            if len(row) < 10:
                continue
            if row[1].strip() != date_str:
                continue
            t.kcal += self._to_float(row[6])
            t.protein += self._to_float(row[7])
            t.fat += self._to_float(row[8])
            t.carbs += self._to_float(row[9])
        t.kcal = round(t.kcal, 1)
        t.protein = round(t.protein, 1)
        t.fat = round(t.fat, 1)
        t.carbs = round(t.carbs, 1)
        return t

    @dataclass
    class DailyResult:
        total_kcal: float
        target_kcal: float
        deficit: float
        total_protein: float
        total_fat: float
        total_carbs: float

    def upsert_daily(
        self,
        date_str: str,
        *,
        target_kcal: float | None = None,
    ) -> DailyResult:
        """Recompute daily row from log (source of truth) and upsert the daily sheet.

        Totals always come from summing columns in log for date_str.
        target_kcal comes from target_kcal if provided; else existing daily row;
        else DEFAULT_TARGET_KCAL.
        deficit = target_kcal - total_kcal (positive means under target).
        """
        totals = self.totals_for_date(date_str)
        rows = self.daily_sheet.get_all_values()

        found_index: int | None = None
        resolved_target = self.default_target_kcal

        for idx, row in enumerate(rows[1:], start=2):
            if not row:
                continue
            if row[0].strip() != date_str:
                continue
            found_index = idx
            if target_kcal is not None:
                resolved_target = target_kcal
            elif len(row) >= 3 and str(row[2]).strip():
                resolved_target = self._to_float(row[2]) or self.default_target_kcal
            else:
                resolved_target = self.default_target_kcal
            break

        if found_index is None:
            resolved_target = target_kcal if target_kcal is not None else self.default_target_kcal

        deficit = round(resolved_target - totals.kcal, 1)
        payload = [
            date_str, totals.kcal, resolved_target, deficit,
            totals.protein, totals.fat, totals.carbs,
        ]

        if found_index is None:
            self.daily_sheet.append_row(payload, value_input_option="USER_ENTERED")
        else:
            self.daily_sheet.update(f"A{found_index}:G{found_index}", [payload])

        return self.DailyResult(
            total_kcal=totals.kcal,
            target_kcal=resolved_target,
            deficit=deficit,
            total_protein=totals.protein,
            total_fat=totals.fat,
            total_carbs=totals.carbs,
        )

    def today_total_calories(self) -> float:
        date_str = datetime.now(self.timezone).strftime("%Y-%m-%d")
        result = self.upsert_daily(date_str)
        return result.total_kcal


bot = CalorieBot()


def nightly_daily_sync() -> None:
    """Refresh daily rows from log for yesterday and today (handles late entries, manual log edits)."""
    try:
        now = datetime.now(bot.timezone)
        today = now.strftime("%Y-%m-%d")
        yesterday = (now.date() - timedelta(days=1)).isoformat()
        for d in (yesterday, today):
            bot.upsert_daily(d)
        logger.info("Nightly daily sync completed for %s and %s", yesterday, today)
    except Exception:
        logger.exception("Nightly daily sync failed")


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Send me your meal text and I will log ingredients into the 'log' sheet\n"
        "and refresh totals in the 'daily' sheet.\n"
        "Example: 'Lunch: 2 eggs, toast with butter, coffee'.\n\n"
        "Commands:\n"
        "/today - today's total and targets\n"
        "/settarget <kcal> [YYYY-MM-DD] - set calorie target for today or a date\n"
        "/help - show this help"
    )


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await start(update, context)


async def today(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    date_str = datetime.now(bot.timezone).strftime("%Y-%m-%d")
    d = bot.upsert_daily(date_str)
    await update.message.reply_text(
        f"Today ({date_str})\n"
        f"Total: {d.total_kcal} kcal / target {d.target_kcal} kcal\n"
        f"Deficit: {d.deficit} kcal\n"
        f"P {d.total_protein} g · F {d.total_fat} g · C {d.total_carbs} g"
    )


async def set_target(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await update.message.reply_text(
            "Usage: /settarget <kcal> [YYYY-MM-DD]\n"
            "Example: /settarget 2200\n"
            "Example: /settarget 2200 2026-04-14"
        )
        return

    try:
        raw_target = context.args[0].replace(",", ".")
        new_target = float(raw_target)
    except ValueError:
        await update.message.reply_text("kcal must be a number, e.g. /settarget 2200")
        return

    if new_target <= 0 or new_target > 20000:
        await update.message.reply_text("Please use a sensible target between 1 and 20000 kcal.")
        return

    tz_now = datetime.now(bot.timezone)
    if len(context.args) >= 2:
        date_str = context.args[1].strip()
        try:
            datetime.strptime(date_str, "%Y-%m-%d")
        except ValueError:
            await update.message.reply_text("Date must be YYYY-MM-DD")
            return
    else:
        date_str = tz_now.strftime("%Y-%m-%d")

    d = bot.upsert_daily(date_str, target_kcal=new_target)
    await update.message.reply_text(
        f"Target set for {date_str}: {d.target_kcal} kcal.\n"
        f"Total (from log): {d.total_kcal} kcal\n"
        f"Deficit: {d.deficit} kcal\n"
        f"P {d.total_protein} g · F {d.total_fat} g · C {d.total_carbs} g"
    )


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.message.text:
        return

    meal_text = update.message.text.strip()
    try:
        ingredients = bot.extract_ingredients(meal_text)
        date_str = bot.append_log_rows(meal_text, ingredients)
        d = bot.upsert_daily(date_str)
        logged_kcal = round(sum(item.kcal for item in ingredients), 1)
        logged_p = round(sum(item.protein for item in ingredients), 1)
        logged_f = round(sum(item.fat for item in ingredients), 1)
        logged_c = round(sum(item.carbs for item in ingredients), 1)
        await update.message.reply_text(
            f"Logged ({len(ingredients)} rows): {logged_kcal} kcal "
            f"(P {logged_p} · F {logged_f} · C {logged_c})\n"
            f"Today: {d.total_kcal} kcal / target {d.target_kcal} kcal\n"
            f"Deficit: {d.deficit} kcal\n"
            f"P {d.total_protein} g · F {d.total_fat} g · C {d.total_carbs} g"
        )
    except Exception as exc:
        logger.exception("Failed to process message")
        await update.message.reply_text(
            "I couldn't process that meal right now. Please try again.\n"
            f"Error: {exc}"
        )


def _build_app() -> Application:
    app = Application.builder().token(bot.telegram_token).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("today", today))
    app.add_handler(CommandHandler("settarget", set_target))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    return app


def _start_scheduler() -> BackgroundScheduler:
    tz_name = os.getenv("TIMEZONE", "UTC")
    sync_hour = int(os.getenv("DAILY_SYNC_HOUR", "0"))
    sync_minute = int(os.getenv("DAILY_SYNC_MINUTE", "5"))
    scheduler = BackgroundScheduler(timezone=tz_name)
    scheduler.add_job(
        nightly_daily_sync,
        "cron",
        hour=sync_hour,
        minute=sync_minute,
        id="nightly_daily_sync",
        replace_existing=True,
    )
    scheduler.start()
    atexit.register(lambda: scheduler.shutdown(wait=False))
    logger.info(
        "Scheduled daily sheet sync at %02d:%02d (%s) - recomputes yesterday + today from log",
        sync_hour,
        sync_minute,
        tz_name,
    )
    return scheduler


def main() -> None:
    _start_scheduler()
    app = _build_app()

    webhook_url = os.getenv("WEBHOOK_URL", "").strip()
    if webhook_url:
        port = int(os.getenv("PORT", "8080"))
        webhook_path = f"/telegram/{bot.telegram_token}"
        secret_token = os.getenv("WEBHOOK_SECRET", uuid.uuid4().hex)
        logger.info("Starting webhook on port %d (path %s)", port, webhook_path)
        app.run_webhook(
            listen="0.0.0.0",
            port=port,
            url_path=webhook_path,
            webhook_url=f"{webhook_url}{webhook_path}",
            secret_token=secret_token,
            close_loop=False,
        )
    else:
        logger.info("No WEBHOOK_URL set — falling back to polling mode")
        app.run_polling(close_loop=False)


if __name__ == "__main__":
    main()
