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
    from indicators import MarketAnalyzer, MarketSnapshot, Setup
    from risk_manager import RiskManager
    from trader import Position, Trader

log = logging.getLogger("telegram")

BTN_STATUS = "📊 وضعیت"
BTN_SCAN = "🔍 بررسی فوری بازار"
BTN_MODE = "🔁 تغییر حالت"
BTN_MODEL = "🧠 مدل هوش مصنوعی"
BTN_PAUSE = "⏸ توقف"
BTN_RESUME = "▶️ ادامه"
BTN_PANIC = "🚨 بستن اضطراری"
BTN_HISTORY = "📜 تاریخچه"
BTN_GUIDE = "📘 راهنمای ترید مجازی"

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
    return "🧪 شبیه‌ساز (پول مجازی)" if mode == "DRY" else "💰 واقعی (پول واقعی صرافی)"


def side_label(side: str) -> str:
    return "🟢 لانگ / خرید (LONG)" if side == "LONG" else "🔴 شورت / فروش (SHORT)"


class TelegramBot:
    def __init__(self, settings: Settings, store: StateStore) -> None:
        self.s = settings
        self.store = store
        self.admin_id = settings.telegram_admin_chat_id
        self.tz = self._load_tz(settings.display_timezone)
        self.trader: Optional["Trader"] = None
        self.risk: Optional["RiskManager"] = None
        self.ai: Optional["AIAgent"] = None
        self.analyzer: Optional["MarketAnalyzer"] = None

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

    def attach(
        self,
        trader: "Trader",
        risk: "RiskManager",
        ai: "AIAgent",
        analyzer: Optional["MarketAnalyzer"] = None,
    ) -> None:
        self.trader = trader
        self.risk = risk
        self.ai = ai
        self.analyzer = analyzer

    # ============================================================ lifecycle
    async def start(self) -> None:
        await self.app.initialize()
        await self.app.start()
        await self.app.bot.set_my_commands(
            [
                BotCommand("status", "وضعیت کامل کیف پول‌ها و پوزیشن"),
                BotCommand("scan", "بررسی فوری بازار و استعلام جمینای"),
                BotCommand("mode", "تغییر راحت بین پول مجازی و پول واقعی"),
                BotCommand("guide", "راهنمای کامل ترید مجازی در LBank"),
                BotCommand("model", "انتخاب مدل هوش مصنوعی جمینای"),
                BotCommand("pause", "توقف موقت اسکن بازار"),
                BotCommand("resume", "ادامه اسکن بازار"),
                BotCommand("panic", "بستن فوری همه پوزیشن‌ها"),
                BotCommand("history", "کارنامه و آخرین معاملات"),
                BotCommand("resetdry", "تغییر یا ریست موجودی پول مجازی"),
                BotCommand("setkeys", "ثبت کلید API صرافی از داخل تلگرام"),
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
        add(CommandHandler("scan", self.cmd_scan, filters=admin))
        add(CommandHandler("guide", self.cmd_guide, filters=admin))
        add(CommandHandler("mode", self.cmd_mode, filters=admin))
        add(CommandHandler("model", self.cmd_model, filters=admin))
        add(CommandHandler("pause", self.cmd_pause, filters=admin))
        add(CommandHandler("resume", self.cmd_resume, filters=admin))
        add(CommandHandler("panic", self.cmd_panic, filters=admin))
        add(CommandHandler("history", self.cmd_history, filters=admin))
        add(CommandHandler("resetdry", self.cmd_resetdry, filters=admin))
        add(CommandHandler("setkeys", self.cmd_setkeys, filters=admin))
        add(MessageHandler(admin & filters.TEXT & ~filters.COMMAND, self.on_text))
        add(CallbackQueryHandler(self.on_callback))
        add(MessageHandler(~admin, self.on_unauthorized))

    def _keyboard(self) -> ReplyKeyboardMarkup:
        return ReplyKeyboardMarkup(
            [
                [KeyboardButton(BTN_STATUS), KeyboardButton(BTN_SCAN)],
                [KeyboardButton(BTN_MODE), KeyboardButton(BTN_MODEL)],
                [KeyboardButton(BTN_PAUSE), KeyboardButton(BTN_RESUME)],
                [KeyboardButton(BTN_HISTORY), KeyboardButton(BTN_GUIDE)],
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
            BTN_SCAN: self.cmd_scan,
            BTN_MODE: self.cmd_mode,
            BTN_MODEL: self.cmd_model,
            BTN_PAUSE: self.cmd_pause,
            BTN_RESUME: self.cmd_resume,
            BTN_PANIC: self.cmd_panic,
            BTN_HISTORY: self.cmd_history,
            BTN_GUIDE: self.cmd_guide,
        }
        handler = routes.get(text)
        if handler:
            await handler(update, context)

    async def cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        text = (
            "🤖 <b>ربات هوشمند اسکالپر فیوچرز بیت‌کوین (LBank + Gemini)</b>\n\n"
            "از دکمه‌های پایین صفحه می‌توانید بدون نیاز به هیچ دستوری همه چیز را کنترل کنید:\n\n"
            "📊 <b>وضعیت</b> (/status) — نمایش هم‌زمان <b>پول مجازی</b> و <b>پول واقعی در LBank</b> + وضعیت ترید باز\n"
            "🔍 <b>بررسی فوری بازار</b> (/scan) — مشاهده تحلیل لحظه‌ای چارت LBank و استعلام فوری از جمینای\n"
            "🔁 <b>تغییر حالت</b> (/mode) — سوئیچ راحت بین <b>پول مجازی (شبیه‌ساز)</b> و <b>پول واقعی صرافی</b> بدون دست زدن به سرور\n"
            "📘 <b>راهنمای ترید مجازی</b> (/guide) — توضیح کامل نحوه دیدن تریدهای مجازی و تغییر مبلغ شبیه‌ساز\n"
            "🧠 <b>مدل هوش مصنوعی</b> (/model) — تغییر مدل جمینای (3.8 / 4 / 3.6 / 2.5)\n"
            "📜 <b>تاریخچه</b> (/history) — کارنامه کامل تریدهای مجازی و واقعی با سود/ضرر دقیق\n"
            "⏸ <b>توقف</b> / ▶️ <b>ادامه</b> — توقف یا شروع مجدد اسکن خودکار بازار\n"
            "🚨 <b>بستن اضطراری</b> (/panic) — بستن آنی پوزیشن باز در هر شرایطی"
        )
        if update.effective_message:
            await update.effective_message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=self._keyboard())

    def _wallet_block(self, info: dict[str, Any]) -> list[str]:
        """Builds a crystal-clear comparison of Virtual vs Real LBank wallets."""
        mode = info.get("mode", "DRY")
        vbal = info.get("virtual_balance", self.s.starting_balance)
        real_fut = info.get("real_futures_balance")
        real_spot = info.get("spot_balance")
        min_bal = info.get("min_balance", self.s.min_balance_to_trade)

        active_tag_dry = " 👈 <b>(فعال در حال حاضر)</b>" if mode == "DRY" else ""
        active_tag_live = " 👈 <b>(فعال در حال حاضر)</b>" if mode == "LIVE" else ""

        lines = [
            "💳 <b>وضعیت کیف پول‌ها (مجازی و واقعی در LBank):</b>",
            f"• 🧪 <b>پول مجازی (شبیه‌ساز ربات):</b> <b>{usd(vbal)}</b>{active_tag_dry}",
        ]
        if real_fut is not None:
            lines.append(f"• 🏦 <b>پول واقعی در LBank (حساب Futures):</b> <b>{usd(real_fut)}</b>{active_tag_live}")
            if real_spot is not None and real_spot != real_fut:
                lines.append(f"• 🪙 <b>پول واقعی در LBank (حساب Spot):</b> <b>{usd(real_spot)}</b>")
            if real_fut < min_bal:
                lines.append(
                    f"  └ ℹ️ <i>موجودی واقعی شما در صرافی LBank الان <b>{usd(real_fut)}</b> است (برای ترید واقعی حداقل {usd(min_bal)} لازم است).</i>"
                )
        else:
            err = info.get("real_balance_error")
            if err:
                lines.append(f"• 🏦 <b>پول واقعی در LBank:</b> <i>قابل دریافت نبود ({esc(err)[:80]})</i>")
            else:
                lines.append("• 🏦 <b>پول واقعی در LBank:</b> <i>کلید API تنظیم نشده است</i>")
        return lines

    async def cmd_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        assert self.trader and self.risk and self.ai
        info = await self.trader.status_snapshot()
        daily = self.risk.daily
        halt = self.risk.halt_state()
        lines = ["📊 <b>وضعیت کامل ربات و کیف پول‌ها</b>", ""]
        lines.append(f"⚙️ حالت فعلی ترید: <b>{mode_label(info['mode'])}</b>")
        lines.append(f"🧠 مدل هوش مصنوعی: <code>{esc(self.ai.active_model)}</code>")
        if halt:
            reason = "رسیدن به هدف سود روزانه 🎯" if halt["reason"] == "target" else "قفل محافظ سرمایه (Circuit Breaker) 🛑"
            lines.append(f"🛑 وضعیت اسکن: متوقف تا <b>{self._fmt_time(halt['until'])}</b> ({reason})")
        elif info["paused"]:
            lines.append("⏸ وضعیت اسکن بازار: <b>متوقف (دستی)</b> — برای شروع روی «▶️ ادامه» بزنید")
        else:
            lines.append(f"✅ وضعیت اسکن بازار: <b>فعال (بررسی خودکار هر {self.s.scan_interval_sec} ثانیه)</b>")

        if info.get("price"):
            lines.append(f"₿ قیمت زنده بیت‌کوین در LBank: <b>{price_fmt(info['price'])} USDT</b>")

        lines.append("")
        lines.extend(self._wallet_block(info))

        bal = info.get("balance")
        min_bal = info.get("min_balance", self.s.min_balance_to_trade)
        if bal is not None and bal < min_bal:
            lines.append(
                f"\n🪫 <b>هشدار مهم:</b> موجودی حالت فعال شما (<b>{usd(bal)}</b>) کمتر از حداقل مجاز ترید (<b>{usd(min_bal)}</b>) است!"
            )

        lines.append("")
        if daily:
            lines.append(f"📅 <b>عملکرد امروز ({daily.date} UTC) — در حالت {mode_label(info['mode'])}:</b>")
            lines.append(f"• سود/ضرر خالص امروز: <b>{usd(daily.realized_pnl, True)}</b>")
            lines.append(f"• هدف سود روزانه: {usd(self.risk.daily_target())} | سقف مجاز ضرر: {usd(self.risk.daily_loss_limit())}")
            lines.append(f"• تعداد معاملات امروز: {daily.trades} (✅ برد: {daily.wins} | ❌ باخت: {daily.losses} | ضرر متوالی: {daily.consecutive_losses})")
        lines.append(f"🧠 استعلام‌های جمینای امروز: {self.ai.calls_today()} از {self.s.ai_max_calls_per_day}")
        lines.append("")
        pos = info.get("position")
        if pos is None:
            lines.append("📭 <b>پوزیشن فعلی:</b> هیچ ترید بازی وجود ندارد (ربات در حال رصد چارت برای شکار موقعیت است).")
        else:
            lines.append(self._position_block(pos, info.get("unrealized"), info.get("price")))

        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("🔍 بررسی فوری چارت الان", callback_data="scan:now"),
                    InlineKeyboardButton("🔁 تغییر حالت (مجازی/واقعی)", callback_data="mode:menu"),
                ]
            ]
        )
        await self._reply(update, "\n".join(lines), keyboard)

    def _position_block(self, pos: "Position", unrealized: Optional[float], live_price: Optional[float] = None) -> str:
        stage_text = {
            0: "مرحله ۰: منتظر رسیدن به حد سود اول (TP1)",
            1: "مرحله ۱: ۵۰٪ سود سیو شده + استاپ در نقطه ورود (ریسک‌فری) + تریلینگ فعال",
            2: "مرحله ۲: عبور از TP2 + حد ضرر متحرک فشرده برای شکار سود حداکثری",
        }
        rows = [f"📌 <b>ترید فعال: {side_label(pos.side)}</b> — {mode_label(pos.mode)}"]
        if pos.status == "PENDING":
            rows.append(f"⏳ <b>وضعیت:</b> سفارش ورود ثبت شده و منتظر رسیدن قیمت به <b>{price_fmt(pos.entry_price)}</b> است")
            if live_price:
                rows.append(f"• قیمت فعلی LBank: <b>{price_fmt(live_price)}</b>")
            rows.append(f"• حد ضرر (SL): {price_fmt(pos.stop_loss)} | TP1: {price_fmt(pos.tp1)} | TP2: {price_fmt(pos.tp2)}")
        else:
            rows.append("🟢 <b>وضعیت: پوزیشن باز و در حال معامله است!</b>")
            rows.append(f"• قیمت ورود: <b>{price_fmt(pos.fill_price)}</b>" + (f" | قیمت الان: <b>{price_fmt(live_price)}</b>" if live_price else ""))
            rows.append(f"• حجم فعال: {pos.remaining_contracts:g} قرارداد | اهرم: <b>{pos.leverage}x</b> (مارجین: {usd(pos.margin)})")
            rows.append(f"• ⛔ حد ضرر فعلی (SL): <b>{price_fmt(pos.stop_loss)}</b>")
            rows.append(f"• ✅ حد سود ۱ (TP1): <b>{price_fmt(pos.tp1)}</b> | 🏆 حد سود ۲ (TP2): <b>{price_fmt(pos.tp2)}</b>")
            rows.append(f"• 📍 وضعیت مدیریت: {stage_text.get(pos.stage, '')}")
            if unrealized is not None:
                roe = (unrealized / pos.margin * 100) if pos.margin else 0.0
                icon = "🟢" if unrealized >= 0 else "🔴"
                rows.append(f"• {icon} <b>سود/ضرر لحظه‌ای (خالص با کسر کارمزد): {usd(unrealized, True)} ({roe:+.1f}% ROE)</b>")
        return "\n".join(rows)

    async def _build_scan_report(self) -> tuple[str, InlineKeyboardMarkup]:
        """Runs an immediate market analysis on LBank 15m + 1m candles and formats a clear Persian report."""
        assert self.analyzer and self.trader and self.ai
        snap = await self.analyzer.snapshot()
        setup = self.analyzer.detect_setup(snap)

        trend_map = {
            "UP": "🟢 صعودی (EMA9 بالای EMA26)",
            "DOWN": "🔴 نزولی (EMA9 زیر EMA26)",
            "RANGE": "⚪ رِنج / خنثی (نزدیک بودن میانگین‌ها)",
        }
        div_map = {
            "BULLISH": "🟢 واگرایی مثبت (صعودی)",
            "BEARISH": "🔴 واگرایی منفی (نزولی)",
            None: "ندارد",
        }
        sup_str = price_fmt(snap.supports[0].price) if snap.supports else "—"
        res_str = price_fmt(snap.resistances[0].price) if snap.resistances else "—"

        lines = [
            "🔍 <b>گزارش بررسی لحظه‌ای بازار (زنده از LBank)</b>",
            "",
            f"₿ <b>قیمت لحظه‌ای بیت‌کوین:</b> <b>{price_fmt(snap.price)} USDT</b>",
            f"⚙️ حالت فعال ربات: <b>{mode_label(self.trader.mode)}</b>",
            "",
            "📊 <b>تایم‌فریم ۱۵ دقیقه (تعیین روند اصلی):</b>",
            f"• روند: <b>{trend_map.get(snap.trend15, snap.trend15)}</b>",
            f"• EMA9: <code>{price_fmt(snap.ema_fast_15)}</code> | EMA26: <code>{price_fmt(snap.ema_slow_15)}</code>",
            f"• RSI(14): <code>{snap.rsi15:.1f}</code> | نوسان (ATR): <code>{snap.atr15:.1f}$</code>",
            f"• نزدیک‌ترین حمایت: <code>{sup_str}</code> | مقاومت: <code>{res_str}</code>",
            "",
            "⚡ <b>تایم‌فریم ۱ دقیقه (ماشه ورود اسکالپ):</b>",
            f"• RSI(14): <code>{snap.rsi1:.1f}</code> (اشباع فروش &lt;{self.s.rsi_oversold:g} | اشباع خرید &gt;{self.s.rsi_overbought:g})",
            f"• واگرایی RSI: <b>{div_map.get(snap.divergence_1m, 'ندارد')}</b>",
            f"• EMA9: <code>{price_fmt(snap.ema_fast_1)}</code> | ATR(1m): <code>{snap.atr1:.1f}$</code>",
            "",
        ]
        if setup is not None:
            lines.append(f"🎯 <b>نتیجه فیلتر تکنیکال: ستاپ {side_label(setup.direction)} شناسایی شد!</b>")
            for r in setup.reasons:
                lines.append(f"  ✓ {esc(r)}")
            lines.append(
                "\n🧠 ربات به صورت خودکار این ستاپ را به جمینای می‌فرستد (یا می‌توانید دکمه زیر را برای استعلام فوری بزنید)."
            )
        else:
            lines.append("⏳ <b>نتیجه فیلتر تکنیکال در این ثانیه:</b> هنوز شرایط کامل ورود شکل نگرفته است.")
            if snap.trend15 == "RANGE":
                lines.append("  └ <i>علت: بازار ۱۵ دقیقه در حالت رِنج/خنثی است و ربات منتظر شکل‌گیری روند صعودی یا نزولی واضح است.</i>")
            elif snap.trend15 == "UP":
                lines.append(
                    f"  └ <i>علت: روند ۱۵ دقیقه صعودی است؛ ربات منتظر پولبک قیمت به محدوده EMA26 ({price_fmt(snap.ema_slow_15)}) یا حمایت ({sup_str}) و برگشت مومنتوم ۱ دقیقه است.</i>"
                )
            else:
                lines.append(
                    f"  └ <i>علت: روند ۱۵ دقیقه نزولی است؛ ربات منتظر پولبک قیمت به محدوده EMA26 ({price_fmt(snap.ema_slow_15)}) یا مقاومت ({res_str}) و برگشت مومنتوم ۱ دقیقه است.</i>"
                )

        lines.append("")
        if self.trader.paused:
            lines.append("⏸ <b>توجه:</b> اسکن خودکار الان متوقف است. برای شروع اسکن خودکار دکمه «▶️ ادامه» را بزنید.")
        else:
            lines.append(
                f"⏱ <b>بررسی خودکار بعدی:</b> هر <b>{self.s.scan_interval_sec} ثانیه</b> چارت مجدداً اسکن می‌شود و به محض شکل‌گیری ستاپ، پیام شروع بررسی جمینای برایتان ارسال خواهد شد."
            )

        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "🧠 استعلام فوری از جمینای همین الان (تحلیل دستی)",
                        callback_data="scan:force_ai",
                    )
                ],
                [
                    InlineKeyboardButton("🔄 بروزرسانی مجدد چارت", callback_data="scan:now"),
                ],
            ]
        )
        return "\n".join(lines), keyboard

    async def cmd_scan(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self.analyzer:
            await self._reply(update, "ماژول تحلیلگر آماده نیست.")
            return
        await self._reply(update, "🔍 در حال دریافت کندل‌های زنده از LBank و بررسی اندیکاتورها...")
        try:
            text, keyboard = await self._build_scan_report()
            await self._reply(update, text, keyboard)
        except Exception as exc:  # noqa: BLE001
            await self._reply(update, f"❌ خطا در بررسی بازار: {esc(exc)}")

    async def cmd_guide(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        assert self.trader
        vbal = self.trader.virtual_balance
        text = (
            "📘 <b>راهنمای کامل ترید با پول مجازی (شبیه‌ساز) و پول واقعی در LBank</b>\n\n"
            "<b>۱. چرا تریدهای پول مجازی داخل اپلیکیشن LBank دیده نمی‌شوند؟</b>\n"
            "• در حالت <b>🧪 شبیه‌ساز (پول مجازی)</b>، قیمت‌های بیت‌کوین، کندل‌ها و اسپرد <b>به‌صورت ۱۰۰٪ زنده و واقعی از صرافی LBank</b> خوانده می‌شوند، اما چون پول مجازی (مثلاً ۱۰ دلار) داخل خودِ ربات شبیه‌سازی شده، سفارشی به حساب خالی شما در LBank ارسال نمی‌شود.\n"
            "• بنابراین، <b>محل دیدن تمام تریدهای مجازی، همین ربات تلگرام است!</b>\n\n"
            "<b>۲. چطور مراحل بررسی و ترید را لحظه‌به‌لحظه ببینم؟</b>\n"
            "ربات تمام مراحل را با پیام‌های واضح در همین تلگرام به شما خبر می‌دهد:\n"
            "1️⃣ <b>شروع بررسی هوش مصنوعی:</b> به محض اینکه چارت شرایط ورود پیدا کند، ربات پیام می‌دهد: <i>«🔍 شروع بررسی هوش مصنوعی... جواب تا ۵ الی ۱۰ ثانیه دیگر اعلام می‌شود»</i>.\n"
            "2️⃣ <b>اعلام جواب بررسی:</b> بلافاصله بعد از پاسخ جمینای، نتیجه را می‌گوید (یا <b>⏸ صبر / عدم ورود</b> با ذکر دلیل، یا <b>🚀 تایید سیگنال و ثبت سفارش ورود</b> با قیمت ورود، حد ضرر و اهداف سود).\n"
            "3️⃣ <b>شروع ترید:</b> به محض رسیدن قیمت LBank به نقطه ورود، پیام <i>«🟢 ترید رسماً شروع شد (پوزیشن باز است)»</i> ارسال می‌شود.\n"
            "4️⃣ <b>مشاهده سود/ضرر در حین ترید:</b> در هر ثانیه که روی دکمه <b>📊 وضعیت</b> بزنید، قیمت زنده LBank و سود/ضرر لحظه‌ای ترید باز را می‌بینید.\n"
            "5️⃣ <b>بسته شدن ترید:</b> وقتی قیمت به حد سود اول (ریسک‌فری)، حد سود دوم یا حد ضرر برسد، پیام بسته شدن ترید با محاسبه دقیق کارمزد و موجودی جدید کیف پول مجازی برایتان می‌آید!\n"
            "6️⃣ <b>کارنامه کل تریدها:</b> با زدن دکمه <b>📜 تاریخچه</b>، لیست تمام تریدهای انجام‌شده، درصد برد (Win Rate) و سود خالص را می‌بینید.\n\n"
            "<b>۳. چطور بدون دست زدن به سرور بین پول مجازی و پول واقعی سوئیچ کنم؟</b>\n"
            "• کافیست روی دکمه <b>🔁 تغییر حالت</b> بزنید! در همان‌جا موجودی هر دو کیف پول (مجازی و واقعی LBank) را می‌بینید و با یک کلیک بین آن‌ها جابه‌جا می‌شوید.\n\n"
            f"💵 <b>موجودی فعلی پول مجازی شما: {usd(vbal)}</b>\n"
            "برای تغییر یا ریست کردن مبلغ پول مجازی، یکی از دکمه‌های زیر را بزنید (یا بنویسید <code>/resetdry 25</code>):"
        )
        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("♻️ تنظیم پول مجازی روی $10", callback_data="resetdry:10"),
                    InlineKeyboardButton("♻️ تنظیم روی $25", callback_data="resetdry:25"),
                ],
                [
                    InlineKeyboardButton("♻️ تنظیم روی $50", callback_data="resetdry:50"),
                    InlineKeyboardButton("♻️ تنظیم روی $100", callback_data="resetdry:100"),
                ],
                [
                    InlineKeyboardButton("🔍 بررسی فوری بازار همین الان", callback_data="scan:now"),
                ],
            ]
        )
        await self._reply(update, text, keyboard)

    async def cmd_mode(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        assert self.trader
        info = await self.trader.status_snapshot()
        current = info["mode"]
        lines = [
            "🔁 <b>تغییر حالت معامله (بدون نیاز به تغییر فایل .env)</b>",
            "",
            f"⚙️ حالت فعلی: <b>{mode_label(current)}</b>",
            "",
        ]
        lines.extend(self._wallet_block(info))
        lines.append("\n👇 حالت مورد نظر خود را با یک کلیک انتخاب کنید:")
        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        f"{'✅ ' if current == 'DRY' else ''}🧪 شبیه‌ساز (پول مجازی: {usd(info.get('virtual_balance', 10.0))})",
                        callback_data="mode:DRY",
                    )
                ],
                [
                    InlineKeyboardButton(
                        f"{'✅ ' if current == 'LIVE' else ''}💰 پول واقعی صرافی LBank ({usd(info['real_futures_balance']) if info.get('real_futures_balance') is not None else 'اتصال با کلید API'})",
                        callback_data="mode:LIVE",
                    )
                ],
            ]
        )
        await self._reply(update, "\n".join(lines), keyboard)

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
        await self._reply(
            update,
            "⏸ <b>اسکن خودکار بازار متوقف شد.</b>\n"
            "پوزیشن باز فعلی (در صورت وجود) همچنان مدیریت می‌شود. هر زمان آماده بودید روی «▶️ ادامه» بزنید.",
        )

    async def cmd_resume(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        assert self.trader and self.risk
        self.trader.set_paused(False)
        text = (
            f"▶️ <b>اسکن خودکار بازار فعال شد!</b>\n"
            f"⏱ ربات هر <b>{self.s.scan_interval_sec} ثانیه</b> کندل‌های ۱ دقیقه و ۱۵ دقیقه بیت‌کوین در LBank را بررسی می‌کند.\n"
            "• به محض پیدا شدن ستاپ تکنیکال، پیام <b>«🔍 شروع بررسی هوش مصنوعی»</b> برایتان می‌آید و ۵ تا ۱۰ ثانیه بعد نتیجه اعلام می‌شود."
        )
        halt = self.risk.halt_state()
        if halt:
            text += (
                f"\n\n⚠️ توجه: به دلیل قوانین مدیریت ریسک، معاملات تا <b>{self._fmt_time(halt['until'])}</b> "
                "قفل است و با /resume باز نمی‌شود (جلوگیری از معاملات انتقامی)."
            )
        await self._reply(update, text)
        if self.analyzer:
            try:
                report_text, keyboard = await self._build_scan_report()
                await self._reply(update, report_text, keyboard)
            except Exception as exc:  # noqa: BLE001
                log.warning("Resume scan preview failed: %s", exc)

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
        summary_dry = self.store.summary("DRY")
        summary_live = self.store.summary("LIVE")
        lines = ["📜 <b>کارنامه و تاریخچه معاملات (مجازی و واقعی)</b>", ""]
        if not trades:
            lines.append("📭 هنوز هیچ معامله بسته‌شده‌ای ثبت نشده است.")
            if self.trader.has_position() and self.trader.position:
                p = self.trader.position
                lines.append(
                    f"📌 <i>در حال حاضر یک ترید {side_label(p.side)} در حالت {mode_label(p.mode)} روی قیمت {price_fmt(p.fill_price or p.entry_price)} باز دارید (برای جزئیات روی «📊 وضعیت» بزنید).</i>"
                )
        for t in trades:
            icon = "✅ برد" if (t["net_pnl"] or 0) > 0 else "❌ باخت"
            side = "🟢 LONG" if t["side"] == "LONG" else "🔴 SHORT"
            mode = "🧪 مجازی" if t["mode"] == "DRY" else "💰 واقعی"
            reason = CLOSE_REASONS.get(t.get("reason", ""), t.get("reason", ""))
            lines.append(
                f"{icon} | {mode} | {side} | <b>{usd(t['net_pnl'] or 0, True)}</b>\n"
                f"  └ ورود: <code>{price_fmt(t['entry_price'] or 0)}</code> ← خروج: <code>{price_fmt(t['exit_price'] or 0)}</code> | {esc(reason)} | {self._fmt_time(t['closed_at'])}"
            )
        lines.append("")
        lines.append(
            f"🧪 <b>مجموع کارنامه پول مجازی (شبیه‌ساز):</b> {summary_dry['trades']} ترید | "
            f"نرخ برد: <b>{summary_dry['win_rate']:.0f}%</b> | سود/ضرر کل: <b>{usd(summary_dry['net_pnl'], True)}</b> "
            f"(موجودی فعلی: <b>{usd(self.trader.virtual_balance)}</b>)"
        )
        lines.append(
            f"💰 <b>مجموع کارنامه پول واقعی (LBank):</b> {summary_live['trades']} ترید | "
            f"نرخ برد: <b>{summary_live['win_rate']:.0f}%</b> | سود/ضرر کل: <b>{usd(summary_live['net_pnl'], True)}</b>"
        )
        await self._reply(update, "\n".join(lines))

    async def cmd_resetdry(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        assert self.trader and self.risk
        if self.trader.has_position() and self.trader.position and self.trader.position.mode == "DRY":
            await self._reply(update, "ابتدا پوزیشن باز شبیه‌ساز باید بسته شود (می‌توانید از 🚨 بستن اضطراری استفاده کنید).")
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
        if self.trader.mode == "DRY":
            self.store.delete(self.risk.DAILY_KEY)
            self.risk.roll_day(value)
            self.risk.clear_halt()
        await self._reply(update, f"♻️ موجودی کیف پول مجازی (شبیه‌ساز) به <b>{usd(value)}</b> تنظیم شد و آماده ترید است.")

    async def cmd_setkeys(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        assert self.trader
        if not context.args or len(context.args) < 2:
            await self._reply(
                update,
                "🔑 <b>ثبت کلیدهای API صرافی از داخل تلگرام</b>\n\n"
                "کلید API و Secret خود را با یک فاصله بعد از دستور بفرستید:\n"
                "<code>/setkeys API_KEY SECRET_KEY</code>",
            )
            return
        api_key, api_secret = context.args[0], context.args[1]
        self.trader.set_exchange_keys(api_key, api_secret)
        info = await self.trader.status_snapshot()
        lines = [
            "✅ <b>کلیدهای API صرافی با موفقیت در حافظه ربات ذخیره شد!</b>",
            "",
        ]
        lines.extend(self._wallet_block(info))
        lines.append("\nحالا می‌توانید با زدن دکمه «🔁 تغییر حالت» هر زمان خواستید بین پول مجازی و پول واقعی جابه‌جا شوید.")
        await self._reply(update, "\n".join(lines))

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
        assert self.trader and self.ai and self.risk
        if data == "mode:menu":
            info = await self.trader.status_snapshot()
            current = info["mode"]
            lines = [
                "🔁 <b>تغییر حالت معامله</b>",
                "",
                f"⚙️ حالت فعلی: <b>{mode_label(current)}</b>",
                "",
            ]
            lines.extend(self._wallet_block(info))
            keyboard = InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            f"{'✅ ' if current == 'DRY' else ''}🧪 شبیه‌ساز (پول مجازی: {usd(info.get('virtual_balance', 10.0))})",
                            callback_data="mode:DRY",
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            f"{'✅ ' if current == 'LIVE' else ''}💰 پول واقعی صرافی LBank",
                            callback_data="mode:LIVE",
                        )
                    ],
                ]
            )
            await query.edit_message_text("\n".join(lines), parse_mode=ParseMode.HTML, reply_markup=keyboard)
        elif data == "mode:DRY":
            await self._apply_mode(query, "DRY")
        elif data == "mode:LIVE":
            keyboard = InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton("⚠️ تأیید: سوئیچ به پول واقعی صرافی", callback_data="mode:LIVE:confirm"),
                        InlineKeyboardButton("❌ انصراف", callback_data="mode:cancel"),
                    ]
                ]
            )
            await query.edit_message_text(
                "⚠️ <b>تأیید تغییر به حالت واقعی (LIVE)</b>\n"
                "در این حالت، ربات موجودی واقعی کیف پول LBank شما را بررسی کرده و با پول واقعی معامله می‌کند (اگر موجودی صرافی شما کمتر از ۳ دلار باشد، به شما هشدار می‌دهد و وارد معامله نمی‌شود).\n\n"
                "آیا ادامه می‌دهید؟",
                parse_mode=ParseMode.HTML,
                reply_markup=keyboard,
            )
        elif data == "mode:LIVE:confirm":
            await self._apply_mode(query, "LIVE")
        elif data == "mode:cancel":
            await query.edit_message_text("تغییر حالت لغو شد.")
        elif data == "scan:now":
            if not self.analyzer:
                await query.edit_message_text("ماژول تحلیلگر آماده نیست.")
                return
            await query.edit_message_text("🔍 در حال دریافت کندل‌های زنده از LBank و بروزرسانی تحلیل...")
            try:
                text, keyboard = await self._build_scan_report()
                await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=keyboard)
            except Exception as exc:  # noqa: BLE001
                await query.edit_message_text(f"❌ خطا در بررسی بازار: {esc(exc)}", parse_mode=ParseMode.HTML)
        elif data == "scan:force_ai":
            await self._run_manual_ai_scan(query)
        elif data.startswith("resetdry:"):
            if self.trader.has_position() and self.trader.position and self.trader.position.mode == "DRY":
                await query.edit_message_text("ابتدا پوزیشن باز شبیه‌ساز باید بسته شود.", parse_mode=ParseMode.HTML)
                return
            amt = float(data.split(":", 1)[1])
            val = self.trader.reset_virtual_balance(amt)
            if self.trader.mode == "DRY":
                self.store.delete(self.risk.DAILY_KEY)
                self.risk.roll_day(val)
                self.risk.clear_halt()
            await query.edit_message_text(
                f"✅ موجودی کیف پول مجازی (شبیه‌ساز) به <b>{usd(val)}</b> تغییر یافت و آمار امروز ریست شد.",
                parse_mode=ParseMode.HTML,
            )
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

    async def _run_manual_ai_scan(self, query: Any) -> None:
        """Runs an on-demand Gemini market evaluation and displays start + finish timestamps clearly."""
        assert self.analyzer and self.ai and self.trader
        from indicators import Setup

        now_str = datetime.now(self.tz).strftime("%H:%M:%S")
        await query.edit_message_text(
            f"🔍 <b>شروع بررسی هوش مصنوعی ({esc(self.ai.active_model)})</b>\n"
            f"🕒 ساعت شروع بررسی: <b>{now_str}</b>\n\n"
            "⏳ در حال دریافت آخرین کندل‌های ۱ دقیقه و ۱۵ دقیقه از LBank و ارسال به جمینای...\n"
            "<i>(جواب بررسی تا ۵ الی ۱۰ ثانیه دیگر روی همین پیام نمایش داده می‌شود)</i>",
            parse_mode=ParseMode.HTML,
        )
        try:
            snap = await self.analyzer.snapshot()
            setup = self.analyzer.detect_setup(snap)
            is_manual_setup = False
            if setup is None:
                is_manual_setup = True
                default_dir = "LONG" if snap.trend15 == "UP" else ("SHORT" if snap.trend15 == "DOWN" else ("LONG" if snap.rsi1 <= 50 else "SHORT"))
                setup = Setup(
                    direction=default_dir,
                    reasons=[
                        f"استعلام دستی توسط کاربر در قیمت {price_fmt(snap.price)}",
                        f"روند ۱۵ دقیقه: {snap.trend15} | RSI(1m): {snap.rsi1:.1f}",
                    ],
                    key=f"MANUAL:{int(datetime.now().timestamp())}",
                )
            signal = await self.ai.analyze(snap, setup, force=True)
            done_str = datetime.now(self.tz).strftime("%H:%M:%S")
            if signal is None:
                await query.edit_message_text(
                    "⚠️ پاسخ معتبری از جمینای دریافت نشد (یا سقف روزانه پر شده است).",
                    parse_mode=ParseMode.HTML,
                )
                return

            verdict = (
                f"✅ پیشنهاد ورود: {side_label(signal.action)}"
                if signal.is_trade and signal.confidence >= self.s.ai_min_confidence
                else f"⏸ صبر / عدم ورود ({signal.action})"
            )
            lines = [
                "🧠 <b>جواب بررسی هوش مصنوعی جمینای دریافت شد!</b>",
                f"🕒 ساعت اعلام جواب: <b>{done_str}</b> | مدل: <code>{esc(self.ai.active_model)}</code>",
                "",
                f"₿ قیمت لحظه‌ای LBank: <b>{price_fmt(snap.price)} USDT</b>",
                f"📣 <b>تصمیم جمینای: {verdict}</b>",
                f"🎯 درجه اطمینان: <b>{signal.confidence}%</b> (حداقل لازم برای ترید: {self.s.ai_min_confidence}%)",
                "",
                "📝 <b>توضیح و استدلال جمینای:</b>",
                esc(signal.reasoning_fa),
            ]
            if signal.is_trade:
                lines += [
                    "",
                    "📐 <b>سطوح پیشنهادی جمینای:</b>",
                    f"• ورود: <code>{price_fmt(signal.entry_price)}</code> | اهرم: <code>{signal.suggested_leverage}x</code>",
                    f"• حد ضرر (SL): <code>{price_fmt(signal.stop_loss)}</code>",
                    f"• حد سود ۱ (TP1): <code>{price_fmt(signal.take_profit_1)}</code> | حد سود ۲ (TP2): <code>{price_fmt(signal.take_profit_2)}</code>",
                ]
            if signal.adjustments:
                lines += ["", "🛠 <b>اصلاحات مدیریت ریسک:</b>"] + [f"• {esc(a)}" for a in signal.adjustments]

            if signal.is_trade and signal.confidence >= self.s.ai_min_confidence and not is_manual_setup:
                if not self.trader.paused and not self.trader.has_position() and self.risk and self.risk.can_trade()[0]:
                    lines.append("\n🚀 <b>شرایط کامل است؛ در حال باز کردن ترید...</b>")
                    await query.edit_message_text("\n".join(lines), parse_mode=ParseMode.HTML)
                    await self.trader.open_from_signal(signal, snap)
                    return

            keyboard = InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton("🔄 بررسی مجدد چارت", callback_data="scan:now"),
                        InlineKeyboardButton("🧠 استعلام دوباره از جمینای", callback_data="scan:force_ai"),
                    ]
                ]
            )
            await query.edit_message_text("\n".join(lines), parse_mode=ParseMode.HTML, reply_markup=keyboard)
        except Exception as exc:  # noqa: BLE001
            await query.edit_message_text(f"❌ خطا در استعلام جمینای: {esc(exc)}", parse_mode=ParseMode.HTML)

    async def _apply_mode(self, query: Any, mode: str) -> None:
        assert self.trader
        ok, message, balance = await self.trader.switch_mode(mode)
        if ok:
            info = await self.trader.status_snapshot()
            lines = [
                f"✅ <b>حالت ربات با موفقیت به {mode_label(mode)} تغییر کرد!</b>",
                "",
            ]
            lines.extend(self._wallet_block(info))
            if balance < self.s.min_balance_to_trade:
                lines.append(
                    f"\n🪫 <b>توجه مهم:</b> موجودی کیف پول فعال شما (<b>{usd(balance)}</b>) کمتر از حداقل مجاز برای ترید (<b>{usd(self.s.min_balance_to_trade)}</b>) است!\n"
                    "ربات تا زمان شارژ حساب، هیچ معامله‌ای باز نمی‌کند. هر زمان خواستید با پول مجازی تست کنید، دوباره حالت شبیه‌ساز را انتخاب نمایید."
                )
            if message:
                lines.append(f"\n{esc(message)}")
            keyboard = InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "🧪 بازگشت به شبیه‌ساز (پول مجازی)" if mode == "LIVE" else "💰 سوئیچ به پول واقعی صرافی",
                            callback_data="mode:DRY" if mode == "LIVE" else "mode:LIVE",
                        )
                    ]
                ]
            )
            await query.edit_message_text("\n".join(lines), parse_mode=ParseMode.HTML, reply_markup=keyboard)
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
        info = await self.trader.status_snapshot() if self.trader else {"mode": mode, "virtual_balance": balance or 10.0}
        lines = [
            "🤖 <b>ربات اسکالپر روشن شد و آماده به کار است!</b>",
            "",
            f"⚙️ حالت فعلی: <b>{mode_label(mode)}</b>",
            f"🏦 صرافی: <b>{esc(exchange)}</b> | نماد: <code>{esc(self.s.symbol)}</code>",
            f"🧠 مدل هوش مصنوعی: <code>{esc(active_model)}</code>",
            f"⏱ دوره بررسی خودکار چارت: <b>هر {self.s.scan_interval_sec} ثانیه</b>",
            "",
        ]
        lines.extend(self._wallet_block(info))
        lines += [
            "",
            "💡 <i>برای دیدن وضعیت لحظه‌ای چارت روی «🔍 بررسی فوری بازار» و برای راهنمای کامل ترید مجازی روی «📘 راهنمای ترید مجازی» در کیبورد پایین بزنید.</i>",
        ]
        if restored:
            lines.append(f"\n♻️ {esc(restored)}")
        await self.send("\n".join(lines), self._keyboard())

    async def notify_ai_checking(self, snap: "MarketSnapshot", setup: "Setup", model_name: str) -> None:
        """Sent immediately when a technical setup is found and sent to Gemini."""
        now_str = datetime.now(self.tz).strftime("%H:%M:%S")
        lines = [
            "🔍 <b>شروع بررسی هوش مصنوعی (ستاپ تکنیکال پیدا شد!)</b>",
            f"🕒 ساعت شروع بررسی: <b>{now_str}</b>",
            "",
            f"📍 جهت تشخیص داده‌شده: <b>{side_label(setup.direction)}</b>",
            f"₿ قیمت لحظه‌ای بیت‌کوین در LBank: <b>{price_fmt(snap.price)} USDT</b>",
            "🔎 <b>دلایل تکنیکال:</b>",
        ]
        for r in setup.reasons:
            lines.append(f"• {esc(r)}")
        lines += [
            "",
            f"🧠 در حال استعلام از مدل <code>{esc(model_name)}</code>...",
            "⏳ <b>جواب بررسی تا ۵ الی ۱۰ ثانیه دیگر در پیام بعدی اعلام می‌شود.</b>",
        ]
        await self.send("\n".join(lines))

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
                    "\n💡 لطفاً کیف پول <b>Futures (USDT-M)</b> خود را در صرافی شارژ کنید (یا با دکمه «🔁 تغییر حالت» به حالت 🧪 شبیه‌ساز بروید تا با پول مجازی تست کنید)."
                )
        else:
            lines.append(
                "\n💡 در حالت شبیه‌ساز هستید؛ برای شارژ مجدد موجودی مجازی دستور <code>/resetdry 10</code> را بفرستید."
            )
        await self.send("\n".join(lines))

    async def notify_signal_opened(
        self, pos: "Position", plan: PositionPlan, signal: "TradeSignal", warnings: list[str]
    ) -> None:
        now_str = datetime.now(self.tz).strftime("%H:%M:%S")
        lines = [
            "✅ <b>جواب بررسی جمینای: تایید ورود! سفارش ثبت شد 🚀</b>",
            f"🕒 ساعت ثبت سفارش: <b>{now_str}</b>",
            "",
            f"📍 نوع معامله: <b>{side_label(pos.side)}</b>",
            f"⚙️ کیف پول: <b>{mode_label(pos.mode)}</b>",
            f"🎯 اطمینان جمینای: <b>{signal.confidence}%</b>",
            "",
            f"💲 قیمت ورود (لیمیت): <b>{price_fmt(pos.entry_price)}</b>",
            f"📦 حجم: <b>{pos.contracts:g}</b> قرارداد (≈ {pos.base_qty:.4f} BTC | ارزش کل ≈ {usd(plan.notional)})",
            f"⚡ لوریج: <b>{pos.leverage}x</b> (Isolated) | مارجین درگیر ≈ <b>{usd(plan.margin)}</b>",
            f"⛔ حد ضرر (SL): <b>{price_fmt(pos.stop_loss)}</b> ({plan.sl_pct * 100:.2f}%)",
            f"✅ حد سود ۱ (TP1 - سیو ۵۰٪ و ریسک‌فری): <b>{price_fmt(pos.tp1)}</b> (R/R 1:{plan.rr_tp1:.1f})",
            f"🏆 حد سود ۲ (TP2 - هدف اصلی): <b>{price_fmt(pos.tp2)}</b> (R/R 1:{plan.rr_tp2:.1f})",
            f"💀 قیمت تقریبی لیکوئید: {price_fmt(plan.liquidation_price)}",
            "",
            f"🔻 حداکثر ریسک دلاری این ترید (با کارمزد): <b>{usd(plan.expected_loss)}</b> از سقف {usd(plan.risk_budget)}",
        ]
        if plan.risk_reduced:
            lines.append("ℹ️ به دلیل محدودیت مارجین/لوریج، حجم کمتر از سقف ریسک تنظیم شد.")
        lines += ["", "🔎 <b>شرایط تکنیکال:</b>"]
        lines += [f"• {esc(r)}" for r in signal.setup_reasons]
        lines += ["", "🧠 <b>تحلیل و دلیل ورود جمینای:</b>", esc(signal.reasoning_fa)]
        if signal.adjustments:
            lines += ["", "🛠 <b>اصلاحات خودکار:</b>"] + [f"• {esc(a)}" for a in signal.adjustments]
        if warnings:
            lines += ["", "⚠️ <b>هشدارها:</b>"] + [f"• {esc(w)}" for w in warnings]
        lines += ["", f"⏳ مهلت پر شدن سفارش ورود: {self.s.entry_timeout_min:g} دقیقه (به محض رسیدن قیمت، پیام شروع ترید ارسال می‌شود)"]
        await self.send("\n".join(lines))

    async def notify_entry_filled(self, pos: "Position") -> None:
        now_str = datetime.now(self.tz).strftime("%H:%M:%S")
        stop_mode = "نرم‌افزاری (در حال رصد لحظه‌ای قیمت LBank)" if pos.soft_stop else "ثبت‌شده روی صرافی"
        await self.send(
            "🟢 <b>ترید رسماً شروع شد! (سفارش ورود پر شد و پوزیشن باز است)</b>\n"
            f"🕒 ساعت شروع ترید: <b>{now_str}</b>\n\n"
            f"📍 {side_label(pos.side)} | {mode_label(pos.mode)}\n"
            f"💲 قیمت دقیق ورود: <b>{price_fmt(pos.fill_price)}</b>\n"
            f"📦 حجم: {pos.contracts:g} قرارداد | ⚡ لوریج: <b>{pos.leverage}x</b>\n"
            f"⛔ حد ضرر فعال (SL): <b>{price_fmt(pos.stop_loss)}</b> ({stop_mode})\n"
            f"✅ حد سود ۱ (TP1): <b>{price_fmt(pos.tp1)}</b>\n"
            f"🏆 حد سود ۲ (TP2): <b>{price_fmt(pos.tp2)}</b>\n\n"
            "💡 <i>ربات هر ۳ ثانیه قیمت LBank را چک می‌کند. برای دیدن سود/ضرر لحظه‌ای در هر زمان روی «📊 وضعیت» بزنید.</i>"
        )

    async def notify_entry_cancelled(self, pos: "Position", reason: str) -> None:
        await self.send(
            "⌛ <b>سفارش ورود پر نشد و لغو گردید</b>\n\n"
            f"📍 {side_label(pos.side)} @ {price_fmt(pos.entry_price)}\n"
            f"📝 دلیل: {esc(reason)}\n"
            "ربات به اسکن خودکار بازار ادامه می‌دهد."
        )

    async def notify_trade_rejected(self, signal: "TradeSignal", reason: str) -> None:
        await self.send(
            "🚫 <b>جواب بررسی جمینای مثبت بود اما مدیریت ریسک جلوی ورود را گرفت</b>\n\n"
            f"📍 {side_label(signal.action)} | اطمینان {signal.confidence}%\n"
            f"📝 دلیل رد شدن: {esc(reason)}"
        )

    async def notify_ai_wait(self, signal: "TradeSignal", direction: str) -> None:
        if not self.s.notify_ai_wait:
            return
        now_str = datetime.now(self.tz).strftime("%H:%M:%S")
        verdict = "⏸ صبر و عدم ورود (WAIT)" if signal.action == "WAIT" else f"⏸ عدم ورود ({signal.action} با اطمینان پایین)"
        lines = [
            "🧠 <b>جواب بررسی هوش مصنوعی جمینای اعلام شد</b>",
            f"🕒 ساعت اعلام جواب: <b>{now_str}</b>",
            "",
            f"📍 جهت بررسی‌شده: {side_label(direction)}",
            f"📣 نتیجه نهایی: <b>{esc(verdict)}</b> (درجه اطمینان: <b>{signal.confidence}%</b> از حداقل {self.s.ai_min_confidence}%)",
            "",
            "📝 <b>دلیل جمینای برای صبر کردن:</b>",
            esc(signal.reasoning_fa),
        ]
        if signal.adjustments:
            lines += [""] + [f"• {esc(a)}" for a in signal.adjustments]
        lines.append("\n🔍 <i>ربات وارد این موقعیت نشد و به اسکن خودکار چارت ادامه می‌دهد.</i>")
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
