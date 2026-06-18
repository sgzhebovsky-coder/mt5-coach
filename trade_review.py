"""
MT5 Trade Coach
-----------------
Pulls closed-trade history from your MT5 account via the cloud MetaApi service
(no local terminal needed — works even if you only trade from the MT5 mobile app),
analyzes trading behaviour, and emails you:

  - after EVERY closed trade        -> a short review of that trade
  - after every 5th closed trade    -> a summary of the last 5 trades
  - after every 10th closed trade   -> a deeper review of the last 10 trades
    (psychology, discipline, tilt, equity curve chart)

The script runs periodically (see .github/workflows/trade-review.yml) and stores
its state (what has already been processed) in state.json, which it commits back
to the repository.
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
        return state
    return {
        "last_check_time": None,
        "seen_deal_ids": [],
        "total_closed_trades": 0,
        "recent_trades": [],  # last up to 10 closed trades
        "equity_curve": [],  # balance history per closed trade since tracking began
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
    trades in a different colour. Returns the path to the saved png, or None if too few points."""
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

    if depth == "10":
        lines.append("")
        lines.append("Psychology (over 10 trades):")
        streak = max_loss_streak(trades)
        if streak <= 2:
            lines.append(f"✅ Longest losing streak — {streak}. Drawdowns are short, easier to handle emotionally.")
        else:
            lines.append(
                f"💡 There was a streak of {streak} losing trades in a row. Streaks are normal, the market "
                "isn't obligated to move smoothly. The main thing is not to try to 'win it back' by sharply "
                "increasing risk during a streak like that."
            )
        if detect_lot_increase_after_loss(trades):
            lines.append(
                "⚠️ Lot size noticeably increased right after a losing trade — looks like an attempt to win "
                "it back quickly. Next step — after a loss, deliberately keep the position size the same or "
                "even smaller, not bigger."
            )
        if detect_fast_reentry_after_loss(trades):
            lines.append(
                "⚠️ There's an entry into a new trade less than 5 minutes after a loss — looks like an "
                "impulsive decision. Try taking a pause of at least 10–15 minutes after any loss before the "
                "next entry."
            )
        if not detect_lot_increase_after_loss(trades) and not detect_fast_reentry_after_loss(trades):
            lines.append("✅ No clear signs of tilt (lot size increase or instant re-entry after a loss) — good emotional control.")
        best_hour, worst_hour = hour_breakdown(trades)
        if best_hour is not None:
            lines.append(f"📊 Best hour by result: {best_hour}:00, worst: {worst_hour}:00 (broker server time) — might be useful when picking times to enter.")

    return "\n".join(lines)


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

        send_email(
            f"MT5: Trade #{n} closed ({trade['symbol']}, {fmt_money(trade['profit'])})",
            build_single_email(trade, n),
        )

        if n % 5 == 0:
            send_email(f"MT5: Summary for trades {n-4}-{n}", build_summary_email(state["recent_trades"][-5:], n, "5"))

        if n % 10 == 0:
            summary_text = build_summary_email(state["recent_trades"][-10:], n, "10")
            chart_path = build_equity_chart(state["equity_curve"], highlight_last=10)
            if chart_path:
                send_email_with_chart(f"MT5: Deep review for trades {n-9}-{n}", summary_text, chart_path)
            else:
                send_email(f"MT5: Deep review for trades {n-9}-{n}", summary_text)

    state["last_check_time"] = now.isoformat()
    save_state(state)
    print(f"Done. New closed trades: {len(closing_deals)}. Total processed: {state['total_closed_trades']}")


if __name__ == "__main__":
    asyncio.run(main())
