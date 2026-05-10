"""
Fixed-income fund performance fetcher.

Reads funds.json (list of UCITS funds with ISINs), fetches daily NAV history
from Yahoo Finance (primary) or FT.com (fallback for funds yfinance can't
return a series for), computes 1D / 1W / MTD / YTD / 1M / 3M / 6M / 1Y returns,
writes the result to data/funds.json for the dashboard to consume.
"""

import json
import datetime
import re
import urllib.request
import warnings
import sys
from pathlib import Path

try:
    import yfinance as yf
except ImportError:
    import subprocess
    subprocess.check_call([sys.executable, "-m", "pip", "install", "yfinance"])
    import yfinance as yf

warnings.filterwarnings('ignore')

UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
      '(KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36')


# ── METRIC HELPERS ────────────────────────────────────────────────────────────
def _pct(new, old):
    if old and old != 0:
        return round((float(new) - float(old)) / abs(float(old)) * 100, 2)
    return None


def _last_trading_day_of_prev_month(history_index):
    """Given a DatetimeIndex of trading days, return the index of the last
    trading day strictly before the start of the current month (relative to
    the most recent date in the index)."""
    if history_index is None or len(history_index) == 0:
        return None
    last = history_index[-1]
    cur_month_start = last.replace(day=1)
    prev = history_index[history_index < cur_month_start]
    if len(prev) == 0:
        return None
    return prev[-1]


def _last_trading_day_of_year(history_index, year):
    """Return the last trading day of the given calendar year present in the index."""
    if history_index is None or len(history_index) == 0:
        return None
    matches = history_index[history_index.year == year]
    if len(matches) == 0:
        return None
    return matches[-1]


def compute_metrics(closes_dict_or_df, name=''):
    """Given an object with a DatetimeIndex and a Close series (DataFrame or
    DataFrame-like), compute return metrics. Returns a dict of metrics, with
    None for periods that can't be computed from the available history."""
    df = closes_dict_or_df
    if df is None or len(df) == 0:
        return {}

    closes = df['Close']
    last_date = df.index[-1]
    last = float(closes.iloc[-1])

    out = {
        'price':     round(last, 4),
        'as_of':     str(last_date.date()),
        'd1':        None,
        'w1':        None,
        'mtd':       None,
        'ytd':       None,
        'm1':        None,
        'm3':        None,
        'm6':        None,
        'y1':        None,
        'history_days': len(df),
    }

    # 1-day
    if len(closes) >= 2:
        out['d1'] = _pct(closes.iloc[-1], closes.iloc[-2])

    # 1-week (5 trading days back)
    if len(closes) >= 6:
        out['w1'] = _pct(closes.iloc[-1], closes.iloc[-6])

    # MTD: relative to the close of the last trading day of the previous month
    pm = _last_trading_day_of_prev_month(df.index)
    if pm is not None:
        out['mtd'] = _pct(last, df.loc[pm, 'Close'])

    # YTD: relative to the close of the last trading day of the previous year
    prev_year = last_date.year - 1
    py = _last_trading_day_of_year(df.index, prev_year)
    if py is not None:
        out['ytd'] = _pct(last, df.loc[py, 'Close'])

    # Trailing 1M / 3M / 6M / 1Y by calendar date — find the trading day on or
    # before (last_date - N days). If history doesn't reach back that far (e.g.,
    # yfinance's '1y' period sometimes starts ~360 days back instead of 365+),
    # fall back to the earliest available row, but only when the gap is small
    # (within 10 days). For shorter periods (1M/3M/6M), insufficient history
    # leaves the metric as None.
    try:
        import pandas as pd
        for label, days, allow_fallback in [
            ('m1', 30, False), ('m3', 91, False),
            ('m6', 182, False), ('y1', 365, True),
        ]:
            target = last_date - pd.Timedelta(days=days)
            earlier = df.index[df.index <= target]
            if len(earlier) > 0:
                out[label] = _pct(last, df.loc[earlier[-1], 'Close'])
            elif allow_fallback and len(df.index) > 0:
                gap_days = (df.index[0] - target).days
                if 0 <= gap_days <= 10:
                    out[label] = _pct(last, df['Close'].iloc[0])
    except ImportError:
        pass

    return out


