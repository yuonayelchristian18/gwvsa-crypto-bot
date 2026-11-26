"""IPO scanner V11: Finnhub + Nasdaq calendars + news -> daily Telegram alert. No orders."""
import json
import os
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

DAYS_BACK = 90
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


NEWS_QUERIES = (
    ('US', '(NYSE OR Nasdaq) IPO when:2d'),
    ('Canada', '(TSX OR "TSX Venture") IPO when:2d'),
    ('UK', 'London Stock Exchange IPO when:2d'),
    ('Europe', '(Euronext OR Frankfurt OR Swiss) IPO when:2d'),
    ('India', '(NSE OR BSE) India IPO when:2d'),
    ('China and Hong Kong', '("Hong Kong" OR Shanghai OR Shenzhen) IPO when:2d'),
    ('Japan and Korea', '(Tokyo OR Korea) IPO when:2d'),
    ('Southeast Asia', '(Singapore OR Indonesia OR Malaysia) IPO when:2d'),
    ('Australia', 'ASX IPO when:2d'),
    ('Middle East', '(Saudi OR Dubai OR "Abu Dhabi") IPO when:2d'),
    ('Africa', '(Ghana OR Nigeria OR Kenya OR Johannesburg) IPO when:2d'),
    ('Latin America', '(Brazil OR Mexico) IPO when:2d'),
)


def parse_news(xml_text, limit=3):
    items = ET.fromstring(xml_text).findall("./channel/item")[:limit]
    return [f"• {i.findtext('title')}" for i in items]


def news_section(notes):
    parts = ["\nNEWS HEADLINES (unverified)"]
    for region, query in NEWS_QUERIES:
        url = "https://news.google.com/rss/search?" + urllib.parse.urlencode(
            {"q": query, "hl": "en-CA", "gl": "CA", "ceid": "CA:en"})
        try:
            request = urllib.request.Request(url, headers={"User-Agent": UA["User-Agent"]})
            with urllib.request.urlopen(request, timeout=25) as response:
                rows = parse_news(response.read())
            parts += [f"\n{region}:"] + (rows or ["none found"])
        except Exception as error:
            print(f"{region} news failed:", type(error).__name__, error)
            notes.append(f"{region} news failed")
    return "\n".join(parts)


def score_metrics(m):
    """0-100 from four checks. None if any number is missing."""
    values = [m.get(k) for k in ("revenueGrowthTTMYoy", "netProfitMarginTTM",
                                 "currentRatioQuarterly",
                                 "totalDebt/totalEquityQuarterly", "psTTM")]
    if any(v is None for v in values):
        return None
    growth, margin, current, debt, ps = values
    clamp = lambda x: max(0.0, min(1.0, x))
    return round(
        30 * clamp(growth / 40)                    # 40%+ growth = full
        + 30 * clamp(margin / 15)                  # 15%+ net margin = full
        + 10 * clamp((current - 1) / 1)            # current ratio 2+ = full
        + 10 * (clamp((2 - debt) / 1.5) if debt >= 0 else 0)
        + 20 * (clamp((15 - ps) / 12) if ps > 0 else 0))  # P/S 3 or less = full


def expected_check(rows, today):
    """Show every 'expected' row from each source and why it was hidden."""
    out = ["\nEXPECTED DEALS CHECK (before filters)"]
    for r in rows:
        if r.get("status") != "expected":
            continue
        date = r.get("date") or ""
        if not wanted(r):
            why = "hidden: exchange is not NYSE/Nasdaq"
        elif date and date < today:
            why = "hidden: date has passed"
        else:
            why = "passes filters"
        out.append(f"• {r.get('symbol') or '?'} - {r.get('name')}\n"
                   f"  {date or 'date TBA'} | {r.get('exchange') or 'exchange blank'}"
                   f" | {r.get('source')} | {why}")
    return "\n".join(out if len(out) > 1 else out + ["none returned"])


def scorecard(ipos, today):
    scored, unscored = [], []
    for ipo in ipos:
        date, symbol = ipo.get("date") or "", ipo.get("symbol")
        if (ipo.get("status") != "priced" or not wanted(ipo) or not symbol
                or not date or date > today
                or "Acquisition" in (ipo.get("name") or "")):
            continue
        try:
            time.sleep(1.1)  # stay under the free rate limit
            data = finnhub("stock/metric", symbol=symbol, metric="all")
            score = score_metrics(data.get("metric") or {})
        except Exception as error:
            print(f"{symbol}: metrics failed ({type(error).__name__})")
            score = None
        if score is None:
            unscored.append(symbol)
        else:
            scored.append((score, symbol, ipo.get("name")))
    parts = [f"\nSCORECARD, listed in last {DAYS_BACK} days (experimental)"]
    parts += [f"• {sc}/100 {sym} - {name}"
              for sc, sym, name in sorted(scored, reverse=True)[:10]] or ["none scored"]
    parts.append(f"Unscored (missing data): {len(unscored)} "
                 + ", ".join(unscored[:15]))
    return "\n".join(parts)


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
    listed, priced, watch, filed = [], [], [], []
    keep = (i for i in ipos if wanted(i)
            or (i.get("status") == "filed" and not i.get("exchange")))
    for ipo in sorted(keep,
                      key=lambda i: i.get("date") or "9999"):
        status = ipo.get("status")
        date = ipo.get("date") or ""
        if status == "priced":
            symbol = ipo.get("symbol")
            last = quote(symbol) if symbol and date and date <= today else None
            if not last and (not date or date >= today):
                priced.append(line(ipo))
        elif status == "expected" and (not date or date >= today):
            watch.append(line(ipo))
        elif status == "filed":
            filed.append(f"• {ipo.get('symbol') or '?'} - {ipo.get('name')}"
                         " (listing date not set)"
                         + (" [likely SPAC]" if "Acquisition Corp" in (ipo.get("name") or "") else ""))

    parts = [f"IPO scan V11 {today} worldwide"]
    for title, rows in (("PRICED, not trading yet", [r for r in priced if r]),
                        ("UPCOMING / WATCHLIST, not confirmed", watch),
                        ("FILED, no listing date (newest 15)", filed[::-1][:15])):
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
    report += "\n" + scorecard(merge([dict(r) for r in fin_rows],
                                     [dict(r) for r in nas_rows]), str(now))
    report += "\n" + expected_check(fin_rows + nas_rows, str(now))
    counts = []
    for name, rows in (("Finnhub", fin_rows), ("Nasdaq", nas_rows)):
        c = [sum(r.get("status") == k for r in rows)
             for k in ("priced", "expected", "filed")]
        counts.append(f"{name}: priced {c[0]}, expected {c[1]}, filed {c[2]}")
    report += "\n\nSOURCE CHECK\n" + "\n".join(counts)
    report += "\n" + news_section(notes)
    if notes:
        report += "\n\nNote: " + "; ".join(notes)
    print(report)
    send_telegram(report)


if __name__ == "__main__":
    main()
