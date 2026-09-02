import json
import time
import datetime
import sys
import csv
import math
import xml.etree.ElementTree as ET
import concurrent.futures
from pathlib import Path
from io import StringIO
try:
    import yfinance as yf
except ImportError:
    print("Installing yfinance...")
    import subprocess
    subprocess.check_call([sys.executable, "-m", "pip", "install", "yfinance", "requests"])
    import yfinance as yf
try:
    import pandas as pd
except ImportError:
    import subprocess
    subprocess.check_call([sys.executable, "-m", "pip", "install", "pandas", "lxml", "html5lib"])
    import pandas as pd
import requests

# ── PATHS ──────────────────────────────────────────────────────────────────────
# Everything is resolved relative to this file, never the working directory, so
# the script behaves identically whether it is run from the repo root, from cron,
# or from anywhere else.
ROOT     = Path(__file__).resolve().parent
DATA_DIR = ROOT / 'data'
OUT_PATH = DATA_DIR / 'data.json'

# Minimum fraction of a section's tickers that must come back before we trust the
# new data outright. Below this we keep the previous values for whatever is
# missing rather than silently dropping rows off the dashboard.
COVERAGE_FLOOR = 0.60

# Warnings surfaced to the dashboard so a degraded run is visible in the UI
# instead of only in the Actions log.
WARNINGS = []

def warn(msg):
    print(f"  ⚠ {msg}")
    WARNINGS.append(msg)

def utcnow():
    return datetime.datetime.now(datetime.timezone.utc)

# ── DEFAULT TICKERS (overridden by tickers.json if present) ────────────────────
ETF_MAIN   = ['SPY','QQQ','DIA','IWM']
SUBMARKET  = ['IVW','IVE','IJK','IJJ','IJT','IJS','MGK','VUG','VTV']
SECTOR     = ['XLK','XLV','XLF','XLE','XLY','XLI','XLB','XLU','XLRE','XLC','XLP']
SECTOR_EW  = []
THEMATIC   = ['BOTZ','HACK','SOXX','ICLN','SKYY','XBI','ITA','FINX','ARKG','URA',
              'AIQ','CIBR','ROBO','ARKK','DRIV','OGIG','ACES','PAVE','HERO','CLOU']
COUNTRY    = ['ARGT','EUFN','MCHI','EWZ','EWI','EWY','EWH',
              'EWC','EWL','EWA','IEV','IEUR','INDA','EWG',
              'EZU','EEM','EFA','TUR','ACWI','EWJ','EWT']
FUTURES    = ['ES=F','NQ=F','RTY=F','YM=F']
METALS     = ['GC=F','SI=F','HG=F','PL=F','PA=F']
ENERGY     = ['CL=F','NG=F']
GLOBAL_IDX = ['^N225','^KS11','^NSEI','000001.SS','000300.SS','^HSI','^FTSE','^FCHI','^GDAXI']
YIELDS     = ['^TNX','^TYX']
DX_VIX     = ['DX-Y.NYB','^VIX']
CRYPTO_YF  = ['BTC-USD','ETH-USD','SOL-USD','XRP-USD']

# ── LOAD FROM tickers.json ──────────────────────────────────────────────────────────────────────
config_path = ROOT / 'tickers.json'
if config_path.exists():
    with open(config_path) as f:
        CFG = json.load(f)
    ETF_MAIN   = CFG.get('etfmain',    ETF_MAIN)
    SUBMARKET  = CFG.get('submarket',  SUBMARKET)
    SECTOR     = CFG.get('sectors',    SECTOR)
    SECTOR_EW  = CFG.get('sectors_ew', SECTOR_EW)
    THEMATIC   = CFG.get('thematic',   THEMATIC)
    COUNTRY    = CFG.get('country',    COUNTRY)
    FUTURES    = CFG.get('futures',    FUTURES)
    METALS     = CFG.get('metals',     METALS)
    ENERGY     = CFG.get('energy',     ENERGY)
    GLOBAL_IDX = CFG.get('global',     GLOBAL_IDX)
    YIELDS     = CFG.get('yields',     YIELDS)
    DX_VIX     = CFG.get('dxvix',      DX_VIX)
    CRYPTO_YF  = CFG.get('crypto',     CRYPTO_YF)
    print(f"✓ Loaded tickers from tickers.json ({len(THEMATIC)} thematic, {len(COUNTRY)} country)")
else:
    print("⚠ tickers.json not found — using built-in defaults")

# ── TICKER REMAPS ────────────────────────────────────────────────────────────────────────────────
TICKER_REMAP = {
    'ES=F':'ES1!', 'NQ=F':'NQ1!', 'RTY=F':'RTY1!', 'YM=F':'YM1!',
    'GC=F':'GC1!', 'SI=F':'SI1!', 'HG=F':'HG1!', 'PL=F':'PL1!', 'PA=F':'PA1!',
    'CL=F':'CL1!', 'NG=F':'NG1!',
    '^TNX':'US10Y', '^TYX':'US30Y',
    'DX-Y.NYB':'DX-Y.NYB', '^VIX':'CBOE:VIX',
    'BTC-USD':'BTC','ETH-USD':'ETH','SOL-USD':'SOL','XRP-USD':'XRP',
}

