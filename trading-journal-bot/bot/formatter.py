"""
bot/formatter.py — Telegram 訊息格式化

產生各種通知文字（Telegram HTML parse mode）：
  format_entry         ⚡ 進場通知
  format_partial_exit  📤 分批出場通知
  format_settlement    📊 交易結算框線表格（放在 <pre> 內以等寬字型顯示）
  format_status / format_today / format_history  指令回覆

結算表格會依中文全形字元（寬度 2）計算顯示寬度，讓框線在等寬字型下對齊。
"""

from __future__ import annotations

import html
import unicodedata
from typing import Any, Iterable

from core.trade_record import TradeRecord, direction_label, ExitRecord

BOX_MIN_WIDTH = 22      # 框線內部最小寬度（顯示欄位數）
REASON_MAX_WIDTH = 14   # 原因欄位最大顯示寬度，超過以「…」截斷


# ── 數字格式 ───────────────────────────────────────────
def fmt_price(value: float | None) -> str:
    """18250.0 → 18,250；18250.5 → 18,250.5"""
    if value is None:
        return "-"
    if float(value).is_integer():
        return f"{value:,.0f}"
    return f"{value:,.2f}".rstrip("0").rstrip(".")


def fmt_pts(value: float) -> str:
    """+170點 / -70點"""
    number = fmt_price(abs(value))
    sign = "+" if value > 0 else "-" if value < 0 else "±"
    return f"{sign}{number}點"


def fmt_amount(value: float) -> str:
    """+$8,500 / -$3,500"""
    sign = "+" if value > 0 else "-" if value < 0 else "±"
    return f"{sign}${abs(value):,.0f}"


def esc(text: Any) -> str:
    return html.escape(str(text), quote=False)


# ── 顯示寬度工具 ────────────────────────────────────────
def display_width(text: str) -> int:
    width = 0
    for ch in text:
        if unicodedata.combining(ch) or ch in "‍️":
            continue
        width += 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
    return width


def truncate(text: str, max_width: int) -> str:
    if display_width(text) <= max_width:
        return text
    out = ""
    for ch in text:
        if display_width(out + ch + "…") > max_width:
            break
        out += ch
    return out + "…"


def pad_right(text: str, width: int) -> str:
    return text + " " * max(width - display_width(text), 0)


def spread(left: str, right: str, width: int) -> str:
    """左右對齊：左邊文字靠左、右邊文字靠右。"""
    gap = max(width - display_width(left) - display_width(right), 1)
    return left + " " * gap + right


Row = str | tuple[str, str]   # 字串＝靠左；(左, 右)＝左右對齊


def _row_width(row: Row) -> int:
    if isinstance(row, tuple):
        return display_width(row[0]) + 1 + display_width(row[1])
    return display_width(row)


def render_box(sections: Iterable[list[Row]]) -> str:
    """把多個區塊畫成雙線框表格。每個區塊之間以 ╠═╣ 分隔。"""
    sections = [s for s in sections if s]
    inner = max(
        [BOX_MIN_WIDTH] + [_row_width(row) + 3 for sec in sections for row in sec]
    )
    top = "╔" + "═" * inner + "╗"
    sep = "╠" + "═" * inner + "╣"
    bottom = "╚" + "═" * inner + "╝"

    lines = [top]
    for i, sec in enumerate(sections):
        if i > 0:
            lines.append(sep)
        for row in sec:
            text = spread(row[0], row[1], inner - 2) if isinstance(row, tuple) else row
            lines.append("║ " + pad_right(text, inner - 1) + "║")
    lines.append(bottom)
    return "\n".join(lines)


# ── 通知訊息 ───────────────────────────────────────────
def format_entry(trade: TradeRecord) -> str:
    lines = [
        "⚡ <b>進場通知</b>",
        f"方向：{direction_label(trade.direction)}　口數：{trade.total_qty}口",
        f"進場價：{fmt_price(trade.entry_price)}",
    ]
    if trade.initial_stop is not None and trade.risk_pts is not None:
        lines.append(f"停損位：{fmt_price(trade.initial_stop)}（風險 {fmt_pts(trade.risk_pts)}）")
    else:
        lines.append("停損位：未設定")
    lines.append(f"時間：{esc(trade.entry_time)}")
    return "\n".join(lines)


