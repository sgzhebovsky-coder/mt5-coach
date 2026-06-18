"""
MT5 Trade Coach
-----------------
Pulls closed-trade history from your MT5 account via the cloud MetaApi service
(no local terminal needed — works even if you only trade from the MT5 mobile app),
analyzes trading behaviour, and emails you:

  - after every 5th closed trade    -> a summary of the last 5 trades
  - after every 10th closed trade   -> a structured progress review of the last 10 trades
    (Profit / Risk management / Focus / Psychology / Suggestions), compared against the
    previous 10-trade batch, with an equity curve chart attached

The script runs periodically (see .github/workflows/trade-review.yml) and stores
its state (what has already been processed) in state.json, which it commits back
to the repository.
"""

import asyncio
import json
import os
import random
import smtplib
import tempfile
from collections import defaultdict
import statistics
from datetime import datetime, timedelta, timezone
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.image import MIMEImage
from email.utils import formataddr
from pathlib import Path

import matplotlib
matplotlib.use("Agg")  # no display available — required for running in GitHub Actions
import matplotlib.pyplot as plt

from metaapi_cloud_sdk import MetaApi

STATE_PATH = Path(__file__).parent / "state.json"

METAAPI_TOKEN = os.environ["METAAPI_TOKEN"]
METAAPI_ACCOUNT_ID = os.environ["METAAPI_ACCOUNT_ID"]
GMAIL_ADDRESS = os.environ["GMAIL_ADDRESS"]
GMAIL_APP_PASSWORD = os.environ["GMAIL_APP_PASSWORD"]
RECIPIENT_EMAIL = os.environ.get("RECIPIENT_EMAIL", GMAIL_ADDRESS)

SENDER_NAME = "Trading Coach"

# On the very first run, don't pull the entire account history — just the last N days
LOOKBACK_DAYS_ON_FIRST_RUN = 2

REASON_MAP = {
    "DEAL_REASON_SL": "stop-loss triggered",
    "DEAL_REASON_TP": "take-profit triggered",
    "DEAL_REASON_CLIENT": "closed manually",
    "DEAL_REASON_MOBILE": "closed manually (mobile app)",
    "DEAL_REASON_WEB": "closed manually (web terminal)",
    "DEAL_REASON_EXPERT": "closed by an expert advisor/script",
    "DEAL_REASON_SO": "closed by stop-out (margin call)",
}


# ---------------------------------------------------------------- state

def load_state():
    if STATE_PATH.exists():
        state = json.loads(STATE_PATH.read_text())
        state.setdefault("equity_curve", [])  # in case of an older state.json without this field
        state.setdefault("last_10_metrics", None)  # metrics from the previous 10-trade review, for comparison
        state.setdefault("last_headline", None)  # last headline used, so we don't repeat it next time
        return state
    return {
        "last_check_time": None,
        "seen_deal_ids": [],
        "total_closed_trades": 0,
        "recent_trades": [],  # last up to 10 closed trades
        "equity_curve": [],  # balance history per closed trade since tracking began
        "last_10_metrics": None,  # metrics from the previous 10-trade review, for comparison
        "last_headline": None,  # last headline used, so we don't repeat it next time
    }


def save_state(state):
    state["seen_deal_ids"] = state["seen_deal_ids"][-500:]
    state["recent_trades"] = state["recent_trades"][-10:]
    state["equity_curve"] = state["equity_curve"][-500:]  # keep state.json from growing forever
    STATE_PATH.write_text(json.dumps(state, indent=2, ensure_ascii=False, default=str))


# ---------------------------------------------------------------- email

def send_email(subject, body):
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = formataddr((SENDER_NAME, GMAIL_ADDRESS))
    msg["To"] = RECIPIENT_EMAIL
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
        server.sendmail(GMAIL_ADDRESS, [RECIPIENT_EMAIL], msg.as_string())
    print(f"[email] sent: {subject}")


def send_email_with_chart(subject, body, image_path):
    """Email with text + an attached chart image."""
    msg = MIMEMultipart()
    msg["Subject"] = subject
    msg["From"] = formataddr((SENDER_NAME, GMAIL_ADDRESS))
    msg["To"] = RECIPIENT_EMAIL
    msg.attach(MIMEText(body, "plain", "utf-8"))
    if image_path and os.path.exists(image_path):
        with open(image_path, "rb") as f:
            img = MIMEImage(f.read(), name="equity_curve.png")
        msg.attach(img)
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
        server.sendmail(GMAIL_ADDRESS, [RECIPIENT_EMAIL], msg.as_string())
    print(f"[email] sent (with chart): {subject}")


def send_html_email(subject, plain_body, html_body, image_path=None):
    """HTML email with a plain-text fallback, and an optional chart image embedded
    inline (referenced from the HTML via cid:equity_chart)."""
    msg_root = MIMEMultipart("related")
    msg_root["Subject"] = subject
    msg_root["From"] = formataddr((SENDER_NAME, GMAIL_ADDRESS))
    msg_root["To"] = RECIPIENT_EMAIL

    msg_alt = MIMEMultipart("alternative")
    msg_root.attach(msg_alt)
    msg_alt.attach(MIMEText(plain_body, "plain", "utf-8"))
    msg_alt.attach(MIMEText(html_body, "html", "utf-8"))

    if image_path and os.path.exists(image_path):
        with open(image_path, "rb") as f:
            img = MIMEImage(f.read(), name="equity_curve.png")
        img.add_header("Content-ID", "<equity_chart>")
        img.add_header("Content-Disposition", "inline", filename="equity_curve.png")
        msg_root.attach(img)

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
        server.sendmail(GMAIL_ADDRESS, [RECIPIENT_EMAIL], msg_root.as_string())
    print(f"[email] sent (html): {subject}")


