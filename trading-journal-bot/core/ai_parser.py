"""
core/ai_parser.py — Claude AI 自然語言解析介面（備用）

把使用者在 Telegram 輸入的自然語言（例如「18250 多三口 停損 18180」）
轉成與 TradingView webhook 相同格式的事件 dict，交給 TradeManager.handle_event()。

本 Part 只建立介面：bot/handlers.py 目前僅把解析結果回覆給使用者確認，
不會自動送進狀態機。未設定 ANTHROPIC_API_KEY 時 parse() 一律回傳 None。
"""

from __future__ import annotations

import json
import logging
from typing import Any

import anthropic

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """你是台指期交易日誌助手。把使用者的一句話轉成交易事件 JSON。
事件類型：
- ENTRY：進場。欄位 dir（"做多" 或 "做空"）、qty、price、stop（可省略）、time（可省略）
- EXIT_PARTIAL：分批出場。欄位 qty、price、reason、batch（可省略）
- EXIT_FINAL：最後出場（全部清倉）。欄位 qty、price、reason
如果這句話不是交易指令或資訊不足，event 填 "NONE"，並在 note 說明缺什麼。
不要自行編造數字。"""

EVENT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "event": {"type": "string", "enum": ["ENTRY", "EXIT_PARTIAL", "EXIT_FINAL", "NONE"]},
        "dir": {"type": ["string", "null"]},
        "qty": {"type": ["integer", "null"]},
        "price": {"type": ["number", "null"]},
        "stop": {"type": ["number", "null"]},
        "reason": {"type": ["string", "null"]},
        "batch": {"type": ["integer", "null"]},
        "time": {"type": ["string", "null"]},
        "note": {"type": ["string", "null"]},
    },
    "required": ["event", "dir", "qty", "price", "stop", "reason", "batch", "time", "note"],
    "additionalProperties": False,
}


class ClaudeParser:
    """以 Claude 解析自然語言交易指令。"""

    def __init__(self, api_key: str, model: str = "claude-opus-5-5") -> None:
        self.model = model
        self.client: anthropic.AsyncAnthropic | None = (
            anthropic.AsyncAnthropic(api_key=api_key) if api_key else None
        )

    @property
    def enabled(self) -> bool:
        return self.client is not None

    async def parse(self, text: str) -> dict[str, Any] | None:
        """回傳事件 dict（去除 null 欄位）；非交易指令或失敗時回傳 None。"""
        if self.client is None:
            return None
        try:
            response = await self.client.beta.messages.create(
                model=self.model,
                max_tokens=1024,
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": text}],
                output_config={
                    "effort": "low",
                    "format": {"type": "json_schema", "schema": EVENT_SCHEMA},
                },
                # 若主模型因安全分類拒答，由 API 自動改用備援模型
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
            )
        except anthropic.RateLimitError:
            logger.warning("Claude API 達到速率限制")
            return None
        except anthropic.APIStatusError as exc:
            logger.error("Claude API 錯誤 %s：%s", exc.status_code, exc.message)
            return None
        except anthropic.APIConnectionError:
            logger.exception("無法連線到 Claude API")
            return None

        if response.stop_reason == "refusal":
            logger.warning("Claude 拒絕解析此訊息")
            return None

        raw = "".join(b.text for b in response.content if b.type == "text")
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            logger.error("Claude 回傳非 JSON：%r", raw[:200])
            return None

        if data.get("event") in (None, "NONE"):
            logger.info("非交易指令：%s", data.get("note"))
            return None
        return {k: v for k, v in data.items() if v is not None and k != "note"}
