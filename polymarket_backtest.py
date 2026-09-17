#!/usr/bin/env python3
"""Backtest Binance signal vs historical Polymarket BTC 15m prices.

Read-only. No wallet, credentials or orders.

Important: historical token price is not historical executable ask. The
execution layer below therefore applies explicit stress buffers instead of
pretending that historical bid/ask is available.
"""

import json
import math
import os
import time
from datetime import datetime, timezone
from urllib.request import Request, urlopen

BINANCE_URL = "https://data-api.binance.vision"
MARKET_FILE = "data/markets_btc15m.json"
OUTPUT_FILE = "data/edge_backtest.json"
SYMBOL = "BTCUSDT"
DAYS = 45
LIMIT = 1000
TRAIN_DAYS = 21
MIN_TRAIN_SAMPLES = 100
ENTRY_MINUTES = (1, 3, 5)
EDGE_THRESHOLDS = (0.03, 0.05, 0.10)
# These are explicit entry-friction stress cases, not reconstructed historical
# spreads. A value of 0.01 means we pay 1 cent/share above the stored token price.
EXECUTION_BUFFERS = (0.00, 0.005, 0.01, 0.02)
MOVE_BINS = [-math.inf, -0.30, -0.20, -0.10, -0.05, 0.00, 0.05, 0.10, 0.20, 0.30, math.inf]
MIN_MARKETS_FOR_SERIOUS_BACKTEST = 500