# ---------------------------------------------------------------- formatting

def fmt_money(x):
    if x is None:
        return "N/A"
    sign = "+" if x > 0 else ("-" if x < 0 else "")
    return f"{sign}${abs(x):,.2f}".replace(",", " ")


def fmt_duration(seconds):
    if seconds is None:
        return "N/A"
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, _ = divmod(rem, 60)
    if h:
        return f"{h}h {m}m"
    return f"{m}m"


def fmt_price(x):
    return "N/A" if x is None else x


def to_iso(x):
    """Turns a time value into a string (MetaApi can return a raw datetime object instead
    of a string — such an object can't be saved to json without this normalization)."""
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


# ---------------------------------------------------------------- enriching trade data

async def get_symbol_risk_per_unit(connection, symbol):
    """Returns (tick_value, tick_size) or (None, None) if it couldn't be retrieved."""
    try:
        spec = await connection.get_symbol_specification(symbol)
        return spec.get("tickValue"), spec.get("tickSize")
    except Exception as e:
        print(f"[warn] failed to get symbol specification for {symbol}: {e}")
        return None, None


async def enrich_trade(connection, deal, all_deals):
    """Reconstructs the full context for a closing deal: SL/TP, entry time, etc.

    all_deals — the full list of deals for the period (not just closing ones), so we can
    find the position's opening deal (entryType == DEAL_ENTRY_IN). This is more reliable
    than querying order history separately, and needs no extra API call.
    """
    info = {
        "position_id": deal.get("positionId"),
        "symbol": deal.get("symbol"),
        "side": "BUY" if deal.get("type") == "DEAL_TYPE_SELL" else "SELL",
        # the closing deal always has the opposite type of the original position
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
        "sl_known": False,  # whether we managed to get any data about the entry order at all
        "risk_amount": None,
        "r_multiple": None,
        "risk_pct_of_balance": None,
        "holding_seconds": None,
    }

    # 1) Entry price/time — first try to find it among the deals we already fetched
    entry_deal = next(
        (d for d in all_deals if d.get("positionId") == info["position_id"] and d.get("entryType") == "DEAL_ENTRY_IN"),
        None,
    )
    if entry_deal:
        info["open_time"] = to_iso(entry_deal.get("time"))
        info["open_price"] = entry_deal.get("price")

    # 2) SL/TP can only come from order history (deals don't carry them)
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
            # if we didn't find the entry price among the deals, fall back to the order
            if info["open_price"] is None:
                info["open_time"] = info["open_time"] or to_iso(first.get("time"))
                info["open_price"] = first.get("openPrice") or first.get("price")
    except Exception as e:
        print(f"[warn] failed to get orders for position {info['position_id']}: {e}")

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


# ---------------------------------------------------------------- behavioural patterns

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
    """Draws a conclusion from comparing how long winning vs losing trades were held —
    the classic 'cut profits fast, let losses run' pattern shows up exactly here."""
    win_holds = [t["holding_seconds"] for t in trades if t["profit"] > 0 and t["holding_seconds"] is not None]
    loss_holds = [t["holding_seconds"] for t in trades if t["profit"] <= 0 and t["holding_seconds"] is not None]
    if not win_holds or not loss_holds:
        return None
    avg_win = statistics.mean(win_holds)
    avg_loss = statistics.mean(loss_holds)
    if avg_loss > avg_win * 1.5 and avg_loss - avg_win > 60:
        return (
            f"⏱️ You're holding losing trades longer than winning ones on average ({fmt_duration(avg_loss)} vs "
            f"{fmt_duration(avg_win)}). This is a common pattern — hoping a loss will turn around, while "
            "closing profits quickly out of fear of losing them. In practice it usually works the other way "
            "round. Next step: treat exiting a loss and exiting a profit with the same discipline, for "
            "example by setting both a stop-loss and a take-profit in advance."
        )
    if avg_win > avg_loss * 1.5 and avg_win - avg_loss > 60:
        return (
            f"⏱️ You're holding winning trades longer than losing ones ({fmt_duration(avg_win)} vs "
            f"{fmt_duration(avg_loss)}) — this is a healthy pattern: losses get cut quickly, profits are "
            "given room to grow. This is exactly what separates profitable traders from losing ones in the "
            "long run — keep it up."
        )
    return None


# ---------------------------------------------------------------- equity curve chart

