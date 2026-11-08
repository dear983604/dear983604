"""
main.py — 程式入口

用 asyncio 在同一個 event loop 同時執行：
  1. python-telegram-bot Application（polling 模式）
  2. uvicorn + FastAPI webhook server

兩者共用同一個 TradeManager，因此 webhook 收到事件後能直接透過 bot 推播通知。
未設定 TELEGRAM_TOKEN 時只啟動 webhook server，通知改寫入 log（方便本機測試）。
Ctrl+C / SIGTERM 時 uvicorn 結束，接著依序關閉 bot。
"""

from __future__ import annotations

import asyncio
import logging
import sys

import uvicorn

from config import ConfigError, load_settings
from core.ai_parser import ClaudeParser
from core.journal import Journal
from core.trade_manager import TradeManager
from webhook.receiver import create_app

logger = logging.getLogger("main")


async def run() -> None:
    settings = load_settings()
    settings.validate()

    logging.basicConfig(
        level=settings.log_level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    # 避免 httpx 把含 bot token 的 URL 印到 log
    logging.getLogger("httpx").setLevel(logging.WARNING)

    journal = Journal(settings.data_dir)
    trade_manager = TradeManager(journal, settings.points_per_lot)
    await trade_manager.restore()

    ai_parser = ClaudeParser(settings.anthropic_api_key, settings.claude_model)

    web_app = create_app(trade_manager, settings.webhook_secret)
    server = uvicorn.Server(
        uvicorn.Config(
            web_app,
            host=settings.webhook_host,
            port=settings.webhook_port,
            log_level=settings.log_level.lower(),
        )
    )

    if not settings.telegram_enabled:
        logger.warning("未設定 TELEGRAM_TOKEN，僅啟動 Webhook Server（通知寫入 log）")
        await server.serve()
        return

    from bot.handlers import build_application, make_telegram_notifier

    tg_app = build_application(settings, trade_manager, journal, ai_parser)
    assert settings.telegram_allowed_user_id is not None
    trade_manager.set_notifier(make_telegram_notifier(tg_app, settings.telegram_allowed_user_id))

    async with tg_app:
        await tg_app.start()
        assert tg_app.updater is not None
        await tg_app.updater.start_polling(drop_pending_updates=True)
        logger.info(
            "Telegram Bot 已啟動；Webhook Server 監聽 %s:%d",
            settings.webhook_host, settings.webhook_port,
        )
        try:
            await server.serve()
        finally:
            logger.info("正在關閉 Telegram Bot…")
            await tg_app.updater.stop()
            await tg_app.stop()


def main() -> None:
    try:
        asyncio.run(run())
    except ConfigError as exc:
        print(f"設定錯誤：{exc}", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
