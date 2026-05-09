# Market Command Centre — Madhur7coder

Personal end-of-day market dashboard, hosted on GitHub Pages, with data auto-fetched on weekday evenings from Yahoo Finance via GitHub Actions.

Live URL (after deploy): `https://madhur7coder.github.io/market-dashboard/`

## How it works

1. Mon–Fri at 22:00 UTC (17:00 EST / 18:00 EDT, after the US cash close) GitHub Actions runs `fetch_data.py`.
2. The script pulls ~120 tickers from Yahoo Finance (no API key required) plus Fear & Greed, NAAIM, and S&P 500 breadth.
3. Output is written to `data/data.json` and committed back to the repo.
4. GitHub Pages serves `index.html`, which fetches `data/data.json` on load and renders every panel.

## Repo layout

```
market-dashboard/
├── index.html              ← The dashboard (GitHub Pages serves this)
├── fetch_data.py           ← Yahoo Finance fetcher
├── tickers.json            ← Ticker config (edit this to change the universe)
├── README.md
├── data/
│   └── data.json           ← Generated daily — do NOT edit manually
└── .github/workflows/
    └── fetch-data.yml      ← Weekday cron + manual trigger
```

## One-time setup

1. **Create the repo on GitHub** named `market-dashboard` under `Madhur7coder`. Make it **Public** (required for free GitHub Pages).
2. **Push this folder** to the repo:
   ```
   cd C:\Users\madhu\market-dashboard
   git init
   git add .
   git commit -m "Initial dashboard"
   git branch -M main
   git remote add origin https://github.com/Madhur7coder/market-dashboard.git
   git push -u origin main
   ```
3. **Enable GitHub Pages**: repo Settings → Pages → Source: *Deploy from a branch* → Branch: `main` / `(root)` → Save.
4. **Run the action manually once** so `data/data.json` is generated immediately: Actions tab → *Fetch Market Data* → *Run workflow*.
5. Open `https://madhur7coder.github.io/market-dashboard/` — the dashboard should load with live EOD prices.

## Customizations vs. upstream

This fork applies these tweaks:

- Clock and timestamps display in **US Eastern** (America/New_York), not HKT.
- Removed the Benjamin Franklin quote banner.
- Removed the 5-day sparkline column from every individual table.
- Removed the **S&P 500 EW Sub-Sector** section entirely.
- Trimmed Country ETFs to drop Saudi Arabia, Indonesia, Sweden, Chile, France, Spain, Thailand, Singapore, Greece, Mexico, South Africa.
- Added a **Trend Identification** panel at the top showing whether SPY is above its 21 / 50 / 200-day EMA (Short-term / Intermediate / Long-term).
- Added a header link to Pradeep Bonde's Stockbee Market Monitor methodology page.

## Schedule

The action runs Mon–Fri at 22:00 UTC (about 17:00 EST / 18:00 EDT, after the US cash close). You can also trigger it on demand from the Actions tab — useful after editing `tickers.json`.

## Data sources

- **Equities, ETFs, Futures, Commodities, Crypto, Yields**: Yahoo Finance via `yfinance` (free).
- **Fear & Greed Index**: CNN graph endpoint.
- **NAAIM Exposure Index**: scraped from `naaim.org`.
- **S&P 500 component breadth**: tickers from Wikipedia, prices from Yahoo Finance.
- **2-Year Treasury fallback**: FRED CSV endpoint.

## Troubleshooting

- **"Demo data" banner**: `data/data.json` doesn't exist yet. Trigger the action manually.
- **Some tickers missing**: Yahoo occasionally has gaps; check the Action's run log.
- **Action failed**: usually a transient Yahoo outage — re-run from the Actions tab.
- **Dashboard not updating after a code change**: GitHub Pages caches aggressively. Hard-refresh (Ctrl+F5) or wait ~1 minute.
