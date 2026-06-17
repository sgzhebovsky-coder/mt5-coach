"""
MT5 Trade Coach
-----------------
Тянет историю закрытых сделок с твоего MT5-счёта через облачный MetaApi
(локальный терминал не нужен — работает даже если торгуешь только с мобильного MT5),
считает статистику поведения и шлёт письма на почту:

  - после КАЖДОЙ закрытой сделки     -> короткий разбор этой сделки
  - после каждой 5-й сделке          -> сводка по последним 5 сделкам
  - после каждой 10-й сделке         -> более глубокий разбор по последним 10 сделкам
    (психология, дисциплина, тильт)

Скрипт запускается периодически (см. .github/workflows/trade-review.yml) и хранит
состояние (что уже разобрано) в state.json, который коммитится обратно в репозиторий.
"""

import asyncio
import json
import os
import smtplib
from collections import defaultdict
import statistics
from datetime import datetime, timedelta, timezone
from email.mime.text import MIMEText
from pathlib import Path

from metaapi_cloud_sdk import MetaApi

STATE_PATH = Path(__file__).parent / "state.json"

METAAPI_TOKEN = os.environ["METAAPI_TOKEN"]
METAAPI_ACCOUNT_ID = os.environ["METAAPI_ACCOUNT_ID"]
GMAIL_ADDRESS = os.environ["GMAIL_ADDRESS"]
GMAIL_APP_PASSWORD = os.environ["GMAIL_APP_PASSWORD"]
RECIPIENT_EMAIL = os.environ.get("RECIPIENT_EMAIL", GMAIL_ADDRESS)

# На первом запуске не тащим всю историю с начала счёта, а берём только последние N дней
LOOKBACK_DAYS_ON_FIRST_RUN = 2

REASON_MAP = {
    "DEAL_REASON_SL": "сработал стоп-лосс",
    "DEAL_REASON_TP": "сработал тейк-профит",
    "DEAL_REASON_CLIENT": "закрыта вручную",
    "DEAL_REASON_MOBILE": "закрыта вручную (мобильное приложение)",
    "DEAL_REASON_WEB": "закрыта вручную (веб-терминал)",
