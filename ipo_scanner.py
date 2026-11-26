"""IPO scanner, step 1: US IPO calendar -> daily Telegram alert. No orders."""
import json
import os
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

DAYS_BACK = 7
DAYS_AHEAD = 14


def get_json(url, params, data=None):
    if data is None:
        url += "?" + urllib.parse.urlencode(params)
    else:
        data = urllib.parse.urlencode(data).encode()
    with urllib.request.urlopen(url, data=data, timeout=25) as response:
        return json.load(response)


def finnhub(path, **params):
    params["token"] = os.environ["FINNHUB_API_KEY"]
    return get_json("https://finnhub.io/api/v1/" + path, params)


def wanted(ipo):
    exchange = (ipo.get("exchange") or "").upper()
    return exchange == "NYSE" or exchange.startswith("NASDAQ")


def is_trading(symbol):
    try:
        quote = finnhub("quote", symbol=symbol)
        return quote.get("c") or None
    except Exception as error:
        print(f"{symbol}: quote failed ({type(error).__name__})")
        return None


def line(ipo, extra=""):
    return (f"• {ipo.get('symbol') or '?'} - {ipo.get('name')}\n"
            f"  {ipo.get('date')} | {ipo.get('exchange')} | "
            f"price {ipo.get('price') or 'TBA'}{extra}")


def build_report(ipos, today, quote=is_trading):
    listed, priced, watch = [], [], []
    for ipo in sorted(filter(wanted, ipos), key=lambda i: i.get("date") or ""):
        status = ipo.get("status")
        if status == "priced":
            symbol = ipo.get("symbol")
            last = quote(symbol) if symbol and ipo.get("date", "") <= today else None
            if last:
                listed.append(line(ipo, f" | last ${last:.2f}"))
            else:
                priced.append(line(ipo))
        elif status in ("expected", "filed") and (ipo.get("date") or "") >= today:
            watch.append(line(ipo))

    parts = [f"IPO scan {today} (US, unscored)"]
    for title, rows in (("LISTED (has a quote)", listed),
                        ("PRICED, no quote yet", priced),
                        ("WATCHLIST, not confirmed", watch)):
        parts.append(f"\n{title}: {len(rows)}")
        parts.extend(rows)
    return "\n".join(parts)


def send_telegram(text):
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    chunk = ""
    chunks = []
    for row in text.split("\n"):
        if len(chunk) + len(row) > 3900:
            chunks.append(chunk)
            chunk = ""
        chunk += row + "\n"
    chunks.append(chunk)
    for part in chunks:
        get_json(f"https://api.telegram.org/bot{token}/sendMessage", None,
                 {"chat_id": os.environ["TELEGRAM_CHAT_ID"], "text": part})


def main():
    now = datetime.now(timezone.utc).date()
    result = finnhub("calendar/ipo",
                     **{"from": str(now - timedelta(days=DAYS_BACK)),
                        "to": str(now + timedelta(days=DAYS_AHEAD))})
    report = build_report(result.get("ipoCalendar") or [], str(now))
    print(report)
    send_telegram(report)


if __name__ == "__main__":
    main()
