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
import tempfile
from collections import defaultdict
import statistics
from datetime import datetime, timedelta, timezone
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.image import MIMEImage
from pathlib import Path

import matplotlib
matplotlib.use("Agg")  # без графического дисплея — нужно для запуска в GitHub Actions
import matplotlib.pyplot as plt

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
    "DEAL_REASON_EXPERT": "закрыта советником/скриптом",
    "DEAL_REASON_SO": "закрыта по стоп-ауту (маржин-колл)",
}


# ---------------------------------------------------------------- состояние

def load_state():
    if STATE_PATH.exists():
        state = json.loads(STATE_PATH.read_text())
        state.setdefault("equity_curve", [])  # на случай старого state.json без этого поля
        return state
    return {
        "last_check_time": None,
        "seen_deal_ids": [],
        "total_closed_trades": 0,
        "recent_trades": [],  # последние до 10 закрытых сделок
        "equity_curve": [],  # история баланса по каждой закрытой сделке с самого начала наблюдений
    }


def save_state(state):
    state["seen_deal_ids"] = state["seen_deal_ids"][-500:]
    state["recent_trades"] = state["recent_trades"][-10:]
    state["equity_curve"] = state["equity_curve"][-500:]  # ограничиваем размер state.json
    STATE_PATH.write_text(json.dumps(state, indent=2, ensure_ascii=False, default=str))


# ---------------------------------------------------------------- почта

def send_email(subject, body):
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = GMAIL_ADDRESS
    msg["To"] = RECIPIENT_EMAIL
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
        server.sendmail(GMAIL_ADDRESS, [RECIPIENT_EMAIL], msg.as_string())
    print(f"[email] отправлено: {subject}")


def send_email_with_chart(subject, body, image_path):
    """Письмо с текстом + прикреплённым графиком (картинка)."""
    msg = MIMEMultipart()
    msg["Subject"] = subject
    msg["From"] = GMAIL_ADDRESS
    msg["To"] = RECIPIENT_EMAIL
    msg.attach(MIMEText(body, "plain", "utf-8"))
    if image_path and os.path.exists(image_path):
        with open(image_path, "rb") as f:
            img = MIMEImage(f.read(), name="equity_curve.png")
        msg.attach(img)
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
        server.sendmail(GMAIL_ADDRESS, [RECIPIENT_EMAIL], msg.as_string())
    print(f"[email] отправлено (с графиком): {subject}")


# ---------------------------------------------------------------- форматирование

def fmt_money(x):
    if x is None:
        return "н/д"
    sign = "+" if x > 0 else ("-" if x < 0 else "")
    return f"{sign}${abs(x):,.2f}".replace(",", " ")


def fmt_duration(seconds):
    if seconds is None:
        return "н/д"
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, _ = divmod(rem, 60)
    if h:
        return f"{h} ч {m} мин"
    return f"{m} мин"


def fmt_price(x):
    return "н/д" if x is None else x


def to_iso(x):
    """Превращает значение времени в строку (MetaApi может вернуть datetime-объект напрямую,
    а не строку — такой объект нельзя сохранить в json без этой нормализации)."""
    if x is None:
        return None
    if hasattr(x, "isoformat"):
        return x.isoformat()
    return str(x)


def parse_dt(s):
    if not s:
        return None
    if isinstance(s, datetime):
        return s
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return None


# ---------------------------------------------------------------- обогащение данных по сделке

async def get_symbol_risk_per_unit(connection, symbol):
    """Возвращает (tick_value, tick_size) либо (None, None), если не удалось получить."""
    try:
        spec = await connection.get_symbol_specification(symbol)
        return spec.get("tickValue"), spec.get("tickSize")
    except Exception as e:
        print(f"[warn] не удалось получить специфику символа {symbol}: {e}")
        return None, None


