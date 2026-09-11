#!/usr/bin/env python3
"""Collect historical BTC 15m markets and CLOB price history from Polymarket.

Read-only collector. No wallet, private key, order or trading API is used.
"""

import json
import os
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone

GAMMA_URL = "https://gamma-api.polymarket.com/markets"
CLOB_HISTORY_URL = "https://clob.polymarket.com/prices-history"
OUTPUT = "data/markets_btc15m.json"


def get_json(url, params=None):
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": "PolymarketBTC15mLab/0.1"})
    with urllib.request.urlopen(req, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


def parse_json_field(value, default=None):
    if value is None:
        return default if default is not None else []
    if isinstance(value, (list, dict)):
        return value
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return default if default is not None else []


def is_btc_15m(market):
    text = " ".join(
        str(market.get(key, "")) for key in ("question", "slug", "description")
    ).lower()
    btc = "btc" in text or "bitcoin" in text
    fifteen = any(x in text for x in ("15m", "15 min", "15-min", "15 minute", "15minute"))
    return btc and fifteen


def collect_markets(limit=100, max_pages=10):
    markets = []
    cursor = None

    for _ in range(max_pages):
        params = {"closed": "true", "limit": limit}
        if cursor:
            params["after_cursor"] = cursor
        data = get_json(GAMMA_URL, params)
        rows = data.get("data", data) if isinstance(data, dict) else data
        if not rows:
            break
        for market in rows:
            if is_btc_15m(market):
                markets.append(market)
        cursor = data.get("next_cursor") if isinstance(data, dict) else None
        if not cursor:
            break
        time.sleep(0.15)

    return markets


def collect_price_history(token_id, start_ts=None, end_ts=None):
    params = {"market": token_id, "interval": "1m"}
    if start_ts is not None:
        params["startTs"] = int(start_ts)
    if end_ts is not None:
        params["endTs"] = int(end_ts)
    data = get_json(CLOB_HISTORY_URL, params)
    return data.get("history", []) if isinstance(data, dict) else []


def normalize_market(market):
    outcomes = parse_json_field(market.get("outcomes"))
    prices = parse_json_field(market.get("outcomePrices"))
    token_ids = parse_json_field(market.get("clobTokenIds"))

    end_date = market.get("endDate")
    start_date = market.get("startDate")
    result = {
        "id": market.get("id"),
        "question": market.get("question"),
        "slug": market.get("slug"),
        "condition_id": market.get("conditionId"),
        "start_date": start_date,
        "end_date": end_date,
        "outcomes": outcomes,
        "outcome_prices": prices,
        "clob_token_ids": token_ids,
        "active": market.get("active"),
        "closed": market.get("closed"),
        "volume": market.get("volumeNum", market.get("volume")),
        "liquidity": market.get("liquidityNum", market.get("liquidity")),
        "best_bid": market.get("bestBid"),
        "best_ask": market.get("bestAsk"),
        "last_trade_price": market.get("lastTradePrice"),
        "price_history": {},
    }

    for token_id in token_ids:
        try:
            start_ts = None
            end_ts = None
            if start_date:
                start_ts = int(datetime.fromisoformat(start_date.replace("Z", "+00:00")).timestamp())
            if end_date:
                end_ts = int(datetime.fromisoformat(end_date.replace("Z", "+00:00")).timestamp())
            result["price_history"][str(token_id)] = collect_price_history(token_id, start_ts, end_ts)
            time.sleep(0.15)
        except Exception as exc:
            result["price_history"][str(token_id)] = {"error": str(exc)}

    return result


def main():
    os.makedirs(os.path.dirname(OUTPUT), exist_ok=True)
    markets = collect_markets()
    normalized = []
    for index, market in enumerate(markets, 1):
        try:
            normalized.append(normalize_market(market))
            print(f"[{index}/{len(markets)}] collected {market.get('slug')}")
        except Exception as exc:
            print(f"[{index}/{len(markets)}] skipped: {exc}")

    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source": "Polymarket Gamma API + CLOB prices-history",
        "markets_found": len(normalized),
        "markets": normalized,
    }
    with open(OUTPUT, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
    print(f"Saved {len(normalized)} BTC 15m markets to {OUTPUT}")


if __name__ == "__main__":
    main()