# ── 2-YEAR TREASURY YIELD ───────────────────────────────────────────────────────────────────────────────
def _series_record(sym, dates, values):
    """Build a dashboard record from a plain (date, value) yield series so the
    2-year gets real 1D / 1W / 52W / YTD numbers instead of hardcoded zeros."""
    if not values:
        return None
    price = values[-1]
    prev_year = dates[-1].year - 1
    ytd_base = None
    for d, v in zip(dates, values):
        if d.year <= prev_year:
            ytd_base = v          # keeps overwriting → last close of the prior year
        else:
            break
    window = values[-252:]
    spark = [round(pct(values[i], values[i - 1]) or 0.0, 2)
             for i in range(max(1, len(values) - 5), len(values))]
    while len(spark) < 5:
        spark.insert(0, 0.0)
    return {
        'sym':   sym,
        'price': round(price, 4),
        'd1':    pct(price, values[-2]) if len(values) >= 2 else None,
        'w1':    pct(price, values[-6]) if len(values) >= 6 else None,
        'hi52':  pct(price, max(window)) if window else None,
        'ytd':   pct(price, ytd_base) if ytd_base else None,
        'd1_bps':  round((price - values[-2]) * 100, 1) if len(values) >= 2 else None,
        'w1_bps':  round((price - values[-6]) * 100, 1) if len(values) >= 6 else None,
        'ytd_bps': round((price - ytd_base) * 100, 1) if ytd_base else None,
        'spark': spark,
    }

def fetch_treasury_2y():
    # FRED carries the full daily history, so we can derive the same metrics we
    # compute for every other series rather than shipping placeholder zeros.
    try:
        url = 'https://fred.stlouisfed.org/graph/fredgraph.csv?id=DGS2'
        resp = requests.get(url, timeout=15, headers={'User-Agent': 'Mozilla/5.0'})
        resp.raise_for_status()
        reader = csv.reader(StringIO(resp.text))
        rows = list(reader)
        header = [h.strip().lower() for h in rows[0]] if rows else []
        # FRED renamed this column from DATE to observation_date; accept either,
        # and fall back to positional access if they rename it again.
        try:
            di = next(i for i, h in enumerate(header) if 'date' in h)
        except StopIteration:
            di = 0
        try:
            vi = next(i for i, h in enumerate(header) if 'dgs2' in h)
        except StopIteration:
            vi = 1
        dates, values = [], []
        for row in rows[1:]:
            if len(row) <= max(di, vi):
                continue
            raw = row[vi].strip()
            if raw in ('.', '', 'VALUE'):
                continue
            try:
                dates.append(datetime.date.fromisoformat(row[di].strip()))
                values.append(float(raw))
            except ValueError:
                continue
        if values:
            rec = _series_record('US2Y', dates, values)
            age = (datetime.date.today() - dates[-1]).days
            if age > 5:
                warn(f"US2Y: FRED series is {age} days stale (last {dates[-1]})")
            print(f"  ✓ US2Y = {values[-1]}% (FRED, {len(values)} obs)")
            return rec
    except Exception as e:
        print(f"  FRED CSV failed: {e}")
    try:
        now = utcnow()
        # Query this month and last month — on the 1st of a month the current
        # month's file is empty and the fallback would otherwise return nothing.
        months = [now.strftime('%Y%m'),
                  (now.replace(day=1) - datetime.timedelta(days=1)).strftime('%Y%m')]
        for month in months:
            url = ("https://home.treasury.gov/resource-center/data-chart-center/"
                   "interest-rates/pages/xml?data=daily_treasury_yield_curve"
                   f"&field_tdr_date_value={month}")
            resp = requests.get(url, timeout=15, headers={'User-Agent': 'Mozilla/5.0'})
            resp.raise_for_status()
            ns_m = 'http://schemas.microsoft.com/ado/2007/08/dataservices/metadata'
            ns_d = 'http://schemas.microsoft.com/ado/2007/08/dataservices'
            root = ET.fromstring(resp.content)
            entries = root.findall(f'.//{{{ns_m}}}properties')
            parsed = []
            for e in entries:
                dv = e.find(f'{{{ns_d}}}NEW_DATE')
                rv = e.find(f'{{{ns_d}}}BC_2YEAR')
                if dv is not None and dv.text and rv is not None and rv.text:
                    try:
                        parsed.append((datetime.date.fromisoformat(dv.text[:10]), float(rv.text)))
                    except ValueError:
                        continue
            if parsed:
                parsed.sort()   # don't assume the feed is already in date order
                rate = parsed[-1][1]
                print(f"  ✓ US2Y = {rate}% (Treasury XML, {parsed[-1][0]})")
                # Only a level is available here — leave the deltas null rather
                # than inventing zeros the dashboard would render as "unchanged".
                return {'sym': 'US2Y', 'price': round(rate, 4), 'd1': None, 'w1': None,
                        'hi52': None, 'ytd': None, 'spark': []}
    except Exception as e:
        print(f"  Treasury XML failed: {e}")
    warn("US2Y: no source returned a value")
    return None