# ── PRIMARY SOURCE: YFINANCE ─────────────────────────────────────────────────
def fetch_via_yfinance(symbol, auto_adjust=False):
    """Pull 1Y of daily NAV history from Yahoo Finance. `symbol` may be an ISIN
    or one of Yahoo's internal '0P...' identifiers.
    `auto_adjust=True` is required for distributing money-market funds where
    income is paid out rather than reinvested — without it, NAV stays at $1.00
    and reported returns are zero."""
    try:
        df = yf.Ticker(symbol).history(period='1y', interval='1d', auto_adjust=auto_adjust)
        if df is None or df.empty or len(df) < 2:
            return None
        return df
    except Exception as e:
        print(f"    yfinance error for {symbol}: {e}")
        return None


# ── FALLBACK: FT.COM HISTORICAL ──────────────────────────────────────────────
def _http_get(url):
    req = urllib.request.Request(url, headers={'User-Agent': UA})
    return urllib.request.urlopen(req, timeout=20).read().decode('utf-8', errors='ignore')


def fetch_via_ft(isin, currency='USD'):
    """Scrape FT.com /tearsheet/historical for daily NAV. Returns a pandas-like
    DataFrame, or None on failure. FT only returns ~21 trading days, so MTD/1W
    will work but YTD and longer periods will not."""
    try:
        import pandas as pd
    except ImportError:
        print("    pandas not available for FT fallback")
        return None

    url = f'https://markets.ft.com/data/funds/tearsheet/historical?s={isin}:{currency}'
    try:
        html = _http_get(url)
    except Exception as e:
        print(f"    FT fetch error for {isin}: {e}")
        return None

    # Heuristic: FT redirects unknown ISINs to a search results page with
    # title "Equities, ETF and Funds prices..." — bail out if we hit that.
    if '<title>Equities, ETF and Funds prices' in html or '<title>Search' in html:
        print(f"    FT: ISIN {isin} not indexed (redirected to search)")
        return None

    # Pull the historical table rows. Each row in the rendered table looks like:
    #   Friday, May 08, 2026 | Fri, May 08, 2026 | open | high | low | close | volume | %change
    table_match = re.search(r'<table[^>]*>(.*?)</table>', html, re.DOTALL)
    if not table_match:
        print(f"    FT: no historical table found for {isin}")
        return None
    table_html = table_match.group(1)

    # Each row's <td> cells may contain nested <span>s (long+short date variants
    # for responsive layouts), so we capture the full td body and strip tags.
    rows_html = table_html.split('</tr>')
    parsed = []
    long_date_pat = re.compile(r'\b([A-Z][a-z]+,\s*[A-Z][a-z]+\s+\d{1,2},\s*\d{4})\b')
    for r in rows_html:
        cells_raw = re.findall(r'<td[^>]*>(.*?)</td>', r, re.DOTALL)
        if len(cells_raw) < 5:
            continue
        # Strip tags and collapse whitespace within each cell.
        cells = [re.sub(r'\s+', ' ', re.sub(r'<[^>]+>', ' ', c)).strip() for c in cells_raw]
        # First cell holds the date(s); extract the long form like "Friday, May 08, 2026".
        m = long_date_pat.search(cells[0])
        if not m:
            continue
        try:
            d = datetime.datetime.strptime(m.group(1), '%A, %B %d, %Y').date()
        except Exception:
            continue
        # Close is the 4th price column among the remaining cells (open/high/low/close).
        # Cells layout: [date, open, high, low, close, volume, ...]
        try:
            close = float(cells[4].replace(',', ''))
        except Exception:
            continue
        parsed.append((d, close))
    if len(parsed) < 2:
        print(f"    FT: parsed only {len(parsed)} rows for {isin}")
        return None

    # FT lists newest first; sort ascending for our metric helpers.
    parsed.sort(key=lambda x: x[0])
    idx  = pd.DatetimeIndex([p[0] for p in parsed])
    df   = pd.DataFrame({'Close': [p[1] for p in parsed]}, index=idx)
    return df


