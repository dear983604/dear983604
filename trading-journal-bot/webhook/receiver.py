"""
webhook/receiver.py — FastAPI webhook endpoint，接收 TradingView alert

POST /webhook/tradingview
  驗證：Header `X-Webhook-Secret: {WEBHOOK_SECRET}`，不符回 403。
        （TradingView 的 alert webhook 無法自訂 header，因此也接受
         query string `?secret=...` 或 JSON 內的 "secret" 欄位作為備援。）
  Body ：ENTRY / EXIT_PARTIAL / EXIT_FINAL 三種 JSON 事件。

錯誤處理：JSON 格式不符或與狀態不符時記 log 並回 4xx，絕不讓服務 crash。
GET /health 提供健康檢查。
"""

from __future__ import annotations

import hmac
import json
import logging
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from core.trade_manager import TradeEventError, TradeManager

logger = logging.getLogger(__name__)


def _secret_ok(provided: str | None, expected: str) -> bool:
    if not provided or not expected:
        return False
    return hmac.compare_digest(provided.encode("utf-8"), expected.encode("utf-8"))


def create_app(trade_manager: TradeManager, webhook_secret: str) -> FastAPI:
    app = FastAPI(title="Trading Journal Webhook", docs_url=None, redoc_url=None)

    @app.get("/health")
    async def health() -> dict[str, Any]:
        return {"ok": True, "state": trade_manager.state.value}

    @app.post("/webhook/tradingview")
    async def tradingview(request: Request) -> JSONResponse:
        raw = await request.body()

        payload: Any = None
        parse_error: str | None = None
        try:
            payload = json.loads(raw.decode("utf-8")) if raw else None
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            parse_error = str(exc)

        # ── 驗證 secret（header 優先，其次 query / body）──
        provided = request.headers.get("X-Webhook-Secret") or request.query_params.get("secret")
        if not provided and isinstance(payload, dict):
            provided = payload.get("secret")
        if not _secret_ok(provided, webhook_secret):
            logger.warning("Webhook secret 驗證失敗（來源 %s）", request.client.host if request.client else "?")
            return JSONResponse({"ok": False, "error": "forbidden"}, status_code=403)

        # ── 解析 JSON ──
        if parse_error is not None or not isinstance(payload, dict):
            logger.error("Webhook JSON 格式錯誤：%s｜原始內容：%r", parse_error or "不是 JSON 物件", raw[:500])
            return JSONResponse({"ok": False, "error": "invalid JSON object"}, status_code=400)

        event = {k: v for k, v in payload.items() if k != "secret"}
        logger.info("收到 webhook 事件：%s", event)

        # ── 交給狀態機 ──
        try:
            trade = await trade_manager.handle_event(event)
        except TradeEventError as exc:
            logger.error("Webhook 事件無法處理：%s｜事件：%s", exc, event)
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=422)
        except Exception:
            logger.exception("處理 webhook 事件時發生未預期錯誤｜事件：%s", event)
            return JSONResponse({"ok": False, "error": "internal error"}, status_code=500)

        return JSONResponse(
            {
                "ok": True,
                "trade_id": trade.trade_id,
                "status": trade.status.value,
                "remaining": trade.remaining_qty,
            }
        )

    return app