# ── ETF HOLDINGS ─────────────────────────────────────────────────────────────────────────────────────────
def _safe_float(val):
    try:
        f = float(val)
        return f if math.isfinite(f) else None
    except Exception:
        return None

def _sanitize(obj):
    """Recursively replace non-finite floats (NaN/Infinity) with None so the
    output is always valid JSON — the browser's JSON.parse rejects NaN, and a
    single bad value would break the entire dashboard."""
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    if isinstance(obj, dict):
        return {k: _sanitize(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_sanitize(v) for v in obj]
    return obj

def _pct_from_val(val):
    f = _safe_float(val)
    if f is None or f == 0:
        return 0.0
    if 0 < f <= 1.0:
        return round(f * 100, 2)
    return round(f, 2)

def _holdings_for(sym):
    """Fetch the top-10 holdings for one ETF. Returns a list of rows (possibly
    empty). Never raises — a single bad ETF must not abort the batch."""
    rows = []
    try:
        t = yf.Ticker(sym)
        try:
            fd = t.funds_data
            if fd is not None:
                th = fd.top_holdings
                if th is not None and hasattr(th, 'iterrows') and not th.empty:
                    for idx, row in th.head(10).iterrows():
                        s = str(idx).strip() if str(idx) not in ('', 'nan') else ''
                        n = ''
                        if 'Name' in row.index:
                            v = str(row['Name']).strip()
                            if v and v != 'nan':
                                n = v
                        if not n:
                            n = s
                        w = 0.0
                        for pct_col in ['Holding Percent', 'holdingPercent', 'holdingpercent',
                                        '% Assets', 'weight', 'Weight', 'percent', 'Percent']:
                            if pct_col in row.index:
                                w = _pct_from_val(row[pct_col])
                                break
                        if w == 0.0:
                            for col_name in row.index:
                                if col_name in ('symbol', 'Symbol', 'ticker',
                                                'holdingName', 'name', 'Name'):
                                    continue
                                f = _safe_float(row[col_name])
                                if f and f > 0:
                                    w = _pct_from_val(f)
                                    break
                        if n or s:
                            rows.append({'s': s, 'n': n, 'w': w})
        except Exception:
            pass

        if not rows:
            try:
                info = t.info
                for h in info.get('holdings', [])[:10]:
                    s = str(h.get('symbol', ''))
                    n = str(h.get('holdingName', s))
                    w = _pct_from_val(h.get('holdingPercent', 0))
                    rows.append({'s': s, 'n': n, 'w': w})
            except Exception:
                pass
    except Exception:
        return []
    return rows

def fetch_etf_holdings(tickers, previous=None):
    """Fetch top-10 holdings for every ETF, in parallel.

    Holdings change slowly, so anything that fails today keeps yesterday's rows
    (`previous`) instead of disappearing from the dashboard. This used to be a
    serial loop with a 0.4s sleep per ticker — ~126 tickers ≈ 4-8 minutes, the
    single biggest cost in the script."""
    previous = previous or {}
    holdings_map = {}
    total = len(tickers)
    done = 0

    # 6 workers keeps us comfortably under Yahoo's rate limiting while cutting
    # the wall-clock cost by roughly an order of magnitude.
    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
        futures = {pool.submit(_holdings_for, sym): sym for sym in tickers}
        for fut in concurrent.futures.as_completed(futures):
            sym = futures[fut]
            done += 1
            rows = fut.result()
            if rows:
                holdings_map[sym] = rows
            print(f"  Holdings [{done}/{total}] {sym}: "
                  f"{len(rows) if rows else '—'}")

    fetched = len(holdings_map)
    # Backfill anything that failed from the last good run.
    restored = 0
    for sym in tickers:
        if sym not in holdings_map and sym in previous:
            holdings_map[sym] = previous[sym]
            restored += 1
    if restored:
        print(f"  ↩ restored {restored} ETFs' holdings from the previous run")
    if total and fetched / total < COVERAGE_FLOOR:
        warn(f"holdings: only {fetched}/{total} ETFs returned data")

    return holdings_map

# ── CORE METRICS ──────────────────────────────────────────────────────────────────────────────────────────────────
def pct(new, old):
    """Percent change, or None when it cannot be computed. Returning None rather
    than 0.0 matters: 0.0 renders as a real 'unchanged' reading on the dashboard,
    which is indistinguishable from a genuine flat day."""
    try:
        new = float(new); old = float(old)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(new) or not math.isfinite(old) or old == 0:
        return None
    return round((new - old) / abs(old) * 100, 2)

def _calc_ema_series(closes, period):
    """Full EMA series aligned with `closes`. First (period-1) entries are None
    (insufficient data); thereafter EMA seeded with SMA of the first `period` values."""
    closes = list(closes)
    if len(closes) < period:
        return None
    series = [None] * (period - 1)
    k = 2.0 / (period + 1)
    ema = sum(closes[:period]) / period
    series.append(ema)
    for c in closes[period:]:
        ema = float(c) * k + ema * (1.0 - k)
        series.append(ema)
    return series

def _calc_ema(closes, period):
    """EMA value at the last bar (convenience wrapper around _calc_ema_series)."""
    s = _calc_ema_series(closes, period)
    return s[-1] if s else None

def _ema_streak(closes, ema_series):
    """Consecutive days the close-vs-EMA relationship has held, looking back from
    the latest bar. Returns the count (>=1) or None if EMA series is unavailable."""
    if not ema_series or ema_series[-1] is None:
        return None
    closes = list(closes)
    latest_above = float(closes[-1]) > ema_series[-1]
    count = 0
    for i in range(len(closes) - 1, -1, -1):
        if ema_series[i] is None:
            break
        if (float(closes[i]) > ema_series[i]) == latest_above:
            count += 1
        else:
            break
    return count

# 2 years of daily bars. 1y was not enough: the 200-EMA needs ~200 warm-up bars
# before it yields a value, which capped the "consecutive days above the 200-EMA"
# streak at ~53 (SPY, US10Y and US30Y were all pinned there), and YTD needs last
# year's final close as its baseline.
HISTORY_PERIOD = '2y'

def fetch_individual(tickers, retries=3):
    results = {}
    for sym in tickers:
        for attempt in range(retries):
            df = None
            try:
                df = yf.Ticker(sym).history(period=HISTORY_PERIOD, interval='1d',
                                            auto_adjust=True)
            except Exception as e:
                print(f"  Attempt {attempt+1} failed for {sym}: {e}")
            # An empty frame is yfinance's *normal* failure mode — it does not
            # raise. The old `break` sat outside this check, so a silent empty
            # response exited the retry loop on the first try.
            if df is not None and not df.empty:
                rec = extract_metrics(df, sym)
                if rec:
                    results[sym] = rec
                break
            if attempt < retries - 1:
                time.sleep(2 * (attempt + 1))
        time.sleep(0.3)
    return results

def fetch_batch(tickers, retries=3):
    results = {}
    data = None
    for attempt in range(retries):
        try:
            data = yf.download(tickers, period=HISTORY_PERIOD, interval='1d',
                               group_by='ticker', auto_adjust=True,
                               progress=False, threads=True)
        except Exception as e:
            print(f"  Attempt {attempt+1} failed: {e}")
            data = None
        if data is not None and not data.empty:
            break
        if attempt < retries - 1:
            time.sleep(5 * (attempt + 1))   # back off — Yahoo rate-limits bursts
    if data is None or data.empty:
        print(f"  All retries failed for batch: {tickers[:3]}...")
        return results

    for sym in tickers:
        try:
            if isinstance(data.columns, pd.MultiIndex):
                if sym not in data.columns.get_level_values(0):
                    continue
                df = data[sym].dropna(how='all')
            elif len(tickers) == 1:
                # Single-ticker downloads can come back with flat columns.
                df = data.dropna(how='all')
            else:
                continue
            rec = extract_metrics(df, sym)
            if rec:
                results[sym] = rec
        except Exception as e:
            print(f"  Error extracting {sym}: {e}")
    return results

def extract_metrics(df, sym):
    df = df.dropna(subset=['Close'])
    if len(df) < 2:
        return None
    closes = df['Close'].values
    price  = float(closes[-1])
    d1     = pct(closes[-1], closes[-2]) if len(closes) >= 2 else None
    w1     = pct(closes[-1], closes[-6]) if len(closes) >= 6 else None

    # 52-week high must be measured over the trailing ~252 sessions, not over the
    # whole 2-year history we now download for the EMAs.
    win = df.iloc[-252:]
    hi52_price = float(win['High'].max()) if 'High' in win else float(win['Close'].max())
    hi52_pct   = pct(price, hi52_price)

    # YTD baseline is the LAST close of the previous year, not the first close of
    # this one — the old version silently dropped the 31-Dec → 2-Jan move from
    # every YTD figure on the dashboard. The year comes from the data's own last
    # bar rather than the runner's clock, which can already have rolled over to
    # the next UTC day by the time the job finishes.
    last_year = df.index[-1].year
    prior = df[df.index.year < last_year]
    if len(prior) > 0:
        ytd_base = float(prior['Close'].iloc[-1])
    else:
        ytd_df = df[df.index.year == last_year]
        ytd_base = float(ytd_df['Close'].iloc[0]) if len(ytd_df) else None
    ytd = pct(price, ytd_base) if ytd_base is not None else None

    spark = []
    for i in range(max(1, len(closes)-5), len(closes)):
        spark.append(round(pct(closes[i], closes[i-1]) or 0.0, 2))
    while len(spark) < 5:
        spark.insert(0, 0.0)

    # 10-EMA vs 20-EMA uptrend signal (legacy column)
    ema_uptrend = None
    if len(closes) >= 20:
        ema10 = _calc_ema(closes, 10)
        ema20 = _calc_ema(closes, 20)
        if ema10 is not None and ema20 is not None:
            ema_uptrend = bool(ema10 > ema20)

    # Trend identification: price vs 21/50/200 EMA + consecutive-day streaks
    ema21_s  = _calc_ema_series(closes, 21)
    ema50_s  = _calc_ema_series(closes, 50)
    ema200_s = _calc_ema_series(closes, 200)

    out_sym = TICKER_REMAP.get(sym, sym)
    result = {
        'sym':   out_sym,
        'price': round(price, 4),
        'd1':    d1,
        'w1':    w1,
        'hi52':  hi52_pct,
        'ytd':   ytd,
        'spark': spark,
        'asof':  df.index[-1].strftime('%Y-%m-%d'),
    }

    # Treasury yields are quoted in percent, so a *percent change* of the yield
    # is not what anyone means by "the 10-year moved X". Ship the real basis-point
    # move alongside, and let the dashboard label the column honestly.
    if out_sym in ('US2Y', 'US10Y', 'US30Y'):
        result['d1_bps']  = round((price - float(closes[-2])) * 100, 1) if len(closes) >= 2 else None
        result['w1_bps']  = round((price - float(closes[-6])) * 100, 1) if len(closes) >= 6 else None
        result['ytd_bps'] = round((price - ytd_base) * 100, 1) if ytd_base is not None else None
    if ema_uptrend is not None:
        result['ema_uptrend'] = ema_uptrend
    if ema21_s is not None:
        result['above_ema21']  = bool(price > ema21_s[-1])
        result['ema21_val']    = round(ema21_s[-1], 4)
        streak21 = _ema_streak(closes, ema21_s)
        if streak21 is not None:
            result['streak_21'] = streak21
    if ema50_s is not None:
        result['above_ema50']  = bool(price > ema50_s[-1])
        result['ema50_val']    = round(ema50_s[-1], 4)
        streak50 = _ema_streak(closes, ema50_s)
        if streak50 is not None:
            result['streak_50'] = streak50
    if ema200_s is not None:
        result['above_ema200'] = bool(price > ema200_s[-1])
        result['ema200_val']   = round(ema200_s[-1], 4)
        streak200 = _ema_streak(closes, ema200_s)
        if streak200 is not None:
            result['streak_200'] = streak200

    crypto_ids   = {'BTC-USD':'bitcoin','ETH-USD':'ethereum','SOL-USD':'solana','XRP-USD':'ripple'}
    crypto_names = {'BTC-USD':'Bitcoin','ETH-USD':'Ethereum','SOL-USD':'Solana','XRP-USD':'Ripple'}
    if sym in crypto_ids:
        result['id']   = crypto_ids[sym]
        result['name'] = crypto_names[sym]
    return result

# ── FEAR & GREED ──────────────────────────────────────────────────────────────────────────────
def fetch_fear_greed():
    urls = [
        "https://production.dataviz.cnn.io/index/fearandgreed/graphdata",
    ]
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
        'Accept': 'application/json',
        'Referer': 'https://edition.cnn.com/',
    }
    for attempt in range(3):
        try:
            r = requests.get(urls[0], timeout=15, headers=headers)
            print(f"  Fear & Greed HTTP {r.status_code}")
            r.raise_for_status()
            data = r.json()
            fg = data.get('fear_and_greed') or {}
            # No silent defaults. The old code fell back to score=50/"neutral",
            # so a CNN schema change would have published a plausible-looking
            # fabricated reading instead of failing visibly.
            if 'score' not in fg:
                raise ValueError("response has no fear_and_greed.score")
            score = round(float(fg['score']), 1)
            if not 0 <= score <= 100:
                raise ValueError(f"score {score} out of range")
            rating = str(fg.get('rating') or '').replace('_', ' ').title() or None
            print(f"  ✓ Fear & Greed: {score} ({rating})")
            return {'score': score, 'rating': rating,
                    'asof': (fg.get('timestamp') or None)}
        except Exception as e:
            print(f"  Fear & Greed attempt {attempt+1} failed: {e}")
            if attempt < 2:
                time.sleep(3 * (attempt + 1))
    warn("Fear & Greed unavailable")
    return None

