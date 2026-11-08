"""
core/journal.py — 交易日誌讀寫（JSON 檔案）

檔案配置：
  data/trades/{trade_id}.json  每筆交易一個檔，trade_id = YYYYMMDD_流水號
  data/journal.json            彙總索引：
      {
        "open_trade_id": "20261108_001" | null,   # 目前未平倉的交易（重啟時復原用）
        "trades": [ {summary...}, ... ]            # 已結算交易，依結算順序排列
      }

所有寫入都經過 asyncio.Lock() 序列化，並以「寫暫存檔 → os.replace」方式原子更新，
避免同時寫入或寫到一半當機造成 JSON 毀損。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Any

import aiofiles

from core.trade_record import TradeRecord, TradeStatus

logger = logging.getLogger(__name__)


class Journal:
    """交易日誌存取層。"""

    def __init__(self, data_dir: Path) -> None:
        self.data_dir = Path(data_dir)
        self.trades_dir = self.data_dir / "trades"
        self.index_path = self.data_dir / "journal.json"
        self._lock = asyncio.Lock()
        self.trades_dir.mkdir(parents=True, exist_ok=True)

    # ── 低階 JSON 讀寫 ───────────────────────────────────
    @staticmethod
    async def _read_json(path: Path, default: Any) -> Any:
        if not path.exists():
            return default
        try:
            async with aiofiles.open(path, "r", encoding="utf-8") as f:
                content = await f.read()
            return json.loads(content) if content.strip() else default
        except (OSError, json.JSONDecodeError):
            logger.exception("讀取 %s 失敗，改用預設值", path)
            return default

    @staticmethod
    async def _write_json(path: Path, data: Any) -> None:
        tmp_path = path.with_suffix(path.suffix + ".tmp")
        async with aiofiles.open(tmp_path, "w", encoding="utf-8") as f:
            await f.write(json.dumps(data, ensure_ascii=False, indent=2))
        os.replace(tmp_path, path)

    async def _read_index(self) -> dict[str, Any]:
        index = await self._read_json(self.index_path, {})
        if not isinstance(index, dict):
            index = {}
        index.setdefault("open_trade_id", None)
        index.setdefault("trades", [])
        return index

    # ── trade_id ────────────────────────────────────────
    async def next_trade_id(self, date: datetime) -> str:
        """產生當日下一個流水號，例如 20261108_001。"""
        async with self._lock:
            prefix = date.strftime("%Y%m%d")
            seqs = []
            for p in self.trades_dir.glob(f"{prefix}_*.json"):
                try:
                    seqs.append(int(p.stem.split("_", 1)[1]))
                except (IndexError, ValueError):
                    continue
            return f"{prefix}_{max(seqs, default=0) + 1:03d}"

    # ── 寫入 ────────────────────────────────────────────
    async def save_trade(self, trade: TradeRecord) -> None:
        """寫入（或覆寫）單筆交易檔，並同步更新索引。

        - 未平倉：記錄為 open_trade_id（方便重啟後復原）
        - 已結算：加入/更新 trades 索引並清除 open_trade_id
        """
        async with self._lock:
            await self._write_json(self.trades_dir / f"{trade.trade_id}.json", trade.to_dict())

            index = await self._read_index()
            if trade.status == TradeStatus.CLOSED:
                index["trades"] = [t for t in index["trades"] if t.get("trade_id") != trade.trade_id]
                index["trades"].append(trade.to_summary())
                if index["open_trade_id"] == trade.trade_id:
                    index["open_trade_id"] = None
            else:
                index["open_trade_id"] = trade.trade_id
            await self._write_json(self.index_path, index)

    async def clear_open_trade(self) -> None:
        async with self._lock:
            index = await self._read_index()
            index["open_trade_id"] = None
            await self._write_json(self.index_path, index)

    # ── 讀取 ────────────────────────────────────────────
    async def load_trade(self, trade_id: str) -> TradeRecord | None:
        data = await self._read_json(self.trades_dir / f"{trade_id}.json", None)
        if not data:
            return None
        try:
            return TradeRecord.from_dict(data)
        except (KeyError, ValueError, TypeError):
            logger.exception("交易檔 %s 格式錯誤", trade_id)
            return None

    async def load_open_trade(self) -> TradeRecord | None:
        index = await self._read_index()
        trade_id = index.get("open_trade_id")
        if not trade_id:
            return None
        trade = await self.load_trade(trade_id)
        if trade and not trade.is_closed:
            return trade
        return None

    async def list_summaries(self) -> list[dict[str, Any]]:
        """所有已結算交易的索引（依結算順序）。"""
        index = await self._read_index()
        return list(index["trades"])

    async def recent(self, n: int = 5) -> list[dict[str, Any]]:
        summaries = await self.list_summaries()
        return list(reversed(summaries[-n:])) if n > 0 else []

    async def last_trade(self) -> TradeRecord | None:
        summaries = await self.list_summaries()
        if not summaries:
            return None
        return await self.load_trade(summaries[-1]["trade_id"])

    async def trades_on(self, date: datetime) -> list[TradeRecord]:
        """指定日期（依 trade_id 日期）的已結算交易。"""
        prefix = date.strftime("%Y%m%d")
        result: list[TradeRecord] = []
        for s in await self.list_summaries():
            if str(s.get("trade_id", "")).startswith(prefix):
                trade = await self.load_trade(s["trade_id"])
                if trade:
                    result.append(trade)
        return result
