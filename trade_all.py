import os
import time
import urllib.parse
import hashlib
import hmac
import base64
import requests

API_KEY = os.environ["KRAKEN_API_KEY"].strip()
API_SEC = os.environ["KRAKEN_PRIVATE_KEY"].strip()
API_URL = "https://kraken.com"

TOP_6_PAIRS = {
    "XXBTZCAD": "XXBT",
    "XETHZCAD": "XETH",
    "SOLCAD":   "SOL",
    "XXRPZCAD": "XXRP",
    "ADACAD":   "ADA",
    "XDGZCAD":  "XXDG",
}

SPEND_PER_COIN_CAD = 15.0
PROFIT_TARGET_PCT = 0.05
TEST_MODE = True  # Change to False after test succeeds

def kraken_public(endpoint):
    headers = {
        "Accept": "application/json",
        "User-Agent": "Mozilla/5.0 (compatible; KrakenAPI/1.0)",
    }
    resp = requests.get(f"{API_URL}{endpoint}", headers=headers, timeout=15)
    try:
        return resp.json()
    except Exception:
        print(f"Public Error (HTTP {resp.status_code}): {resp.text[:200]}")
        return {}

def kraken_private(uri_path, data=None):
    if data is None:
        data = {}
    data["nonce"] = str(int(1000 * time.time()))
    postdata = urllib.parse.urlencode(data)
    encoded = (str(data["nonce"]) + postdata).encode()
    message = uri_path.encode() + hashlib.sha256(encoded).digest()
    mac = hmac.new(base64.b64decode(API_SEC), message, hashlib.sha512)
    sigdigest = base64.b64encode(mac.digest()).decode()

    headers = {
        "API-Key": API_KEY,
        "API-Sign": sigdigest,
        "Accept": "application/json",
        "Content-Type": "application/x-www-form-urlencoded; charset=utf-8",
        "User-Agent": "Mozilla/5.0 (compatible; KrakenAPI/1.0)",
    }
    resp = requests.post(API_URL + uri_path, headers=headers, data=postdata, timeout=15)
    try:
        return resp.json()
    except Exception:
        print(f"Private Error (HTTP {resp.status_code}): {resp.text[:200]}")
        return {"error": [f"HTTP {resp.status_code} non-JSON response"]}

def get_pair_specs():
    joined = ",".join(TOP_6_PAIRS.keys())
    return kraken_public(f"/0/public/AssetPairs?pair={joined}").get("result", {})

def get_current_prices():
    joined = ",".join(TOP_6_PAIRS.keys())
    resp = kraken_public(f"/0/public/Ticker?pair={joined}")
    prices = {}
    for pair, info in resp.get("result", {}).items():
        prices[pair] = float(info["c"])
    return prices

def get_last_buy_price(pair):
    trades = kraken_private("/0/private/TradesHistory").get("result", {}).get("trades", {})
    for txid, trade in trades.items():
        if trade.get("pair") == pair and trade.get("type") == "buy":
            return float(trade["price"])
    return None

def run_top_6_bot():
    bal_resp = kraken_private("/0/private/Balance")
    if bal_resp.get("error"):
        print("Balance Error:", bal_resp["error"])
        return

    balances = bal_resp.get("result", {})
    cad_balance = float(balances.get("ZCAD", 0))
    print(f"Available CAD Balance: ${cad_balance:.2f}")

    specs = get_pair_specs()
    prices = get_current_prices()

    for pair, asset_code in TOP_6_PAIRS.items():
        if pair not in prices or pair not in specs:
            continue

        current_price = prices[pair]
        held_amount = float(balances.get(asset_code, 0))
        min_vol = float(specs[pair].get("ordermin", 0))
        decimals = specs[pair].get("lot_decimals", 8)

        if held_amount >= min_vol:
            last_buy = get_last_buy_price(pair)
            if last_buy and current_price >= last_buy * (1 + PROFIT_TARGET_PCT):
                sell_vol = round(held_amount, decimals)
                print(f"[SELL] {pair}: Price ${current_price:.4f} hit +5% target over ${last_buy:.4f}")
                res = kraken_private("/0/private/AddOrder", {
                    "ordertype": "market",
                    "type": "sell",
                    "volume": str(sell_vol),
                    "pair": pair,
                    "validate": TEST_MODE,
                })
                print("Sell Response:", res)
                continue

        if held_amount < min_vol and cad_balance >= SPEND_PER_COIN_CAD:
            buy_vol = round(SPEND_PER_COIN_CAD / current_price, decimals)
            if buy_vol >= min_vol:
                print(f"[BUY] {pair}: Buying {buy_vol} (~${SPEND_PER_COIN_CAD} CAD) at ${current_price:.4f}")
                res = kraken_private("/0/private/AddOrder", {
                    "ordertype": "market",
                    "type": "buy",
                    "volume": str(buy_vol),
                    "pair": pair,
                    "validate": TEST_MODE,
                })
                print("Buy Response:", res)
                if not res.get("error"):
                    cad_balance -= SPEND_PER_COIN_CAD
            else:
                print(f"[SKIP] {pair}: ${SPEND_PER_COIN_CAD} is below Kraken minimum volume ({min_vol}).")

if __name__ == "__main__":
    run_top_6_bot()