# ── NAAIM EXPOSURE INDEX ────────────────────────────────────────────────────────────────────────────
def _naaim_from_table(table):
    """Pull (date, mean exposure) out of a NAAIM table.

    The old version grabbed the first <table> on the page and then the first
    number in *any* column that happened to fall in -200..300 — a column reorder
    would silently return the wrong series (e.g. a quartile instead of the mean).
    Here we locate the mean-exposure column by its header and fall back to the
    first numeric column only if no header matches."""
    from bs4 import BeautifulSoup  # noqa: F401  (import kept local, as before)
    header_cells = [th.get_text(strip=True).lower()
                    for th in table.find_all('th')]
    col = None
    for i, h in enumerate(header_cells):
        if 'mean' in h or 'naaim number' in h or 'exposure index' in h:
            col = i
            break
    for row in table.find_all('tr')[1:]:
        cells = [td.get_text(strip=True) for td in row.find_all('td')]
        if len(cells) < 2:
            continue
        date_str = cells[0]
        candidates = []
        if col is not None and col < len(cells):
            candidates = [cells[col]]
        else:
            candidates = cells[1:]
        for cell in candidates:
            try:
                val = float(cell.replace(',', '').replace('%', ''))
            except ValueError:
                continue
            if -200 <= val <= 300:
                return date_str, round(val, 1)
    return None, None