def format_partial_exit(trade: TradeRecord, exit_record: ExitRecord) -> str:
    return "\n".join(
        [
            f"📤 <b>第{exit_record.batch}批出場（{exit_record.qty}口）</b>",
            f"原因：{esc(exit_record.reason or '-')}",
            f"出場價：{fmt_price(exit_record.price)}",
            f"本批損益：{fmt_pts(exit_record.pnl_pts)}（{fmt_amount(exit_record.pnl_amount)}）",
            f"剩餘持倉：{trade.remaining_qty}口",
        ]
    )


def settlement_table(trade: TradeRecord) -> str:
    """純文字結算表格（不含 HTML）。"""
    header = ["  📊 交易結算"]
    entry = [f"{direction_label(trade.direction)}  進場 {fmt_price(trade.entry_price)}"]
    batches: list[list[Row]] = []
    for e in trade.exits:
        reason = truncate(e.reason or "-", REASON_MAX_WIDTH)
        batches.append(
            [
                f"第{e.batch}批 {e.qty}口 @{fmt_price(e.price)}",
                (reason, fmt_pts(e.pnl_pts)),
            ]
        )
    total: list[Row] = [
        ("💰 總損益", fmt_pts(trade.total_pnl_pts)),
        ("   金額", fmt_amount(trade.total_pnl_amount)),
    ]
    return render_box([header, entry, *batches, total])


def format_settlement(trade: TradeRecord) -> str:
    footer = f"#{trade.trade_id}　{esc(trade.entry_time)} → {esc(trade.closed_time or '-')}"
    return f"<pre>{esc(settlement_table(trade))}</pre>\n{footer}"


# ── 指令回覆 ───────────────────────────────────────────
def format_status(trade: TradeRecord | None) -> str:
    if trade is None or trade.is_closed:
        return "😴 <b>目前狀態：IDLE</b>\n無持倉"
    lines = [
        f"📈 <b>目前狀態：{trade.status.value}</b>",
        f"交易編號：{trade.trade_id}",
        f"方向：{direction_label(trade.direction)}　進場口數：{trade.total_qty}口",
        f"進場價：{fmt_price(trade.entry_price)}",
        f"停損位：{fmt_price(trade.initial_stop)}",
        f"進場時間：{esc(trade.entry_time)}",
        f"剩餘持倉：{trade.remaining_qty}口",
    ]
    if trade.exits:
        lines.append(
            f"已實現：{fmt_pts(trade.total_pnl_pts)}（{fmt_amount(trade.total_pnl_amount)}）"
        )
        for e in trade.exits:
            lines.append(
                f"  • 第{e.batch}批 {e.qty}口 @{fmt_price(e.price)} {fmt_pts(e.pnl_pts)} {esc(e.reason)}"
            )
    return "\n".join(lines)


def format_today(date_label: str, trades: list[TradeRecord]) -> str:
    if not trades:
        return f"📅 <b>{esc(date_label)}</b>\n今日尚無已結算交易"
    total_pts = sum(t.total_pnl_pts for t in trades)
    total_amt = sum(t.total_pnl_amount for t in trades)
    wins = sum(1 for t in trades if t.total_pnl_amount > 0)
    lines = [f"📅 <b>{esc(date_label)} 今日交易彙總</b>"]
    for t in trades:
        icon = "🟢" if t.total_pnl_amount > 0 else "🔴" if t.total_pnl_amount < 0 else "⚪"
        lines.append(
            f"{icon} {t.trade_id} {direction_label(t.direction)} {t.total_qty}口 "
            f"@{fmt_price(t.entry_price)}　{fmt_pts(t.total_pnl_pts)}（{fmt_amount(t.total_pnl_amount)}）"
        )
    lines += [
        "",
        f"筆數：{len(trades)}　勝率：{wins / len(trades):.0%}",
        f"💰 合計：{fmt_pts(total_pts)}（{fmt_amount(total_amt)}）",
    ]
    return "\n".join(lines)


def format_history(summaries: list[dict[str, Any]]) -> str:
    if not summaries:
        return "📚 尚無交易記錄"
    lines = [f"📚 <b>最近 {len(summaries)} 筆交易</b>"]
    for s in summaries:
        amount = float(s.get("total_pnl_amount", 0.0))
        icon = "🟢" if amount > 0 else "🔴" if amount < 0 else "⚪"
        lines.append(
            f"{icon} {esc(s.get('trade_id'))} {direction_label(s.get('direction', '做多'))} "
            f"{s.get('total_qty')}口 @{fmt_price(float(s.get('entry_price', 0)))}　"
            f"{fmt_pts(float(s.get('total_pnl_pts', 0)))}（{fmt_amount(amount)}）"
        )
    return "\n".join(lines)