async def enrich_trade(connection, deal, all_deals):
    """По закрывающей сделке (deal) восстанавливает контекст: SL/TP/время входа и т.д.

    all_deals — полный список сделок за период (а не только закрывающих), чтобы найти
    сделку открытия позиции (entryType == DEAL_ENTRY_IN) — это надёжнее, чем запрашивать
    отдельно историю ордеров, и не требует лишнего запроса к API.
    """
    info = {
        "position_id": deal.get("positionId"),
        "symbol": deal.get("symbol"),
        "side": "BUY" if deal.get("type") == "DEAL_TYPE_SELL" else "SELL",
        # закрывающая сделка всегда противоположного типа исходной позиции
        "close_time": to_iso(deal.get("time")),
        "close_price": deal.get("price"),
        "profit": (deal.get("profit") or 0) + (deal.get("commission") or 0) + (deal.get("swap") or 0),
        "gross_profit": deal.get("profit") or 0,
        "costs": (deal.get("commission") or 0) + (deal.get("swap") or 0),
        "volume": deal.get("volume"),
        "reason": deal.get("reason"),
        "balance_after": deal.get("balance"),
        "open_time": None,
        "open_price": None,
        "stop_loss": None,
        "take_profit": None,
        "sl_known": False,  # удалось ли вообще получить данные об ордере входа (а не просто "стопа нет")
        "risk_amount": None,
        "r_multiple": None,
        "risk_pct_of_balance": None,
        "holding_seconds": None,
    }

    # 1) Цена/время входа — сначала пробуем найти прямо среди уже скачанных сделок
    entry_deal = next(
        (d for d in all_deals if d.get("positionId") == info["position_id"] and d.get("entryType") == "DEAL_ENTRY_IN"),
        None,
    )
    if entry_deal:
        info["open_time"] = to_iso(entry_deal.get("time"))
        info["open_price"] = entry_deal.get("price")

    # 2) SL/TP можно получить только из истории ордеров (в сделках их нет)
    try:
        orders = await connection.get_history_orders_by_position(position_id=str(info["position_id"]))
        if isinstance(orders, dict):
            orders = orders.get("historyOrders", [])
        entry_orders = [o for o in orders if o.get("type") in ("ORDER_TYPE_BUY", "ORDER_TYPE_SELL")]
        if entry_orders:
            entry_orders.sort(key=lambda o: o.get("time", ""))
            first = entry_orders[0]
            info["sl_known"] = True
            info["stop_loss"] = first.get("stopLoss") or None
            info["take_profit"] = first.get("takeProfit") or None
            # если из сделок цену входа не нашли — берём из ордера как запасной вариант
            if info["open_price"] is None:
                info["open_time"] = info["open_time"] or to_iso(first.get("time"))
                info["open_price"] = first.get("openPrice") or first.get("price")
    except Exception as e:
        print(f"[warn] не удалось получить ордера по позиции {info['position_id']}: {e}")

    if info["stop_loss"] and info["open_price"]:
        tick_value, tick_size = await get_symbol_risk_per_unit(connection, info["symbol"])
        if tick_value and tick_size:
            distance = abs(info["open_price"] - info["stop_loss"])
            risk_amount = (distance / tick_size) * tick_value * (info["volume"] or 0)
            info["risk_amount"] = risk_amount
            if risk_amount:
                info["r_multiple"] = info["profit"] / risk_amount

    if info["balance_after"] is not None:
        balance_before = info["balance_after"] - info["profit"]
        if info["risk_amount"] and balance_before:
            info["risk_pct_of_balance"] = 100 * info["risk_amount"] / balance_before

    t0, t1 = parse_dt(info["open_time"]), parse_dt(info["close_time"])
    if t0 and t1:
        info["holding_seconds"] = (t1 - t0).total_seconds()

    return info


# ---------------------------------------------------------------- поведенческие паттерны

def max_loss_streak(trades):
    streak = best = 0
    for t in trades:
        if t["profit"] <= 0:
            streak += 1
            best = max(best, streak)
        else:
            streak = 0
    return best


def detect_lot_increase_after_loss(trades):
    for prev, nxt in zip(trades, trades[1:]):
        if prev["profit"] <= 0 and prev["volume"] and nxt["volume"] and nxt["volume"] > prev["volume"] * 1.3:
            return True
    return False


