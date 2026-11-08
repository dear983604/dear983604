"""
config.py — 集中管理所有設定

從 .env（或環境變數）讀取設定，提供唯一的 Settings 物件給其他模組使用。
必要欄位缺漏時於啟動階段直接報錯，避免執行到一半才失敗。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent

# 讀取專案目錄下的 .env；已存在的環境變數優先（方便雲端/容器部署）
load_dotenv(BASE_DIR / ".env", override=False)


class ConfigError(RuntimeError):
    """設定錯誤（缺少必要欄位或格式不符）。"""


def _get_int(name: str, default: int | None = None) -> int | None:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} 必須是整數，目前為：{raw!r}") from exc


def _get_float(name: str, default: float) -> float:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} 必須是數字，目前為：{raw!r}") from exc


@dataclass(frozen=True)
class Settings:
    """應用程式設定。"""

    telegram_token: str
    telegram_allowed_user_id: int | None
    anthropic_api_key: str
    claude_model: str
    webhook_host: str
    webhook_port: int
    webhook_secret: str
    points_per_lot: float
    data_dir: Path
    log_level: str

    @property
    def telegram_enabled(self) -> bool:
        """有 token 且有授權使用者時才啟動 Telegram Bot。"""
        return bool(self.telegram_token) and self.telegram_allowed_user_id is not None

    @property
    def ai_enabled(self) -> bool:
        return bool(self.anthropic_api_key)

    def validate(self) -> None:
        """檢查必要設定。webhook secret 為必填，避免任何人都能下單紀錄。"""
        if not self.webhook_secret:
            raise ConfigError("WEBHOOK_SECRET 未設定：請在 .env 填入一組長亂數字串")
        if self.telegram_token and self.telegram_allowed_user_id is None:
            raise ConfigError("已設定 TELEGRAM_TOKEN 但缺少 TELEGRAM_ALLOWED_USER_ID")
        if self.points_per_lot <= 0:
            raise ConfigError("POINTS_PER_LOT 必須大於 0")


def load_settings() -> Settings:
    """從環境變數建立 Settings。"""
    data_dir = Path(os.getenv("DATA_DIR", "data").strip() or "data")
    if not data_dir.is_absolute():
        data_dir = BASE_DIR / data_dir

    return Settings(
        telegram_token=os.getenv("TELEGRAM_TOKEN", "").strip(),
        telegram_allowed_user_id=_get_int("TELEGRAM_ALLOWED_USER_ID"),
        anthropic_api_key=os.getenv("ANTHROPIC_API_KEY", "").strip(),
        claude_model=os.getenv("CLAUDE_MODEL", "").strip() or "claude-opus-5-5",
        webhook_host=os.getenv("WEBHOOK_HOST", "0.0.0.0").strip() or "0.0.0.0",
        webhook_port=_get_int("WEBHOOK_PORT", 8000) or 8000,
        webhook_secret=os.getenv("WEBHOOK_SECRET", "").strip(),
        points_per_lot=_get_float("POINTS_PER_LOT", 200.0),
        data_dir=data_dir,
        log_level=os.getenv("LOG_LEVEL", "INFO").strip().upper() or "INFO",
    )
