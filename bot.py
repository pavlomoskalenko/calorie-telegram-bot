import json
import logging
import os
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from google.cloud import firestore
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


@dataclass
class DailyResult:
    total_kcal: float
    target_kcal: float
    deficit: float
    total_protein: float
    total_fat: float
    total_carbs: float


class UserStore:
    """Per-user calorie data in Firestore.

    Data model:
        users/{user_id}                  -- user prefs (target_kcal, timezone)
        users/{user_id}/log/{auto_id}    -- one doc per ingredient row
        users/{user_id}/daily/{date_str} -- one doc per day (derived totals)
    """

    def __init__(
        self,
        *,
        default_timezone: ZoneInfo,
        default_target_kcal: float,
        collection: str = "users",
    ) -> None:
        self._db = firestore.Client()
        self._collection = collection
        self._default_tz = default_timezone
        self._default_target = default_target_kcal

    def _user_ref(self, user_id: int) -> firestore.DocumentReference:
        return self._db.collection(self._collection).document(str(user_id))

    def get_timezone(self, user_id: int) -> ZoneInfo:
        snap = self._user_ref(user_id).get(field_paths=["timezone"])
        if snap.exists:
            tz_str = (snap.to_dict() or {}).get("timezone", "")
            if tz_str:
                try:
                    return ZoneInfo(tz_str)
                except KeyError:
                    pass
        return self._default_tz

    def get_target_kcal(self, user_id: int) -> float:
        snap = self._user_ref(user_id).get(field_paths=["target_kcal"])
        if snap.exists:
            val = (snap.to_dict() or {}).get("target_kcal")
            if val is not None:
                try:
                    return float(val)
                except (TypeError, ValueError):
                    pass
        return self._default_target

    def set_target(self, user_id: int, target_kcal: float) -> None:
        self._user_ref(user_id).set({"target_kcal": target_kcal}, merge=True)

    def append_log(
        self,
        user_id: int,
        raw_input: str,
        ingredients: list[IngredientLog],
        timezone: ZoneInfo,
    ) -> str:
        now = datetime.now(timezone)
        date_str = now.strftime("%Y-%m-%d")
        logged_at = now.strftime("%Y-%m-%d %H:%M:%S")
        entry_id = uuid.uuid4().hex[:12]

        log_col = self._user_ref(user_id).collection("log")
        batch = self._db.batch()
        for ing in ingredients:
            doc_ref = log_col.document()
            batch.set(doc_ref, {
                "logged_at": logged_at,
                "date": date_str,
                "entry_id": entry_id,
                "ingredient": ing.ingredient,
                "amount": ing.amount,
                "unit": ing.unit,
                "kcal": ing.kcal,
                "protein": ing.protein,
                "fat": ing.fat,
                "carbs": ing.carbs,
                "raw_input": raw_input,
                "note": ing.note,
            })
        batch.commit()
        return date_str

    @dataclass
    class _DayTotals:
        kcal: float = 0.0
        protein: float = 0.0
        fat: float = 0.0
        carbs: float = 0.0

    def totals_for_date(self, user_id: int, date_str: str) -> _DayTotals:
        log_col = self._user_ref(user_id).collection("log")
        docs = log_col.where(filter=firestore.FieldFilter("date", "==", date_str)).stream()
        t = self._DayTotals()
        for doc in docs:
            d = doc.to_dict()
            t.kcal += _to_float(d.get("kcal"))
            t.protein += _to_float(d.get("protein"))
            t.fat += _to_float(d.get("fat"))
            t.carbs += _to_float(d.get("carbs"))
        t.kcal = round(t.kcal, 1)
        t.protein = round(t.protein, 1)
        t.fat = round(t.fat, 1)
        t.carbs = round(t.carbs, 1)
        return t

    def get_meals_for_date(self, user_id: int, date_str: str) -> list[dict[str, Any]]:
        log_col = self._user_ref(user_id).collection("log")
        docs = (
            log_col
            .where(filter=firestore.FieldFilter("date", "==", date_str))
            .order_by("logged_at")
            .stream()
        )
        return [doc.to_dict() for doc in docs]

    def upsert_daily(
        self,
        user_id: int,
        date_str: str,
        *,
        target_kcal: float | None = None,
    ) -> DailyResult:
        totals = self.totals_for_date(user_id, date_str)

        daily_ref = self._user_ref(user_id).collection("daily").document(date_str)

        if target_kcal is not None:
            resolved_target = target_kcal
        else:
            snap = daily_ref.get(field_paths=["target_kcal"])
            if snap.exists:
                existing = _to_float((snap.to_dict() or {}).get("target_kcal"))
                resolved_target = existing if existing > 0 else self.get_target_kcal(user_id)
            else:
                resolved_target = self.get_target_kcal(user_id)

        deficit = round(resolved_target - totals.kcal, 1)

        daily_ref.set({
            "total_kcal": totals.kcal,
            "target_kcal": resolved_target,
            "deficit": deficit,
            "total_protein": totals.protein,
            "total_fat": totals.fat,
            "total_carbs": totals.carbs,
        })

        return DailyResult(
            total_kcal=totals.kcal,
            target_kcal=resolved_target,
            deficit=deficit,
            total_protein=totals.protein,
            total_fat=totals.fat,
            total_carbs=totals.carbs,
        )