def build_equity_chart(equity_curve, highlight_last=10):
    """Plots the balance curve since tracking began, highlighting the last highlight_last
    trades in a different colour, with a label showing the $ change over that highlighted
    segment. Returns the path to the saved png, or None if too few points."""
    if len(equity_curve) < 2:
        return None

    xs = [p["n"] for p in equity_curve]
    ys = [p["balance"] for p in equity_curve]

    fig, ax = plt.subplots(figsize=(8, 4))
    ax.plot(xs, ys, color="#9aa0a6", linewidth=1.5, label="Since tracking began")

    if len(equity_curve) > highlight_last:
        hl_xs = [xs[-highlight_last - 1]] + xs[-highlight_last:]
        hl_ys = [ys[-highlight_last - 1]] + ys[-highlight_last:]
    else:
        hl_xs, hl_ys = xs, ys
    ax.plot(hl_xs, hl_ys, color="#e63946", linewidth=2.5, label=f"Last {highlight_last} trades")

    period_change = hl_ys[-1] - hl_ys[0]
    label_color = "#1d9e75" if period_change >= 0 else "#e63946"
    ax.annotate(
        f"{fmt_money(period_change)} over last {highlight_last} trades",
        xy=(hl_xs[-1], hl_ys[-1]),
        xytext=(-10, 10 if period_change >= 0 else -18),
        textcoords="offset points",
        ha="right",
        fontsize=10,
        fontweight="bold",
        color=label_color,
    )

    ax.set_xlabel("Trade number")
    ax.set_ylabel("Balance, $")
    ax.set_title("Balance curve since tracking began")
    ax.legend(loc="best", fontsize=9)
    ax.grid(alpha=0.3)
    fig.tight_layout()

    path = os.path.join(tempfile.gettempdir(), "equity_curve.png")
    fig.savefig(path, dpi=130)
    plt.close(fig)
    return path


# ---------------------------------------------------------------- coaching assessments
#
# Idea: always evaluate PROCESS (risk management, discipline) separately from OUTCOME
# (profit/loss). Good process gets praised regardless of outcome. Bad process gets gently
# flagged with a concrete next step — even if the trade ended up profitable (luck isn't
# the same as being right). A loss always comes with encouragement and a concrete focus
# for what to improve.

RISK_COMFORT_PCT = 2.0  # reference point for a healthy risk per trade, %


def assess_stop_loss(trade):
    if trade["stop_loss"]:
        return "✅ A stop-loss was set on entry — that's the foundation of healthy risk management, keep it up."
    if not trade["sl_known"]:
        return (
            "ℹ️ Couldn't get stop-loss data for this trade (the broker didn't return the entry order history) — "
            "so no judgement here. Either way, keep the habit of setting an SL right when you enter."
        )
    return (
        "⚠️ No stop-loss was set on entry. This is the main thing worth fixing: a fixed stop protects you "
        "from one bad entry turning into a serious loss. Next step — set the SL right when you open the "
        "position, before watching where the price goes next."
    )


def assess_risk_size(trade):
    if trade["risk_pct_of_balance"] is None:
        return None
    pct = trade["risk_pct_of_balance"]
    if pct <= RISK_COMFORT_PCT:
        return f"✅ Risk on this trade — {pct:.1f}% of balance, within a healthy range (typically 1–2%)."
    return (
        f"⚠️ Risk on this trade — {pct:.1f}% of balance, above the typical 1–2% guideline. "
        "A large risk on a single trade is a common cause of emotional decisions right after it. "
        "Next step — try reducing the lot size so any single trade matters less."
    )


def assess_close_reason(trade):
    manual = trade["reason"] in ("DEAL_REASON_CLIENT", "DEAL_REASON_MOBILE", "DEAL_REASON_WEB")
    if not manual:
        return "✅ The trade closed according to plan (stop or target hit) — the plan was set and you let it work."
    if trade["stop_loss"]:
        # a stop was definitely set — so the manual close happened before it would have triggered
        if trade["profit"] <= 0:
            return (
                "💡 Closed manually before the stop would have triggered. If that was a deliberate decision — "
                "fine. If it was an emotional reaction during the drawdown — next step: trust the stop you "
                "set in advance and don't close manually in the moment, it removes some of the anxiety."
            )
        return (
            "💡 Closed manually while in profit. If the target was reached as planned — great. If it was just "
            "fear of giving the profit back — worth considering whether you're cutting good trades short."
        )
    # no stop was set — phrased without implying a stop that didn't exist
    if trade["sl_known"] and trade["profit"] <= 0:
        return (
            "💡 Closed manually. With no stop-loss set, the decision of when to exit the loss rested entirely "
            "on you in the moment — a stop set in advance would have taken that load off you and defined the "
            "boundary itself."
        )
    if trade["sl_known"]:
        return (
            "💡 Closed manually while in profit. If the target was reached as planned — great. If the decision "
            "was emotional — worth considering whether you're cutting good trades short."
        )
    # unknown whether a stop was set — just note the manual close without assuming either way
    if trade["profit"] <= 0:
        return (
            "💡 Closed manually. Hard to say whether there was a stop-loss plan in place — there's no entry "
            "order data. If the decision was deliberate, that's fine; if it was emotional, worth paying "
            "attention to that."
        )
    return (
        "💡 Closed manually while in profit. If that was according to plan — great; if it was out of fear of "
        "losing the profit — worth thinking about ahead of the next trade."
    )


def assess_cost_impact(trade):
    """Explicitly flags the case where price genuinely moved in the right direction (the
    entry was directionally correct), but the net result is still negative — commission/swap
    ate the gain."""
    if trade["profit"] <= 0 and trade["gross_profit"] > 0:
        return (
            f"📌 Worth noting: on price alone, this trade was profitable ({fmt_money(trade['gross_profit'])}) — "
            f"the entry direction was correct. But the net result is still negative because commission/swap "
            f"({fmt_money(trade['costs'])}) outweighed what the price move earned. This isn't a market-reading "
            "mistake — it's a sign the profit target on this trade was too small relative to the costs. "
            "Either aim for a larger move, or check in advance whether the expected profit covers the costs "
            "on this instrument."
        )
    return None