def detect_fast_reentry_after_loss(trades, threshold_minutes=5):
    for prev, nxt in zip(trades, trades[1:]):
        if prev["profit"] <= 0:
            t_close, t_open = parse_dt(prev["close_time"]), parse_dt(nxt["open_time"])
            if t_close and t_open and 0 <= (t_open - t_close).total_seconds() < threshold_minutes * 60:
                return True
    return False


def hour_breakdown(trades):
    by_hour = {}
    for t in trades:
        dt = parse_dt(t["open_time"])
        if not dt:
            continue
        by_hour.setdefault(dt.hour, []).append(t["profit"])
    if not by_hour:
        return None, None
    avg_by_hour = {h: sum(v) / len(v) for h, v in by_hour.items()}
    return max(avg_by_hour, key=avg_by_hour.get), min(avg_by_hour, key=avg_by_hour.get)


def holding_time_insight(trades):
    """Делает вывод из сравнения времени удержания прибыльных и убыточных сделок —
    классический паттерн 'режу прибыль быстро, даю убытку расти' виден именно отсюда."""
    win_holds = [t["holding_seconds"] for t in trades if t["profit"] > 0 and t["holding_seconds"] is not None]
    loss_holds = [t["holding_seconds"] for t in trades if t["profit"] <= 0 and t["holding_seconds"] is not None]
    if not win_holds or not loss_holds:
        return None
    avg_win = statistics.mean(win_holds)
    avg_loss = statistics.mean(loss_holds)
    if avg_loss > avg_win * 1.5 and avg_loss - avg_win > 60:
        return (
            f"⏱️ Убыточные сделки ты держишь в среднем дольше прибыльных ({fmt_duration(avg_loss)} против "
            f"{fmt_duration(avg_win)}). Это распространённый паттерн — надеяться, что минус развернётся, и "
            "при этом быстро фиксировать прибыль из страха её потерять. На практике это обычно работает в "
            "обратную сторону. Следующий шаг — относиться к выходу из убытка и из прибыли одинаково "
            "дисциплинированно, например через заранее выставленный стоп и тейк."
        )
    if avg_win > avg_loss * 1.5 and avg_win - avg_loss > 60:
        return (
            f"⏱️ Прибыльные сделки ты держишь дольше убыточных ({fmt_duration(avg_win)} против "
            f"{fmt_duration(avg_loss)}) — это здоровый паттерн: убытки режутся быстро, прибыли дают расти. "
            "Это именно то, что отличает прибыльных трейдеров от убыточных в долгую — продолжай в том же духе."
        )
    return None


# ---------------------------------------------------------------- график кривой эквити

def build_equity_chart(equity_curve, highlight_last=10):
    """Строит кривую баланса с начала наблюдений, выделяя последние highlight_last сделок
    другим цветом. Возвращает путь к сохранённому png-файлу либо None, если точек мало."""
    if len(equity_curve) < 2:
        return None

    xs = [p["n"] for p in equity_curve]
    ys = [p["balance"] for p in equity_curve]

    fig, ax = plt.subplots(figsize=(8, 4))
    ax.plot(xs, ys, color="#9aa0a6", linewidth=1.5, label="С начала наблюдений")

    if len(equity_curve) > highlight_last:
        hl_xs = [xs[-highlight_last - 1]] + xs[-highlight_last:]
        hl_ys = [ys[-highlight_last - 1]] + ys[-highlight_last:]
    else:
        hl_xs, hl_ys = xs, ys
    ax.plot(hl_xs, hl_ys, color="#e63946", linewidth=2.5, label=f"Последние {highlight_last} сделок")

    ax.set_xlabel("Номер сделки")
    ax.set_ylabel("Баланс, $")
    ax.set_title("Кривая баланса с начала наблюдений")
    ax.legend(loc="best", fontsize=9)
    ax.grid(alpha=0.3)
    fig.tight_layout()

    path = os.path.join(tempfile.gettempdir(), "equity_curve.png")
    fig.savefig(path, dpi=130)
    plt.close(fig)
    return path


