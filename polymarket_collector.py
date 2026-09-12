#!/usr/bin/env python3
"""Collect recent historical BTC 15m markets and CLOB price history from Polymarket.

Read-only. No wallet, private key, order or trading API is used.
"""

import json
import os
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

GAMMA_URL = "https://gamma-api.polymarket.com/markets"
CLOB_HISTORY_URL = "https://clob.polymarket.com/prices-history"
OUTPUT = "data/markets_btc15m.json"
LOOKBACK_DAYS = 14


def get_json(url, params=None):
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": "PolymarketBTC15mLab/0.5"})
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


def parse_end_date(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def is_btc_15m(market):
    slug = str(market.get("slug", "")).lower()
    question = str(market.get("question", "")).lower()
    if slug.startswith("btc-updown-15m-"):
        return True
    return "bitcoin" in question and "15" in question and "minute" in question


def collect_markets(lookback_days=LOOKBACK_DAYS, limit=100, max_pages=100):
    """Discover BTC 15m markets by scanning the ordered closed-market feed.

    We intentionally do not send end_date_min/end_date_max to Gamma here because
    those filters can produce a sparse result set for this market family. We
    filter the returned rows locally and stop once the ordered feed is older than
    the requested lookback window.
    """
    now = datetime.now(timezone.utc)
    min_end = now - timedelta(days=lookback_days)
    markets = []
    seen_ids = set()

    for page in range(max_pages):
        params = {
            "closed": "true",
            "limit": limit,
            "offset": page * limit,
            "order": "endDate",
            "ascending": "false",
        }
        data = get_json(GAMMA_URL, params)
        rows = data.get("data", data) if isinstance(data, dict) else data
        if not rows:
            break

        oldest_seen = None
        for market in rows:
            parsed_end = parse_end_date(market.get("endDate"))
            if parsed_end is not None:
                oldest_seen = parsed_end if oldest_seen is None else min(oldest_seen, parsed_end)

            market_id = str(market.get("id", ""))
            if not market_id or market_id in seen_ids:
                continue
            if parsed_end is None or parsed_end < min_end or parsed_end > now:
                continue
            if is_btc_15m(market):
                seen_ids.add(market_id)
                markets.append(market)

        print(f"Discovery page {page + 1}: {len(rows)} rows, BTC 15m total={len(markets)}")
        if len(rows) < limit or (oldest_seen is not None and oldest_seen < min_end):
            break
        time.sleep(0.15)

    markets.sort(key=lambda item: item.get("endDate") or "")
    return markets


def collect_price_history(token_id, start_ts, end_ts):
    # Explicit absolute range + fidelity=1 gives minute-level history.
    # interval=1m means one month and conflicts with startTs/endTs.
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
        try:
            result["price_history"][str(token_id)] = collect_price_history(token_id, start_ts, end_ts)
            time.sleep(0.12)
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
        "lookback_days": LOOKBACK_DAYS,
        "price_history_fidelity_minutes": 1,
        "markets_found": len(normalized),
        "markets": normalized,
    }
    with open(OUTPUT, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
    print(f"Saved {len(normalized)} BTC 15m markets to {OUTPUT}")


if __name__ == "__main__":
    main()
