import json
import logging
import os
import tempfile
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol, runtime_checkable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import gspread
from google.auth.exceptions import MalformedError
from dotenv import load_dotenv
from gspread.exceptions import APIError, SpreadsheetNotFound, WorksheetNotFound
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


def _validate_service_account_dict(data: dict[str, Any]) -> None:
    if "installed" in data or "web" in data:
        raise RuntimeError(
            "This file is OAuth client credentials (Desktop/Web app), not a service account key. "
            "Use Google Cloud: IAM & Admin → Service Accounts → select/create account → "
            "Keys → Add key → JSON. Share your spreadsheet with the `client_email` from that file."
        )
    for key in ("client_email", "private_key", "token_uri"):
        if key not in data:
            raise RuntimeError(
                f"Not a valid service account key (missing {key!r}). "
                "Download JSON from Service Accounts → Keys, not from Credentials → OAuth client."
            )


@runtime_checkable
class UserConfigBackend(Protocol):
    def load(self, user_id: int) -> dict[str, Any] | None: ...
    def save(self, user_id: int, payload: dict[str, Any]) -> None: ...
    def describe_location(self, user_id: int) -> str: ...

    def has_complete_config(self, user_id: int) -> bool:
        data = self.load(user_id)
        if not data:
            return False
        sa = data.get("service_account")
        name = (data.get("sheet_name") or "").strip()
        return isinstance(sa, dict) and bool(name)


def _has_complete_config(backend: UserConfigBackend, user_id: int) -> bool:
    """Shared logic — Protocol default methods aren't inherited by concrete classes."""
    data = backend.load(user_id)
    if not data:
        return False
    sa = data.get("service_account")
    name = (data.get("sheet_name") or "").strip()
    return isinstance(sa, dict) and bool(name)