# ---------------------------------------------------------------- коучинговые оценки сделки
#
# Идея: всегда отдельно оценивать ПРОЦЕСС (риск-менеджмент, дисциплина) и РЕЗУЛЬТАТ (профит/лосс).
# Хороший процесс хвалим независимо от результата. Плохой процесс мягко подсвечиваем и даём
# конкретный следующий шаг — даже если сделка вышла в плюс (повезло — не значит правильно).
# На минусе всегда подбадриваем и переключаем фокус на то, что можно улучшить.

RISK_COMFORT_PCT = 2.0  # ориентир здорового риска на сделку, %


def assess_stop_loss(trade):
    if trade["stop_loss"]:
        return "✅ Стоп-лосс был выставлен на входе — это база здорового риск-менеджмента, продолжай в том же духе."
    if not trade["sl_known"]:
        return (
            "ℹ️ Не удалось получить данные о стоп-лоссе по этой сделке (брокер не вернул историю ордера входа) — "
            "так что тут без оценки. На будущее в любом случае держи привычку выставлять SL сразу при входе."
        )
    return (
        "⚠️ Стоп-лосс на входе не стоял. Это главное, что стоит подтянуть: фиксированный стоп "
        "защищает от того, что один неудачный вход перерастёт в серьёзный убыток. Следующий шаг — "
        "выставлять SL сразу при открытии позиции, до того как смотреть, куда пойдёт цена дальше."
    )


def assess_risk_size(trade):
    if trade["risk_pct_of_balance"] is None:
        return None
    pct = trade["risk_pct_of_balance"]
    if pct <= RISK_COMFORT_PCT:
        return f"✅ Риск на сделку — {pct:.1f}% от баланса, в пределах здорового ориентира (обычно это 1–2%)."
    return (
        f"⚠️ Риск на сделку — {pct:.1f}% от баланса, выше типичного ориентира в 1–2%. "
        "Крупный риск на одну сделку — частая причина эмоциональных решений сразу после неё. "
        "Следующий шаг — попробовать уменьшить лот, чтобы каждая отдельная сделка значила меньше."
    )


def assess_close_reason(trade):
    manual = trade["reason"] in ("DEAL_REASON_CLIENT", "DEAL_REASON_MOBILE", "DEAL_REASON_WEB")
    if not manual:
        return "✅ Сделка закрылась по плану (сработал стоп или тейк) — значит план был выставлен и ты ему не мешал."
    if trade["stop_loss"]:
        # стоп точно был выставлен — значит закрытие вручную произошло раньше, чем сработал бы он
        if trade["profit"] <= 0:
            return (
                "💡 Закрыта вручную раньше срабатывания стопа. Если решение было осознанным — окей. "
                "Если это была эмоция во время просадки — следующий шаг — довериться заранее выставленному "
                "стопу и не закрывать вручную в моменте, это снимает часть тревоги."
            )
        return (
            "💡 Закрыта вручную в плюсе. Если цель была достигнута по плану — отлично. Если просто стало "
            "страшно отдавать прибыль — стоит подумать, не срезаешь ли ты потенциал у хороших сделок."
        )
    # стопа точно не было — формулировка без упоминания несуществующего стопа
    if trade["sl_known"] and trade["profit"] <= 0:
        return (
            "💡 Закрыта вручную. Без выставленного стопа решение о том, когда выйти из убытка, целиком "
            "держалось на тебе в моменте — стоп заранее снял бы с тебя эту нагрузку и сам определил границу."
        )
    if trade["sl_known"]:
        return (
            "💡 Закрыта вручную в плюсе. Если цель была достигнута по плану — отлично. Если решение было на "
            "эмоциях — стоит подумать, не срезаешь ли ты потенциал у хороших сделок."
        )
    # неизвестно, был ли стоп — просто фиксируем факт ручного закрытия без предположений
    if trade["profit"] <= 0:
        return (
            "💡 Закрыта вручную. Сложно сказать, был ли при этом план на стоп — данных по ордеру входа нет. "
            "Если решение было осознанным, всё ок; если на эмоциях — стоит обратить на это внимание."
        )
    return (
        "💡 Закрыта вручную в плюсе. Если это было по плану — отлично, если на эмоциях из страха потерять "
        "прибыль — стоит подумать над этим к следующей сделке."
    )


