"""IPO scanner v2: Finnhub + Nasdaq calendars -> daily Telegram alert. No orders."""
import json
import os
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

DAYS_BACK = 7
DAYS_AHEAD = 30
UA = {"User-Agent": "Mozilla/5.0 (IPO-Scanner)", "Accept": "application/json"}


def get_json(url, params=None, data=None, headers=None):
    if params:
        url += "?" + urllib.parse.urlencode(params)
    body = urllib.parse.urlencode(data).encode() if data else None
    request = urllib.request.Request(url, data=body, headers=headers or {})
    with urllib.request.urlopen(request, timeout=25) as response:
        return json.load(response)


def finnhub(path, **params):
    params["token"] = os.environ["FINNHUB_API_KEY"]
    return get_json("https://finnhub.io/api/v1/" + path, params)


def wanted(ipo):
    exchange = (ipo.get("exchange") or "").upper()
    return exchange == "NYSE" or exchange.startswith("NASDAQ")


def norm_date(text):
    """Turn 11/27/2026 or 2026-11-27 into 2026-11-27 (else empty)."""
    text = (text or "").strip()
    for fmt in ("%m/%d/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt).strftime("%Y-%m-%d")
        except ValueError:
            pass
    return ""


def nasdaq_month(year, month):
    """Nasdaq's public monthly feed. Returns normalised dicts."""
    data = get_json("https://api.nasdaq.com/api/ipo/calendar",
                    {"date": f"{year}-{month:02d}"}, headers=UA)
    block = (data.get("data") or {})
    out = []
    for key, status in (("priced", "priced"), ("upcoming", "expected"),
                        ("filed", "filed")):
        section = block.get(key) or {}
        rows = section.get("rows") or []
        if key == "upcoming" and isinstance(section.get("upcomingTable"), dict):
            rows = section["upcomingTable"].get("rows") or rows
        for row in rows:
            out.append({
                "symbol": row.get("proposedTickerSymbol") or "",
                "name": row.get("companyName") or "",
                "exchange": row.get("proposedExchange") or "",
                "date": norm_date(row.get("pricedDate")
                                  or row.get("expectedPriceDate")
                                  or row.get("filedDate")),
                "price": row.get("proposedSharePrice") or "",
                "status": status,
                "source": "Nasdaq",
            })
    return out


def months_between(start, end):
    y, m = start.year, start.month
    while (y, m) <= (end.year, end.month):
        yield y, m
        m += 1
        if m == 13:
            y, m = y + 1, 1


def merge(finnhub_rows, nasdaq_rows):
    """One row per ticker (or name); Nasdaq fills gaps, Finnhub kept first."""
    seen, merged = {}, []
    for row in list(finnhub_rows) + list(nasdaq_rows):
        key = (row.get("symbol") or row.get("name") or "").upper()
        if not key:
            continue
        if key in seen:
            old = seen[key]
            for field in ("price", "date", "exchange"):
                if not old.get(field) and row.get(field):
                    old[field] = row[field]
            if old.get("status") != "priced" and row.get("status") == "priced":
                old["status"] = "priced"
            continue
        seen[key] = row
        merged.append(row)
    return merged


def is_trading(symbol):
    try:
        quote = finnhub("quote", symbol=symbol)
        return quote.get("c") or None
    except Exception as error:
        print(f"{symbol}: quote failed ({type(error).__name__})")
        return None


def line(ipo, extra=""):
    return (f"• {ipo.get('symbol') or '?'} - {ipo.get('name')}\n"
            f"  {ipo.get('date') or 'date TBA'} | {ipo.get('exchange')} | "
            f"price {ipo.get('price') or 'TBA'}{extra}")


def build_report(ipos, today, quote=is_trading):
    listed, priced, watch = [], [], []
    for ipo in sorted(filter(wanted, ipos),
                      key=lambda i: i.get("date") or "9999"):
        status = ipo.get("status")
        date = ipo.get("date") or ""
        if status == "priced":
            symbol = ipo.get("symbol")
            last = quote(symbol) if symbol and date and date <= today else None
            if last:
                listed.append(line(ipo, f" | last ${last:.2f}"))
            else:
                priced.append(line(ipo))
        elif status in ("expected", "filed") and (not date or date >= today):
            watch.append(line(ipo))

    parts = [f"IPO scan {today} (US, unscored)"]
    for title, rows in (("LISTED (has a quote)", listed),
                        ("PRICED, no quote yet", priced),
                        ("UPCOMING / WATCHLIST, not confirmed", watch)):
        parts.append(f"\n{title}: {len(rows)}")
        parts.extend(rows)
    return "\n".join(parts)


def send_telegram(text):
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    chunks, chunk = [], ""
    for row in text.split("\n"):
        if len(chunk) + len(row) > 3900:
            chunks.append(chunk)
            chunk = ""
        chunk += row + "\n"
    chunks.append(chunk)
    for part in chunks:
        get_json(f"https://api.telegram.org/bot{token}/sendMessage",
                 data={"chat_id": os.environ["TELEGRAM_CHAT_ID"], "text": part})


def main():
    now = datetime.now(timezone.utc).date()
    start, end = now - timedelta(days=DAYS_BACK), now + timedelta(days=DAYS_AHEAD)

    fin_rows = []
    try:
        result = finnhub("calendar/ipo", **{"from": str(start), "to": str(end)})
        fin_rows = result.get("ipoCalendar") or []
        for row in fin_rows:
            row["source"] = "Finnhub"
    except Exception as error:
        print("Finnhub calendar failed:", type(error).__name__, error)

    nas_rows, notes = [], []
    for year, month in months_between(start, end):
        try:
            nas_rows += nasdaq_month(year, month)
        except Exception as error:
            print(f"Nasdaq {year}-{month:02d} failed:", type(error).__name__, error)
            notes.append(f"Nasdaq feed failed for {year}-{month:02d}")

    report = build_report(merge(fin_rows, nas_rows), str(now))
    if notes:
        report += "\n\nNote: " + "; ".join(notes)
    print(report)
    send_telegram(report)


if __name__ == "__main__":
    main()