def overall_takeaway(trade, process_ok):
    if trade["profit"] > 0 and process_ok:
        return "Bottom line: a profitable trade made the right way — this is the kind of trade to aim for."
    if trade["profit"] > 0 and not process_ok:
        return (
            "Bottom line: a profit is nice, but it doesn't mean the entry was right. Without a stop, or with "
            "too much risk, one bad break can cost far more than this lucky outcome earned. Take the process "
            "from the section above into the next trade, not just this result."
        )
    if trade["profit"] <= 0 and process_ok:
        return (
            "Bottom line: there's a loss, but the process was right — risk was limited and the plan was "
            "followed. A loss within the plan is part of the strategy's statistics, not a mistake. Don't "
            "fixate on this single trade; the picture over the next 5 and 10 trades will tell you more."
        )
    return (
        "Bottom line: a loss is a normal part of trading, even for the best traders. Don't blame yourself for "
        "the outcome, but take one concrete focus into the next trade — it's written above."
    )


def symbol_breakdown(trades):
    stats = defaultdict(lambda: {"pl": 0.0, "count": 0})
    for t in trades:
        s = t["symbol"] or "N/A"
        stats[s]["pl"] += t["profit"]
        stats[s]["count"] += 1
    return stats


def compute_batch_metrics(trades):
    """Aggregates a batch of trades into comparable numbers, used to track progress
    from one 10-trade review to the next."""
    wins = [t for t in trades if t["profit"] > 0]
    risk_pcts = [t["risk_pct_of_balance"] for t in trades if t["risk_pct_of_balance"] is not None]
    return {
        "trade_count": len(trades),
        "total_pl": sum(t["profit"] for t in trades),
        "winrate": 100 * len(wins) / len(trades) if trades else 0,
        "avg_risk_pct": statistics.mean(risk_pcts) if risk_pcts else None,
        "no_sl_count": sum(1 for t in trades if not t["stop_loss"]),
        "manual_closes": sum(1 for t in trades if t["reason"] in ("DEAL_REASON_CLIENT", "DEAL_REASON_MOBILE", "DEAL_REASON_WEB")),
        "num_instruments": len(symbol_breakdown(trades)),
        "loss_streak": max_loss_streak(trades),
        "tilt_signal": detect_lot_increase_after_loss(trades) or detect_fast_reentry_after_loss(trades),
    }


def instrument_recommendation(stats, any_profitable):
    if len(stats) <= 1:
        return (
            "✅ The whole period was traded on a single instrument — that kind of focus helps you really get "
            "a feel for how it behaves instead of spreading yourself thin. Good practice, keep it up."
        )
    best = max(stats, key=lambda s: stats[s]["pl"])
    worst = min(stats, key=lambda s: stats[s]["pl"])
    if not any_profitable:
        return (
            f"No instrument came out positive this period — that points to the overall approach in this "
            f"batch of trades rather than to any one pair. {worst} produced the biggest loss "
            f"({fmt_money(stats[worst]['pl'])} over {stats[worst]['count']} trades) — worth a closer look on "
            "its own. But before hunting for a 'lucky' instrument, it's often more useful to trade fewer "
            "pairs at once and tighten up the process — that usually pays off more than instrument-hopping."
        )
    msg = (
        f"💡 {best} brought in the most profit this period ({fmt_money(stats[best]['pl'])} over "
        f"{stats[best]['count']} trades) — this instrument seems to suit you better than the others right "
        "now. Worth giving it a bit more attention next period and not spreading yourself across too many "
        "pairs at once — focus usually gives a more stable result."
    )
    if worst != best and stats[worst]["pl"] < 0:
        msg += (
            f" {worst} is currently dragging the result down ({fmt_money(stats[worst]['pl'])} over "
            f"{stats[worst]['count']} trades) — not a big deal, but a signal to check how well it fits your "
            "approach right now."
        )
    return msg


# ---------------------------------------------------------------- email text