def assess_cost_impact(trade):
    """Явно подсвечивает случай, когда цена реально пошла в нужную сторону (вход был верным по направлению),
    но итоговый результат всё равно отрицательный — съели комиссия/своп."""
    if trade["profit"] <= 0 and trade["gross_profit"] > 0:
        return (
            f"📌 Важный момент: по цене сделка была в плюсе ({fmt_money(trade['gross_profit'])}) — направление "
            f"входа было верным. Но итог всё равно минус, потому что комиссия/своп ({fmt_money(trade['costs'])}) "
            "оказались больше, чем заработала сама цена. Это не ошибка в анализе рынка — это сигнал, что цель "
            "по этой сделке была слишком маленькой относительно издержек. Стоит либо целиться на более крупное "
            "движение, либо заранее прикидывать, покроет ли потенциальный профит комиссию на этом инструменте."
        )
    return None


def overall_takeaway(trade, process_ok):
    if trade["profit"] > 0 and process_ok:
        return "Итог: сделка в плюсе и сделана правильно — вот на такие сделки и стоит ориентироваться дальше."
    if trade["profit"] > 0 and not process_ok:
        return (
            "Итог: профит — это приятно, но он не говорит о том, что вход был правильным. "
            "Без стопа или с большим риском один раз может не повезти куда сильнее, чем сейчас повезло. "
            "Возьми в следующую сделку процесс из пункта выше, а не только этот результат."
        )
    if trade["profit"] <= 0 and process_ok:
        return (
            "Итог: минус есть, но процесс был правильным — риск был ограничен и план соблюдён. "
            "Убыток в рамках плана — это часть статистики стратегии, а не ошибка. Не зацикливайся "
            "на этой сделке, дальше показательнее будет картина по 5 и 10 сделкам."
        )
    return (
        "Итог: минус — это нормальная часть торговли, даже у лучших трейдеров. Не вини себя за результат, "
        "но возьми с собой один конкретный фокус на следующую сделку — он написан выше."
    )


def symbol_breakdown(trades):
    stats = defaultdict(lambda: {"pl": 0.0, "count": 0})
    for t in trades:
        s = t["symbol"] or "н/д"
        stats[s]["pl"] += t["profit"]
        stats[s]["count"] += 1
    return stats


def instrument_recommendation(stats, any_profitable):
    if len(stats) <= 1:
        return (
            "✅ Весь период отработан на одном инструменте — такая концентрация помогает глубже "
            "прочувствовать его характер и не распыляться. Хорошая практика, можно продолжать так же."
        )
    best = max(stats, key=lambda s: stats[s]["pl"])
    worst = min(stats, key=lambda s: stats[s]["pl"])
    if not any_profitable:
        return (
            f"Ни один инструмент в этом периоде не вышел в плюс — это говорит не про конкретную пару, "
            f"а скорее про общий подход в этом блоке сделок. {worst} принёс наибольший убыток "
            f"({fmt_money(stats[worst]['pl'])} за {stats[worst]['count']} сделок) — стоит присмотреться к нему "
            "отдельно. Но прежде чем искать 'удачный' инструмент, иногда полезнее сократить число пар, "
            "которыми торгуешь одновременно, и навести порядок в процессе — это обычно даёт больше, "
            "чем перебор инструментов."
        )
    msg = (
        f"💡 {best} принёс больше всего прибыли в этом периоде ({fmt_money(stats[best]['pl'])} "
        f"за {stats[best]['count']} сделок) — похоже, этот инструмент сейчас тебе подходит лучше остальных. "
        "Стоит попробовать уделить ему чуть больше внимания в следующем периоде и не распыляться сразу "
        "на много разных пар — фокус обычно даёт более стабильный результат."
    )
    if worst != best and stats[worst]["pl"] < 0:
        msg += (
            f" {worst} пока тянет результат вниз ({fmt_money(stats[worst]['pl'])} за {stats[worst]['count']} "
            "сделок) — не страшно, но это сигнал присмотреться, насколько он тебе сейчас подходит."
        )
    return msg


# ---------------------------------------------------------------- тексты писем