def get_json(url):
    req = Request(url, headers={"User-Agent": "PolymarketBTC15mLab/0.6"})
    with urlopen(req, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


def pct_change(a, b):
    return 0.0 if a == 0 else (b - a) / a * 100.0


def move_bucket(move):
    for i in range(len(MOVE_BINS) - 1):
        if MOVE_BINS[i] <= move < MOVE_BINS[i + 1]:
            return i
    return len(MOVE_BINS) - 2


def fetch_binance():
    now = int(datetime.now(timezone.utc).timestamp() * 1000)
    start = now - DAYS * 24 * 60 * 60 * 1000
    rows = []
    current = start
    while current < now:
        url = (f"{BINANCE_URL}/api/v3/klines?symbol={SYMBOL}&interval=1m"
               f"&startTime={current}&endTime={now}&limit={LIMIT}")
        data = get_json(url)
        if not data:
            break
        rows.extend({"open_time": int(k[0]), "open": float(k[1]), "close": float(k[4])} for k in data)
        nxt = int(data[-1][0]) + 60000
        if nxt <= current:
            break
        current = nxt
        time.sleep(0.06)
    return sorted({r["open_time"]: r for r in rows}.values(), key=lambda x: x["open_time"])


def build_observations(rows):
    grouped = {}
    for r in rows:
        start = (r["open_time"] // 900000) * 900000
        grouped.setdefault(start, []).append(r)
    observations = []
    for start, rs in sorted(grouped.items()):
        rs.sort(key=lambda x: x["open_time"])
        if len(rs) < 15:
            continue
        base = rs[0]["open"]
        actual = 1 if rs[14]["close"] > base else 0
        for minute in ENTRY_MINUTES:
            current = rs[minute - 1]["close"]
            move = pct_change(base, current)
            observations.append({"start": start, "minute": minute, "move": move,
                                 "bucket": move_bucket(move), "actual_binance": actual})
    return observations


def fit_model(observations, before_ms):
    cutoff = before_ms - TRAIN_DAYS * 24 * 60 * 60 * 1000
    stats = {}
    for o in observations:
        if cutoff <= o["start"] < before_ms:
            key = (o["minute"], o["bucket"])
            item = stats.setdefault(key, [0, 0])
            item[0] += 1
            item[1] += o["actual_binance"]
    return {k: wins / n for k, (n, wins) in stats.items() if n >= MIN_TRAIN_SAMPLES}


def history_price(history, target_ts):
    if not isinstance(history, list):
        return None
    best = None
    for item in history:
        try:
            t = int(item["t"])
            p = float(item["p"])
        except (KeyError, TypeError, ValueError):
            continue
        if t <= target_ts:
            best = p
        else:
            break
    return best


def taker_fee(price):
    """Current eligible-crypto taker fee for 1 share at token price p."""
    return 0.07 * price * (1.0 - price)


def execution_price(market_price, buffer):
    return min(0.999999, market_price + buffer)


def net_expected_edge(model_probability, market_price, buffer):
    """Expected profit/share after execution buffer and taker fee."""
    price = execution_price(market_price, buffer)
    fee = taker_fee(price)
    return model_probability - price - fee


def actual_outcome(market):
    prices = market.get("outcome_prices") or []
    try:
        if len(prices) >= 2:
            up = float(prices[0])
            down = float(prices[1])
            if up > 0.9 and down < 0.1:
                return 1
            if down > 0.9 and up < 0.1:
                return 0
    except (TypeError, ValueError):
        pass
    return None


def token_map(market):
    ids = market.get("clob_token_ids") or []
    outcomes = market.get("outcomes") or []
    result = {}
    for i, outcome in enumerate(outcomes):
        if i < len(ids):
            result[str(outcome).lower()] = str(ids[i])
    return result


def summarize(candidates, threshold, minute, buffer):
    subset = [
        c for c in candidates
        if c["entry_minute"] == minute
        and c["net_edge_by_buffer"][f"{buffer:.3f}"] >= threshold
    ]
    if not subset:
        return {
            "edge_threshold": threshold,
            "entry_minute": minute,
            "execution_buffer": buffer,
            "trades": 0,
            "win_rate": None,
            "net_pnl_per_trade": None,
            "return_on_cost_percent": None,
        }

    pnl_values = []
    cost_values = []
    for c in subset:
        price = c["execution_price_by_buffer"][f"{buffer:.3f}"]
        fee = c["fee_by_buffer"][f"{buffer:.3f}"]
        pnl = ((1.0 - price) if c["won"] else -price) - fee
        pnl_values.append(pnl)
        cost_values.append(price + fee)

    wins = sum(1 for c in subset if c["won"])
    pnl = sum(pnl_values)
    cost = sum(cost_values)
    return {
        "edge_threshold": threshold,
        "entry_minute": minute,
        "execution_buffer": buffer,
        "trades": len(subset),
        "wins": wins,
        "losses": len(subset) - wins,
        "win_rate": round(wins / len(subset) * 100, 2),
        "net_pnl_per_trade": round(pnl / len(subset), 6),
        "return_on_cost_percent": round(pnl / cost * 100, 3) if cost else None,
    }


def main():
    with open(MARKET_FILE, "r", encoding="utf-8") as f:
        dataset = json.load(f)
    markets = dataset.get("markets", [])

    if len(markets) < MIN_MARKETS_FOR_SERIOUS_BACKTEST:
        raise RuntimeError(
            f"Only {len(markets)} BTC 15m markets available. "
            f"Need at least {MIN_MARKETS_FOR_SERIOUS_BACKTEST} before running the serious backtest."
        )

    rows = fetch_binance()
    observations = build_observations(rows)
    obs_map = {(o["start"], o["minute"]): o for o in observations}

    candidates = []
    skipped = 0
    for market in markets:
        try:
            end_ts = int(datetime.fromisoformat(market["end_date"].replace("Z", "+00:00")).timestamp())
            period_start = end_ts - 900
            actual = actual_outcome(market)
            tokens = token_map(market)
            history = market.get("price_history", {})
            up_hist = history.get(tokens.get("up"), [])
            down_hist = history.get(tokens.get("down"), [])
            model = fit_model(observations, period_start * 1000)

            for minute in ENTRY_MINUTES:
                obs = obs_map.get((period_start * 1000, minute))
                if not obs:
                    continue
                probability_up = model.get((minute, obs["bucket"]))
                if probability_up is None:
                    continue

                target_ts = period_start + (minute - 1) * 60
                up_price = history_price(up_hist, target_ts)
                down_price = history_price(down_hist, target_ts)
                if up_price is None or down_price is None:
                    skipped += 1
                    continue

                side = "up" if probability_up >= 0.5 else "down"
                model_p = probability_up if side == "up" else 1.0 - probability_up
                market_p = up_price if side == "up" else down_price
                gross_edge = model_p - market_p

                if actual is None:
                    skipped += 1
                    continue

                won = (side == "up" and actual == 1) or (side == "down" and actual == 0)
                net_edges = {}
                exec_prices = {}
                fees = {}
                for buffer in EXECUTION_BUFFERS:
                    key = f"{buffer:.3f}"
                    price = execution_price(market_p, buffer)
                    exec_prices[key] = round(price, 6)
                    fees[key] = round(taker_fee(price), 6)
                    net_edges[key] = round(net_expected_edge(model_p, market_p, buffer), 6)

                candidates.append({
                    "slug": market.get("slug"),
                    "market_end": market.get("end_date"),
                    "entry_minute": minute,
                    "side": side,
                    "model_probability": round(model_p, 6),
                    "market_price": round(market_p, 6),
                    "gross_edge": round(gross_edge, 6),
                    "net_edge_by_buffer": net_edges,
                    "execution_price_by_buffer": exec_prices,
                    "fee_by_buffer": fees,
                    "actual": "UP" if actual == 1 else "DOWN",
                    "won": won,
                })
        except Exception as exc:
            print(f"skip {market.get('slug')}: {exc}")
            skipped += 1

    summaries = []
    for buffer in EXECUTION_BUFFERS:
        for threshold in EDGE_THRESHOLDS:
            for minute in ENTRY_MINUTES:
                summaries.append(summarize(candidates, threshold, minute, buffer))

    output = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "markets": len(markets),
        "binance_rows": len(rows),
        "train_days": TRAIN_DAYS,
        "entry_minutes": ENTRY_MINUTES,
        "edge_thresholds": EDGE_THRESHOLDS,
        "execution_buffers": EXECUTION_BUFFERS,
        "fee_model": "0.07 * price * (1 - price) per share for eligible crypto taker entry",
        "model": "minute_from_15m_open + current_move_bucket, trained only on prior 21 Binance days",
        "candidate_observations": len(candidates),
        "summary": summaries,
        "candidates": candidates,
        "skipped": skipped,
        "status": "research_only_execution_stress_test",
        "notes": [
            "A trade is evaluated only once per market/entry minute and then filtered independently by net edge threshold.",
            "Polymarket historical token price is treated as the available historical reference price; historical bid/ask execution is not reconstructed.",
            "Execution buffers are explicit stress cases above the stored token price, not historical spread estimates.",
            "The taker fee is applied to the stressed entry price using the current eligible-crypto fee formula.",
            "Net expected edge is model probability minus stressed entry price minus taker fee.",
            "P&L uses settlement outcome and stressed entry costs; order size, historical liquidity, fill probability and queue position are not modeled.",
            "This remains a research backtest and is not sufficient for live trading; paper trading remains mandatory."
        ]
    }
    os.makedirs(os.path.dirname(OUTPUT_FILE), exist_ok=True)
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2, ensure_ascii=False)
    print(json.dumps({
        "markets": len(markets),
        "candidate_observations": len(candidates),
        "summary": summaries,
    }, indent=2))


if __name__ == "__main__":
    main()
