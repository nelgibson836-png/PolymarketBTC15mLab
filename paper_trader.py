#!/usr/bin/env python3
"""
Continuous paper trader for Polymarket BTC 15m.

No credentials and no order placement. Public Binance and Polymarket data only.
Uses live ask-book simulation, SQLite ledger, risk gates, and public settlement.
"""
import argparse
import json
import math
import os
import sqlite3
import threading
import time
from datetime import datetime, timezone
from urllib.request import Request, urlopen

import websocket

from paper_engine import RiskState, net_expected_edge, simulate_market_buy

GAMMA_URL = "https://gamma-api.polymarket.com/markets/slug/{}"
BINANCE_REST = "https://api.binance.com"
BINANCE_WS = "wss://stream.binance.com:9443/ws/btcusdt@kline_1m"
POLYMARKET_WS = "wss://ws-subscriptions-clob.polymarket.com/ws/market"

TRAIN_DAYS = 21
TRAIN_LIMIT = 1000
ENTRY_MINUTES = (3, 5)
MIN_TRAIN_SAMPLES = 100
MOVE_BINS = [-math.inf, -0.30, -0.20, -0.10, -0.05, 0.00, 0.05, 0.10, 0.20, 0.30, math.inf]

INITIAL_CAPITAL = float(os.getenv("PAPER_INITIAL_CAPITAL", "10"))
POSITION_USDC = float(os.getenv("PAPER_POSITION_USDC", "1"))
MAX_POSITION_FRACTION = float(os.getenv("PAPER_MAX_POSITION_FRACTION", "0.20"))
MAX_DAILY_LOSS_FRACTION = float(os.getenv("PAPER_MAX_DAILY_LOSS_FRACTION", "0.10"))
MIN_NET_EDGE = float(os.getenv("PAPER_MIN_NET_EDGE", "0.05"))
MIN_FILL_RATIO = float(os.getenv("PAPER_MIN_FILL_RATIO", "0.80"))
MAX_DATA_AGE_MS = int(os.getenv("PAPER_MAX_DATA_AGE_MS", "3000"))
MODEL_REFRESH_SECONDS = int(os.getenv("PAPER_MODEL_REFRESH_SECONDS", str(4 * 60 * 60)))
LOOP_SECONDS = float(os.getenv("PAPER_LOOP_SECONDS", "1.0"))
DB_PATH = os.getenv("PAPER_DB_PATH", "data/paper_trader.sqlite3")


def utc_now_ms():
    return int(datetime.now(timezone.utc).timestamp() * 1000)


def iso_now():
    return datetime.now(timezone.utc).isoformat()