def fetch_naaim():
    from bs4 import BeautifulSoup
    url = "https://www.naaim.org/programs/naaim-exposure-index/"
    for attempt in range(3):
        try:
            r = requests.get(url, timeout=20, headers={
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                              'AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36',
                'Accept': 'text/html,application/xhtml+xml',
            })
            r.raise_for_status()
            soup = BeautifulSoup(r.text, 'html.parser')
            tables = soup.find_all('table')
            if not tables:
                raise ValueError("no table on NAAIM page")
            for table in tables:
                date_str, val = _naaim_from_table(table)
                if val is not None:
                    print(f"  ✓ NAAIM: {val:.1f}% ({date_str})")
                    return {'value': val, 'date': date_str}
            raise ValueError("no parsable exposure value in any table")
        except Exception as e:
            print(f"  NAAIM attempt {attempt+1} failed: {e}")
            if attempt < 2:
                time.sleep(3 * (attempt + 1))
    warn("NAAIM unavailable")
    return None

# ── S&P 500 BREADTH COMPUTATION ──────────────────────────────────────────────────────────────────────
def compute_sp500_breadth():
    try:
        from bs4 import BeautifulSoup
        print("  Fetching S&P 500 component list...")
        r = requests.get('https://en.wikipedia.org/wiki/List_of_S%26P_500_companies',
                         timeout=15, headers={'User-Agent': 'Mozilla/5.0'})
        r.raise_for_status()
        soup = BeautifulSoup(r.text, 'html.parser')
        table = soup.find('table', {'id': 'constituents'})
        if not table:
            table = soup.find('table', {'class': 'wikitable'})
        if not table:
            raise ValueError("constituents table not found")
        tickers = []
        for row in table.find_all('tr')[1:]:
            cells = row.find_all('td')
            if not cells:
                continue
            t = cells[0].get_text(strip=True).replace('.', '-').upper()
            # Guard against grabbing the wrong table/column: real tickers are
            # 1-5 characters of A-Z plus an optional class suffix.
            if 1 <= len(t) <= 6 and all(c.isalpha() or c == '-' for c in t):
                tickers.append(t)
        # The S&P 500 has ~500 members; anything far off means we parsed the
        # wrong table and must not go on to publish breadth from it.
        if len(tickers) < 400:
            raise ValueError(f"parsed only {len(tickers)} constituents from Wikipedia")
        print(f"  Downloading {len(tickers)} tickers (1 year of daily closes)...")
        raw = yf.download(tickers, period='1y', interval='1d',
                          auto_adjust=True, progress=False, threads=True)
        if raw.empty:
            raise ValueError("No data returned")

        close = raw['Close'] if isinstance(raw.columns, pd.MultiIndex) else raw
        close = close.dropna(axis=1, how='all')
        covered = close.shape[1]
        # A rate-limited batch used to be silently averaged over whatever
        # survived; refuse to publish breadth computed off a thin sample.
        if covered < len(tickers) * COVERAGE_FLOOR:
            raise ValueError(f"only {covered}/{len(tickers)} constituents returned data")
        close = close.ffill()
        if len(close) < 5:
            raise ValueError("Not enough trading days in data")

        last = close.iloc[-1]
        prev = close.iloc[-2]

        changes = last - prev
        advancers = int((changes > 0).sum())
        decliners = int((changes < 0).sum())
        unchanged = int(covered - advancers - decliners)
        print(f"  A/D: {advancers} adv / {decliners} dec / {unchanged} unch")

        window = close.iloc[-252:] if len(close) >= 252 else close
        hi52 = window.max()
        lo52 = window.min()
        # A *new* 52-week high means today's close IS the highest close in the
        # window — not "within 1% of it", which is what this used to count and
        # report under the same label.
        new_highs = int((last >= hi52).sum())
        new_lows  = int((last <= lo52).sum())
        # Keep the looser measure too, correctly named, since it is the more
        # useful day-to-day signal.
        near_highs = int((last >= hi52 * 0.99).sum())
        near_lows  = int((last <= lo52 * 1.01).sum())
        print(f"  NH/NL: {new_highs} highs / {new_lows} lows "
              f"({near_highs}/{near_lows} within 1%)")

        def pct_above(n):
            # Return None, never 0.0 — "0% of the S&P is above its 200-day" is a
            # maximally bearish reading and must never stand in for missing data.
            if len(close) < n:
                return None
            sma = close.rolling(n).mean().iloc[-1]
            valid = sma.dropna()
            if valid.empty:
                return None
            return round(float((last[valid.index] > valid).sum()) / len(valid) * 100, 1)

        p20  = pct_above(20)
        p50  = pct_above(50)
        p200 = pct_above(200)
        print(f"  % above SMA: 20={p20} | 50={p50} | 200={p200}")

        return {
            'universe':        {'expected': len(tickers), 'covered': covered},
            'advance_decline': {'advancers': advancers, 'decliners': decliners,
                                'unchanged': unchanged},
            'new_high_low':    {'new_highs': new_highs, 'new_lows': new_lows,
                                'near_highs': near_highs, 'near_lows': near_lows},
            'pct_above_sma20':  p20,
            'pct_above_sma50':  p50,
            'pct_above_sma200': p200,
            'asof': close.index[-1].strftime('%Y-%m-%d'),
        }
    except Exception as e:
        warn(f"S&P 500 breadth failed: {e}")
        return None