def build_single_email(trade, n):
    win = trade["profit"] > 0
    opener = (
        f"Сделка №{n} закрыта в плюс: {trade['symbol']} {trade['side']} 🎉"
        if win else
        f"Сделка №{n} закрыта в минус: {trade['symbol']} {trade['side']}. Разберём, что взять из неё:"
    )

    lines = [
        opener,
        "",
        f"Результат: {fmt_money(trade['profit'])}",
    ]
    if trade["costs"]:
        lines.append(
            f"  (из них по цене: {fmt_money(trade['gross_profit'])}, комиссия/своп: {fmt_money(trade['costs'])})"
        )
    lines += [
        f"Лот: {trade['volume']}",
        f"Цена входа: {fmt_price(trade['open_price'])}   Цена выхода: {fmt_price(trade['close_price'])}",
        f"Время в позиции: {fmt_duration(trade['holding_seconds'])}",
    ]
    if trade["r_multiple"] is not None:
        lines.append(f"Результат в R: {trade['r_multiple']:.2f}R")

    sl_note = assess_stop_loss(trade)
    risk_note = assess_risk_size(trade)
    reason_note = assess_close_reason(trade)
    cost_note = assess_cost_impact(trade)
    process_ok = trade["stop_loss"] is not None and "⚠️" not in (risk_note or "") and "💡" not in reason_note

    lines += ["", "Разбор процесса:", sl_note]
    if risk_note:
        lines.append(risk_note)
    lines.append(reason_note)
    if cost_note:
        lines.append(cost_note)

    lines += ["", overall_takeaway(trade, process_ok)]
    return "\n".join(lines)


