#!/usr/bin/env python3
"""Collect historical BTC 15m markets and CLOB price history from Polymarket.

Read-only. No wallet, private key, order or trading API is used.

BTC 15m market slugs are deterministic: btc-updown-15m-<window_start_unix>.
This avoids the sparse/unstable generic Gamma market feed for this recurring series.
"""

import json
import os
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone

CLOB_HISTORY_URL = "https://clob.polymarket.com/prices-history"
MARKET_URL = "https://gamma-api.polymarket.com/markets/slug/{}"
OUTPUT = "data/markets_btc15m.json"
LOOKBACK_DAYS = 30
WINDOW_SECONDS = 900


def get_json(url, params=None):
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": "PolymarketBTC15mLab/0.6"})
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


def collect_market_by_slug(slug):
    try:
        data = get_json(MARKET_URL.format(slug))
    except Exception as exc:
        print(f"missing {slug}: {exc}")
        return None
    if isinstance(data, dict):
        return data
    return None


def collect_price_history(token_id, start_ts, end_ts):
    params = {
        "market": token_id,
        "startTs": int(start_ts),
        "endTs": int(end_ts),
        "fidelity": 1,
    }
    data = get_json(CLOB_HISTORY_URL, params)
    return data.get("history", []) if isinstance(data, dict) else []


def normalize_market(market):
    outcomes = parse_json_field(market.get("outcomes"))
    prices = parse_json_field(market.get("outcomePrices"))
    token_ids = parse_json_field(market.get("clobTokenIds"))

    end_date = market.get("endDate")
    start_date = market.get("startDate")
    start_ts = None
    end_ts = None
    if start_date:
        start_ts = int(datetime.fromisoformat(start_date.replace("Z", "+00:00")).timestamp())
    if end_date:
        end_ts = int(datetime.fromisoformat(end_date.replace("Z", "+00:00")).timestamp())

    result = {
        "id": market.get("id"),
        "question": market.get("question"),
        "slug": market.get("slug"),
        "condition_id": market.get("conditionId"),
        "start_date": start_date,
        "end_date": end_date,
        "closed_time": market.get("closedTime"),
        "resolution_source": market.get("resolutionSource"),
        "resolved_by": market.get("resolvedBy"),
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

    if start_ts is None or end_ts is None:
        return result

    for token_id in token_ids:
        if not token_id:
            continue
        try:
            result["price_history"][str(token_id)] = collect_price_history(token_id, start_ts, end_ts)
            time.sleep(0.10)
        except Exception as exc:
            result["price_history"][str(token_id)] = {"error": str(exc)}

    return result


def main():
    os.makedirs(os.path.dirname(OUTPUT), exist_ok=True)

    now = int(datetime.now(timezone.utc).timestamp())
    latest_start = (now // WINDOW_SECONDS) * WINDOW_SECONDS
    first_start = latest_start - LOOKBACK_DAYS * 24 * 60 * 60
    expected = (latest_start - first_start) // WINDOW_SECONDS + 1

    normalized = []
    missing = 0
    print(f"Deterministic discovery: {expected} BTC 15m slugs over {LOOKBACK_DAYS} days")

    current = first_start
    index = 0
    while current <= latest_start:
        index += 1
        slug = f"btc-updown-15m-{current}"
        market = collect_market_by_slug(slug)
        if market is None:
            missing += 1
            current += WINDOW_SECONDS
            continue
        try:
            normalized.append(normalize_market(market))
            if index % 50 == 0 or index == 1:
                print(f"[{index}/{expected}] collected={len(normalized)} missing={missing}")
        except Exception as exc:
            print(f"[{index}/{expected}] normalize failed {slug}: {exc}")
        current += WINDOW_SECONDS
        time.sleep(0.05)

    normalized.sort(key=lambda item: item.get("endDate") or item.get("end_date") or "")

    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source": "Polymarket Gamma API market-by-slug + CLOB prices-history",
        "lookback_days": LOOKBACK_DAYS,
        "price_history_fidelity_minutes": 1,
        "markets_expected": expected,
        "markets_found": len(normalized),
        "markets_missing": missing,
        "markets": normalized,
    }
    with open(OUTPUT, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
    print(f"Saved {len(normalized)} BTC 15m markets to {OUTPUT}; missing={missing}")


if __name__ == "__main__":
    main()