class FilesystemUserConfigStore:
    """Per-Telegram-user config as JSON files on disk (local dev / fallback)."""

    def __init__(self, base_dir: Path) -> None:
        self.base_dir = base_dir
        self.base_dir.mkdir(parents=True, exist_ok=True)

    def describe_location(self, user_id: int) -> str:
        return str(self.base_dir / f"{user_id}.json")

    def load(self, user_id: int) -> dict[str, Any] | None:
        path = self.base_dir / f"{user_id}.json"
        if not path.is_file():
            return None
        try:
            with path.open(encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            logger.exception("Failed to read user config for %s", user_id)
            return None

    def save(self, user_id: int, payload: dict[str, Any]) -> None:
        path = self.base_dir / f"{user_id}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(
            dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as tmp:
                json.dump(payload, tmp, indent=2)
                tmp.flush()
                os.fsync(tmp.fileno())
            os.replace(tmp_name, path)
            try:
                path.chmod(0o600)
            except OSError:
                pass
        finally:
            if os.path.isfile(tmp_name):
                try:
                    os.unlink(tmp_name)
                except OSError:
                    pass

    def has_complete_config(self, user_id: int) -> bool:
        return _has_complete_config(self, user_id)


class FirestoreUserConfigStore:
    """Per-Telegram-user config in Google Cloud Firestore."""

    def __init__(self, collection: str = "user_configs") -> None:
        from google.cloud import firestore as _firestore  # noqa: delayed import
        self._db = _firestore.Client()
        self._collection = collection

    def _doc_ref(self, user_id: int):  # noqa: ANN204 (returns DocumentReference)
        return self._db.collection(self._collection).document(str(user_id))

    def describe_location(self, user_id: int) -> str:
        return f"Firestore {self._collection}/{user_id}"

    def load(self, user_id: int) -> dict[str, Any] | None:
        snap = self._doc_ref(user_id).get()
        if not snap.exists:
            return None
        return snap.to_dict()

    def save(self, user_id: int, payload: dict[str, Any]) -> None:
        self._doc_ref(user_id).set(payload)

    def has_complete_config(self, user_id: int) -> bool:
        return _has_complete_config(self, user_id)


def _create_config_backend() -> UserConfigBackend:
    backend_type = os.getenv("USER_CONFIG_BACKEND", "").strip().lower()

    if backend_type == "firestore":
        collection = os.getenv("FIRESTORE_COLLECTION", "user_configs")
        logger.info("Using Firestore backend (collection=%s)", collection)
        return FirestoreUserConfigStore(collection=collection)

    if backend_type == "filesystem":
        data_dir = Path(os.getenv("USER_DATA_DIR", "user_data")).resolve()
        logger.info("Using filesystem backend (dir=%s)", data_dir)
        return FilesystemUserConfigStore(data_dir)

    # Auto-detect: try Firestore if running on GCP, fall back to filesystem.
    if os.getenv("GOOGLE_CLOUD_PROJECT") or os.getenv("K_SERVICE"):
        try:
            collection = os.getenv("FIRESTORE_COLLECTION", "user_configs")
            store = FirestoreUserConfigStore(collection=collection)
            logger.info("Auto-detected GCP — using Firestore backend (collection=%s)", collection)
            return store
        except Exception:
            logger.info("Firestore unavailable, falling back to filesystem backend")

    data_dir = Path(os.getenv("USER_DATA_DIR", "user_data")).resolve()
    logger.info("Using filesystem backend (dir=%s)", data_dir)
    return FilesystemUserConfigStore(data_dir)


class UserSpreadsheet:
    """Google Sheets client + worksheets for one user."""

    def __init__(
        self,
        *,
        service_account: dict[str, Any],
        sheet_name: str,
        timezone: ZoneInfo,
        default_target_kcal: float,
    ) -> None:
        _validate_service_account_dict(service_account)
        try:
            self._gspread_client = gspread.service_account_from_dict(service_account)
        except MalformedError as exc:
            raise RuntimeError(
                "Invalid Google service account JSON. "
                "Create a key under IAM & Admin → Service Accounts → Keys → Add key → JSON."
            ) from exc
        self.timezone = timezone
        self.default_target_kcal = default_target_kcal
        try:
            spreadsheet = self._gspread_client.open(sheet_name)
        except SpreadsheetNotFound as exc:
            raise RuntimeError(
                f"Spreadsheet not found: {sheet_name!r}. "
                "Copy the exact title from Google Sheets, and ensure the service account email "
                "from your JSON has Editor access to the file."
            ) from exc
        except APIError as exc:
            raise RuntimeError(f"Google Sheets API error while opening spreadsheet: {exc}") from exc

        self.log_sheet = self._get_or_create_ws(spreadsheet, "log", rows=5000, cols=12)
        self.daily_sheet = self._get_or_create_ws(spreadsheet, "daily", rows=4000, cols=8)
        self._ensure_headers()

    @staticmethod
    def _get_or_create_ws(
        spreadsheet: gspread.Spreadsheet, title: str, *, rows: int, cols: int
    ) -> gspread.Worksheet:
        try:
            return spreadsheet.worksheet(title)
        except WorksheetNotFound:
            return spreadsheet.add_worksheet(title=title, rows=rows, cols=cols)

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


class CalorieBot:
    """Shared OpenAI + per-user Google Sheets."""

    def __init__(self) -> None:
        load_dotenv()

        self.telegram_token = self._required_env("TELEGRAM_BOT_TOKEN")
        self.openai_model = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
        self.env_timezone = ZoneInfo(os.getenv("TIMEZONE", "UTC"))
        self.env_default_target_kcal = float(os.getenv("DEFAULT_TARGET_KCAL", "2000"))

        openai_key = self._required_env("OPENAI_API_KEY")
        self.openai = OpenAI(api_key=openai_key)

        self._store: UserConfigBackend = _create_config_backend()
        self._sheet_cache: dict[int, UserSpreadsheet] = {}

    def read_user_config(self, user_id: int) -> dict[str, Any] | None:
        return self._store.load(user_id)

    def user_sheets_configured(self, user_id: int) -> bool:
        return self._store.has_complete_config(user_id)

    def describe_user_config(self, user_id: int) -> str:
        return self._store.describe_location(user_id)

    @staticmethod
    def _required_env(key: str) -> str:
        value = os.getenv(key)
        if not value:
            raise RuntimeError(f"Missing required environment variable: {key}")
        return value

    def _resolve_timezone(self, stored: str | None) -> ZoneInfo:
        if not stored or not str(stored).strip():
            return self.env_timezone
        try:
            return ZoneInfo(str(stored).strip())
        except ZoneInfoNotFoundError as exc:
            raise RuntimeError(f"Unknown timezone: {stored!r}") from exc

    def _invalidate_user_cache(self, user_id: int) -> None:
        self._sheet_cache.pop(user_id, None)

    def get_user_spreadsheet(self, user_id: int) -> UserSpreadsheet | None:
        if user_id in self._sheet_cache:
            return self._sheet_cache[user_id]
        data = self._store.load(user_id)
        if not data or not self._store.has_complete_config(user_id):
            return None
        sa = data["service_account"]
        sheet_name = str(data["sheet_name"]).strip()
        tz = self._resolve_timezone(data.get("timezone"))
        target = float(data.get("default_target_kcal") or self.env_default_target_kcal)
        try:
            us = UserSpreadsheet(
                service_account=sa,
                sheet_name=sheet_name,
                timezone=tz,
                default_target_kcal=target,
            )
        except RuntimeError:
            raise
        except Exception as exc:
            raise RuntimeError(f"Could not open your Google Sheet: {exc}") from exc
        self._sheet_cache[user_id] = us
        return us

    def save_service_account_json(self, user_id: int, sa: dict[str, Any]) -> None:
        _validate_service_account_dict(sa)
        existing = self._store.load(user_id) or {}
        payload = {
            "service_account": sa,
            "sheet_name": "",
            "timezone": existing.get("timezone") or "",
            "default_target_kcal": existing.get("default_target_kcal") or self.env_default_target_kcal,
        }
        self._store.save(user_id, payload)
        self._invalidate_user_cache(user_id)

    def set_sheet_name(self, user_id: int, sheet_name: str) -> UserSpreadsheet:
        data = self._store.load(user_id)
        if not data or not isinstance(data.get("service_account"), dict):
            raise RuntimeError(
                "No Google credentials on file yet. Upload your service account JSON first "
                "(send the file as a document in this chat)."
            )
        name = sheet_name.strip()
        if not name:
            raise RuntimeError("Spreadsheet title cannot be empty.")

        data["sheet_name"] = name
        self._store.save(user_id, data)
        self._invalidate_user_cache(user_id)
        us = self.get_user_spreadsheet(user_id)
        if not us:
            raise RuntimeError("Failed to load spreadsheet after saving configuration.")
        return us

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

    @staticmethod
    def _to_float(value: Any) -> float:
        try:
            return float(value or 0)
        except (TypeError, ValueError):
            return 0.0


bot = CalorieBot()


def _require_user(update: Update) -> int | None:
    user = update.effective_user
    if not user:
        return None
    return user.id


def _not_configured_message() -> str:
    return (
        "This chat is not linked to a Google Sheet yet.\n\n"
        "1) In Google Cloud, create a service account and download its JSON key.\n"
        "2) Share your spreadsheet with the service account email (Editor).\n"
        "3) Send the JSON file here as a document (not a text message).\n"
        "4) Run /setsheet followed by the exact spreadsheet title, e.g.:\n"
        "   /setsheet Daily Calorie Tracker\n\n"
        "Use /setup for the full guide. Your OpenAI usage is billed to the bot host; "
        "only Google access is yours."
    )


def _daily_summary(d: UserSpreadsheet.DailyResult) -> str:
    return (
        f"\U0001f4ca Today: {d.total_kcal} / {d.target_kcal} kcal\n"
        f"\U0001f525 Remaining: {d.deficit} kcal\n"
        f"\U0001f969 P {d.total_protein}  \U0001f9c8 F {d.total_fat}  \U0001f35e C {d.total_carbs}"
    )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Send a meal description and I will log ingredients into your Google Sheet "
        "(tabs `log` and `daily`).\n"
        "Example: 'Lunch: 2 eggs, toast with butter, coffee'.\n\n"
        "First-time setup: /setup\n\n"
        "Commands:\n"
        "/today — today's total and targets\n"
        "/settarget <kcal> [YYYY-MM-DD] — set calorie target\n"
        "/setsheet <title> — link the spreadsheet after uploading credentials\n"
        "/status — show whether Google Sheets is configured\n"
        "/help — show this help"
    )


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await start(update, context)


