#!/usr/bin/env python3
"""Fast historical collector for Polymarket BTC 15m markets."""
import json, os, time, urllib.parse, urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

MARKET_URL = "https://gamma-api.polymarket.com/markets/slug/{}"
CLOB_URL = "https://clob.polymarket.com/prices-history"
OUTPUT = "data/markets_btc15m.json"
LOOKBACK_DAYS = 7
WINDOW = 900
WORKERS = 16
RETRIES = 3


def get_json(url, params=None):
    if params:
        url += "?" + urllib.parse.urlencode(params)
    for attempt in range(RETRIES):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "PolymarketBTC15mLab/1.0"})
            with urllib.request.urlopen(req, timeout=20) as r:
                return json.loads(r.read().decode())
        except Exception:
            if attempt + 1 == RETRIES:
                raise
            time.sleep(0.5 * (attempt + 1))


def field(v):
    if isinstance(v, (list, dict)):
        return v
    if not v:
        return []
    try:
        return json.loads(v)
    except Exception:
        return []


def market(slug):
    try:
        x = get_json(MARKET_URL.format(slug))
        return x if isinstance(x, dict) else None
    except Exception:
        return None


def history(token, start, end):
    x = get_json(CLOB_URL, {"market": token, "startTs": start, "endTs": end, "fidelity": 1})
    return x.get("history", []) if isinstance(x, dict) else []


def normalize(m):
    outcomes = field(m.get("outcomes"))
    prices = field(m.get("outcomePrices"))
    tokens = field(m.get("clobTokenIds"))
    start = m.get("startDate")
    end = m.get("endDate")
    start_ts = int(datetime.fromisoformat(start.replace("Z", "+00:00")).timestamp()) if start else None
    end_ts = int(datetime.fromisoformat(end.replace("Z", "+00:00")).timestamp()) if end else None
    out = {"id":m.get("id"),"question":m.get("question"),"slug":m.get("slug"),"condition_id":m.get("conditionId"),"start_date":start,"end_date":end,"closed_time":m.get("closedTime"),"resolution_source":m.get("resolutionSource"),"resolved_by":m.get("resolvedBy"),"outcomes":outcomes,"outcome_prices":prices,"clob_token_ids":tokens,"active":m.get("active"),"closed":m.get("closed"),"volume":m.get("volumeNum",m.get("volume")),"liquidity":m.get("liquidityNum",m.get("liquidity")),"best_bid":m.get("bestBid"),"best_ask":m.get("bestAsk"),"last_trade_price":m.get("lastTradePrice"),"price_history":{}}
    if start_ts is None or end_ts is None:
        return out
    jobs = {}
    with ThreadPoolExecutor(max_workers=4) as pool:
        for token in tokens:
            if token:
                jobs[token] = pool.submit(history, token, start_ts, end_ts)
        for token, job in jobs.items():
            try:
                out["price_history"][str(token)] = job.result()
            except Exception as e:
                out["price_history"][str(token)] = {"error": str(e)}
    return out


def main():
    os.makedirs("data", exist_ok=True)
    now = int(datetime.now(timezone.utc).timestamp())
    latest = now // WINDOW * WINDOW
    first = latest - LOOKBACK_DAYS * 86400
    starts = list(range(first, latest + 1, WINDOW))
    print(f"Collecting {len(starts)} BTC 15m markets over {LOOKBACK_DAYS} days with {WORKERS} workers", flush=True)
    markets, missing = [], 0
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        futures = {pool.submit(market, f"btc-updown-15m-{s}"): s for s in starts}
        done = 0
        for f in as_completed(futures):
            done += 1
            m = f.result()
            if m:
                try:
                    markets.append(normalize(m))
                except Exception as e:
                    print(f"normalize error: {e}", flush=True)
            else:
                missing += 1
            if done % 50 == 0 or done == len(starts):
                print(f"Discovery {done}/{len(starts)} found={len(markets)} missing={missing}", flush=True)
    markets.sort(key=lambda x: x.get("end_date") or "")
    payload = {"generated_at":datetime.now(timezone.utc).isoformat(),"source":"Polymarket Gamma market-by-slug + CLOB prices-history","lookback_days":LOOKBACK_DAYS,"price_history_fidelity_minutes":1,"markets_expected":len(starts),"markets_found":len(markets),"markets_missing":missing,"markets":markets}
    with open(OUTPUT,"w",encoding="utf-8") as f:
        json.dump(payload,f,ensure_ascii=False,indent=2)
    print(f"Saved {len(markets)} markets to {OUTPUT}; missing={missing}", flush=True)

if __name__ == "__main__":
    main()