def build_summary_email(trades, n, depth):
    wins = [t for t in trades if t["profit"] > 0]
    winrate = 100 * len(wins) / len(trades) if trades else 0
    total_pl = sum(t["profit"] for t in trades)
    r_values = [t["r_multiple"] for t in trades if t["r_multiple"] is not None]
    risk_pcts = [t["risk_pct_of_balance"] for t in trades if t["risk_pct_of_balance"] is not None]
    holds = [t["holding_seconds"] for t in trades if t["holding_seconds"] is not None]
    no_sl_count = sum(1 for t in trades if not t["stop_loss"])
    manual_closes = sum(1 for t in trades if t["reason"] in ("DEAL_REASON_CLIENT", "DEAL_REASON_MOBILE", "DEAL_REASON_WEB"))

    symbols = symbol_breakdown(trades)
    all_losing = winrate == 0 and total_pl <= 0

    if total_pl > 0 and winrate >= 50:
        opener = f"Сводка по последним {depth} сделкам — блок прошёл сильно, и винрейт, и итог в плюсе. Закрепим, что сработало:"
    elif all_losing:
        focus_points = []
        if no_sl_count > 0:
            focus_points.append("выставлять стоп-лосс на каждый вход без исключений")
        if manual_closes >= len(trades) / 2:
            focus_points.append("дожидаться срабатывания SL/TP вместо закрытия вручную")
        if len(symbols) >= 4:
            focus_points.append("сократить число одновременно торгуемых инструментов")
        if not focus_points:
            focus_points.append("не увеличивать риск после убыточных сделок и сохранять текущий размер позиции")
        opener = (
            f"Сводка по последним {depth} сделкам — все сделки в этом блоке закрылись в минус. Такое бывает "
            "даже у опытных трейдеров, и сам по себе минус ничего плохого не говорит о тебе как о трейдере. "
            f"Раз весь блок отрицательный — есть смысл не множить попытки, а сфокусироваться на одном "
            f"конкретном: {'; '.join(focus_points)}. Это про процесс, который полностью в твоих руках, "
            "а не про поиск 'удачного' инструмента."
        )
    elif total_pl <= 0:
        opener = (
            f"Сводка по последним {depth} сделкам — итог блока в минусе, но это не повод опускать руки: "
            "статистика по стратегии выравнивается на больших числах, важнее процесс ниже."
        )
    else:
        opener = f"Сводка по последним {depth} сделкам (всего закрыто сделок: {n}):"

    lines = [
        opener,
        "",
        f"Винрейт: {winrate:.0f}% ({len(wins)} из {len(trades)})",
        f"Суммарный финрезультат: {fmt_money(total_pl)}",
    ]
    if r_values:
        lines.append(f"Средний результат: {statistics.mean(r_values):.2f}R")
    if risk_pcts:
        lines.append(
            f"Средний риск на сделку: {statistics.mean(risk_pcts):.1f}% от баланса "
            f"(от {min(risk_pcts):.1f}% до {max(risk_pcts):.1f}%)"
        )
    if holds:
        lines.append(f"Среднее время в позиции: {fmt_duration(statistics.mean(holds))}")
    hold_insight = holding_time_insight(trades)
    if hold_insight:
        lines.append(hold_insight)

    lines.append("")
    lines.append("Инструменты:")
    lines.append(f"Торговал {len(symbols)} инструмент(ов) за период: {', '.join(symbols.keys())}")
    best_symbol = max(symbols, key=lambda s: symbols[s]["pl"])
    worst_symbol = min(symbols, key=lambda s: symbols[s]["pl"])
    any_profitable = symbols[best_symbol]["pl"] > 0
    if any_profitable:
        lines.append(
            f"🏆 Самый прибыльный: {best_symbol} ({fmt_money(symbols[best_symbol]['pl'])} "
            f"за {symbols[best_symbol]['count']} сделок)"
        )
        if worst_symbol != best_symbol:
            lines.append(
                f"Самый убыточный: {worst_symbol} ({fmt_money(symbols[worst_symbol]['pl'])} "
                f"за {symbols[worst_symbol]['count']} сделок)"
            )
    else:
        lines.append(
            f"Ни один инструмент не вышел в плюс. Наименьший убыток: {best_symbol} "
            f"({fmt_money(symbols[best_symbol]['pl'])} за {symbols[best_symbol]['count']} сделок), "
            f"наибольший: {worst_symbol} ({fmt_money(symbols[worst_symbol]['pl'])} за "
            f"{symbols[worst_symbol]['count']} сделок)."
        )
    lines.append(instrument_recommendation(symbols, any_profitable))

    lines.append("")
    lines.append("Дисциплина:")
    if no_sl_count == 0:
        lines.append(f"✅ Стоп-лосс стоял на всех {len(trades)} сделках — это сильная база, держи так и дальше.")
    else:
        lines.append(
            f"⚠️ {no_sl_count} из {len(trades)} сделок были без стоп-лосса. Следующий шаг — сделать выставление "
            "SL обязательным действием при входе, без исключений, даже когда кажется, что 'тут точно сработает'."
        )
    if manual_closes == 0:
        lines.append("✅ Все сделки закрылись по плану (по стопу или тейку) — план соблюдался, и это видно.")
    else:
        lines.append(
            f"💡 {manual_closes} из {len(trades)} сделок закрыты вручную, не по SL/TP. Если это были осознанные "
            "решения по плану — нормально. Если на эмоциях — стоит обратить внимание именно на это к следующему отчёту."
        )

    cost_eaten_count = sum(1 for t in trades if t["profit"] <= 0 and t["gross_profit"] > 0)
    if cost_eaten_count:
        lines.append(
            f"📌 В {cost_eaten_count} из {len(trades)} сделок цена шла в твою сторону, но итог всё равно был в "
            "минусе из-за комиссии/спреда. Направление входа было верным — дело в том, что цель по прибыли "
            "была меньше, чем издержки на сделку. Стоит либо целиться на более крупное движение, либо заранее "
            "прикидывать, оправдывает ли потенциальный профит расходы на конкретном инструменте."
        )

    if depth == "10":
        lines.append("")
        lines.append("Психология (по 10 сделкам):")
        streak = max_loss_streak(trades)
        if streak <= 2:
            lines.append(f"✅ Максимальная серия убытков подряд — {streak}. Просадки короткие, эмоционально это легче держать.")
        else:
            lines.append(
                f"💡 Была серия из {streak} убыточных сделок подряд. Серии — это нормально, рынок не обязан "
                "идти ровно. Главное — не пытаться 'отыграться' резким увеличением риска внутри такой серии."
            )
        if detect_lot_increase_after_loss(trades):
            lines.append(
                "⚠️ Заметно увеличение лота сразу после убыточной сделки — похоже на попытку быстро отыграться. "
                "Следующий шаг — после убытка специально оставлять размер позиции таким же или даже меньше, "
                "а не больше."
            )
        if detect_fast_reentry_after_loss(trades):
            lines.append(
                "⚠️ Есть вход в новую сделку менее чем через 5 минут после убытка — похоже на импульсивное решение. "
                "Попробуй взять паузу хотя бы 10–15 минут после любого минуса перед следующим входом."
            )
        if not detect_lot_increase_after_loss(trades) and not detect_fast_reentry_after_loss(trades):
            lines.append("✅ Явных признаков тильта (увеличение лота или мгновенный вход после убытка) не видно — хороший контроль эмоций.")
        best_hour, worst_hour = hour_breakdown(trades)
        if best_hour is not None:
            lines.append(f"📊 Лучший час по результату: {best_hour}:00, худший: {worst_hour}:00 (время сервера брокера) — может пригодиться при выборе времени для входов.")

    return "\n".join(lines)