def _to_float(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


class CalorieBot:
    def __init__(self) -> None:
        load_dotenv()

        self.telegram_token = self._required_env("TELEGRAM_BOT_TOKEN")
        self.openai_model = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
        self.timezone = ZoneInfo(os.getenv("TIMEZONE", "UTC"))
        self.default_target_kcal = float(os.getenv("DEFAULT_TARGET_KCAL", "2000"))

        openai_key = self._required_env("OPENAI_API_KEY")
        self.openai = OpenAI(api_key=openai_key)

        collection = os.getenv("FIRESTORE_COLLECTION", "users")
        self.store = UserStore(
            default_timezone=self.timezone,
            default_target_kcal=self.default_target_kcal,
            collection=collection,
        )

    @staticmethod
    def _required_env(key: str) -> str:
        value = os.getenv(key)
        if not value:
            raise RuntimeError(f"Missing required environment variable: {key}")
        return value

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

            grams = _to_float(item.get("grams"))

            def _recompute(per_100g_key: str, fallback_key: str) -> float:
                per_100g = _to_float(item.get(per_100g_key))
                if grams > 0 and per_100g > 0:
                    return round(per_100g * grams / 100.0, 1)
                return _to_float(item.get(fallback_key))

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
                    kcal=_to_float(parsed.get("total_kcal")),
                    protein=_to_float(parsed.get("total_protein")),
                    fat=_to_float(parsed.get("total_fat")),
                    carbs=_to_float(parsed.get("total_carbs")),
                    note="Fallback ingredient generated from totals",
                )
            )
        return logs


bot = CalorieBot()


def _require_user(update: Update) -> int | None:
    user = update.effective_user
    if not user:
        return None
    return user.id


def _daily_summary(d: DailyResult, label: str = "Today") -> str:
    return (
        f"\U0001f4ca {label}: {d.total_kcal} / {d.target_kcal} kcal\n"
        f"\U0001f525 Remaining: {d.deficit} kcal\n"
        f"\U0001f969 P {d.total_protein}  \U0001f9c8 F {d.total_fat}  \U0001f35e C {d.total_carbs}"
    )


def _format_today_meals(meals: list[dict[str, Any]]) -> str:
    if not meals:
        return ""
    groups: dict[str, list[dict[str, Any]]] = {}
    group_order: list[str] = []
    for row in meals:
        eid = row.get("entry_id", "")
        if eid not in groups:
            groups[eid] = []
            group_order.append(eid)
        groups[eid].append(row)

    parts: list[str] = []
    for eid in group_order:
        rows = groups[eid]
        logged_at = rows[0].get("logged_at", "")
        time_str = logged_at[11:16] if len(logged_at) >= 16 else logged_at
        meal_kcal = round(sum(_to_float(r.get("kcal")) for r in rows), 1)
        lines = [f"\U0001f37d {time_str} \u2014 {meal_kcal} kcal"]
        for r in rows:
            name = r.get("ingredient", "?")
            amt = r.get("amount", "")
            unit = r.get("unit", "")
            kcal = _to_float(r.get("kcal"))
            lines.append(f"  {name} {amt} {unit} \u00b7 {kcal} kcal")
        parts.append("\n".join(lines))

    return "\n\n".join(parts) + "\n\n"


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    await update.message.reply_text(
        "Send a meal description and I\u2019ll log it.\n"
        "Example: \u00ab2 eggs, toast with butter, coffee\u00bb\n\n"
        "Commands:\n"
        "/today \u2014 today\u2019s meals and totals\n"
        "/day -1 or /day 2026-04-15 \u2014 view any day\n"
        "/settarget <kcal> [YYYY-MM-DD] \u2014 set calorie target\n"
        "/help \u2014 show this help"
    )


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await start(update, context)


