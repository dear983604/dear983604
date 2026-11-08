#!/bin/bash
# 同時啟動 Telegram Bot 和 Webhook Server
# 若專案下有 .venv 則使用其 Python（python-telegram-bot 20.7 需 Python 3.11 / 3.12）
cd "$(dirname "$0")"
if [ -x .venv/bin/python ]; then
  exec .venv/bin/python main.py
fi
python main.py