# ---------------------------------------------------------------- основной цикл

async def main():
    state = load_state()
    api = MetaApi(token=METAAPI_TOKEN)
    account = await api.metatrader_account_api.get_account(METAAPI_ACCOUNT_ID)

    connection = account.get_rpc_connection()
    await connection.connect()
    await connection.wait_synchronized()

    now = datetime.now(timezone.utc)
    if state["last_check_time"]:
        start = parse_dt(state["last_check_time"]) - timedelta(hours=1)  # запас на задержки синхронизации
    else:
        start = now - timedelta(days=LOOKBACK_DAYS_ON_FIRST_RUN)

    deals = await connection.get_deals_by_time_range(start_time=start, end_time=now)
    if isinstance(deals, dict):
        deals = deals.get("deals", [])

    closing_deals = [
        d for d in deals
        if d.get("entryType") == "DEAL_ENTRY_OUT"
        and d.get("type") in ("DEAL_TYPE_BUY", "DEAL_TYPE_SELL")
        and d.get("id") not in state["seen_deal_ids"]
    ]
    closing_deals.sort(key=lambda d: d.get("time", ""))

    for deal in closing_deals:
        trade = await enrich_trade(connection, deal, deals)
        state["seen_deal_ids"].append(deal.get("id"))
        state["recent_trades"].append(trade)
        state["recent_trades"] = state["recent_trades"][-10:]
        state["total_closed_trades"] += 1
        n = state["total_closed_trades"]

        # копим точку кривой эквити: реальный баланс после сделки, если он известен,
        # иначе — предыдущая точка + результат сделки (на случай, если брокер не вернул баланс)
        prev_balance = state["equity_curve"][-1]["balance"] if state["equity_curve"] else None
        if trade["balance_after"] is not None:
            equity_value = trade["balance_after"]
        elif prev_balance is not None:
            equity_value = prev_balance + trade["profit"]
        else:
            equity_value = trade["profit"]
        state["equity_curve"].append({"n": n, "balance": equity_value})

        send_email(
            f"MT5: сделка №{n} закрыта ({trade['symbol']}, {fmt_money(trade['profit'])})",
            build_single_email(trade, n),
        )

        if n % 5 == 0:
            send_email(f"MT5: сводка по сделкам {n-4}-{n}", build_summary_email(state["recent_trades"][-5:], n, "5"))

        if n % 10 == 0:
            summary_text = build_summary_email(state["recent_trades"][-10:], n, "10")
            chart_path = build_equity_chart(state["equity_curve"], highlight_last=10)
            if chart_path:
                send_email_with_chart(f"MT5: глубокий разбор сделок {n-9}-{n}", summary_text, chart_path)
            else:
                send_email(f"MT5: глубокий разбор сделок {n-9}-{n}", summary_text)

    state["last_check_time"] = now.isoformat()
    save_state(state)
    print(f"Готово. Новых закрытых сделок: {len(closing_deals)}. Всего обработано: {state['total_closed_trades']}")


if __name__ == "__main__":
    asyncio.run(main())
