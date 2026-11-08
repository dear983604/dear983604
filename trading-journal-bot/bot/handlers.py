"""
bot/handlers.py — Telegram 指令與訊息處理

指令：
  /start        歡迎訊息
  /status       目前持倉狀態（IDLE 或 OPEN 的進場資訊）
  /today        今日所有已結算交易的損益彙總
  /last         最後一筆交易的完整結算明細
  /history [N]  最近 N 筆交易清單（預設 5 筆）

一般文字訊息會交給 Claude 解析（介面預留，目前只回覆解析結果供確認）。
安全性：所有 handler 都套用 filters.User(TELEGRAM_ALLOWED_USER_ID)，其他人的訊息一律忽略。
"""

from __future__ import annotations

import json
import logging
from datetime import datetime

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from bot import formatter
from config import Settings
from core.ai_parser import ClaudeParser
from core.journal import Journal
from core.trade_manager import TradeManager

logger = logging.getLogger(__name__)

HELP_TEXT = (
    "👋 <b>交易日誌 Bot</b>\n"
    "我會接收 TradingView 訊號並記錄每筆交易。\n\n"
    "/status — 目前持倉狀態\n"
    "/today — 今日損益彙總\n"
    "/last — 最後一筆結算明細\n"
    "/history 5 — 最近 N 筆交易"
)


class BotHandlers:
    """把 TradeManager / Journal 綁進 Telegram 指令。"""

    def __init__(
        self,
        trade_manager: TradeManager,
        journal: Journal,
        ai_parser: ClaudeParser | None = None,
    ) -> None:
        self.tm = trade_manager
        self.journal = journal
        self.ai = ai_parser

    @staticmethod
    async def _reply(update: Update, text: str) -> None:
        if update.effective_message:
            await update.effective_message.reply_text(
                text, parse_mode=ParseMode.HTML, disable_web_page_preview=True
            )

    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self._reply(update, HELP_TEXT)

    async def status(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self._reply(update, formatter.format_status(self.tm.current))

    async def today(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        now = datetime.now()
        trades = await self.journal.trades_on(now)
        await self._reply(update, formatter.format_today(now.strftime("%Y-%m-%d"), trades))

    async def last(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        trade = await self.journal.last_trade()
        if trade is None:
            await self._reply(update, "📭 尚無已結算交易")
            return
        await self._reply(update, formatter.format_settlement(trade))

    async def history(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        n = 5
        if context.args:
            try:
                n = max(1, min(int(context.args[0]), 50))
            except ValueError:
                await self._reply(update, "用法：/history 5")
                return
        await self._reply(update, formatter.format_history(await self.journal.recent(n)))

    async def text(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """自然語言輸入：交給 Claude 解析（目前僅回覆結果，不寫入交易）。"""
        message = update.effective_message
        if message is None or not message.text:
            return
        if self.ai is None or not self.ai.enabled:
            await self._reply(update, "🤖 尚未設定 ANTHROPIC_API_KEY，請使用指令操作。\n\n" + HELP_TEXT)
            return
        event = await self.ai.parse(message.text)
        if event is None:
            await self._reply(update, "🤔 看不出是交易指令，請再說清楚一點。")
            return
        pretty = json.dumps(event, ensure_ascii=False, indent=2)
        await self._reply(
            update,
            f"🤖 解析結果（尚未寫入日誌）：\n<pre>{formatter.esc(pretty)}</pre>",
        )

    async def on_error(self, update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        logger.error("Telegram handler 發生錯誤", exc_info=context.error)


def build_application(
    settings: Settings,
    trade_manager: TradeManager,
    journal: Journal,
    ai_parser: ClaudeParser | None = None,
) -> Application:
    """建立 python-telegram-bot Application 並註冊 handler。"""
    assert settings.telegram_allowed_user_id is not None
    app = ApplicationBuilder().token(settings.telegram_token).build()
    h = BotHandlers(trade_manager, journal, ai_parser)
    allowed = filters.User(user_id=settings.telegram_allowed_user_id)

    app.add_handler(CommandHandler("start", h.start, filters=allowed))
    app.add_handler(CommandHandler("help", h.start, filters=allowed))
    app.add_handler(CommandHandler("status", h.status, filters=allowed))
    app.add_handler(CommandHandler("today", h.today, filters=allowed))
    app.add_handler(CommandHandler("last", h.last, filters=allowed))
    app.add_handler(CommandHandler("history", h.history, filters=allowed))
    app.add_handler(MessageHandler(allowed & filters.TEXT & ~filters.COMMAND, h.text))
    app.add_error_handler(h.on_error)
    return app


def make_telegram_notifier(app: Application, chat_id: int):
    """產生給 TradeManager 用的通知函式：傳訊息給授權使用者。"""

    async def notify(message: str) -> None:
        await app.bot.send_message(
            chat_id=chat_id,
            text=message,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )

    return notify