def fetch_breadth(previous=None):
    """Fetch breadth & sentiment, keeping the previous value for any component
    that failed. A full run used to overwrite this block wholesale, so one bad
    scrape replaced good data with nulls — which the dashboard then rendered as
    real, and maximally bearish, readings."""
    previous = previous or {}
    print("\nFetching market breadth & sentiment...")
    fg = fetch_fear_greed()
    nm = fetch_naaim()
    sp = compute_sp500_breadth()

    fresh = {
        'fear_greed': fg,
        'naaim':      nm,
        'advance_decline':  sp.get('advance_decline')  if sp else None,
        'new_high_low':     sp.get('new_high_low')     if sp else None,
        'pct_above_sma20':  sp.get('pct_above_sma20')  if sp else None,
        'pct_above_sma50':  sp.get('pct_above_sma50')  if sp else None,
        'pct_above_sma200': sp.get('pct_above_sma200') if sp else None,
        'universe':         sp.get('universe')         if sp else None,
        'asof':             sp.get('asof')             if sp else None,
    }

    result = {}
    for key, val in fresh.items():
        if val is None and previous.get(key) is not None:
            result[key] = previous[key]
            # Mark it so the dashboard can show the value greyed out rather than
            # passing off yesterday's number as today's.
            result.setdefault('_stale', []).append(key)
            print(f"  ↩ {key}: kept previous value (this run failed)")
        else:
            result[key] = val
    return result

