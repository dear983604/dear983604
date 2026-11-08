"""
core/trade_manager.py — 交易狀態機（核心邏輯）

狀態轉移：IDLE → OPEN → PARTIAL_EXIT(x) → CLOSED（→ 回到 IDLE）

  on_entry(event)        建立 TradeRecord，狀態 OPEN，通知進場
  on_exit_partial(event) 新增出場批次、計算本批損益，狀態 PARTIAL_EXIT，通知分批出場
  on_exit_final(event)   新增最後批次、加總損益、狀態 CLOSED、存檔、更新索引、通知結算

事件來源（TradingView webhook、未來的 Telegram 指令 / Claude 解析）一律呼叫 handle_event()。
所有狀態變更都在 asyncio.Lock 內進行，避免兩個 alert 同時到達造成狀態錯亂。
未平倉交易每次變更都會寫入 data/trades/，程式重啟後可透過 restore() 接續。
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Any, Awaitable, Callable, Mapping

from bot import formatter
from core.journal import Journal
from core.trade_record import (
    ExitRecord,
    TradeRecord,
    TradeStatus,
    direction_sign,
    normalize_direction,
)

logger = logging.getLogger(__name__)

Notifier = Callable[[str], Awaitable[None]]
EventDict = Mapping[str, Any]

TIME_FORMAT = "%Y-%m-%d %H:%M"


class TradeEventError(ValueError):
    """事件格式錯誤或與目前狀態不符（例如沒有持倉卻收到出場）。"""


async def _log_notifier(message: str) -> None:
    """未設定 Telegram 時的預設通知：寫進 log。"""
    logger.info("[通知]\n%s", message)


# ── 欄位解析小工具 ─────────────────────────────────────
def _require(event: EventDict, key: str) -> Any:
    if key not in event or event[key] is None or event[key] == "":
        raise TradeEventError(f"缺少欄位：{key}")
    return event[key]


def _as_float(event: EventDict, key: str, *, required: bool = True) -> float | None:
    if not required and event.get(key) in (None, ""):
        return None
    value = _require(event, key)
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise TradeEventError(f"欄位 {key} 必須是數字：{value!r}") from exc


def _as_int(event: EventDict, key: str, *, required: bool = True) -> int | None:
    if not required and event.get(key) in (None, ""):
        return None
    value = _require(event, key)
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise TradeEventError(f"欄位 {key} 必須是整數：{value!r}") from exc
    if not number.is_integer():
        raise TradeEventError(f"欄位 {key} 必須是整數：{value!r}")
    return int(number)


def _parse_time(raw: Any) -> datetime | None:
    if not raw:
        return None
    for fmt in (TIME_FORMAT, "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M"):
        try:
            return datetime.strptime(str(raw).strip()[:19], fmt)
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(str(raw).strip().replace("Z", "+00:00"))
    except ValueError:
        return None


def _event_time(event: EventDict) -> str:
    """事件時間（統一為 YYYY-MM-DD HH:MM），沒帶或無法解析則用現在時間。"""
    parsed = _parse_time(event.get("time"))
    return (parsed or datetime.now()).strftime(TIME_FORMAT)


class TradeManager:
    """交易狀態機。一次只追蹤一筆未平倉交易。"""

    def __init__(
        self,
        journal: Journal,
        points_per_lot: float,
        notifier: Notifier | None = None,
    ) -> None:
        self.journal: Journal = journal
        self.points_per_lot: float = float(points_per_lot)
        self.notifier: Notifier = notifier or _log_notifier
        self.current: TradeRecord | None = None
        self._lock: asyncio.Lock = asyncio.Lock()

    # ── 狀態 ────────────────────────────────────────────
    @property
    def state(self) -> TradeStatus:
        if self.current is None or self.current.is_closed:
            return TradeStatus.IDLE
        return self.current.status

    def set_notifier(self, notifier: Notifier) -> None:
        self.notifier = notifier

    async def restore(self) -> None:
        """從 journal.json 復原未平倉交易（程式重啟時呼叫）。"""
        self.current = await self.journal.load_open_trade()
        if self.current:
            logger.info(
                "已復原未平倉交易 %s（剩餘 %d 口）",
                self.current.trade_id,
                self.current.remaining_qty,
            )

    async def _notify(self, message: str) -> None:
        try:
            await self.notifier(message)
        except Exception:  # 通知失敗不應影響交易記錄
            logger.exception("發送通知失敗")

    # ── 事件分派 ────────────────────────────────────────
    async def handle_event(self, event: EventDict) -> TradeRecord:
        """依 event 欄位分派到對應處理函式。格式錯誤拋出 TradeEventError。"""
        if not isinstance(event, Mapping):
            raise TradeEventError("事件必須是 JSON 物件")
        kind = str(event.get("event", "")).strip().upper()
        handlers: dict[str, Callable[[EventDict], Awaitable[TradeRecord]]] = {
            "ENTRY": self.on_entry,
            "EXIT_PARTIAL": self.on_exit_partial,
            "EXIT_FINAL": self.on_exit_final,
        }
        handler = handlers.get(kind)
        if handler is None:
            raise TradeEventError(f"未知的事件類型：{event.get('event')!r}")
        return await handler(event)

    # ── 進場 ────────────────────────────────────────────
    async def on_entry(self, event: EventDict) -> TradeRecord:
        async with self._lock:
            if self.state != TradeStatus.IDLE:
                assert self.current is not None
                raise TradeEventError(
                    f"已有未平倉交易 {self.current.trade_id}，請先出場再進場"
                )

            try:
                direction = normalize_direction(_require(event, "dir"))
            except ValueError as exc:
                raise TradeEventError(str(exc)) from exc
            qty = _as_int(event, "qty")
            price = _as_float(event, "price")
            stop = _as_float(event, "stop", required=False)
            assert qty is not None and price is not None
            if qty <= 0:
                raise TradeEventError("qty 必須大於 0")

            entry_time = _event_time(event)
            trade_id = await self.journal.next_trade_id(
                datetime.strptime(entry_time, TIME_FORMAT)
            )
            trade = TradeRecord(
                trade_id=trade_id,
                direction=direction,
                entry_price=price,
                initial_stop=stop,
                total_qty=qty,
                entry_time=entry_time,
                status=TradeStatus.OPEN,
            )
            await self.journal.save_trade(trade)
            self.current = trade
            logger.info("進場 %s %s %d口 @%s", trade_id, direction, qty, price)

        await self._notify(formatter.format_entry(trade))
        return trade

    # ── 出場共用邏輯 ────────────────────────────────────
    def _build_exit(self, trade: TradeRecord, event: EventDict, batch: int) -> ExitRecord:
        qty = _as_int(event, "qty")
        price = _as_float(event, "price")
        assert qty is not None and price is not None
        if qty <= 0:
            raise TradeEventError("qty 必須大於 0")
        if qty > trade.remaining_qty:
            logger.warning(
                "%s 出場口數 %d 大於剩餘 %d 口，仍照實記錄",
                trade.trade_id, qty, trade.remaining_qty,
            )

        # 優先採用 Pine Script 送來的 pnl_pts，沒有則自行計算
        pnl_pts = _as_float(event, "pnl_pts", required=False)
        if pnl_pts is None:
            pnl_pts = (price - trade.entry_price) * direction_sign(trade.direction)
        pnl_amount = pnl_pts * qty * self.points_per_lot

        return ExitRecord(
            batch=batch,
            qty=qty,
            price=price,
            reason=str(event.get("reason") or ""),
            pnl_pts=round(pnl_pts, 4),
            pnl_amount=round(pnl_amount, 2),
            time=_event_time(event),
        )

    def _require_open_trade(self) -> TradeRecord:
        if self.state == TradeStatus.IDLE or self.current is None:
            raise TradeEventError("目前沒有持倉，忽略出場事件")
        return self.current

    # ── 分批出場 ────────────────────────────────────────
    async def on_exit_partial(self, event: EventDict) -> TradeRecord:
        async with self._lock:
            trade = self._require_open_trade()
            batch = _as_int(event, "batch", required=False) or len(trade.exits) + 1
            exit_record = self._build_exit(trade, event, batch)

            trade.exits.append(exit_record)
            trade.recalc_totals()
            trade.status = TradeStatus.PARTIAL_EXIT

            reported = _as_int(event, "remaining", required=False)
            if reported is not None and reported != trade.remaining_qty:
                logger.warning(
                    "%s 剩餘口數不一致：alert=%d，計算=%d",
                    trade.trade_id, reported, trade.remaining_qty,
                )
            await self.journal.save_trade(trade)
            logger.info(
                "分批出場 %s 第%d批 %d口 @%s（%+.1f點）",
                trade.trade_id, batch, exit_record.qty, exit_record.price, exit_record.pnl_pts,
            )

        await self._notify(formatter.format_partial_exit(trade, exit_record))
        return trade

    # ── 最終出場 ────────────────────────────────────────
    async def on_exit_final(self, event: EventDict) -> TradeRecord:
        async with self._lock:
            trade = self._require_open_trade()
            batch = _as_int(event, "batch", required=False) or len(trade.exits) + 1
            exit_record = self._build_exit(trade, event, batch)

            trade.exits.append(exit_record)
            if trade.remaining_qty != 0:
                logger.warning(
                    "%s 最終出場後口數不平：進場 %d 口、出場 %d 口",
                    trade.trade_id, trade.total_qty, trade.exited_qty,
                )
            trade.recalc_totals()
            trade.status = TradeStatus.CLOSED
            trade.closed_time = exit_record.time or datetime.now().strftime(TIME_FORMAT)

            await self.journal.save_trade(trade)
            self.current = None
            logger.info(
                "結算 %s 總損益 %+.1f點 / %+.0f元",
                trade.trade_id, trade.total_pnl_pts, trade.total_pnl_amount,
            )

        await self._notify(formatter.format_settlement(trade))
        return trade
