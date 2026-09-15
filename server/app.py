"""Live capture API for the Quick-Commerce Shelf Monitor.

    uvicorn server.app:app --host 0.0.0.0 --port 8000

POST /jobs   {"keyword": "peanut butter", "pincode": "560001", "topn": 40}  -> {"id": ...}
GET  /jobs/{id}                                                           -> status, log lines, result rows
GET  /health

One browser at a time (a lock), results cached per keyword+pincode for CACHE_TTL
seconds, and a small per-IP budget so a public demo cannot be used to hammer
Blinkit. Rows come back in the same shape as the tracker's allresults.csv so the
dashboard can merge them straight in.
"""
import os, sys, time, uuid, threading, datetime, re
from collections import deque, defaultdict

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "tracker"))
import blinkit_keyword_rank as trk  # noqa: E402

CACHE_TTL = int(os.environ.get("CACHE_TTL", 6 * 3600))
PER_IP_PER_HOUR = int(os.environ.get("PER_IP_PER_HOUR", 6))
MAX_TOPN = 60
ALLOWED_ORIGINS = [o for o in os.environ.get("ALLOWED_ORIGINS", "https://illusionist-creator.github.io,http://127.0.0.1:8000,http://localhost:8000,null").split(",") if o]

app = FastAPI(title="Quick-Commerce Shelf Monitor API", version="1.0")
app.add_middleware(CORSMiddleware, allow_origins=ALLOWED_ORIGINS, allow_methods=["GET", "POST"], allow_headers=["*"])

jobs: dict = {}
cache: dict = {}
ip_hits: dict = defaultdict(deque)
queue: deque = deque()
lock = threading.Lock()
worker_started = False


class JobIn(BaseModel):
    keyword: str = Field(min_length=2, max_length=40)
    pincode: str = Field(pattern=r"^\d{6}$")
    topn: int = Field(default=40, ge=12, le=MAX_TOPN)


def _rows(cards, keyword, pincode, status, ts):
    out = []
    for c in cards:
        out.append({"Keyword": keyword, "Pincode": pincode, "Search Status": status, "Timestamp": ts,
                    "Position": c["position"], "Is Sponsored": str(bool(c["is_sponsored"])),
                    "Product ID": c["product_id"], "Brand Name": c["brand"], "Name": c["name"], "Pack Size": c["pack"],
                    "Price (Rs)": c["price"], "MRP (Rs)": c["mrp"], "Discount (Rs)": c["discount"],
                    "Discount % of MRP": c["discount_pct"], "Inventory (stock)": c["inventory"], "Max Cart": c["max_cart"],
                    "Sold Out": str(bool(c["sold_out"])), "URL": c["url"]})
    return out


def _run_job(job):
    log = job["log"]
    key = (job["keyword"].lower(), job["pincode"], job["topn"])
    hit = cache.get(key)
    if hit and time.time() - hit["t"] < CACHE_TTL:
        log.append(f"cache hit: captured {int((time.time() - hit['t']) // 60)} min ago")
        job.update(status="done", result=hit["rows"], cached=True)
        return
    from playwright.sync_api import sync_playwright
    log.append("launching chromium")
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])
        try:
            ctx = browser.new_context(viewport={"width": 1366, "height": 900},
                                      user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36")
            page = ctx.new_page()
            log.append(f"setting delivery location to {job['pincode']}")
            serviceable = trk.set_location(page, job["pincode"])
            if not serviceable:
                log.append("blinkit says this pincode is not serviceable")
            log.append(f"searching '{job['keyword']}' and scrolling for up to {job['topn']} results")
            captured = {"n": 0}

            def progress(resp):
                if "/v1/layout/search" in resp.url:
                    captured["n"] += 1
                    log.append(f"captured API page {captured['n']}")
            page.on("response", progress)
            status, cards = trk.collect_products(page, job["keyword"], target=job["topn"], delay_ms=1200)
            if not serviceable:
                status = "not-serviceable"
            ts = datetime.datetime.now().isoformat(timespec="seconds")
            rows = _rows(cards, job["keyword"], job["pincode"], status, ts)
            ads = sum(1 for c in cards if c["is_sponsored"]); oos = sum(1 for c in cards if c["sold_out"])
            log.append(f"done: {len(cards)} products, {ads} sponsored, {oos} sold out, status={status}")
            cache[key] = {"t": time.time(), "rows": rows}
            job.update(status="done", result=rows, cached=False)
        finally:
            browser.close()


def _worker():
    while True:
        if queue:
            job = queue.popleft()
            job["status"] = "running"
            try:
                with lock:
                    _run_job(job)
            except Exception as e:  # noqa: BLE001
                job["log"].append(f"error: {type(e).__name__}: {str(e)[:200]}")
                job["status"] = "error"
        else:
            time.sleep(0.5)
        # forget old jobs
        cutoff = time.time() - 1800
        for jid in [k for k, v in jobs.items() if v["created"] < cutoff]:
            jobs.pop(jid, None)


@app.on_event("startup")
def _start():
    global worker_started
    if not worker_started:
        threading.Thread(target=_worker, daemon=True).start()
        worker_started = True


PAGE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "docs", "index.html")


@app.get("/", include_in_schema=False)
def page():
    """Serve the dashboard from the same origin, so the page finds the API with no config."""
    if not os.path.exists(PAGE):
        raise HTTPException(404, "docs/index.html not built yet")
    return FileResponse(PAGE, media_type="text/html")


@app.get("/health")
def health():
    return {"ok": True, "queued": len(queue), "cached": len(cache)}


@app.post("/jobs")
def create_job(body: JobIn, request: Request):
    ip = request.headers.get("x-forwarded-for", request.client.host if request.client else "?").split(",")[0].strip()
    now = time.time()
    hits = ip_hits[ip]
    while hits and hits[0] < now - 3600:
        hits.popleft()
    key = (body.keyword.lower(), body.pincode, body.topn)
    cached = key in cache and now - cache[key]["t"] < CACHE_TTL
    if not cached and len(hits) >= PER_IP_PER_HOUR:
        raise HTTPException(429, f"Limit is {PER_IP_PER_HOUR} live captures per hour per IP. Try a cached keyword or come back later.")
    if not cached:
        hits.append(now)
    keyword = re.sub(r"\s+", " ", body.keyword).strip()
    jid = uuid.uuid4().hex[:12]
    job = {"id": jid, "keyword": keyword, "pincode": body.pincode, "topn": body.topn, "status": "queued",
           "log": [f"queued ({len(queue)} ahead)"], "result": None, "created": now, "cached": cached}
    jobs[jid] = job
    queue.append(job)
    return {"id": jid, "cached": cached}


@app.get("/jobs/{jid}")
def get_job(jid: str):
    job = jobs.get(jid)
    if not job:
        raise HTTPException(404, "unknown job")
    return {k: job[k] for k in ("id", "keyword", "pincode", "topn", "status", "log", "result", "cached")}