def build_single_email(trade, n):
    win = trade["profit"] > 0
    opener = (
        f"Trade #{n} closed in profit: {trade['symbol']} {trade['side']} 🎉"
        if win else
        f"Trade #{n} closed at a loss: {trade['symbol']} {trade['side']}. Let's break down what to take from it:"
    )

    lines = [
        opener,
        "",
        f"Result: {fmt_money(trade['profit'])}",
    ]
    if trade["costs"]:
        lines.append(
            f"  (of which, price move: {fmt_money(trade['gross_profit'])}, commission/swap: {fmt_money(trade['costs'])})"
        )
    lines += [
        f"Lot: {trade['volume']}",
        f"Entry price: {fmt_price(trade['open_price'])}   Exit price: {fmt_price(trade['close_price'])}",
        f"Time in position: {fmt_duration(trade['holding_seconds'])}",
    ]
    if trade["r_multiple"] is not None:
        lines.append(f"Result in R: {trade['r_multiple']:.2f}R")

    sl_note = assess_stop_loss(trade)
    risk_note = assess_risk_size(trade)
    reason_note = assess_close_reason(trade)
    cost_note = assess_cost_impact(trade)
    process_ok = trade["stop_loss"] is not None and "⚠️" not in (risk_note or "") and "💡" not in reason_note

    lines += ["", "Process review:", sl_note]
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
        opener = f"Summary for the last {depth} trades — a strong batch, both win rate and net result are positive. Let's lock in what worked:"
    elif all_losing:
        focus_points = []
        if no_sl_count > 0:
            focus_points.append("set a stop-loss on every entry without exception")
        if manual_closes >= len(trades) / 2:
            focus_points.append("let SL/TP trigger instead of closing manually")
        if len(symbols) >= 4:
            focus_points.append("cut down the number of instruments traded at once")
        if not focus_points:
            focus_points.append("avoid increasing risk after losing trades and keep position size steady")
        opener = (
            f"Summary for the last {depth} trades — every trade in this batch closed at a loss. That happens "
            "even to experienced traders, and a loss by itself says nothing bad about you as a trader. Since "
            f"the whole batch is negative, it's worth not multiplying attempts but focusing on one concrete "
            f"thing: {'; '.join(focus_points)}. This is about the process, which is fully in your hands — not "
            "about finding a 'lucky' instrument."
        )
    elif total_pl <= 0:
        opener = (
            f"Summary for the last {depth} trades — the batch is in the red, but that's no reason to give up: "
            "strategy statistics even out over a larger sample, the process below matters more."
        )
    else:
        opener = f"Summary for the last {depth} trades (total closed trades: {n}):"

    lines = [
        opener,
        "",
        f"Win rate: {winrate:.0f}% ({len(wins)} of {len(trades)})",
        f"Total result: {fmt_money(total_pl)}",
    ]
    if r_values:
        lines.append(f"Average result: {statistics.mean(r_values):.2f}R")
    if risk_pcts:
        lines.append(
            f"Average risk per trade: {statistics.mean(risk_pcts):.1f}% of balance "
            f"(from {min(risk_pcts):.1f}% to {max(risk_pcts):.1f}%)"
        )
    if holds:
        lines.append(f"Average time in position: {fmt_duration(statistics.mean(holds))}")
    hold_insight = holding_time_insight(trades)
    if hold_insight:
        lines.append(hold_insight)

    lines.append("")
    lines.append("Instruments:")
    lines.append(f"Traded {len(symbols)} instrument(s) this period: {', '.join(symbols.keys())}")
    best_symbol = max(symbols, key=lambda s: symbols[s]["pl"])
    worst_symbol = min(symbols, key=lambda s: symbols[s]["pl"])
    any_profitable = symbols[best_symbol]["pl"] > 0
    if any_profitable:
        lines.append(
            f"🏆 Most profitable: {best_symbol} ({fmt_money(symbols[best_symbol]['pl'])} "
            f"over {symbols[best_symbol]['count']} trades)"
        )
        if worst_symbol != best_symbol:
            lines.append(
                f"Least profitable: {worst_symbol} ({fmt_money(symbols[worst_symbol]['pl'])} "
                f"over {symbols[worst_symbol]['count']} trades)"
            )
    else:
        lines.append(
            f"No instrument came out positive. Smallest loss: {best_symbol} "
            f"({fmt_money(symbols[best_symbol]['pl'])} over {symbols[best_symbol]['count']} trades), "
            f"biggest loss: {worst_symbol} ({fmt_money(symbols[worst_symbol]['pl'])} over "
            f"{symbols[worst_symbol]['count']} trades)."
        )
    lines.append(instrument_recommendation(symbols, any_profitable))

    lines.append("")
    lines.append("Discipline:")
    if no_sl_count == 0:
        lines.append(f"✅ A stop-loss was set on all {len(trades)} trades — that's a strong foundation, keep it up.")
    else:
        lines.append(
            f"⚠️ {no_sl_count} of {len(trades)} trades had no stop-loss. Next step — make setting an SL a "
            "mandatory part of every entry, no exceptions, even when it feels like 'this one will definitely work out'."
        )
    if manual_closes == 0:
        lines.append("✅ All trades closed according to plan (by stop or target) — the plan was followed, and it shows.")
    else:
        lines.append(
            f"💡 {manual_closes} of {len(trades)} trades were closed manually, not via SL/TP. If those were "
            "deliberate decisions per plan — that's fine. If they were emotional — worth paying attention to "
            "this specifically in the next report."
        )

    cost_eaten_count = sum(1 for t in trades if t["profit"] <= 0 and t["gross_profit"] > 0)
    if cost_eaten_count:
        lines.append(
            f"📌 In {cost_eaten_count} of {len(trades)} trades, price moved in your favour but the result was "
            "still negative because of commission/spread. The entry direction was right — the issue is that "
            "the profit target was smaller than the cost of the trade. Either aim for a bigger move, or check "
            "in advance whether the potential profit justifies the cost on that particular instrument."
        )

    return "\n".join(lines)


# ---------------------------------------------------------------- 10-trade progress review

HEADLINE_POOLS = {
    "first": [
        "💪 Day one of tracking — every review from here builds the bigger picture.",
        "🌱 This is your starting point — nowhere to go from here but forward.",
        "📍 Baseline set. Now we get to watch the climb from here.",
    ],
    "great": [
        "🚀 This is what progress looks like — keep this exact approach going.",
        "🔥 You're stacking good decisions — this batch proves it.",
        "📈 The work is paying off. Keep building on this.",
        "💪 Real, measurable progress here — this is your new baseline to beat.",
    ],
    "good": [
        "📈 Solid steps forward this batch — a couple of things below to lock in next.",
        "✅ Progress is happening, even if it's not all at once.",
        "🌱 You're moving in the right direction — keep watering this.",
        "👍 More wins than setbacks this batch — worth building on what worked.",
    ],
    "mixed": [
        "🔄 A mixed batch — nothing's broken, just a clear focus point below to course-correct.",
        "🧭 Some signal, some noise — here's where to point your attention next.",
        "⚖️ Not a clean win, not a setback either — the fix below is usually one specific habit.",
        "🌤️ A cloudy batch, but the path forward is clear below.",
    ],
    "tough": [
        "💡 A tougher batch — every trader has these, and they don't define the journey.",
        "🌅 Rough patch, real lesson — the traders who improve are the ones who keep reviewing like this.",
        "🛠️ This is exactly the kind of batch that builds discipline — here's the one thing to fix.",
        "🧗 Every comeback starts with a batch like this one. Let's find the fix below.",
    ],
}