async def setup_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "\U0001f527 Setting up your Google Sheet\n\n"
        "This bot logs meals to your Google Sheet using "
        "your own Google Cloud service account. "
        "The bot host never sees your spreadsheet data.\n\n"
        "1\ufe0f\u20e3 Create a Google Cloud project\n"
        "Go to console.cloud.google.com\n"
        "Click the project dropdown \u27a1 New Project\n"
        "(skip if you already have one)\n\n"
        "2\ufe0f\u20e3 Enable APIs\n"
        "In your project, open APIs & Services \u27a1 Library\n"
        "Search and enable:\n"
        "\u2022 Google Sheets API\n"
        "\u2022 Google Drive API\n\n"
        "3\ufe0f\u20e3 Create a service account key\n"
        "Open IAM & Admin \u27a1 Service Accounts\n"
        "\u27a1 Create Service Account (any name works)\n"
        "Then click into it \u27a1 Keys \u27a1 Add Key \u27a1 JSON\n"
        "A .json file will download \u2014 save it.\n\n"
        "4\ufe0f\u20e3 Share your spreadsheet\n"
        'Open the .json, find the "client_email" value.\n'
        "In Google Sheets, click Share and add that "
        "email as an Editor.\n\n"
        "5\ufe0f\u20e3 Send the JSON key here\n"
        "Tap \U0001f4ce \u27a1 File and send the .json file "
        "in this chat. Don\u2019t paste the key as text!\n\n"
        "6\ufe0f\u20e3 Link the spreadsheet\n"
        "/setsheet My Calorie Log\n"
        "Use the exact title from the browser tab.\n\n"
        "\u2705 Done! Send a meal to start logging.\n\n"
        "\U0001f504 To rotate credentials later, re-send a new "
        "JSON file and run /setsheet again."
    )