def get_json(url, timeout=20):
    req = Request(url, headers={"User-Agent": "PolymarketBTC15mLab-paper/1.0"})
    with urlopen(req, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def pct_change(a, b):
    return 0.0 if a == 0 else (b - a) / a * 100.0


def move_bucket(move):
    for i in range(len(MOVE_BINS) - 1):
        if MOVE_BINS[i] <= move < MOVE_BINS[i + 1]:
            return i
    return len(MOVE_BINS) - 2


def fetch_training_klines():
    now_ms = utc_now_ms()
    start_ms = now_ms - TRAIN_DAYS * 86400 * 1000
    rows = []
    current = start_ms
    while current < now_ms:
        url = (
            f"{BINANCE_REST}/api/v3/klines?symbol=BTCUSDT&interval=1m"
            f"&startTime={current}&endTime={now_ms}&limit={TRAIN_LIMIT}"
        )
        data = get_json(url)
        if not data:
            break
        for k in data:
            if int(k[6]) >= now_ms:
                continue
            rows.append({
                "open_time": int(k[0]),
                "open": float(k[1]),
                "close": float(k[4]),
                "close_time": int(k[6]),
            })
        nxt = int(data[-1][0]) + 60000
        if nxt <= current:
            break
        current = nxt
        time.sleep(0.06)
    return list({r["open_time"]: r for r in rows}.values())


def build_training_model(rows):
    grouped = {}
    for row in rows:
        start = (row["open_time"] // 900000) * 900000
        grouped.setdefault(start, []).append(row)

    stats = {}
    for candle_start, candle_rows in grouped.items():
        candle_rows.sort(key=lambda x: x["open_time"])
        if len(candle_rows) != 15:
            continue
        expected = [candle_start + i * 60000 for i in range(15)]
        if [r["open_time"] for r in candle_rows] != expected:
            continue

        base = candle_rows[0]["open"]
        final_close = candle_rows[14]["close"]
        if final_close == base:
            continue
        actual = 1 if final_close > base else 0

        for minute in ENTRY_MINUTES:
            move = pct_change(base, candle_rows[minute - 1]["close"])
            key = (minute, move_bucket(move))
            item = stats.setdefault(key, [0, 0])
            item[0] += 1
            item[1] += actual

    model = {
        key: wins / count
        for key, (count, wins) in stats.items()
        if count >= MIN_TRAIN_SAMPLES
    }
    return model, len(rows), len(grouped)


def parse_list(value):
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        return json.loads(value)
    return []


def parse_market(market):
    outcomes = parse_list(market.get("outcomes"))
    tokens = parse_list(market.get("clobTokenIds"))
    result = {}
    for idx, outcome in enumerate(outcomes):
        if idx < len(tokens):
            result[str(outcome).lower()] = str(tokens[idx])
    return result


def resolved_outcome(market):
    prices = parse_list(market.get("outcomePrices") or market.get("outcome_prices"))
    try:
        if len(prices) >= 2:
            up = float(prices[0])
            down = float(prices[1])
            if up > 0.99 and down < 0.01:
                return "up"
            if down > 0.99 and up < 0.01:
                return "down"
    except (TypeError, ValueError):
        return None
    return None


def market_slug_for_now():
    start_ts = (int(time.time()) // 900) * 900
    return f"btc-updown-15m-{start_ts}", start_ts


class BinanceFeed:
    def __init__(self):
        self.lock = threading.RLock()
        self.closed = {}
        self.current = None
        self.last_message_ms = 0
        self.stop_event = threading.Event()
        self.thread = None

    def seed(self, market_start_ms):
        url = (
            f"{BINANCE_REST}/api/v3/klines?symbol=BTCUSDT&interval=1m"
            f"&startTime={market_start_ms}&limit=8"
        )
        rows = get_json(url)
        with self.lock:
            self.closed = {}
            self.current = None
            for k in rows:
                row = {
                    "open_time": int(k[0]),
                    "open": float(k[1]),
                    "close": float(k[4]),
                    "closed": int(k[6]) < utc_now_ms(),
                }
                if row["closed"]:
                    self.closed[row["open_time"]] = row
                self.current = row
            self.closed = dict(sorted(self.closed.items())[-20:])
            self.last_message_ms = utc_now_ms()

    def start(self):
        def on_message(_, raw):
            try:
                message = json.loads(raw)
                k = message.get("k", {})
                row = {
                    "open_time": int(k["t"]),
                    "open": float(k["o"]),
                    "close": float(k["c"]),
                    "closed": bool(k["x"]),
                }
                with self.lock:
                    self.last_message_ms = utc_now_ms()
                    self.current = row
                    if row["closed"]:
                        self.closed[row["open_time"]] = row
                        self.closed = dict(sorted(self.closed.items())[-20:])
            except Exception as exc:
                print(f"[BINANCE] message error: {exc}")

        def runner():
            while not self.stop_event.is_set():
                app = websocket.WebSocketApp(
                    BINANCE_WS,
                    on_message=on_message,
                    on_error=lambda _, error: print(f"[BINANCE] error: {error}"),
                    on_close=lambda *_: print("[BINANCE] disconnected"),
                    on_open=lambda _: print("[BINANCE] connected"),
                )
                try:
                    app.run_forever()
                except Exception as exc:
                    print(f"[BINANCE] connection exception: {exc}")
                if not self.stop_event.is_set():
                    time.sleep(2)

        self.thread = threading.Thread(target=runner, daemon=True)
        self.thread.start()

    def stop(self):
        self.stop_event.set()

    def get_closed(self, open_time):
        with self.lock:
            return self.closed.get(open_time)

    def age_ms(self):
        with self.lock:
            return utc_now_ms() - self.last_message_ms if self.last_message_ms else 10**9


class PolymarketBookFeed:
    def __init__(self):
        self.lock = threading.RLock()
        self.books = {}
        self.token_ids = set()
        self.stop_event = threading.Event()
        self.thread = None
        self.ws = None
        self.market_slug = None

    def set_market(self, slug, token_ids):
        token_set = set(token_ids)
        with self.lock:
            if slug == self.market_slug and token_set == self.token_ids:
                return
            old_stop = self.stop_event
            old_stop.set()
            if self.ws is not None:
                try:
                    self.ws.close()
                except Exception:
                    pass
            run_stop = threading.Event()
            self.stop_event = run_stop
            self.market_slug = slug
            self.token_ids = token_set
            self.books = {}

        def on_open(ws):
            ws.send(json.dumps({
                "assets_ids": list(self.token_ids),
                "type": "market",
            }))
            print(f"[POLY] subscribed {slug}")

        def apply_book(payload):
            token = str(payload.get("tokenId", ""))
            bids = {float(x["price"]): float(x["size"]) for x in payload.get("bids", []) or []}
            asks = {float(x["price"]): float(x["size"]) for x in payload.get("asks", []) or []}
            with self.lock:
                self.books[token] = {
                    "bids": bids,
                    "asks": asks,
                    "received_ms": utc_now_ms(),
                    "exchange_ts": int(payload.get("timestamp") or 0),
                }

        def apply_change(payload):
            with self.lock:
                for change in payload.get("priceChanges", []) or []:
                    token = str(change.get("tokenId", ""))
                    book = self.books.setdefault(
                        token,
                        {"bids": {}, "asks": {}, "received_ms": 0, "exchange_ts": 0},
                    )
                    price = float(change["price"])
                    size = float(change["size"])
                    target = book["bids"] if str(change.get("side", "")).upper() == "BUY" else book["asks"]
                    if size <= 0:
                        target.pop(price, None)
                    else:
                        target[price] = size
                    book["received_ms"] = utc_now_ms()
                    book["exchange_ts"] = int(payload.get("timestamp") or 0)

        def on_message(_, raw):
            try:
                message = json.loads(raw)
                if message == "PONG":
                    return
                payload = message.get("payload", {})
                if message.get("type") == "book":
                    apply_book(payload)
                elif message.get("type") == "price_change":
                    apply_change(payload)
            except Exception as exc:
                print(f"[POLY] message error: {exc}")

        def runner():
            while not self.stop_event.is_set():
                app = websocket.WebSocketApp(
                    POLYMARKET_WS,
                    on_open=on_open,
                    on_message=on_message,
                    on_error=lambda _, error: print(f"[POLY] error: {error}"),
                    on_close=lambda *_: print("[POLY] disconnected"),
                )
                with self.lock:
                    self.ws = app

                def ping():
                    while not run_stop.is_set():
                        time.sleep(10)
                        if run_stop.is_set():
                            return
                        try:
                            app.send("PING")
                        except Exception:
                            return

                threading.Thread(target=ping, daemon=True).start()
                try:
                    app.run_forever()
                except Exception as exc:
                    print(f"[POLY] connection exception: {exc}")
                if not self.stop_event.is_set():
                    time.sleep(2)

        self.thread = threading.Thread(target=runner, daemon=True)
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        with self.lock:
            if self.ws is not None:
                try:
                    self.ws.close()
                except Exception:
                    pass

    def asks(self, token_id):
        with self.lock:
            book = self.books.get(str(token_id))
            if not book:
                return []
            return sorted(book["asks"].items())

    def book_age_ms(self, token_id):
        with self.lock:
            book = self.books.get(str(token_id))
            if not book or not book["received_ms"]:
                return 10**9
            return utc_now_ms() - book["received_ms"]


class PaperTrader:
    def __init__(self, reset=False):
        os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
        if reset and os.path.exists(DB_PATH):
            os.remove(DB_PATH)
        self.db = sqlite3.connect(DB_PATH, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self._init_db()
        self.lock = threading.RLock()
        self.risk = self._load_account()
        self.binance = BinanceFeed()
        self.polymarket = PolymarketBookFeed()
        self.market = None
        self.processed = set()
        self.model = {}
        self.model_loaded_at = 0.0
        self.last_status = 0.0

    def _init_db(self):
        with self.db:
            self.db.execute(
                "CREATE TABLE IF NOT EXISTS account_state("
                "id INTEGER PRIMARY KEY CHECK(id=1), initial_capital REAL NOT NULL,"
                "cash REAL NOT NULL, realized_pnl_today REAL NOT NULL, state_day TEXT NOT NULL)"
            )
            self.db.execute(
                "CREATE TABLE IF NOT EXISTS paper_trades("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, market_slug TEXT NOT NULL,"
                "market_start_ms INTEGER NOT NULL, market_end_ms INTEGER NOT NULL,"
                "entry_minute INTEGER NOT NULL, side TEXT NOT NULL, token_id TEXT NOT NULL,"
                "model_probability REAL NOT NULL, best_ask REAL NOT NULL,"
                "average_fill_price REAL NOT NULL, worst_fill_price REAL NOT NULL,"
                "shares REAL NOT NULL, gross_cost REAL NOT NULL, fees REAL NOT NULL,"
                "total_cost REAL NOT NULL, net_edge REAL NOT NULL, fill_ratio REAL NOT NULL,"
                "latency_ms INTEGER NOT NULL, status TEXT NOT NULL, outcome TEXT,"
                "payout REAL, pnl REAL, created_at TEXT NOT NULL, settled_at TEXT,"
                "UNIQUE(market_slug, entry_minute))"
            )
            self.db.execute(
                "CREATE TABLE IF NOT EXISTS events("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL,"
                "event_type TEXT NOT NULL, market_slug TEXT, reason TEXT, payload TEXT)"
            )

    def _load_account(self):
        day = datetime.now(timezone.utc).date().isoformat()
        row = self.db.execute("SELECT * FROM account_state WHERE id=1").fetchone()
        if row is None:
            state = RiskState.create(INITIAL_CAPITAL)
            self._save_account(state, day)
            return state
        state = RiskState(
            initial_capital=float(row["initial_capital"]),
            cash=float(row["cash"]),
            realized_pnl_today=float(row["realized_pnl_today"]),
        )
        if row["state_day"] != day:
            state.realized_pnl_today = 0.0
            self._save_account(state, day)
        return state

    def _save_account(self):
        day = datetime.now(timezone.utc).date().isoformat()
        with self.db:
            self.db.execute(
                "INSERT INTO account_state(id,initial_capital,cash,realized_pnl_today,state_day)"
                " VALUES(1,?,?,?,?) ON CONFLICT(id) DO UPDATE SET"
                " initial_capital=excluded.initial_capital,cash=excluded.cash,"
                " realized_pnl_today=excluded.realized_pnl_today,state_day=excluded.state_day",
                (self.risk.initial_capital, self.risk.cash, self.risk.realized_pnl_today, day),
            )

    def _event(self, event_type, reason="", payload=None):
        with self.db:
            self.db.execute(
                "INSERT INTO events(ts,event_type,market_slug,reason,payload) VALUES(?,?,?,?,?)",
                (
                    iso_now(),
                    event_type,
                    self.market.get("slug") if self.market else None,
                    reason,
                    json.dumps(payload or {}, ensure_ascii=False),
                ),
            )

    def discover_market(self):
        slug, start_ts = market_slug_for_now()
        if self.market and self.market.get("slug") == slug:
            return self.market
        try:
            market = get_json(GAMMA_URL.format(slug))
            if not isinstance(market, dict):
                return None
            if not market.get("clobTokenIds") or not market.get("outcomes"):
                return None
            market["_window_start_ts"] = start_ts
            market["_window_end_ts"] = start_ts + 900
            return market
        except Exception as exc:
            self._event("market_discovery_error", str(exc), {"slug": slug})
            return None

    def switch_market(self, market):
        if self.market and self.market.get("slug") == market.get("slug"):
            return
        if self.market:
            self.polymarket.stop()
        self.market = market
        tokens = parse_market(market)
        self.polymarket.set_market(
            market["slug"],
            [tokens[name] for name in ("up", "down") if name in tokens],
        )
        self.binance.seed(int(market["_window_start_ts"]) * 1000)
        self._event(
            "market_open",
            payload={
                "slug": market.get("slug"),
                "resolution_source": market.get("resolutionSource"),
                "crypto_market_config": market.get("cryptoMarketConfig"),
            },
        )
        print(f"[MARKET] {market['slug']}")

    def refresh_model(self, force=False):
        if not force and time.time() - self.model_loaded_at < MODEL_REFRESH_SECONDS:
            return
        rows = fetch_training_klines()
        model, sample_rows, candles = build_training_model(rows)
        if not model:
            raise RuntimeError("training model is empty")
        self.model = model
        self.model_loaded_at = time.time()
        self._event("model_refresh", payload={
            "rows": sample_rows, "grouped_candles": candles, "states": len(model)
        })
        print(f"[MODEL] states={len(model)} rows={sample_rows} candles={candles}")

    def already_traded(self, slug):
        row = self.db.execute(
            "SELECT 1 FROM paper_trades WHERE market_slug=? AND status IN('filled','settled') LIMIT 1",
            (slug,),
        ).fetchone()
        return row is not None

    def evaluate_entry(self, entry_minute, candle):
        if entry_minute not in ENTRY_MINUTES:
            return
        slug = self.market["slug"]
        key = (slug, entry_minute)
        if key in self.processed:
            return
        self.processed.add(key)

        if self.already_traded(slug):
            self._event("risk_block", "one_trade_per_market", {"entry_minute": entry_minute})
            return

        start_ms = int(self.market["_window_start_ts"]) * 1000
        base_row = self.binance.get_closed(start_ms)
        if not base_row:
            self._event("signal_block", "missing_base_candle", {"entry_minute": entry_minute})
            return

        move = pct_change(base_row["open"], candle["close"])
        bucket = move_bucket(move)
        probability_up = self.model.get((entry_minute, bucket))
        if probability_up is None:
            self._event("signal_block", "untrained_state", {"entry_minute": entry_minute, "bucket": bucket})
            return

        side = "up" if probability_up >= 0.5 else "down"
        model_probability = probability_up if side == "up" else 1.0 - probability_up
        token_id = parse_market(self.market).get(side)
        if not token_id:
            self._event("signal_block", "missing_token", {"side": side})
            return

        book_age = self.polymarket.book_age_ms(token_id)
        binance_age = self.binance.age_ms()
        if book_age > MAX_DATA_AGE_MS or binance_age > MAX_DATA_AGE_MS:
            self._event("signal_block", "stale_data", {
                "book_age_ms": book_age, "binance_age_ms": binance_age
            })
            return

        asks = self.polymarket.asks(token_id)
        if not asks:
            self._event("signal_block", "missing_ask", {"side": side})
            return

        budget = min(POSITION_USDC, self.risk.max_position_usdc(MAX_POSITION_FRACTION))
        allowed, reason = self.risk.can_enter(
            budget, MAX_POSITION_FRACTION, MAX_DAILY_LOSS_FRACTION
        )
        if not allowed:
            self._event("risk_block", reason, {"budget": budget})
            return

        fill = simulate_market_buy(asks, budget)
        if fill.shares <= 0:
            self._event("fill_block", "no_liquidity")
            return
        if fill.fill_ratio < MIN_FILL_RATIO:
            self._event("fill_block", "insufficient_depth", {
                "fill_ratio": fill.fill_ratio, "required": MIN_FILL_RATIO
            })
            return

        edge = net_expected_edge(model_probability, fill)
        if edge < MIN_NET_EDGE:
            self._event("signal_block", "edge_below_threshold", {
                "model_probability": model_probability,
                "average_fill_price": fill.average_price,
                "fees": fill.fees,
                "net_edge": edge,
                "threshold": MIN_NET_EDGE,
            })
            return

        latency_ms = max(book_age, binance_age)
        self.risk.reserve(fill)
        self._save_account()

        with self.db:
            self.db.execute(
                "INSERT INTO paper_trades("
                "market_slug,market_start_ms,market_end_ms,entry_minute,side,token_id,"
                "model_probability,best_ask,average_fill_price,worst_fill_price,shares,"
                "gross_cost,fees,total_cost,net_edge,fill_ratio,latency_ms,status,created_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    slug, start_ms, int(self.market["_window_end_ts"]) * 1000,
                    entry_minute, side, token_id, model_probability, asks[0][0],
                    fill.average_price, fill.worst_price, fill.shares, fill.gross_cost,
                    fill.fees, fill.total_cost, edge, fill.fill_ratio, latency_ms,
                    "filled", iso_now(),
                ),
            )
        self._event("paper_fill", payload={
            "entry_minute": entry_minute,
            "side": side,
            "model_probability": model_probability,
            "best_ask": asks[0][0],
            "average_fill_price": fill.average_price,
            "worst_fill_price": fill.worst_price,
            "shares": fill.shares,
            "gross_cost": fill.gross_cost,
            "fees": fill.fees,
            "total_cost": fill.total_cost,
            "net_edge": edge,
            "fill_ratio": fill.fill_ratio,
            "latency_ms": latency_ms,
        })
        print(
            f"[PAPER FILL] {side.upper()} m{entry_minute} "
            f"p={model_probability:.3f} ask={asks[0][0]:.3f} "
            f"avg={fill.average_price:.3f} edge={edge:.3f} cost={fill.total_cost:.3f}"
        )

    def settle_trades(self):
        rows = self.db.execute(
            "SELECT * FROM paper_trades WHERE status='filled' AND market_end_ms < ?",
            (utc_now_ms(),),
        ).fetchall()
        for trade in rows:
            try:
                market = get_json(GAMMA_URL.format(trade["market_slug"]))
                outcome = resolved_outcome(market)
                if outcome is None:
                    continue
                payout = float(trade["shares"]) if outcome == trade["side"] else 0.0
                pnl = self.risk.settle(payout, float(trade["total_cost"]))
                self._save_account()
                with self.db:
                    self.db.execute(
                        "UPDATE paper_trades SET status='settled', outcome=?, payout=?, pnl=?, settled_at=? WHERE id=?",
                        (outcome, payout, pnl, iso_now(), trade["id"]),
                    )
                self._event("settlement", payload={
                    "trade_id": trade["id"], "outcome": outcome, "payout": payout, "pnl": pnl
                })
                print(f"[SETTLED] {trade['market_slug']} {outcome.upper()} pnl={pnl:.3f}")
            except Exception as exc:
                self._event("settlement_error", str(exc), {"trade_id": trade["id"]})

    def status(self):
        row = self.db.execute(
            "SELECT COUNT(*) trades,"
            "SUM(CASE WHEN status='settled' THEN 1 ELSE 0 END) settled,"
            "SUM(CASE WHEN status='settled' AND pnl>0 THEN 1 ELSE 0 END) wins,"
            "COALESCE(SUM(CASE WHEN status='settled' THEN pnl ELSE 0 END),0) pnl"
            " FROM paper_trades"
        ).fetchone()
        self._save_account()
        print(
            f"[STATUS] cash={self.risk.cash:.3f} today_pnl={self.risk.realized_pnl_today:.3f} "
            f"trades={row['trades'] or 0} settled={row['settled'] or 0} "
            f"wins={row['wins'] or 0} pnl={row['pnl']:.3f}"
        )

    def run(self, duration=None):
        print("=== Polymarket BTC 15m PAPER TRADER ===")
        print("PAPER ONLY: no credentials, no order placement.")
        print(f"DB={DB_PATH} capital={INITIAL_CAPITAL:.2f} position={POSITION_USDC:.2f}")
        self.refresh_model(force=True)
        self.binance.start()
        started = time.time()

        try:
            while True:
                market = self.discover_market()
                if market:
                    self.switch_market(market)

                if self.market:
                    self.refresh_model()
                    current = self.binance.current
                    if current and current["closed"]:
                        start_ms = int(self.market["_window_start_ts"]) * 1000
                        minute = int((current["open_time"] - start_ms) / 60000) + 1
                        self.evaluate_entry(minute, current)

                self.settle_trades()
                if time.time() - self.last_status >= 30:
                    self.last_status = time.time()
                    self.status()

                if duration and time.time() - started >= duration:
                    break
                time.sleep(LOOP_SECONDS)
        finally:
            self.polymarket.stop()
            self.binance.stop()
            self.status()
            self.db.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--duration", type=int, default=0)
    parser.add_argument("--reset", action="store_true")
    args = parser.parse_args()
    PaperTrader(reset=args.reset).run(args.duration or None)


if __name__ == "__main__":
    main()
