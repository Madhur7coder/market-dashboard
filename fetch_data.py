import json
import time
import datetime
import sys
import math
import concurrent.futures
from pathlib import Path
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
    subprocess.check_call([sys.executable, "-m", "pip", "install", "pandas"])
    import pandas as pd

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
THEMATIC   = ['BOTZ','HACK','SOXX','ICLN','SKYY','XBI','ITA','FINX','ARKG','URA',
              'AIQ','CIBR','ROBO','ARKK','DRIV','OGIG','ACES','PAVE','HERO','CLOU']
COUNTRY    = ['ARGT','EUFN','MCHI','EWZ','EWI','EWY','EWH',
              'EWC','EWL','EWA','IEV','IEUR','INDA','EWG',
              'EZU','EEM','EFA','TUR','ACWI','EWJ','EWT']
FUTURES    = ['ES=F','NQ=F','RTY=F','YM=F']
METALS     = ['GC=F','SI=F','HG=F','PL=F','PA=F']
ENERGY     = ['CL=F','NG=F']
GLOBAL_IDX = ['^N225','^KS11','^NSEI','000001.SS','^HSI','^FTSE','^FCHI','^GDAXI']
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
    if out_sym in ('US10Y', 'US30Y'):
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

# ── MAIN FETCH ────────────────────────────────────────────────────────
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
        'sector':   [], 'thematic': [], 'country': [],
        'crypto':   [],
        'holdings': existing.get('holdings', {}),
    }

    yf_etf_batches = [
        ('etfmain',   ETF_MAIN),
        ('submarket', SUBMARKET),
        ('sector',    SECTOR),
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

    # World indices used to be pulled one symbol at a time — 8 sequential
    # requests, each with its own 0.3s pause and up to 3 retries with 2-4s
    # backoff, for data one batched call returns. Batch first, then retry only
    # the stragglers individually, which is both faster and no less robust:
    # a batch that drops a symbol still gets it a second chance on its own.
    for key, tickers in yf_individual_batches:
        if not tickers: continue
        print(f"Fetching {key} ({len(tickers)} tickers) via yfinance...")
        raw = fetch_batch(tickers)
        stragglers = [s for s in tickers if s not in raw]
        if stragglers:
            print(f"  retrying {len(stragglers)} individually: {stragglers}")
            raw.update(fetch_individual(stragglers))
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

    # ── PER-SYMBOL MERGE ───────────────────────────────────────────────────────
    # The old logic only restored a section when it came back COMPLETELY empty.
    # If Yahoo returned 5 of 81 thematic ETFs, the section was "non-empty", so
    # the other 76 simply vanished from the dashboard with no warning. Merge at
    # the symbol level instead: every ticker we expected either has fresh data or
    # falls back to its last known record, flagged stale.
    #
    expected_syms = {
        'etfmain':   ETF_MAIN,
        'submarket': SUBMARKET,
        'sector':    SECTOR,
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
    for key in ('country', 'sector', 'thematic', 'submarket'):
        output[key].sort(key=lambda x: (x.get('w1') is None, -(x.get('w1') or 0)))

    _yorder = {'US10Y': 0, 'US30Y': 1}
    output['yields'].sort(key=lambda x: _yorder.get(x.get('sym', ''), 99))

    if not prices_only:
        # Only the tables that actually render a holdings drawer: etfmain, sector
        # and thematic. submarket and country are fetched for the daily brief's
        # mover ranking, which never reads holdings, so pulling theirs was ~30
        # ETF requests a run that nothing consumed.
        holdings_tickers = list(dict.fromkeys(ETF_MAIN + SECTOR + THEMATIC))
        print(f"\nFetching ETF holdings ({len(holdings_tickers)} ETFs)...")
        output['holdings'] = fetch_etf_holdings(
            holdings_tickers, previous=existing.get('holdings') or {})
        print(f"✓ Holdings available for {len(output['holdings'])} ETFs")
    else:
        print("\nPrices-only mode — skipping holdings (preserved from last full run)")

    check_equity_freshness(output)
    return output

def last_completed_us_session(now=None):
    """The most recent weekday whose US cash session has finished.

    The US close is 20:00 UTC (21:00 under DST); allow an hour past the later of
    the two before expecting Yahoo to carry that day's daily bar. Weekends roll
    back to Friday. Market holidays are not modelled — they make this one day
    optimistic, which is why a mismatch is a warning and never a hard failure.
    """
    now = now or utcnow()
    d = now.date()
    if now.hour < 22:
        d -= datetime.timedelta(days=1)
    while d.weekday() >= 5:            # 5=Sat, 6=Sun
        d -= datetime.timedelta(days=1)
    return d

def check_equity_freshness(output):
    """Warn when the equity bars are a session behind everything else.

    This is the failure that motivated the check: futures, metals, yields and
    crypto all carried Friday's bar while every equity ETF was still on
    Thursday's, and nothing anywhere said so — the page looked current and the
    brief ranked a stale session's movers as though they were last night's.
    """
    equity_keys = ('etfmain', 'sector', 'thematic', 'submarket', 'country')
    dates = [r['asof'] for k in equity_keys for r in output.get(k, [])
             if r.get('asof') and not r.get('stale')]
    if not dates:
        warn("equities: no fresh bars at all in this run")
        return
    newest = max(dates)
    expected = last_completed_us_session().isoformat()
    if newest < expected:
        warn(f"equities are a session behind: newest bar {newest}, "
             f"expected {expected} — Yahoo had not consolidated the daily bar")
    behind = sorted({d for d in dates if d < newest})
    if behind:
        counts = {d: sum(1 for x in dates if x == d) for d in behind}
        warn(f"mixed equity session dates alongside {newest}: {counts}")

if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description='Market Dashboard Data Fetcher')
    parser.add_argument('--prices-only', action='store_true',
                        help="Refresh prices only; skip ETF holdings (for intraday runs)")
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