def progress_category(m, prev_metrics):
    if not prev_metrics:
        return "first"
    checks = [
        m["total_pl"] >= prev_metrics["total_pl"],
        m["no_sl_count"] <= prev_metrics["no_sl_count"],
        m["tilt_signal"] <= prev_metrics["tilt_signal"],
    ]
    if m["avg_risk_pct"] is not None and prev_metrics["avg_risk_pct"] is not None:
        checks.append(m["avg_risk_pct"] <= prev_metrics["avg_risk_pct"])
    ratio = sum(checks) / len(checks)
    if ratio >= 0.75:
        return "great"
    if ratio >= 0.5:
        return "good"
    if ratio > 0.25:
        return "mixed"
    return "tough"


def progress_headline(m, prev_metrics, last_headline):
    """Picks an energizing, hopeful headline from a pool, avoiding the exact phrase used
    last time so consecutive reports don't feel repetitive."""
    pool = HEADLINE_POOLS[progress_category(m, prev_metrics)]
    candidates = [h for h in pool if h != last_headline] or pool
    return random.choice(candidates)


def build_10_trade_sections(trades, n, prev_metrics):
    """Returns the 10-trade review broken into 5 sections, each as a dict with title,
    icon, a short comparison badge (text + semantic kind), and the full detail lines —
    shared by both the plain-text and HTML email builders so nothing has to be written twice."""
    m = compute_batch_metrics(trades)
    symbols = symbol_breakdown(trades)
    best_symbol = max(symbols, key=lambda s: symbols[s]["pl"])
    any_profitable = symbols[best_symbol]["pl"] > 0
    wins = len([t for t in trades if t["profit"] > 0])

    sections = []

    # 1. Profit
    lines = [
        f"Total result: {fmt_money(m['total_pl'])}",
        f"Win rate: {m['winrate']:.0f}% ({wins} of {m['trade_count']})",
    ]
    badge, badge_kind = None, None
    if prev_metrics:
        pl_delta = m["total_pl"] - prev_metrics["total_pl"]
        wr_delta = m["winrate"] - prev_metrics["winrate"]
        trend = "improved" if pl_delta > 0 else ("declined" if pl_delta < 0 else "stayed flat")
        lines.append(
            f"Compared to the previous 10 trades ({fmt_money(prev_metrics['total_pl'])}, "
            f"{prev_metrics['winrate']:.0f}% win rate): the result {trend} by {fmt_money(abs(pl_delta))}, "
            f"win rate {'up' if wr_delta > 0 else ('down' if wr_delta < 0 else 'unchanged')} "
            f"{abs(wr_delta):.0f} percentage points."
        )
        badge = f"{fmt_money(pl_delta)} vs last"
        badge_kind = "success" if pl_delta >= 0 else "danger"
    else:
        lines.append("This is your first 10-trade batch, so there's nothing to compare it to yet — it'll be the baseline for next time.")
    sections.append({"title": "Profit", "icon": "ti-chart-bar", "icon_kind": "success", "badge": badge, "badge_kind": badge_kind, "lines": lines})

    # 2. Risk management
    lines = []
    if m["no_sl_count"] == 0:
        lines.append(f"✅ A stop-loss was set on all {m['trade_count']} trades.")
    else:
        lines.append(f"⚠️ {m['no_sl_count']} of {m['trade_count']} trades had no stop-loss.")
    if m["avg_risk_pct"] is not None:
        lines.append(f"Average risk per trade: {m['avg_risk_pct']:.1f}% of balance.")
    badge, badge_kind = None, None
    if prev_metrics:
        if m["avg_risk_pct"] is not None and prev_metrics["avg_risk_pct"] is not None:
            rd = m["avg_risk_pct"] - prev_metrics["avg_risk_pct"]
            if abs(rd) >= 0.1:
                lines.append(f"Risk per trade {'increased' if rd > 0 else 'decreased'} by {abs(rd):.1f} pp vs the previous 10 trades.")
        sl_delta = m["no_sl_count"] - prev_metrics["no_sl_count"]
        if sl_delta != 0:
            lines.append(f"Trades without a stop-loss {'increased' if sl_delta > 0 else 'decreased'} by {abs(sl_delta)} vs the previous batch.")
            badge = f"{m['no_sl_count']} missing (was {prev_metrics['no_sl_count']})"
            badge_kind = "success" if sl_delta <= 0 else "danger"
        elif m["no_sl_count"] == 0:
            lines.append("Consistent with the previous batch — stop-loss discipline held steady.")
            badge, badge_kind = "steady", "success"
    sections.append({"title": "Risk management", "icon": "ti-lock", "icon_kind": "info", "badge": badge, "badge_kind": badge_kind, "lines": lines})

    # 3. Focus
    lines = [
        f"Traded {m['num_instruments']} instrument(s) this period: {', '.join(symbols.keys())}.",
        instrument_recommendation(symbols, any_profitable),
    ]
    badge, badge_kind = None, None
    if prev_metrics:
        fd = m["num_instruments"] - prev_metrics["num_instruments"]
        if fd > 0:
            lines.append(
                f"That's {fd} more instrument(s) than the previous batch ({prev_metrics['num_instruments']}). This "
                "is subjective, but worth keeping an eye on — trading more pairs at once can mean spreading "
                "attention thin rather than going deep on a setup that's working."
            )
            badge, badge_kind = "broader vs last", "warning"
        elif fd < 0:
            lines.append(
                f"That's {abs(fd)} fewer instrument(s) than the previous batch ({prev_metrics['num_instruments']}) — "
                "a tighter focus, generally a good sign."
            )
            badge, badge_kind = "tighter vs last", "success"
        else:
            lines.append(f"Same number of instruments as the previous batch ({prev_metrics['num_instruments']}).")
            badge, badge_kind = "steady", "success"
    sections.append({"title": "Focus", "icon": "ti-search", "icon_kind": "warning", "badge": badge, "badge_kind": badge_kind, "lines": lines})

    # 4. Psychology
    lines = []
    if m["loss_streak"] <= 2:
        lines.append(f"✅ Longest losing streak: {m['loss_streak']}. Drawdowns stayed short, easier to handle emotionally.")
    else:
        lines.append(f"💡 Longest losing streak: {m['loss_streak']} trades in a row. Streaks are normal — the key is not increasing risk to try to win it back.")
    if detect_lot_increase_after_loss(trades):
        lines.append("⚠️ Lot size increased right after a losing trade at least once — a possible sign of trying to win it back.")
    if detect_fast_reentry_after_loss(trades):
        lines.append("⚠️ At least one re-entry within 5 minutes of a loss — a possible impulsive-decision signal.")
    if not m["tilt_signal"]:
        lines.append("✅ No clear tilt signals (lot increase or instant re-entry after a loss) this period.")
    hold_insight = holding_time_insight(trades)
    if hold_insight:
        lines.append(hold_insight)
    badge, badge_kind = None, None
    if prev_metrics:
        if m["tilt_signal"] and not prev_metrics["tilt_signal"]:
            lines.append("This is new compared to the previous batch, which showed no tilt signals — worth paying attention to.")
            badge, badge_kind = "new signal", "danger"
        elif not m["tilt_signal"] and prev_metrics["tilt_signal"]:
            lines.append("Good progress: the previous batch showed tilt signals, this one doesn't.")
            badge, badge_kind = "improved", "success"
        elif not m["tilt_signal"]:
            badge, badge_kind = "steady", "success"
        else:
            badge, badge_kind = "ongoing", "warning"
    sections.append({"title": "Psychology", "icon": "ti-heart", "icon_kind": "neutral", "badge": badge, "badge_kind": badge_kind, "lines": lines})

    # 5. Suggestions
    suggestions = []
    if m["no_sl_count"] > 0:
        suggestions.append("Make setting a stop-loss a non-negotiable part of every entry, no exceptions.")
    if m["manual_closes"] >= m["trade_count"] / 2:
        suggestions.append("Let SL/TP do the closing instead of closing manually, to remove in-the-moment decisions.")
    if m["num_instruments"] >= 4:
        suggestions.append("Try trading fewer instruments at once so you can build a clearer read on each one.")
    if m["tilt_signal"]:
        suggestions.append("Add a short mandatory pause after every loss before opening the next trade.")
    if not suggestions:
        suggestions.append(
            "Process looks solid this period — consider keeping a short journal entry per trade (why you "
            "entered, what you expected) so the next gains come from refining entry timing rather than fixing mistakes."
        )
    sections.append({"title": "Suggestions", "icon": "ti-arrow-right", "icon_kind": "neutral", "badge": None, "badge_kind": None, "lines": [f"- {s}" for s in suggestions]})

    return sections


