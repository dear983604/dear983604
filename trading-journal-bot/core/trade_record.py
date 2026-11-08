"""
core/trade_record.py — 交易記錄資料結構

定義一筆交易的完整生命週期資料：
  TradeStatus：IDLE → OPEN → PARTIAL_EXIT → CLOSED
  ExitRecord ：單一批次出場記錄
  TradeRecord：一筆交易（進場資訊 + 多筆出場 + 結算）

所有結構都能與 JSON dict 互轉（to_dict / from_dict）。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any


class TradeStatus(str, Enum):
    """交易狀態。IDLE 僅用於表示「目前無持倉」，不會寫入交易檔。"""

    IDLE = "IDLE"
    OPEN = "OPEN"
    PARTIAL_EXIT = "PARTIAL_EXIT"
    CLOSED = "CLOSED"


LONG_ALIASES = {"做多", "多", "多單", "long", "buy", "LONG", "BUY"}
SHORT_ALIASES = {"做空", "空", "空單", "short", "sell", "SHORT", "SELL"}


def normalize_direction(raw: str) -> str:
    """把各種寫法統一成「做多」或「做空」。不認得則拋出 ValueError。"""
    value = str(raw).strip()
    if value in LONG_ALIASES or value.lower() in LONG_ALIASES:
        return "做多"
    if value in SHORT_ALIASES or value.lower() in SHORT_ALIASES:
        return "做空"
    raise ValueError(f"無法辨識的方向：{raw!r}")


def direction_sign(direction: str) -> int:
    """做多 = +1，做空 = -1。"""
    return 1 if normalize_direction(direction) == "做多" else -1


def direction_label(direction: str) -> str:
    """顯示用：做多 → 多單，做空 → 空單。"""
    return "多單" if normalize_direction(direction) == "做多" else "空單"


@dataclass
class ExitRecord:
    """單一批次的出場記錄。"""

    batch: int
    qty: int
    price: float
    reason: str
    pnl_pts: float          # 每口損益點數
    pnl_amount: float       # pnl_pts × qty × POINTS_PER_LOT
    time: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ExitRecord":
        return cls(
            batch=int(data["batch"]),
            qty=int(data["qty"]),
            price=float(data["price"]),
            reason=str(data.get("reason", "")),
            pnl_pts=float(data.get("pnl_pts", 0.0)),
            pnl_amount=float(data.get("pnl_amount", 0.0)),
            time=data.get("time"),
        )


@dataclass
class TradeRecord:
    """一筆交易的完整記錄。"""

    trade_id: str
    direction: str
    entry_price: float
    initial_stop: float | None
    total_qty: int
    entry_time: str
    status: TradeStatus = TradeStatus.OPEN
    exits: list[ExitRecord] = field(default_factory=list)
    total_pnl_pts: float = 0.0
    total_pnl_amount: float = 0.0
    closed_time: str | None = None

    # ── 計算屬性 ────────────────────────────────────────
    @property
    def exited_qty(self) -> int:
        return sum(e.qty for e in self.exits)

    @property
    def remaining_qty(self) -> int:
        return max(self.total_qty - self.exited_qty, 0)

    @property
    def risk_pts(self) -> float | None:
        """停損風險點數（負值），例如多單 18250 停損 18180 → -70。"""
        if self.initial_stop is None:
            return None
        return (self.initial_stop - self.entry_price) * direction_sign(self.direction)

    @property
    def is_closed(self) -> bool:
        return self.status == TradeStatus.CLOSED

    def recalc_totals(self) -> None:
        """重新加總所有批次損益。

        total_pnl_pts 為「點數 × 口數」的加總（每批 1 口時即為各批點數相加），
        total_pnl_amount 為各批金額加總。
        """
        self.total_pnl_pts = round(sum(e.pnl_pts * e.qty for e in self.exits), 4)
        self.total_pnl_amount = round(sum(e.pnl_amount for e in self.exits), 2)

    # ── 序列化 ──────────────────────────────────────────
    def to_dict(self) -> dict[str, Any]:
        return {
            "trade_id": self.trade_id,
            "direction": self.direction,
            "entry_price": self.entry_price,
            "initial_stop": self.initial_stop,
            "total_qty": self.total_qty,
            "entry_time": self.entry_time,
            "status": self.status.value,
            "exits": [e.to_dict() for e in self.exits],
            "total_pnl_pts": self.total_pnl_pts,
            "total_pnl_amount": self.total_pnl_amount,
            "closed_time": self.closed_time,
        }

    def to_summary(self) -> dict[str, Any]:
        """journal.json 索引用的精簡資訊。"""
        return {
            "trade_id": self.trade_id,
            "direction": self.direction,
            "entry_price": self.entry_price,
            "total_qty": self.total_qty,
            "entry_time": self.entry_time,
            "closed_time": self.closed_time,
            "status": self.status.value,
            "total_pnl_pts": self.total_pnl_pts,
            "total_pnl_amount": self.total_pnl_amount,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TradeRecord":
        stop = data.get("initial_stop")
        return cls(
            trade_id=str(data["trade_id"]),
            direction=str(data["direction"]),
            entry_price=float(data["entry_price"]),
            initial_stop=float(stop) if stop is not None else None,
            total_qty=int(data["total_qty"]),
            entry_time=str(data["entry_time"]),
            status=TradeStatus(data.get("status", TradeStatus.OPEN.value)),
            exits=[ExitRecord.from_dict(e) for e in data.get("exits", [])],
            total_pnl_pts=float(data.get("total_pnl_pts", 0.0)),
            total_pnl_amount=float(data.get("total_pnl_amount", 0.0)),
            closed_time=data.get("closed_time"),
        )
