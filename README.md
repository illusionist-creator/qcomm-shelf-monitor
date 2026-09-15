# Quick-Commerce Shelf Monitor

Know where your products sit on Blinkit search, who is buying the sponsored slots above you, what everyone is charging, and which listings are sold out, by keyword and by pincode, every day.

Three parts:

- **`server/`**: a FastAPI service that runs a live capture for any keyword and pincode typed into the dashboard. One headless browser at a time, results cached for six hours, six live captures per IP per hour. It also serves the dashboard, so `http://127.0.0.1:8000` is the full live app.

- **`tracker/`**: a Python + Playwright collector. For each keyword and pincode it drives a real browser session on blinkit.com, sets the delivery location, runs the search, and captures the JSON that Blinkit's own search API returns as the page scrolls. It records every product's rank, sponsored flag, brand, pack size, price, MRP, discount, stock and sold-out state, then matches the SKUs you track and writes two CSVs plus an Excel file. Runs resume where they stopped.
- **`app/`**: a single-file dashboard (`index.html`) that reads those CSVs. It ships with a sample capture embedded, and you can drop your own `allresults.csv` and `ranks.csv` onto it; nothing leaves the browser.

Live demo: https://illusionist-creator.github.io/qcomm-shelf-monitor/

## What the dashboard shows

| View | Question it answers |
|---|---|
| Share of shelf by brand | Of the top-N slots for a keyword, how many does each brand hold, organic vs sponsored |
| Where the ads sit | Which slots are sponsored at each pincode, where your tracked SKUs land, what is sold out |
| Price ladder | Every product's selling price for the keyword, cheapest first, with MRP and discount on hover |
| Tracked SKUs | Rank, placement, price, stock and status of each tracked product per pincode |
| Availability by brand | Sold-out share of each brand's listings |
| Discount depth by brand | Median discount off MRP per brand |
| Rank over time | Tracked SKU positions across runs (appears once there is more than one run) |
| All captured results | The raw rows, filterable |

## Run the tracker

```
python -m pip install playwright openpyxl
python -m playwright install chromium

python tracker/blinkit_keyword_rank.py --products data/products.csv --keywords data/keywords.csv --pincodes data/pincodes.csv --topn 60
```

Inputs are one-column CSV or XLSX files (header row optional): the products you track, the search keywords, the pincodes. Outputs: `blinkit_allresults.csv` (every result), `blinkit_ranks.csv` and `blinkit_ranks.xlsx` (your tracked SKUs matched to the results). Re-running appends a new timestamped snapshot, so a daily run on a scheduler gives you rank history.

Useful flags: `--limit 1 --show --debug` to watch one keyword in a visible browser; `--delay` to slow down between searches; `--threshold` to loosen the name-matching score.

## Run the live app (type any keyword)

```
python -m venv .venv
.venv\Scripts\pip install -r server/requirements.txt      # macOS/Linux: .venv/bin/pip
.venv\Scripts\python -m playwright install chromium
.venv\Scripts\python -m uvicorn server.app:app --port 8000
```

Open http://127.0.0.1:8000, type a keyword and a pincode in the terminal panel and press run. The log streams each step (location set, API pages captured) and the charts switch to the new capture when it lands, usually in 25 to 45 seconds. Captures for the same keyword and pincode on the same day replace the earlier rows.

The GitHub Pages copy has no server behind it, so there `run` queries the embedded sample dataset. To give it a live backend, deploy `server/` (below) and rebuild the page with `--api https://your-service-url`, or append `?api=https://your-service-url` to the page URL.

### Deploy the API

`server/Dockerfile` builds on Microsoft's Playwright image and `render.yaml` describes the service for Render (New → Blueprint → pick this repo). Any Docker host works: Railway, Fly.io, a small VPS. Two cautions before relying on it:

- Chromium needs memory. A 512 MB free tier may crash under load; 1 GB is comfortable.
- Blinkit may treat datacenter IPs differently from home broadband. Test a capture from the deployed service before sharing the link.

Settings: `ALLOWED_ORIGINS` (comma-separated, for CORS), `PER_IP_PER_HOUR`, `CACHE_TTL` seconds.

## Build the dashboard with your data

```
python app/build.py path/to/blinkit_allresults.csv path/to/blinkit_ranks.csv --label "Sep 2026 capture"
```

This writes `app/index.html` with the data embedded. Open it from disk or host it anywhere static. The published demo is the copy in `docs/`, served by GitHub Pages; copy `app/index.html` there after a rebuild.

## Notes and limits

- The tracker reads the same API the Blinkit website uses. Use it at a polite pace (the default delay is 1.5 s per search) and only for products and keywords you have a legitimate interest in.
- `inventory` is the stock count the API returns for the selected location. Sold-out is derived from three signals (inventory 0, add-to-cart max 0, the sold-out badge) because the API's own flag is unreliable.
- Search results depend on the delivery location. Pick pincodes that map to the dark stores you care about.
- Blinkit only today. Zepto and Swiggy Instamart use the same approach and are on the list.

## The service behind this

I run this for consumer brands as a monthly service: a daily capture of your keywords across the cities you sell in, a dashboard like the one above with your SKUs highlighted, and a weekly note on what moved: lost ranks, new sponsored competitors, price cuts by rivals, stock-outs by dark store. Contact: linkedin.com/in/keyur-makwana.