# ── MAIN FETCH ──────────────────────────────────────────────────────────────────────────────────────────────────────
def fetch_all(prices_only=False):
    existing = {}
    if OUT_PATH.exists():
        try:
            with open(OUT_PATH) as f:
                existing = json.load(f)
            print(f"✓ Loaded existing data.json (fallback if API fails)")
        except Exception as e:
            warn(f"could not load existing data.json: {e}")

    output = {
        'generated_at': utcnow().strftime('%Y-%m-%dT%H:%M:%SZ'),
        'futures':  [], 'dxvix':   [], 'metals':   [], 'commod':  [],
        'yields':   [], 'global':  [], 'etfmain':  [], 'submarket':[],
        'sector':   [], 'sectorew':[], 'thematic': [], 'country': [],
        'crypto':   [],
        'holdings': existing.get('holdings', {}),
        'breadth':  existing.get('breadth',  {}),
    }

    yf_etf_batches = [
        ('etfmain',   ETF_MAIN),
        ('submarket', SUBMARKET),
        ('sector',    SECTOR),
        ('sectorew',  SECTOR_EW),
        ('thematic',  THEMATIC),
        ('country',   COUNTRY),
    ]
    yf_individual_batches = [
        ('global',    GLOBAL_IDX),
    ]
    yf_batches = [
        ('crypto',    CRYPTO_YF),
        # Was hardcoded, which silently ignored the "dxvix" key in tickers.json.
        ('dxvix',     DX_VIX),
        ('futures',   FUTURES),
        ('metals',    METALS),
        ('commod',    ENERGY),
    ]

    for key, tickers in yf_individual_batches:
        if not tickers: continue
        print(f"Fetching {key} ({len(tickers)} tickers) via yfinance (individual)...")
        raw = fetch_individual(tickers)
        for yf_sym in tickers:
            rec = raw.get(yf_sym)
            if rec:
                output[key].append(rec)
            else:
                print(f"  ⚠ No data for {yf_sym}")

    for key, tickers in yf_etf_batches + yf_batches:
        if not tickers: continue
        print(f"Fetching {key} ({len(tickers)} tickers) via yfinance...")
        raw = fetch_batch(tickers)
        for yf_sym in tickers:
            rec = raw.get(yf_sym)
            if rec:
                output[key].append(rec)
            else:
                print(f"  ⚠ No data for {yf_sym}")
        time.sleep(1)

    print("Fetching treasury yields via yfinance + FRED fallback...")
    raw = fetch_batch(YIELDS)
    for yf_sym in YIELDS:
        rec = raw.get(yf_sym)
        if rec:
            yield_map = {'^TNX': 'US10Y', '^TYX': 'US30Y'}
            rec['sym'] = yield_map.get(yf_sym, rec['sym'])
            output['yields'].append(rec)
    rec_2y = fetch_treasury_2y()
    if rec_2y:
        output['yields'].insert(0, rec_2y)

    # ── PER-SYMBOL MERGE ───────────────────────────────────────────────────────
    # The old logic only restored a section when it came back COMPLETELY empty.
    # If Yahoo returned 5 of 81 thematic ETFs, the section was "non-empty", so
    # the other 76 simply vanished from the dashboard with no warning. Merge at
    # the symbol level instead: every ticker we expected either has fresh data or
    # falls back to its last known record, flagged stale.
    #
    # Sections explicitly configured empty (e.g. sectorew) stay empty.
    expected_syms = {
        'etfmain':   ETF_MAIN,
        'submarket': SUBMARKET,
        'sector':    SECTOR,
        'sectorew':  SECTOR_EW,
        'thematic':  THEMATIC,
        'country':   COUNTRY,
        'crypto':    CRYPTO_YF,
        'global':    GLOBAL_IDX,
        'dxvix':     DX_VIX,
        'futures':   FUTURES,
        'metals':    METALS,
        'commod':    ENERGY,
        'yields':    YIELDS,
    }
    for key, source in expected_syms.items():
        if not source:
            continue
        want = {TICKER_REMAP.get(s, s) for s in source}
        if key == 'yields':
            want |= {'US2Y'}   # sourced from FRED, not the yfinance list
        have = {r.get('sym') for r in output.get(key, [])}
        missing = want - have
        if not missing:
            continue
        prev_by_sym = {r.get('sym'): r for r in (existing.get(key) or [])}
        restored = []
        for sym in missing:
            old = prev_by_sym.get(sym)
            if old:
                old = dict(old)
                old['stale'] = True     # dashboard greys these out
                output[key].append(old)
                restored.append(sym)
        still_missing = missing - set(restored)
        if restored:
            print(f"  ↩ {key}: kept previous values for {sorted(restored)}")
        if still_missing:
            warn(f"{key}: no data for {sorted(still_missing)}")
        if len(have) < len(want) * COVERAGE_FLOOR:
            warn(f"{key}: only {len(have)}/{len(want)} tickers returned fresh data")

    if output['dxvix']:
        _order = {'DX-Y.NYB': 0, 'CBOE:VIX': 1}
        output['dxvix'].sort(key=lambda x: _order.get(x.get('sym', ''), 99))

    # Sort AFTER the merge so restored rows land in the right place. Records with
    # no 1W value sort last instead of being treated as 0%.
    for key in ('country', 'sector', 'sectorew', 'thematic', 'submarket'):
        output[key].sort(key=lambda x: (x.get('w1') is None, -(x.get('w1') or 0)))

    _yorder = {'US2Y': 0, 'US10Y': 1, 'US30Y': 2}
    output['yields'].sort(key=lambda x: _yorder.get(x.get('sym', ''), 99))

    if not prices_only:
        holdings_tickers = list(dict.fromkeys(
            ETF_MAIN + SUBMARKET + SECTOR + SECTOR_EW + THEMATIC + COUNTRY
        ))
        print(f"\nFetching ETF holdings ({len(holdings_tickers)} ETFs)...")
        output['holdings'] = fetch_etf_holdings(
            holdings_tickers, previous=existing.get('holdings') or {})
        print(f"✓ Holdings available for {len(output['holdings'])} ETFs")

        output['breadth'] = fetch_breadth(previous=existing.get('breadth') or {})
        print(f"✓ Breadth data fetched")
    else:
        print("\nPrices-only mode — skipping holdings & breadth (preserved from last full run)")

    return output