# ── DRIVER ────────────────────────────────────────────────────────────────────
def fetch_one(fund):
    name        = fund.get('name', '?')
    isin        = fund.get('isin')
    yahoo_sym   = fund.get('yahoo_symbol')
    currency    = fund.get('currency', 'USD')
    source      = fund.get('source', 'yfinance')
    auto_adjust = bool(fund.get('auto_adjust', False))

    # Prefer an explicit yahoo_symbol override (used for funds not indexed by
    # ISIN on Yahoo, or when proxying to a sister share class). Fall back to ISIN.
    lookup_sym = yahoo_sym or isin

    rec = {
        'name':       name,
        'share_class': fund.get('share_class'),
        'isin':       isin,
        'currency':   currency,
        'bloomberg':  fund.get('bloomberg'),
        'yahoo_symbol': yahoo_sym,
        'proxy_note': fund.get('proxy_note'),
        'source_used': None,
        'metrics':    {},
        'note':       fund.get('note'),
    }

    if not lookup_sym:
        rec['source_used'] = 'none'
        rec['note'] = (rec['note'] or '') + ' [ISIN missing]'
        print(f"  [SKIP] {name}: no ISIN/yahoo_symbol")
        return rec

    print(f"  [{source.upper()}] {name} ({lookup_sym})...")
    df = None
    if source == 'yfinance':
        df = fetch_via_yfinance(lookup_sym, auto_adjust=auto_adjust)
        if df is None or len(df) < 2:
            # FT fallback only makes sense when we have a real ISIN to look up.
            if isin:
                print(f"    yfinance returned no series — falling back to FT")
                df = fetch_via_ft(isin, currency)
                if df is not None:
                    rec['source_used'] = 'ft (fallback)'
        else:
            rec['source_used'] = 'yfinance' + (' (adj)' if auto_adjust else '')
    elif source == 'ft':
        df = fetch_via_ft(isin, currency)
        if df is not None:
            rec['source_used'] = 'ft'
    else:
        print(f"    unknown source '{source}'")
        return rec

    if df is None or len(df) < 2:
        print(f"    [FAIL] no usable data")
        rec['note'] = (rec['note'] or '') + ' [data unavailable]'
        return rec

    rec['metrics'] = compute_metrics(df, name=name)
    m = rec['metrics']
    print(f"    rows={m.get('history_days')}  d1={m.get('d1')}  w1={m.get('w1')}  "
          f"mtd={m.get('mtd')}  ytd={m.get('ytd')}  1y={m.get('y1')}")
    return rec


def main():
    print(f"=== Fixed-Income Funds Fetcher ===")
    print(f"Time: {datetime.datetime.utcnow()} UTC\n")

    cfg_path = Path(__file__).parent / 'funds.json'
    with open(cfg_path) as f:
        cfg = json.load(f)

    # Support both shapes:
    #   1) {"sections": [{"title": ..., "funds": [...]}, ...]}   (new, multi-section)
    #   2) {"funds": [...]}                                        (legacy, flat)
    sections_in = cfg.get('sections')
    if sections_in is None:
        sections_in = [{'title': 'Funds', 'funds': cfg.get('funds', [])}]

    sections_out = []
    total_funds = 0
    total_ok = 0
    for section in sections_in:
        title = section.get('title', 'Funds')
        funds = section.get('funds', [])
        print(f"--- Section: {title} ({len(funds)} funds) ---")
        results = [fetch_one(f) for f in funds]
        ok = sum(1 for r in results if r.get('metrics'))
        total_funds += len(funds)
        total_ok += ok
        sections_out.append({
            'title':    title,
            'subtitle': section.get('subtitle'),
            'funds':    results,
        })
        print(f"  -> {ok}/{len(funds)} returned metrics\n")

    out = {
        'generated_at': datetime.datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%SZ'),
        'sections':     sections_out,
    }
    out_path = Path('data/funds.json')
    out_path.parent.mkdir(exist_ok=True)
    with open(out_path, 'w') as f:
        json.dump(out, f, indent=2, default=str)

    print(f"=== {total_ok}/{total_funds} funds returned metrics ===")
    print(f"Wrote {out_path}")


if __name__ == '__main__':
    main()
