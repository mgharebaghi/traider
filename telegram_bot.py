"""
Persian Telegram interface (python-telegram-bot v21+, fully async).

* Only TELEGRAM_ADMIN_CHAT_ID can use commands/buttons.
* Sends real-time Persian notifications for every trading event.
* Commands: /start /help /status /mode /pause /resume /panic /history /resetdry
"""
from __future__ import annotations

import html
import logging
import math
from datetime import datetime
from typing import TYPE_CHECKING, Any, Optional
from zoneinfo import ZoneInfo

from telegram import (
    BotCommand,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    Update,
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from config import Settings
from risk_manager import DailyStats, PositionPlan
from state_store import StateStore

if TYPE_CHECKING:
    from ai_agent import AIAgent, TradeSignal
    from risk_manager import RiskManager
    from trader import Position, Trader

log = logging.getLogger("telegram")

BTN_STATUS = "📊 وضعیت"
BTN_MODE = "🔁 تغییر حالت"
BTN_MODEL = "🧠 مدل هوش مصنوعی"
BTN_PAUSE = "⏸ توقف"
BTN_RESUME = "▶️ ادامه"
BTN_PANIC = "🚨 بستن اضطراری"
BTN_HISTORY = "📜 تاریخچه"

CLOSE_REASONS = {
    "SL": "⛔ برخورد به حد ضرر (Stop Loss)",
    "BREAKEVEN": "🛡️ برخورد به استاپ نقطه ورود (ریسک‌فری)",
    "TRAILING": "📈 برخورد به حد ضرر متحرک (Trailing Stop)",
    "TP2": "🎯 رسیدن به حد سود دوم",
    "PANIC": "🚨 بستن اضطراری توسط شما",
    "EXTERNAL": "🔄 بسته‌شده خارج از ربات / توسط صرافی",
    "RESTART_SYNC": "🔄 بسته‌شده هنگام خاموش بودن ربات",
}


def esc(text: Any) -> str:
    return html.escape(str(text), quote=False)


def usd(value: float, signed: bool = False) -> str:
    if signed:
        return f"{'+' if value >= 0 else '−'}{abs(value):.2f}$"
    return f"{value:.2f}$"


def price_fmt(value: float) -> str:
    return f"{value:,.1f}"


def mode_label(mode: str) -> str:
    return "🧪 شبیه‌ساز (Dry Run)" if mode == "DRY" else "💰 واقعی (Live)"


def side_label(side: str) -> str:
    return "🟢 لانگ (LONG)" if side == "LONG" else "🔴 شورت (SHORT)"


class TelegramBot:
    def __init__(self, settings: Settings, store: StateStore) -> None:
        self.s = settings
        self.store = store
        self.admin_id = settings.telegram_admin_chat_id
        self.tz = self._load_tz(settings.display_timezone)
        self.trader: Optional["Trader"] = None
        self.risk: Optional["RiskManager"] = None
        self.ai: Optional["AIAgent"] = None

        builder = Application.builder().token(settings.telegram_token)
        if settings.telegram_proxy:
            builder = builder.proxy(settings.telegram_proxy).get_updates_proxy(settings.telegram_proxy)
        self.app: Application = builder.build()
        self._register_handlers()

    @staticmethod
    def _load_tz(name: str):
        try:
            return ZoneInfo(name)
        except Exception:  # noqa: BLE001
            return ZoneInfo("UTC")

    def attach(self, trader: "Trader", risk: "RiskManager", ai: "AIAgent") -> None:
        self.trader = trader
        self.risk = risk
        self.ai = ai

    # ============================================================ lifecycle
    async def start(self) -> None:
        await self.app.initialize()
        await self.app.start()
        await self.app.bot.set_my_commands(
            [
                BotCommand("status", "وضعیت حساب و پوزیشن"),
                BotCommand("mode", "تغییر حالت شبیه‌ساز / واقعی"),
                BotCommand("model", "انتخاب مدل هوش مصنوعی جمینای"),
                BotCommand("pause", "توقف موقت اسکن بازار"),
                BotCommand("resume", "ادامه اسکن بازار"),
                BotCommand("panic", "بستن فوری همه پوزیشن‌ها"),
                BotCommand("history", "آخرین معاملات"),
                BotCommand("resetdry", "ریست موجودی شبیه‌ساز"),
                BotCommand("help", "راهنما"),
            ]
        )
        assert self.app.updater is not None
        await self.app.updater.start_polling(drop_pending_updates=True)
        log.info("Telegram bot started")

    async def stop(self) -> None:
        try:
            if self.app.updater and self.app.updater.running:
                await self.app.updater.stop()
            if self.app.running:
                await self.app.stop()
            await self.app.shutdown()
        except Exception as exc:  # noqa: BLE001
            log.warning("Telegram shutdown error: %s", exc)

    async def send(self, text: str, reply_markup: Any = None) -> None:
        if len(text) > 4000:
            text = text[:3990] + "…"
        try:
            await self.app.bot.send_message(
                chat_id=self.admin_id,
                text=text,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
                reply_markup=reply_markup,
            )
        except Exception as exc:  # noqa: BLE001
            log.error("Telegram send failed: %s", exc)

    def _fmt_time(self, iso: str) -> str:
        if not iso:
            return "-"
        try:
            return datetime.fromisoformat(iso).astimezone(self.tz).strftime("%Y-%m-%d %H:%M")
        except ValueError:
            return iso

    # ============================================================= handlers
    def _register_handlers(self) -> None:
        admin = filters.Chat(chat_id=self.admin_id)
        add = self.app.add_handler
        add(CommandHandler(["start", "help"], self.cmd_start, filters=admin))
        add(CommandHandler("status", self.cmd_status, filters=admin))
        add(CommandHandler("mode", self.cmd_mode, filters=admin))
        add(CommandHandler("model", self.cmd_model, filters=admin))
        add(CommandHandler("pause", self.cmd_pause, filters=admin))
        add(CommandHandler("resume", self.cmd_resume, filters=admin))
        add(CommandHandler("panic", self.cmd_panic, filters=admin))
        add(CommandHandler("history", self.cmd_history, filters=admin))
        add(CommandHandler("resetdry", self.cmd_resetdry, filters=admin))
        add(MessageHandler(admin & filters.TEXT & ~filters.COMMAND, self.on_text))
        add(CallbackQueryHandler(self.on_callback))
        add(MessageHandler(~admin, self.on_unauthorized))

    def _keyboard(self) -> ReplyKeyboardMarkup:
        return ReplyKeyboardMarkup(
            [
                [KeyboardButton(BTN_STATUS), KeyboardButton(BTN_MODE)],
                [KeyboardButton(BTN_PAUSE), KeyboardButton(BTN_RESUME)],
                [KeyboardButton(BTN_MODEL), KeyboardButton(BTN_HISTORY)],
                [KeyboardButton(BTN_PANIC)],
            ],
            resize_keyboard=True,
        )

    async def on_unauthorized(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if update.effective_chat:
            log.warning("Unauthorized access attempt from chat %s", update.effective_chat.id)

    async def on_text(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        text = (update.message.text or "").strip() if update.message else ""
        routes = {
            BTN_STATUS: self.cmd_status,
            BTN_MODE: self.cmd_mode,
            BTN_MODEL: self.cmd_model,
            BTN_PAUSE: self.cmd_pause,
            BTN_RESUME: self.cmd_resume,
            BTN_PANIC: self.cmd_panic,
            BTN_HISTORY: self.cmd_history,
        }
        handler = routes.get(text)
        if handler:
            await handler(update, context)

    async def cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        text = (
            "🤖 <b>ربات اسکالپر فیوچرز بیت‌کوین</b>\n\n"
            "دستورات:\n"
            "📊 /status — موجودی، سود/ضرر امروز و پوزیشن باز\n"
            "🔁 /mode — تغییر بین شبیه‌ساز و پول واقعی\n"
            "🧠 /model — تغییر مدل هوش مصنوعی جمینای (3.8 / 3.5 / 2.5)\n"
            "⏸ /pause — توقف موقت اسکن بازار\n"
            "▶️ /resume — ادامه اسکن بازار\n"
            "🚨 /panic — بستن فوری همه پوزیشن‌ها و لغو سفارش‌ها\n"
            "📜 /history — ۱۰ معامله آخر\n"
            "♻️ /resetdry — ریست موجودی شبیه‌ساز به مقدار اولیه\n\n"
            "از دکمه‌های پایین صفحه هم می‌توانید استفاده کنید."
        )
        if update.effective_message:
            await update.effective_message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=self._keyboard())

    async def cmd_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        assert self.trader and self.risk and self.ai
        info = await self.trader.status_snapshot()
        daily = self.risk.daily
        halt = self.risk.halt_state()
        lines = ["📊 <b>وضعیت ربات</b>", ""]
        lines.append(f"⚙️ حالت: <b>{mode_label(info['mode'])}</b>")
        lines.append(f"🧠 مدل فعال: <code>{esc(self.ai.active_model)}</code>")
        if halt:
            reason = "هدف روزانه" if halt["reason"] == "target" else "قطع‌کن مدار (Circuit Breaker)"
            lines.append(f"🛑 معاملات متوقف تا <b>{self._fmt_time(halt['until'])}</b> ({reason})")
        elif info["paused"]:
            lines.append("⏸ اسکن بازار: <b>متوقف (دستی)</b>")
        else:
            lines.append("✅ اسکن بازار: <b>فعال</b>")
        bal = info.get("balance")
        min_bal = info.get("min_balance", self.s.min_balance_to_trade)
        if bal is not None:
            lines.append(f"💵 موجودی فیوچرز: <b>{usd(bal)}</b>")
            if bal < min_bal:
                lines.append(
                    f"🪫 <b>هشدار: موجودی کافی برای ترید ندارید!</b> (حداقل مورد نیاز: <b>{usd(min_bal)}</b>)"
                )
                spot = info.get("spot_balance")
                if spot and spot > 0:
                    lines.append(
                        f"💡 در کیف پول اسپات (Spot) شما <b>{usd(spot)}</b> وجود دارد؛ آن را به حساب Futures منتقل کنید."
                    )
        else:
            lines.append(f"⚠️ دریافت موجودی ناموفق: {esc(info.get('balance_error', ''))}")
        if info.get("price"):
            lines.append(f"₿ قیمت فعلی: <b>{price_fmt(info['price'])}</b>")
        lines.append("")
        if daily:
            lines.append(f"📅 <b>امروز ({daily.date} UTC)</b>")
            lines.append(f"• سود/ضرر خالص: <b>{usd(daily.realized_pnl, True)}</b>")
            lines.append(f"• هدف روزانه: {usd(self.risk.daily_target())} | سقف ضرر: {usd(self.risk.daily_loss_limit())}")
            lines.append(f"• معاملات: {daily.trades} (✅ {daily.wins} / ❌ {daily.losses}) | ضرر متوالی: {daily.consecutive_losses}")
        lines.append(f"🧠 درخواست‌های جمینای امروز: {self.ai.calls_today()}/{self.s.ai_max_calls_per_day}")
        lines.append("")
        pos = info.get("position")
        if pos is None:
            lines.append("📭 پوزیشن بازی وجود ندارد.")
        else:
            lines.append(self._position_block(pos, info.get("unrealized")))
        await self._reply(update, "\n".join(lines))

    def _position_block(self, pos: "Position", unrealized: Optional[float]) -> str:
        stage_text = {0: "مرحله ۰: حد ضرر اولیه", 1: "مرحله ۱: ریسک‌فری + تریلینگ", 2: "مرحله ۲: تریلینگ فشرده (بعد از TP2)"}
        rows = [f"📌 <b>پوزیشن {side_label(pos.side)}</b> — {mode_label(pos.mode)}"]
        if pos.status == "PENDING":
            rows.append(f"⏳ سفارش لیمیت در انتظار پر شدن @ <b>{price_fmt(pos.entry_price)}</b>")
        else:
            rows.append(f"• قیمت ورود: <b>{price_fmt(pos.fill_price)}</b>")
            rows.append(f"• حجم باقی‌مانده: {pos.remaining_contracts:g} قرارداد | لوریج: {pos.leverage}x")
            rows.append(f"• حد ضرر فعلی: <b>{price_fmt(pos.stop_loss)}</b>{' (نرم‌افزاری)' if pos.soft_stop else ''}")
            rows.append(f"• TP1: {price_fmt(pos.tp1)} | TP2: {price_fmt(pos.tp2)}")
            rows.append(f"• {stage_text.get(pos.stage, '')}")
            if unrealized is not None:
                rows.append(f"• سود/ضرر شناور (خالص): <b>{usd(unrealized, True)}</b>")
        return "\n".join(rows)

    async def cmd_mode(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        assert self.trader
        current = self.trader.mode
        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("🧪 شبیه‌ساز (Dry Run)", callback_data="mode:DRY"),
                    InlineKeyboardButton("💰 واقعی (Live)", callback_data="mode:LIVE"),
                ]
            ]
        )
        await self._reply(update, f"⚙️ حالت فعلی: <b>{mode_label(current)}</b>\nحالت جدید را انتخاب کنید:", keyboard)

    async def cmd_model(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        assert self.ai
        if context.args:
            chosen = self.ai.set_model(context.args[0])
            await self._reply(update, f"✅ مدل هوش مصنوعی به <code>{esc(chosen)}</code> تغییر یافت.")
            return
        current = self.ai.active_model
        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("⚡ Gemini 3.8 Flash (پیش‌فرض و سریع)", callback_data="model:gemini-3.8-flash"),
                ],
                [
                    InlineKeyboardButton("🧠 Gemini 4 Argon (پرچم‌دار استدلالی)", callback_data="model:gemini-4-argon"),
                ],
                [
                    InlineKeyboardButton("🚀 Gemini 3.6 Flash", callback_data="model:gemini-3.6-flash"),
                    InlineKeyboardButton("🪶 Gemini 3.5 Flash-Lite", callback_data="model:gemini-3.5-flash-lite"),
                ],
                [
                    InlineKeyboardButton("🔹 Gemini 2.5 Flash", callback_data="model:gemini-2.5-flash"),
                ],
            ]
        )
        await self._reply(
            update,
            f"🧠 مدل فعلی: <code>{esc(current)}</code>\n"
            "مدل مورد نظر را انتخاب کنید (یا بنویسید <code>/model نام_مدل</code>):\n"
            "<i>نکته: اگر کلید شما هنوز به مدل انتخابی دسترسی نداشته باشد، ربات خودکار به مدل در دسترس بعدی سوییچ می‌کند.</i>",
            keyboard,
        )

    async def cmd_pause(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        assert self.trader
        self.trader.set_paused(True)
        await self._reply(update, "⏸ اسکن بازار متوقف شد.\nپوزیشن باز فعلی (در صورت وجود) همچنان مدیریت می‌شود.")

    async def cmd_resume(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        assert self.trader and self.risk
        self.trader.set_paused(False)
        text = "▶️ اسکن بازار از سر گرفته شد."
        halt = self.risk.halt_state()
        if halt:
            text += (
                f"\n\n⚠️ توجه: به دلیل قوانین مدیریت ریسک، معاملات تا <b>{self._fmt_time(halt['until'])}</b> "
                "قفل است و با /resume باز نمی‌شود (جلوگیری از معاملات انتقامی)."
            )
        await self._reply(update, text)

    async def cmd_panic(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("✅ بله، همه را ببند", callback_data="panic:confirm"),
                    InlineKeyboardButton("❌ انصراف", callback_data="panic:cancel"),
                ]
            ]
        )
        await self._reply(update, "🚨 <b>بستن اضطراری</b>\nآیا مطمئن هستید؟ تمام پوزیشن‌ها با قیمت بازار بسته و سفارش‌ها لغو می‌شوند.", keyboard)

    async def cmd_history(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        assert self.trader
        trades = self.store.recent_trades(10)
        summary = self.store.summary(self.trader.mode)
        lines = [f"📜 <b>آخرین معاملات</b>", ""]
        if not trades:
            lines.append("هنوز معامله‌ای ثبت نشده است.")
        for t in trades:
            icon = "✅" if (t["net_pnl"] or 0) > 0 else "❌"
            side = "L" if t["side"] == "LONG" else "S"
            mode = "🧪" if t["mode"] == "DRY" else "💰"
            lines.append(
                f"{icon}{mode} {side} | {usd(t['net_pnl'] or 0, True)} | "
                f"{price_fmt(t['entry_price'] or 0)}→{price_fmt(t['exit_price'] or 0)} | {self._fmt_time(t['closed_at'])}"
            )
        lines.append("")
        lines.append(
            f"📈 مجموع ({mode_label(self.trader.mode)}): {summary['trades']} معامله | "
            f"نرخ برد {summary['win_rate']:.0f}% | خالص {usd(summary['net_pnl'], True)}"
        )
        await self._reply(update, "\n".join(lines))

    async def cmd_resetdry(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        assert self.trader and self.risk
        if self.trader.mode != "DRY":
            await self._reply(update, "این دستور فقط در حالت شبیه‌ساز قابل استفاده است.")
            return
        if self.trader.has_position():
            await self._reply(update, "ابتدا پوزیشن باز شبیه‌ساز باید بسته شود.")
            return
        amount: Optional[float] = None
        if context.args:
            try:
                amount = float(context.args[0])
                if not math.isfinite(amount) or amount <= 0:
                    raise ValueError("Invalid amount")
            except ValueError:
                await self._reply(update, "مقدار نامعتبر است. مثال: <code>/resetdry 10</code>")
                return
        value = self.trader.reset_virtual_balance(amount)
        self.store.delete(self.risk.DAILY_KEY)
        self.risk.roll_day(value)
        self.risk.clear_halt()
        await self._reply(update, f"♻️ موجودی شبیه‌ساز به <b>{usd(value)}</b> ریست شد و آمار روزانه پاک شد.")

    async def on_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        if query is None:
            return
        chat_id = query.message.chat.id if query.message else None
        if chat_id != self.admin_id:
            await query.answer("دسترسی غیرمجاز", show_alert=True)
            return
        await query.answer()
        data = query.data or ""
        assert self.trader and self.ai
        if data == "mode:DRY":
            await self._apply_mode(query, "DRY")
        elif data == "mode:LIVE":
            keyboard = InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton("⚠️ تأیید: معامله با پول واقعی", callback_data="mode:LIVE:confirm"),
                        InlineKeyboardButton("❌ انصراف", callback_data="mode:cancel"),
                    ]
                ]
            )
            await query.edit_message_text(
                "⚠️ <b>هشدار</b>\nدر حالت واقعی، ربات با موجودی واقعی حساب فیوچرز شما معامله می‌کند. ادامه می‌دهید؟",
                parse_mode=ParseMode.HTML,
                reply_markup=keyboard,
            )
        elif data == "mode:LIVE:confirm":
            await self._apply_mode(query, "LIVE")
        elif data == "mode:cancel":
            await query.edit_message_text("تغییر حالت لغو شد.")
        elif data.startswith("model:"):
            chosen = self.ai.set_model(data.split(":", 1)[1])
            await query.edit_message_text(
                f"✅ مدل هوش مصنوعی جمینای به <code>{esc(chosen)}</code> تغییر کرد.",
                parse_mode=ParseMode.HTML,
            )
        elif data == "panic:confirm":
            await query.edit_message_text("⏳ در حال اجرای بستن اضطراری...")
            try:
                messages = await self.trader.panic_close()
                await self.send("🚨 <b>بستن اضطراری انجام شد</b>\n\n" + "\n".join(messages) + "\n\nبرای شروع مجدد: /resume")
            except Exception as exc:  # noqa: BLE001
                log.exception("Panic failed")
                await self.send(f"❌ خطا در بستن اضطراری: {esc(exc)}\nفوراً حساب صرافی را دستی بررسی کنید!")
        elif data == "panic:cancel":
            await query.edit_message_text("بستن اضطراری لغو شد.")

    async def _apply_mode(self, query: Any, mode: str) -> None:
        assert self.trader
        ok, message, balance = await self.trader.switch_mode(mode)
        if ok:
            text = f"✅ حالت به <b>{mode_label(mode)}</b> تغییر کرد.\n💵 موجودی فیوچرز: <b>{usd(balance)}</b>"
            if balance < self.s.min_balance_to_trade:
                text += (
                    f"\n\n🪫 <b>توجه:</b> موجودی شما کمتر از حداقل مجاز برای ترید (<b>{usd(self.s.min_balance_to_trade)}</b>) است! "
                    "ربات تا زمان شارژ حساب فیوچرز وارد معامله نمی‌شود."
                )
            if message:
                text += f"\n{esc(message)}"
            await query.edit_message_text(text, parse_mode=ParseMode.HTML)
        else:
            await query.edit_message_text(f"❌ {esc(message)}", parse_mode=ParseMode.HTML)

    async def _reply(self, update: Update, text: str, markup: Any = None) -> None:
        if update.effective_message:
            await update.effective_message.reply_text(
                text, parse_mode=ParseMode.HTML, reply_markup=markup, disable_web_page_preview=True
            )

    # ======================================================= notifications
    async def notify_startup(self, mode: str, balance: Optional[float], exchange: str, restored: Optional[str]) -> None:
        active_model = self.ai.active_model if self.ai else self.s.gemini_model
        text = (
            "🤖 <b>ربات اسکالپر روشن شد</b>\n\n"
            f"⚙️ حالت: <b>{mode_label(mode)}</b>\n"
            f"🏦 صرافی: <b>{esc(exchange)}</b> | نماد: <code>{esc(self.s.symbol)}</code>\n"
            f"💵 موجودی فیوچرز: <b>{usd(balance) if balance is not None else 'نامشخص'}</b>\n"
            f"🧠 مدل: <code>{esc(active_model)}</code>"
        )
        if restored:
            text += f"\n\n♻️ {esc(restored)}"
        await self.send(text, self._keyboard())

    async def notify_insufficient_balance(
        self, balance: float, min_required: float, mode: str, spot_balance: Optional[float] = None
    ) -> None:
        lines = [
            "🪫 <b>موجودی کافی برای ترید ندارید!</b>",
            "",
            f"⚙️ حالت فعلی: <b>{mode_label(mode)}</b>",
            f"💵 موجودی فعلی کیف پول فیوچرز: <b>{usd(balance)}</b>",
            f"⚠️ حداقل موجودی لازم برای شروع ترید: <b>{usd(min_required)}</b>",
        ]
        if mode == "LIVE":
            if spot_balance and spot_balance > 0:
                lines.append(
                    f"\n💡 <b>راهنمایی مهم:</b> در کیف پول <b>Spot (اسپات)</b> شما <b>{usd(spot_balance)}</b> تتر وجود دارد، "
                    "اما کیف پول <b>Futures (فیوچرز)</b> خالی است! لطفاً در صرافی گزینه <b>Transfer (انتقال)</b> را بزنید "
                    "و تتر را از Spot به Futures منتقل کنید."
                )
            else:
                lines.append(
                    "\n💡 لطفاً کیف پول <b>Futures (USDT-M)</b> خود را در صرافی شارژ کنید. "
                    "به محض شارژ شدن، ربات خودکار متوجه شده و اسکن بازار را ادامه می‌دهد."
                )
        else:
            lines.append(
                "\n💡 در حالت شبیه‌ساز هستید؛ برای شارژ مجدد موجودی مجازی دستور <code>/resetdry 10</code> را بفرستید."
            )
        await self.send("\n".join(lines))

    async def notify_signal_opened(
        self, pos: "Position", plan: PositionPlan, signal: "TradeSignal", warnings: list[str]
    ) -> None:
        lines = [
            "🚀 <b>سیگنال جدید و ثبت سفارش ورود</b>",
            "",
            f"📍 نوع: <b>{side_label(pos.side)}</b>",
            f"⚙️ حالت: <b>{mode_label(pos.mode)}</b>",
            f"🎯 اطمینان جمینای: <b>{signal.confidence}%</b>",
            "",
            f"💲 قیمت ورود (لیمیت): <b>{price_fmt(pos.entry_price)}</b>",
            f"📦 حجم: <b>{pos.contracts:g}</b> قرارداد (≈ {pos.base_qty:.4f} BTC | ارزش ≈ {usd(plan.notional)})",
            f"⚡ لوریج: <b>{pos.leverage}x</b> (Isolated) | مارجین ≈ {usd(plan.margin)}",
            f"⛔ حد ضرر (SL): <b>{price_fmt(pos.stop_loss)}</b> ({plan.sl_pct * 100:.2f}%)",
            f"✅ حد سود ۱ (TP1): <b>{price_fmt(pos.tp1)}</b> (R/R 1:{plan.rr_tp1:.1f})",
            f"🏆 حد سود ۲ (TP2): <b>{price_fmt(pos.tp2)}</b> (R/R 1:{plan.rr_tp2:.1f})",
            f"💀 قیمت تقریبی لیکوئید: {price_fmt(plan.liquidation_price)}",
            "",
            f"🔻 ریسک دلاری (با کارمزد): <b>{usd(plan.expected_loss)}</b> از سقف {usd(plan.risk_budget)}",
        ]
        if plan.risk_reduced:
            lines.append("ℹ️ به دلیل محدودیت مارجین/لوریج، حجم کمتر از سقف ریسک تنظیم شد.")
        lines += ["", "🔎 <b>شرایط تکنیکال:</b>"]
        lines += [f"• {esc(r)}" for r in signal.setup_reasons]
        lines += ["", "🧠 <b>تحلیل جمینای:</b>", esc(signal.reasoning_fa)]
        if signal.adjustments:
            lines += ["", "🛠 <b>اصلاحات خودکار:</b>"] + [f"• {esc(a)}" for a in signal.adjustments]
        if warnings:
            lines += ["", "⚠️ <b>هشدارها:</b>"] + [f"• {esc(w)}" for w in warnings]
        lines += ["", f"⏳ مهلت پر شدن سفارش: {self.s.entry_timeout_min:g} دقیقه"]
        await self.send("\n".join(lines))

    async def notify_entry_filled(self, pos: "Position") -> None:
        stop_mode = "نرم‌افزاری (Soft Stop)" if pos.soft_stop else "ثبت‌شده روی صرافی"
        await self.send(
            "✅ <b>سفارش ورود پر شد — پوزیشن باز است</b>\n\n"
            f"📍 {side_label(pos.side)} | {mode_label(pos.mode)}\n"
            f"💲 قیمت ورود: <b>{price_fmt(pos.fill_price)}</b>\n"
            f"📦 حجم: {pos.contracts:g} قرارداد | ⚡ {pos.leverage}x\n"
            f"⛔ حد ضرر: {price_fmt(pos.stop_loss)} ({stop_mode})\n"
            f"✅ TP1: {price_fmt(pos.tp1)} | 🏆 TP2: {price_fmt(pos.tp2)}"
        )

    async def notify_entry_cancelled(self, pos: "Position", reason: str) -> None:
        await self.send(
            "⌛ <b>سفارش ورود لغو شد</b>\n\n"
            f"📍 {side_label(pos.side)} @ {price_fmt(pos.entry_price)}\n"
            f"📝 دلیل: {esc(reason)}\n"
            "ربات به اسکن بازار ادامه می‌دهد."
        )

    async def notify_trade_rejected(self, signal: "TradeSignal", reason: str) -> None:
        await self.send(
            "🚫 <b>سیگنال تأیید شد اما معامله انجام نشد</b>\n\n"
            f"📍 {side_label(signal.action)} | اطمینان {signal.confidence}%\n"
            f"📝 دلیل (مدیریت ریسک): {esc(reason)}"
        )

    async def notify_ai_wait(self, signal: "TradeSignal", direction: str) -> None:
        if not self.s.notify_ai_wait:
            return
        verdict = "صبر (WAIT)" if signal.action == "WAIT" else f"{signal.action} با اطمینان پایین"
        lines = [
            "🧠 <b>بررسی ستاپ توسط جمینای</b>",
            f"جهت پیشنهادی الگوریتم: {side_label(direction)}",
            f"نتیجه: <b>{esc(verdict)}</b> (اطمینان {signal.confidence}%)",
            "",
            esc(signal.reasoning_fa),
        ]
        if signal.adjustments:
            lines += [""] + [f"• {esc(a)}" for a in signal.adjustments]
        await self.send("\n".join(lines))

    async def notify_risk_free(self, pos: "Position", part_contracts: float, part_price: float, part_net: float) -> None:
        if part_contracts > 0:
            head = (
                "🛡️ <b>ریسک‌فری شد و سود سیو شد!</b>\n\n"
                f"✂️ {self.s.tp1_close_fraction * 100:.0f}٪ پوزیشن "
                f"({part_contracts:g} قرارداد) در قیمت <b>{price_fmt(part_price)}</b> بسته شد.\n"
                f"💰 سود خالص این بخش: <b>{usd(part_net, True)}</b>\n"
            )
        else:
            head = (
                "🛡️ <b>ریسک‌فری شد!</b>\n\n"
                "ℹ️ حجم پوزیشن برای بستن ۵۰٪ از حداقل صرافی کمتر بود؛ کل پوزیشن با استاپ سر به سر ادامه می‌دهد.\n"
            )
        await self.send(
            head
            + f"🔒 حد ضرر باقی‌مانده به نقطه ورود منتقل شد: <b>{price_fmt(pos.stop_loss)}</b>\n"
            f"📈 حد ضرر متحرک (Trailing Stop) برای بقیه پوزیشن فعال شد.\n"
            f"🏆 هدف بعدی: {price_fmt(pos.tp2)}"
        )

    async def notify_tp2_reached(self, pos: "Position") -> None:
        await self.send(
            "🏆 <b>قیمت به حد سود دوم رسید!</b>\n\n"
            "پوزیشن بسته نمی‌شود؛ حد ضرر متحرک فشرده‌تر شد تا از ادامه حرکت سود بیشتری گرفته شود.\n"
            f"🔒 حد ضرر فعلی: <b>{price_fmt(pos.stop_loss)}</b>"
        )

    async def notify_position_closed(
        self,
        pos: "Position",
        net: float,
        balance_after: float,
        daily: Optional[DailyStats],
        target: float,
        loss_limit: float,
    ) -> None:
        win = net > 0
        head = "🎯 <b>پوزیشن با سود بسته شد</b>" if win else "📉 <b>پوزیشن بسته شد</b>"
        pct_account = (net / pos.balance_before * 100) if pos.balance_before else 0.0
        roe = (net / pos.margin * 100) if pos.margin else 0.0
        lines = [
            head,
            "",
            f"📍 {side_label(pos.side)} | {mode_label(pos.mode)}",
            f"📝 علت: {CLOSE_REASONS.get(pos.close_reason, esc(pos.close_reason))}",
            f"💲 ورود: {price_fmt(pos.fill_price)} → خروج (میانگین): {price_fmt(pos.exit_price_avg)}",
            "",
            f"{'💰' if win else '🔻'} سود/ضرر خالص: <b>{usd(net, True)}</b> "
            f"({pct_account:+.2f}% حساب | {roe:+.1f}% ROE)",
            f"🧾 کارمزد پرداختی: {usd(pos.fees_paid)}",
            f"💵 موجودی جدید: <b>{usd(balance_after)}</b>",
        ]
        if daily:
            progress = (daily.realized_pnl / target * 100) if target > 0 else 0.0
            lines += [
                "",
                "📅 <b>وضعیت امروز</b>",
                f"• سود/ضرر روز: <b>{usd(daily.realized_pnl, True)}</b> از هدف {usd(target)} ({max(progress, 0):.0f}%)",
                f"• معاملات: {daily.trades} (✅ {daily.wins} / ❌ {daily.losses}) | ضرر متوالی: {daily.consecutive_losses}",
                f"• سقف ضرر روزانه: −{usd(loss_limit)}",
            ]
        await self.send("\n".join(lines))

    async def notify_daily_halt(self, event: str, daily: Optional[DailyStats], until: str) -> None:
        pnl = usd(daily.realized_pnl, True) if daily else "-"
        if event == "TARGET_HIT":
            text = (
                "🎉 <b>تبریک! هدف سود روزانه محقق شد</b>\n\n"
                f"💰 سود خالص امروز: <b>{pnl}</b>\n"
                f"😴 ربات تا شروع روز جدید (<b>{self._fmt_time(until)}</b>) معامله جدیدی باز نمی‌کند.\n"
                "سود امروز را حفظ کنید؛ بازار فردا هم هست. 🙌"
            )
        elif event == "BREAKER_CONSECUTIVE":
            text = (
                "🛑 <b>توقف هوشمند ۲۴ ساعته (Circuit Breaker)</b>\n\n"
                f"❌ {self.s.max_consecutive_losses} ضرر متوالی ثبت شد.\n"
                f"📉 سود/ضرر امروز: <b>{pnl}</b>\n"
                f"⏳ معاملات تا <b>{self._fmt_time(until)}</b> قفل است تا از معاملات احساسی/انتقامی جلوگیری شود."
            )
        else:
            text = (
                "🛑 <b>توقف هوشمند ۲۴ ساعته (Circuit Breaker)</b>\n\n"
                f"📉 سقف ضرر روزانه رد شد: <b>{pnl}</b>\n"
                f"⏳ معاملات تا <b>{self._fmt_time(until)}</b> قفل است."
            )
        await self.send(text)

    async def notify_new_day(self, previous: DailyStats, balance: float) -> None:
        await self.send(
            "🌅 <b>روز معاملاتی جدید (UTC)</b>\n\n"
            f"📅 خلاصه دیروز ({previous.date}): {usd(previous.realized_pnl, True)} | "
            f"{previous.trades} معامله (✅ {previous.wins} / ❌ {previous.losses})\n"
            f"💵 موجودی شروع امروز: <b>{usd(balance)}</b>"
        )

    async def notify_warning(self, text: str) -> None:
        await self.send(f"⚠️ <b>هشدار</b>\n{esc(text)}")