if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description='Market Dashboard Data Fetcher')
    parser.add_argument('--prices-only', action='store_true',
                        help='Refresh prices only; skip holdings & breadth (for intraday runs)')
    args = parser.parse_args()

    mode = 'PRICES ONLY' if args.prices_only else 'FULL RUN'
    print(f"=== Market Dashboard Data Fetch [{mode}] ===")
    print(f"Time: {utcnow():%Y-%m-%d %H:%M:%S} UTC\n")
    data = fetch_all(prices_only=args.prices_only)

    data['warnings'] = WARNINGS
    # The date the *market data* is for, as opposed to when the job ran. The
    # dashboard uses this to tell "refreshed today" from "refreshed today with
    # Friday's prices" — which is what a holiday run produces.
    asofs = [r.get('asof') for v in data.values() if isinstance(v, list)
             for r in v if isinstance(r, dict) and r.get('asof')]
    data['data_asof'] = max(asofs) if asofs else None

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    # Write to a temp file and rename, so an interrupted run can never leave a
    # half-written data.json for the dashboard to choke on.
    tmp = OUT_PATH.with_suffix('.json.tmp')
    with open(tmp, 'w') as f:
        # allow_nan=False guarantees we never write invalid JSON (NaN/Infinity),
        # which the browser's JSON.parse rejects; _sanitize clears any that slip
        # through so this won't raise.
        json.dump(_sanitize(data), f, indent=2, allow_nan=False)
    tmp.replace(OUT_PATH)

    total = sum(len(v) for v in data.values() if isinstance(v, list))
    print(f"\n✓ Wrote {total} records to {OUT_PATH}")
    print(f"  Data as of: {data['data_asof']}")
    print(f"  Yields: {[x['sym'] for x in data['yields']]}")
    print(f"  Thematic top 3: {[x['sym'] for x in data['thematic'][:3]]}")
    print(f"  Holdings for: {len(data['holdings'])} ETFs")
    if WARNINGS:
        print(f"\n⚠ {len(WARNINGS)} warning(s):")
        for w in WARNINGS:
            # ::warning:: surfaces these in the Actions run summary, so a
            # degraded run is visible without opening the log. We still exit 0 —
            # partial data is worth committing; it just must not look pristine.
            print(f"::warning::{w}")
    else:
        print("\n✓ No warnings — full clean run")