def render_sections_text(sections):
    parts = []
    for i, sec in enumerate(sections, start=1):
        badge_suffix = f" [{sec['badge']}]" if sec["badge"] else ""
        block = [f"{i}. {sec['title']}{badge_suffix}"] + sec["lines"]
        parts.append("\n".join(block))
    return "\n\n".join(parts)




# Email clients (Gmail, Outlook, etc.) don't support CSS custom properties or flexbox
# reliably, and never load external icon fonts — so this uses hardcoded hex colors,
# table-based layout, and emoji instead. Verified by what actually rendered, not just
# how it looks in a browser preview.

_COLORS = {
    "success": {"bg": "#e6f4ea", "fg": "#1e7e34"},
    "danger": {"bg": "#fdecea", "fg": "#c0392b"},
    "warning": {"bg": "#fff4dd", "fg": "#a6700b"},
    "info": {"bg": "#e8f0fe", "fg": "#1a56b0"},
    "neutral": {"bg": "#f1f1f1", "fg": "#5f6368"},
}
_SECTION_EMOJI = {
    "Profit": "💰",
    "Risk management": "🛡️",
    "Focus": "🔍",
    "Psychology": "🧠",
    "Suggestions": "🚀",
}
_HEADLINE_COLOR_BY_CATEGORY = {
    "great": "success",
    "good": "success",
    "mixed": "warning",
    "tough": "info",
    "first": "info",
}