async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = _require_user(update)
    if uid is None:
        return
    if bot.user_sheets_configured(uid):
        data = bot.read_user_config(uid) or {}
        title = (data.get("sheet_name") or "").strip()
        await update.message.reply_text(
            f"Google Sheets: configured.\nSpreadsheet title: {title!r}\n"
            f"Config: {bot.describe_user_config(uid)}"
        )
    else:
        data = bot.read_user_config(uid)
        has_creds = bool(data and isinstance(data.get("service_account"), dict))
        if has_creds:
            await update.message.reply_text(
                "Credentials are saved, but no spreadsheet title yet. "
                "Use /setsheet <exact title from Google Sheets>."
            )
        else:
            await update.message.reply_text("Google Sheets: not configured. See /setup.")


async def today(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = _require_user(update)
    if uid is None:
        return
    try:
        sheets = bot.get_user_spreadsheet(uid)
    except RuntimeError as exc:
        await update.message.reply_text(str(exc))
        return
    if not sheets:
        await update.message.reply_text(_not_configured_message())
        return
    date_str = datetime.now(sheets.timezone).strftime("%Y-%m-%d")
    d = sheets.upsert_daily(date_str)
    await update.message.reply_text(_daily_summary(d))


async def set_sheet(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = _require_user(update)
    if uid is None:
        return
    if not context.args:
        await update.message.reply_text(
            "Usage: /setsheet <exact spreadsheet title>\n"
            "Example: /setsheet Daily Calorie Tracker\n\n"
            "Upload your service account JSON first (as a document)."
        )
        return
    title = " ".join(context.args).strip()
    try:
        bot.set_sheet_name(uid, title)
    except RuntimeError as exc:
        await update.message.reply_text(str(exc))
        return
    await update.message.reply_text(
        f"Linked spreadsheet {title!r}. Tabs `log` and `daily` are ready.\n"
        "You can start logging meals with plain text messages."
    )


async def set_target(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
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
        sheets = bot.get_user_spreadsheet(uid)
    except RuntimeError as exc:
        await update.message.reply_text(str(exc))
        return
    if not sheets:
        await update.message.reply_text(_not_configured_message())
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

    tz_now = datetime.now(sheets.timezone)
    if len(context.args) >= 2:
        date_str = context.args[1].strip()
        try:
            datetime.strptime(date_str, "%Y-%m-%d")
        except ValueError:
            await update.message.reply_text("Date must be YYYY-MM-DD")
            return
    else:
        date_str = tz_now.strftime("%Y-%m-%d")

    d = sheets.upsert_daily(date_str, target_kcal=new_target)
    await update.message.reply_text(
        f"\U0001f3af Target set: {d.target_kcal} kcal ({date_str})\n\n"
        + _daily_summary(d)
    )


async def handle_credentials_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = _require_user(update)
    if uid is None:
        return
    doc = update.message.document
    if not doc:
        return

    max_bytes = 512_000
    if doc.file_size is not None and doc.file_size > max_bytes:
        await update.message.reply_text("That file is too large for a service account key.")
        return

    try:
        tg_file = await context.bot.get_file(doc.file_id)
        raw = await tg_file.download_as_bytearray()
    except Exception:
        logger.exception("Failed to download credentials document")
        await update.message.reply_text("Could not download the file. Please try again.")
        return

    try:
        text = raw.decode("utf-8")
        data = json.loads(text)
    except (UnicodeDecodeError, json.JSONDecodeError):
        await update.message.reply_text("Could not parse JSON. Send the raw .json key file.")
        return

    if not isinstance(data, dict):
        await update.message.reply_text("JSON root must be an object (service account key).")
        return

    try:
        bot.save_service_account_json(uid, data)
    except RuntimeError as exc:
        await update.message.reply_text(str(exc))
        return

    await update.message.reply_text(
        "Saved your Google service account credentials.\n"
        "Now run /setsheet with the exact spreadsheet title from Google Sheets."
    )


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.message.text:
        return
    uid = _require_user(update)
    if uid is None:
        return
    try:
        sheets = bot.get_user_spreadsheet(uid)
    except RuntimeError as exc:
        await update.message.reply_text(str(exc))
        return
    if not sheets:
        await update.message.reply_text(_not_configured_message())
        return

    meal_text = update.message.text.strip()
    try:
        ingredients = bot.extract_ingredients(meal_text)
        date_str = sheets.append_log_rows(meal_text, ingredients)
        d = sheets.upsert_daily(date_str)
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
            "I couldn't process that meal right now. Please try again.\n"
            f"Error: {exc}"
        )


def _build_app() -> Application:
    app = Application.builder().token(bot.telegram_token).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("setup", setup_cmd))
    app.add_handler(CommandHandler("status", status_cmd))
    app.add_handler(CommandHandler("today", today))
    app.add_handler(CommandHandler("settarget", set_target))
    app.add_handler(CommandHandler("setsheet", set_sheet))
    json_docs = (
        filters.Document.FileExtension("json")
        | filters.Document.MimeType("application/json")
        | filters.Document.MimeType("text/json")
    )
    app.add_handler(MessageHandler(json_docs, handle_credentials_document))
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
