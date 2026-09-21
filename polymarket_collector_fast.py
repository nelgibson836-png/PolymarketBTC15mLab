#!/usr/bin/env python3
"""Robust historical collector for Polymarket BTC 15m markets."""
import json
import os
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

MARKET_URL = "https://gamma-api.polymarket.com/markets/slug/{}"
CLOB_URL = "https://clob.polymarket.com/prices-history"
OUTPUT = "data/markets_btc15m.json"
LOOKBACK_DAYS = 14
WINDOW = 900
DISCOVERY_WORKERS = 12
HISTORY_WORKERS = 6
RETRIES = 4


def get_json(url, params=None):
    if params:
        url += "?" + urllib.parse.urlencode(params)
    last_error = None
    for attempt in range(RETRIES):
        try:
            req = urllib.request.Request(
                url,
                headers={"User-Agent": "PolymarketBTC15mLab/1.2", "Accept": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=25) as r:
                return json.loads(r.read().decode("utf-8"))
        except Exception as exc:
            last_error = exc
            if attempt + 1 < RETRIES:
                time.sleep(min(4.0, 0.75 * (attempt + 1)))
    raise last_error


def field(value):
    if isinstance(value, (list, dict)):
        return value
    if not value:
        return []
    try:
        return json.loads(value)
    except Exception:
        return []


def fetch_market(start_ts):
    slug = f"btc-updown-15m-{start_ts}"
    try:
        data = get_json(MARKET_URL.format(slug))
        if not isinstance(data, dict):
            return None
        data["_window_start_ts"] = start_ts
        return data
    except Exception as exc:
        print(f"market miss {slug}: {exc}", flush=True)
        return None


def fetch_history(token, start_ts, end_ts):
    try:
        data = get_json(
            CLOB_URL,
            {"market": token, "startTs": start_ts, "endTs": end_ts, "fidelity": 1},
        )
        return data.get("history", []) if isinstance(data, dict) else []
    except Exception as exc:
        return {"error": str(exc)}


def normalize_market(market):
    start_ts = int(market["_window_start_ts"])
    end_ts = start_ts + WINDOW
    outcomes = field(market.get("outcomes"))
    prices = field(market.get("outcomePrices"))
    tokens = field(market.get("clobTokenIds"))
    result = {
        "id": market.get("id"),
        "question": market.get("question"),
        "slug": market.get("slug"),
        "condition_id": market.get("conditionId"),
        "start_date": market.get("startDate"),
        "end_date": market.get("endDate"),
        "window_start_ts": start_ts,
        "window_end_ts": end_ts,
        "closed_time": market.get("closedTime"),
        "resolution_source": market.get("resolutionSource"),
        "resolved_by": market.get("resolvedBy"),
        "outcomes": outcomes,
        "outcome_prices": prices,
        "clob_token_ids": tokens,
        "active": market.get("active"),
        "closed": market.get("closed"),
        "volume": market.get("volumeNum", market.get("volume")),
        "liquidity": market.get("liquidityNum", market.get("liquidity")),
        "best_bid": market.get("bestBid"),
        "best_ask": market.get("bestAsk"),
        "last_trade_price": market.get("lastTradePrice"),
        "price_history": {},
    }

    valid_tokens = [str(token) for token in tokens if token]
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = {
            token: pool.submit(fetch_history, token, start_ts, end_ts)
            for token in valid_tokens[:2]
        }
        for token, future in futures.items():
            try:
                result["price_history"][token] = future.result()
            except Exception as exc:
                result["price_history"][token] = {"error": str(exc)}
    return result


def main():
    os.makedirs("data", exist_ok=True)
    now = int(datetime.now(timezone.utc).timestamp())
    latest = now // WINDOW * WINDOW
    first = latest - LOOKBACK_DAYS * 86400
    starts = list(range(first, latest, WINDOW))

    print(
        f"Discovery: {len(starts)} BTC 15m windows over {LOOKBACK_DAYS} days with {DISCOVERY_WORKERS} workers",
        flush=True,
    )

    raw_markets = []
    with ThreadPoolExecutor(max_workers=DISCOVERY_WORKERS) as pool:
        futures = {pool.submit(fetch_market, start): start for start in starts}
        done = 0
        for future in as_completed(futures):
            done += 1
            market = future.result()
            if market:
                raw_markets.append(market)
            if done % 50 == 0 or done == len(starts):
                print(f"Discovery {done}/{len(starts)} found={len(raw_markets)}", flush=True)

    raw_markets.sort(key=lambda m: m.get("_window_start_ts", 0))
    print(f"History: downloading CLOB histories for {len(raw_markets)} markets with {HISTORY_WORKERS} workers", flush=True)

    markets = []
    errors = 0
    with ThreadPoolExecutor(max_workers=HISTORY_WORKERS) as pool:
        futures = {pool.submit(normalize_market, m): m for m in raw_markets}
        done = 0
        for future in as_completed(futures):
            done += 1
            try:
                markets.append(future.result())
            except Exception as exc:
                errors += 1
                print(f"history error: {exc}", flush=True)
            if done % 25 == 0 or done == len(futures):
                print(f"History {done}/{len(futures)} completed={len(markets)} errors={errors}", flush=True)

    markets.sort(key=lambda m: m.get("window_start_ts", 0))
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source": "Polymarket Gamma market-by-slug + CLOB prices-history",
        "lookback_days": LOOKBACK_DAYS,
        "price_history_fidelity_minutes": 1,
        "markets_expected": len(starts),
        "markets_found": len(markets),
        "markets_missing": len(starts) - len(raw_markets),
        "history_errors": errors,
        "markets": markets,
    }
    with open(OUTPUT, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    print(f"Saved {len(markets)} markets to {OUTPUT}; history_errors={errors}", flush=True)


if __name__ == "__main__":
    main()