def render_sections_html(sections):
    rows = []
    for sec in sections:
        icon_colors = _COLORS.get(sec["icon_kind"], _COLORS["neutral"])
        emoji = _SECTION_EMOJI.get(sec["title"], "\u2022")
        if sec["badge"]:
            badge_colors = _COLORS.get(sec["badge_kind"], _COLORS["success"])
            badge_html = (
                f'<span style="font-size:12px;font-weight:bold;padding:3px 8px;border-radius:6px;'
                f'background:{badge_colors["bg"]};color:{badge_colors["fg"]};white-space:nowrap;">{sec["badge"]}</span>'
            )
        else:
            badge_html = ""
        detail_html = "".join(
            f'<div style="margin-top:4px;">{line}</div>' for line in sec["lines"]
        )
        rows.append(f'''
<table role="presentation" cellpadding="0" cellspacing="0" width="100%" style="background:#f7f7f7;border-radius:8px;margin-bottom:8px;">
  <tr>
    <td style="padding:10px 12px;">
      <table role="presentation" cellpadding="0" cellspacing="0" width="100%">
        <tr>
          <td width="30" style="vertical-align:middle;">
            <table role="presentation" cellpadding="0" cellspacing="0" width="28" height="28" style="background:{icon_colors["bg"]};border-radius:14px;">
              <tr><td align="center" style="font-size:14px;line-height:28px;">{emoji}</td></tr>
            </table>
          </td>
          <td style="padding-left:10px;font-size:14px;font-weight:bold;color:#1a1a1a;vertical-align:middle;">{sec["title"]}</td>
          <td align="right" style="vertical-align:middle;">{badge_html}</td>
        </tr>
      </table>
      <div style="font-size:13px;color:#5f6368;padding-left:38px;line-height:1.5;margin-top:6px;">{detail_html}</div>
    </td>
  </tr>
</table>''')
    return "".join(rows)


def build_10_trade_review(sections, headline, n):
    """Plain-text version of the 10-trade review."""
    lines = [f"10-trade progress review — trades {n-9}-{n}", headline, "", render_sections_text(sections)]
    return "\n".join(lines)


def build_10_trade_review_html(sections, headline, n, has_chart, category="first"):
    """HTML version of the 10-trade review: same content as the plain-text one, laid
    out with a chart and 5 colour-coded sections, using email-safe markup only
    (tables, hex colors, emoji — no CSS variables, flexbox, or icon fonts)."""
    chart_html = (
        '<img src="cid:equity_chart" alt="Balance curve" width="540" style="width:100%;max-width:540px;border-radius:8px;margin-bottom:16px;display:block;" />'
        if has_chart else ""
    )
    headline_colors = _COLORS[_HEADLINE_COLOR_BY_CATEGORY.get(category, "info")]
    return f'''
<table role="presentation" cellpadding="0" cellspacing="0" width="100%" style="max-width:600px;font-family:Arial,Helvetica,sans-serif;">
  <tr><td style="padding:12px 16px 4px;font-size:13px;color:#5f6368;">Trading coach &middot; 10-trade review &middot; trades {n-9}-{n}</td></tr>
  <tr><td style="padding:0 16px 16px;">
    <table role="presentation" cellpadding="0" cellspacing="0" width="100%" style="border:1px solid #e0e0e0;border-radius:10px;">
      <tr><td style="padding:16px;">
        <table role="presentation" cellpadding="0" cellspacing="0" width="100%" style="background:{headline_colors["bg"]};border-radius:8px;margin-bottom:16px;">
          <tr><td style="padding:12px 14px;font-size:14px;font-weight:bold;color:{headline_colors["fg"]};">{headline}</td></tr>
        </table>
        {chart_html}
        {render_sections_html(sections)}
      </td></tr>
    </table>
  </td></tr>
</table>'''


# ---------------------------------------------------------------- main loop

async def main():
    state = load_state()
    api = MetaApi(token=METAAPI_TOKEN)
    account = await api.metatrader_account_api.get_account(METAAPI_ACCOUNT_ID)

    connection = account.get_rpc_connection()
    await connection.connect()
    await connection.wait_synchronized()

    now = datetime.now(timezone.utc)
    if state["last_check_time"]:
        start = parse_dt(state["last_check_time"]) - timedelta(hours=1)  # buffer for sync delays
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

        # accumulate an equity-curve point: the real balance after the trade if known,
        # otherwise the previous point + this trade's result (in case the broker didn't return balance)
        prev_balance = state["equity_curve"][-1]["balance"] if state["equity_curve"] else None
        if trade["balance_after"] is not None:
            equity_value = trade["balance_after"]
        elif prev_balance is not None:
            equity_value = prev_balance + trade["profit"]
        else:
            equity_value = trade["profit"]
        state["equity_curve"].append({"n": n, "balance": equity_value})

        if n % 5 == 0:
            send_email(f"MT5: Summary for trades {n-4}-{n}", build_summary_email(state["recent_trades"][-5:], n, "5"))

        if n % 10 == 0:
            last10 = state["recent_trades"][-10:]
            prev_metrics = state.get("last_10_metrics")
            m = compute_batch_metrics(last10)
            headline = progress_headline(m, prev_metrics, state.get("last_headline"))
            category = progress_category(m, prev_metrics)
            sections = build_10_trade_sections(last10, n, prev_metrics)
            chart_path = build_equity_chart(state["equity_curve"], highlight_last=10)

            plain_body = build_10_trade_review(sections, headline, n)
            html_body = build_10_trade_review_html(sections, headline, n, has_chart=bool(chart_path), category=category)
            send_html_email(f"MT5: 10-trade review — trades {n-9}-{n}", plain_body, html_body, chart_path)

            state["last_10_metrics"] = m
            state["last_headline"] = headline

    state["last_check_time"] = now.isoformat()
    save_state(state)
    print(f"Done. New closed trades: {len(closing_deals)}. Total processed: {state['total_closed_trades']}")


if __name__ == "__main__":
    asyncio.run(main())