async def today(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    uid = _require_user(update)
    if uid is None:
        return
    tz = bot.store.get_timezone(uid)
    date_str = datetime.now(tz).strftime("%Y-%m-%d")
    d = bot.store.upsert_daily(uid, date_str)
    meals = bot.store.get_meals_for_date(uid, date_str)
    await update.message.reply_text(
        _format_today_meals(meals) + _daily_summary(d)
    )


async def day_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    uid = _require_user(update)
    if uid is None:
        return
    tz = bot.store.get_timezone(uid)
    today = datetime.now(tz).date()

    if not context.args:
        target_date = today
    else:
        arg = context.args[0].strip()
        if arg.lstrip("-").isdigit() and arg.startswith("-"):
            try:
                offset = int(arg)
                target_date = today + timedelta(days=offset)
            except (ValueError, OverflowError):
                await update.message.reply_text("Invalid offset. Example: /day -1")
                return
        else:
            try:
                target_date = datetime.strptime(arg, "%Y-%m-%d").date()
            except ValueError:
                await update.message.reply_text(
                    "Usage: /day [date or offset]\n"
                    "Examples:\n"
                    "  /day \u2014 today\n"
                    "  /day -1 \u2014 yesterday\n"
                    "  /day -3 \u2014 three days ago\n"
                    "  /day 2026-04-15 \u2014 specific date"
                )
                return

    date_str = target_date.strftime("%Y-%m-%d")
    label = "Today" if target_date == today else date_str
    d = bot.store.upsert_daily(uid, date_str)
    meals = bot.store.get_meals_for_date(uid, date_str)
    await update.message.reply_text(
        _format_today_meals(meals) + _daily_summary(d, label=label)
    )


async def set_target(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    uid = _require_user(update)
    if uid is None:
        return
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

    bot.store.set_target(uid, new_target)

    tz = bot.store.get_timezone(uid)
    tz_now = datetime.now(tz)
    if len(context.args) >= 2:
        date_str = context.args[1].strip()
        try:
            datetime.strptime(date_str, "%Y-%m-%d")
        except ValueError:
            await update.message.reply_text("Date must be YYYY-MM-DD")
            return
    else:
        date_str = tz_now.strftime("%Y-%m-%d")

    d = bot.store.upsert_daily(uid, date_str, target_kcal=new_target)
    await update.message.reply_text(
        f"\U0001f3af Target set: {d.target_kcal} kcal ({date_str})\n\n"
        + _daily_summary(d)
    )


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.message.text:
        return
    uid = _require_user(update)
    if uid is None:
        return

    meal_text = update.message.text.strip()
    try:
        ingredients = bot.extract_ingredients(meal_text)
        tz = bot.store.get_timezone(uid)
        date_str = bot.store.append_log(uid, meal_text, ingredients, tz)
        d = bot.store.upsert_daily(uid, date_str)
        logged_kcal = round(sum(item.kcal for item in ingredients), 1)
        logged_p = round(sum(item.protein for item in ingredients), 1)
        logged_f = round(sum(item.fat for item in ingredients), 1)
        logged_c = round(sum(item.carbs for item in ingredients), 1)
        await update.message.reply_text(
            f"\U0001f37d {logged_kcal} kcal \u00b7 P {logged_p} \u00b7 F {logged_f} \u00b7 C {logged_c}\n\n"
            + _daily_summary(d)
        )
    except Exception as exc:
        logger.exception("Failed to process message")
        await update.message.reply_text(
            "I couldn\u2019t process that meal right now. Please try again.\n"
            f"Error: {exc}"
        )


def _build_app() -> Application:
    app = Application.builder().token(bot.telegram_token).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("today", today))
    app.add_handler(CommandHandler("day", day_cmd))
    app.add_handler(CommandHandler("settarget", set_target))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    return app


def main() -> None:
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
