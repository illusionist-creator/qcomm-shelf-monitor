"""
blinkit_keyword_rank.py  (API edition)
--------------------------------------
Rebuilt to read Blinkit's search API instead of scraping the DOM. The results grid is
virtualized (only ~24 cards ever in the DOM), which is why DOM-scraping capped at 24.
The page fetches products from  /v1/layout/search  (12 per page) as you scroll. This
script drives the browser (for your session + location), then CAPTURES those API
responses and parses their JSON.

FIELDS (verified against a real captured response you provided):
  data.product_id / data.identity.id      -> product id
  data.name.text                          -> product name
  data.brand_name.text                    -> BRAND (real, no more guessing)
  data.variant.text                       -> pack size (e.g. '150 g')
  data.normal_price.text / data.mrp.text  -> price / MRP
  data.inventory                          -> stock count at this pincode (interpretation:
                                             sellable qty; VERIFY against 'add to cart' max)
  data.is_sold_out                        -> sold-out flag
  data.overlay_badges[] image ad_without_bg.png -> SPONSORED/ad flag (reliable)
  response.pagination.next_url            -> next page (offset += 12)

VERIFIED vs NOT (be aware):
  * VERIFIED here: the JSON parser (tested on your sample) + the run/resume/output plumbing.
  * NOT tested from the author's environment: the live response capture and that scrolling
    triggers every page. Your own captured URL (offset=48, page 4) shows scrolling DOES
    paginate, so this is well-grounded, but confirm with a --debug run.
  * 'inventory' as true stock is an INTERPRETATION, not confirmed by Blinkit docs.

SETUP: python -m pip install playwright openpyxl ; python -m playwright install chromium
CALIBRATE: python blinkit_keyword_rank.py --products p.xlsx --keywords k.xlsx --pincodes Pin.xlsx --limit 1 --show --debug --topn 70
"""

import sys, os, re, csv, json, time, argparse, datetime, urllib.parse
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
from openpyxl import Workbook, load_workbook

RANK_COLS = ["Keyword", "Pincode", "Search Status", "Timestamp", "Brand Name", "Tracked Product",
             "Matched", "Matched Card", "Pack Size", "Inventory (stock)", "Max Cart", "Overall Position",
             "Organic Position", "Sponsored Position", "Is Sponsored", "Price (Rs)", "MRP (Rs)",
             "Discount (Rs)", "Discount % of MRP", "Sold Out", "Total Results", "Match Score", "Product URL"]
ALL_COLS = ["Keyword", "Pincode", "Search Status", "Timestamp", "Position", "Is Sponsored",
            "Product ID", "Brand Name", "Name", "Pack Size", "Price (Rs)", "MRP (Rs)",
            "Discount (Rs)", "Discount % of MRP", "Inventory (stock)", "Max Cart", "Sold Out", "URL"]


# ---------------------------------------------------------------- small helpers
def norm(s): return re.sub(r"[^a-z0-9]+", " ", str(s).lower()).strip()
def toks(s): return [t for t in norm(s).split() if t]

def read_list(path):
    if path.lower().endswith((".xlsx", ".xlsm")):
        ws = load_workbook(path).active
        col = [ws.cell(r, 1).value for r in range(1, ws.max_row + 1)]
    else:
        with open(path, encoding="utf-8-sig") as f:
            col = [row[0] if row else None for row in csv.reader(f)]
    vals = []
    for i, v in enumerate(col):
        if v is None or str(v).strip() == "":
            continue
        s = str(v).strip()
        if i == 0 and s.lower() in ("product", "products", "product name", "keyword", "keywords",
                                    "pincode", "pincodes", "pin", "name", "title",
                                    "brand", "brands", "brand name"):
            continue
        vals.append(s)
    seen, out = set(), []
    for v in vals:
        if v not in seen:
            seen.add(v); out.append(v)
    return out

def match_score(tracked, card):
    tt, ct = set(toks(tracked)), set(toks(card))
    if not tt: return 0.0
    if norm(tracked) in norm(card): return 1.0
    return len(tt & ct) / len(tt)

def _rupee(t):
    if not t: return None
    m = re.search(r"\d+", str(t).replace(",", ""))
    return int(m.group()) if m else None

def _offset(url):
    try:
        q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        return int(q.get("offset", ["0"])[0])
    except Exception:
        return 0

def not_serviceable(body): return "not available at this location" in (body or "").lower()


# ---------------------------------------------------------------- API JSON parsing (VERIFIED on sample)
def parse_api_products(obj):
    """Extract product dicts from one /v1/layout/search JSON response."""
    out = []
    resp = (obj or {}).get("response") or {}
    for s in resp.get("snippets", []):
        if s.get("widget_type") != "product_card_snippet_type_2":
            continue
        dt = s.get("data", {}) or {}
        def t(k):
            v = dt.get(k)
            return v.get("text") if isinstance(v, dict) else None
        pid = dt.get("product_id") or (dt.get("identity") or {}).get("id")
        badges = dt.get("overlay_badges") or []
        is_ad = any("ad_without_bg" in json.dumps(b) for b in badges)
        price = _rupee(t("normal_price"))
        mrp = _rupee(t("mrp"))
        if mrp is None:                      # (2) no MRP shown -> selling price IS the MRP
            mrp = price
        disc = (mrp - price) if (mrp is not None and price is not None) else None
        disc_pct = round(disc / mrp * 100, 1) if (disc is not None and mrp) else None
        max_cart = (dt.get("stepper_data_v2") or {}).get("max_count")  # (1) verified field
        inv = dt.get("inventory")
        api_sold_out = dt.get("is_sold_out")

        # ---- Out-of-stock detection (FIX) --------------------------------------
        # Blinkit's API `is_sold_out` came back False even for products the storefront
        # renders with the "Out of Stock" overlay. Verified in a real capture on
        # pids 669341 and 555304: both had inventory=0 AND stepper max_count=0 while
        # is_sold_out=False. So we no longer trust is_sold_out alone.
        #
        # Signals (any one -> treated as sold out):
        #   api_flag     : is_sold_out is truthy (kept, in case Blinkit does set it)
        #   inventory_0  : inventory == 0        (INTERPRETATION: 0 sellable stock)
        #   max_cart_0   : stepper max_count == 0 (can't add to cart)
        #   badge_text   : the raw snippet JSON contains an "out of stock" label.
        #                  HEURISTIC / UNVERIFIED: the API may encode this as a
        #                  boolean/enum rather than the literal DOM text, in which
        #                  case this signal simply won't fire (the inventory checks
        #                  still catch it). Confirm the real key with a --debug
        #                  raw JSON dump if you want a first-class field.
        # Note: None (missing field) is NOT treated as 0 -> avoids false positives.
        oos = []
        if api_sold_out:                     oos.append("api_flag")
        if inv == 0:                         oos.append("inventory_0")
        if max_cart == 0:                    oos.append("max_cart_0")
        if re.search(r"out\s*of\s*stock", json.dumps(dt), re.I):
            oos.append("badge_text")
        sold_out = bool(oos)
        # ------------------------------------------------------------------------

        out.append({
            "product_id": str(pid) if pid is not None else "",
            "name": t("name") or "",
            "brand": t("brand_name") or "",
            "pack": t("variant") or "",
            "price": price,
            "mrp": mrp,
            "discount": disc,               # (3) MRP - Price (0 when MRP auto-filled)
            "discount_pct": disc_pct,       # (3) % of MRP
            "inventory": inv,
            "max_cart": max_cart,
            "sold_out": sold_out,           # FIX: derived, not raw is_sold_out
            "oos_reason": ",".join(oos),    # debug only; not written to CSV schema
            "is_sponsored": is_ad,
        })
    return out


# ---------------------------------------------------------------- browser
def set_location(page, query):
    page.goto("https://blinkit.com/", wait_until="domcontentloaded")
    page.wait_for_timeout(2500)
    for sel in ["text=Select Location", "[class*='LocationBar']", "header [class*='location' i]"]:
        try: page.click(sel, timeout=3000); break
        except PWTimeout: continue
    try:
        box = page.wait_for_selector(
            "input[placeholder*='search delivery location' i], input[name*='select-locality' i], input[type='text']",
            timeout=6000)
        box.fill(str(query)); page.wait_for_timeout(2500)
        page.click("[class*='LocationSearch'] [class*='address'], [class*='suggestion'] >> nth=0", timeout=6000)
        page.wait_for_timeout(3000)
    except PWTimeout:
        print(f"  [warn] location {query}: modal/selectors didn't match (SELECTOR-FIX).")
    body = ""
    try: body = page.inner_text("body")
    except Exception: pass
    return not not_serviceable(body)

def do_search(page, keyword):
    try:
        page.keyboard.press("Escape"); page.wait_for_timeout(400)
    except Exception: pass
    q = urllib.parse.quote(str(keyword))
    try:
        page.goto(f"https://blinkit.com/s/?q={q}", wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(3500)
        return
    except Exception:
        pass
    for sel in ["header input[type='text']", "[class*='SearchBar'] input", "input[type='search']"]:
        try:
            page.click(sel, timeout=3000); page.wait_for_timeout(700)
            inp = page.wait_for_selector("input[type='text'], input[type='search']", timeout=3000)
            inp.fill(str(keyword)); page.keyboard.press("Enter"); page.wait_for_timeout(3500)
            return
        except PWTimeout:
            continue


def collect_products(page, keyword, target=0, delay_ms=1500, max_scrolls=30, debug=False, tag=""):
    """Capture /v1/layout/search responses while scrolling; return ordered, de-duped products."""
    captured = {}    # offset -> [product dicts]
    raw_dump = []

    def handler(resp):
        try:
            if "/v1/layout/search" in resp.url:
                data = resp.json()
                prods = parse_api_products(data)
                off = _offset(resp.url)
                if prods and off not in captured:
                    captured[off] = prods
                    if debug:
                        raw_dump.append(off)
        except Exception:
            pass

    page.on("response", handler)
    do_search(page, keyword)
    page.wait_for_timeout(2500)
    prev, stag = -1, 0
    for _ in range(max_scrolls):
        total = sum(len(v) for v in captured.values())
        if target and total >= target:
            break
        if total == prev:
            stag += 1
            if stag >= 3:
                break
        else:
            stag = 0
        prev = total
        try:
            page.evaluate("() => window.scrollTo(0, document.body.scrollHeight)")
        except Exception:
            break
        page.wait_for_timeout(delay_ms)
    try:
        page.remove_listener("response", handler)
    except Exception:
        pass
    if debug:
        print(f"  [debug]{tag} captured API pages at offsets: {sorted(captured.keys())}")
    combined, seen = [], set()
    for off in sorted(captured.keys()):
        for p in captured[off]:
            pid = p.get("product_id", "")
            if pid and pid in seen:
                continue
            if pid:
                seen.add(pid)
            p["position"] = len(combined) + 1
            p["url"] = f"https://blinkit.com/prn/product/prid/{pid}" if pid else ""
            combined.append(p)
    if target:
        combined = combined[:target]
    # status
    if combined:
        status = "ok"
    else:
        body = ""
        try: body = page.inner_text("body")
        except Exception: pass
        status = "not-serviceable" if not_serviceable(body) else "search-failed (no api data)"
    return status, combined


# ---------------------------------------------------------------- output helpers
def rank_xlsx_from_csv(csv_path, xlsx_path):
    with open(csv_path, encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    wb = Workbook(); ws = wb.active; ws.title = "Keyword Ranks"
    for c, h in enumerate(RANK_COLS, 1):
        ws.cell(1, c, h)
    for ri, row in enumerate(rows, 2):
        for c, h in enumerate(RANK_COLS, 1):
            v = row.get(h, "")
            if h in ("Overall Position", "Organic Position", "Sponsored Position", "Price (Rs)",
                     "MRP (Rs)", "Discount (Rs)", "Inventory (stock)", "Max Cart", "Total Results") \
                    and str(v).strip() not in ("", "None"):
                try: v = int(float(v))
                except ValueError: pass
            elif h == "Discount % of MRP" and str(v).strip() not in ("", "None"):
                try: v = float(v)
                except ValueError: pass
            ws.cell(ri, c, v)
    ws.freeze_panes = "A2"
    if rows:
        ws.auto_filter.ref = f"A1:{ws.cell(1, len(RANK_COLS)).column_letter}{len(rows)+1}"
    widths = {"Keyword": 20, "Search Status": 22, "Tracked Product": 32, "Matched Card": 38,
              "Brand Name": 18, "Product URL": 46, "Timestamp": 20}
    for c, h in enumerate(RANK_COLS, 1):
        ws.column_dimensions[ws.cell(1, c).column_letter].width = widths.get(h, 13)
    wb.save(xlsx_path)


def load_snapshots(all_csv):
    """Rebuild {(pincode,keyword): [cards]} + status from a prior allresults.csv (latest run wins)."""
    snaps, stat = {}, {}
    if not os.path.exists(all_csv):
        return snaps, stat
    groups = {}
    with open(all_csv, encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            key = (row.get("Pincode"), row.get("Keyword"))
            groups.setdefault(key, {}).setdefault(row.get("Timestamp", ""), []).append(row)
    for key, byts in groups.items():
        rows = byts[max(byts.keys())]
        cards = []
        for row in rows:
            def _i(k):
                v = str(row.get(k, "")).strip()
                return int(v) if v.lstrip("-").isdigit() else None
            def _f(k):
                v = str(row.get(k, "")).strip()
                try: return float(v)
                except ValueError: return None
            cards.append({"position": _i("Position"), "product_id": row.get("Product ID", ""),
                          "name": row.get("Name", ""), "brand": row.get("Brand Name", ""),
                          "pack": row.get("Pack Size", ""), "price": _i("Price (Rs)"), "mrp": _i("MRP (Rs)"),
                          "discount": _i("Discount (Rs)"), "discount_pct": _f("Discount % of MRP"),
                          "inventory": _i("Inventory (stock)"), "max_cart": _i("Max Cart"),
                          "sold_out": str(row.get("Sold Out", "")).strip().lower() == "true",
                          "is_sponsored": str(row.get("Is Sponsored", "")).strip().lower() == "true",
                          "url": row.get("URL", "")})
        cards.sort(key=lambda c: (c["position"] is None, c["position"] or 0))
        snaps[key] = cards
        stat[key] = rows[0].get("Search Status", "ok") or "ok"
    return snaps, stat


# ---------------------------------------------------------------- run (Option B resume)
def run(products_f, keywords_f, pincodes_f, out_xlsx, rank_csv, all_csv,
        limit, delay, show, debug, topn, threshold):
    products = read_list(products_f); keywords = read_list(keywords_f); pincodes = read_list(pincodes_f)
    if limit: keywords = keywords[:limit]
    print(f"Products: {len(products)} | Keywords: {len(keywords)} | Pincodes: {len(pincodes)} | topn: {topn}")

    for path, cols in ((rank_csv, RANK_COLS), (all_csv, ALL_COLS)):
        if os.path.exists(path):
            with open(path, encoding="utf-8-sig", newline="") as f:
                hdr = next(csv.reader(f), [])
            if hdr and hdr != cols:
                print(f"  [stop] '{path}' has an older column layout. Delete/rename '{rank_csv}' and '{all_csv}', then re-run.")
                return

    done = set()
    if os.path.exists(rank_csv):
        with open(rank_csv, encoding="utf-8-sig") as f:
            for row in csv.DictReader(f):
                done.add((row.get("Pincode"), row.get("Keyword"), row.get("Tracked Product")))
        print(f"  [resume] {len(done)} (pincode,keyword,product) rows already done.")
    snaps, snap_stat = load_snapshots(all_csv)

    rnew, anew = not os.path.exists(rank_csv), not os.path.exists(all_csv)
    frank = open(rank_csv, "a", newline="", encoding="utf-8-sig"); rw = csv.DictWriter(frank, fieldnames=RANK_COLS)
    if rnew: rw.writeheader(); frank.flush()
    fall = open(all_csv, "a", newline="", encoding="utf-8-sig"); aw = csv.DictWriter(fall, fieldnames=ALL_COLS)
    if anew: aw.writeheader(); fall.flush()

    tasks = []
    for pin in pincodes:
        for kw in keywords:
            pending = [p for p in products if (str(pin), str(kw), p) not in done]
            if pending:
                tasks.append((str(pin), str(kw), pending))
    need_search = any((pin, kw) not in snaps for pin, kw, _ in tasks)
    print(f"  {len(tasks)} keyword/pincode searches pending; browser needed: {need_search}")

    def write_rank(pin, kw, status, ts, prod, cards, total):
        best, bs = None, 0.0
        for c in cards:
            s = match_score(prod, c["name"])
            if s > bs: best, bs = c, s
        if best and bs >= threshold and status == "ok":
            org = None if best["is_sponsored"] else best["position"]
            spo = best["position"] if best["is_sponsored"] else None
            rw.writerow({"Keyword": kw, "Pincode": pin, "Search Status": status, "Timestamp": ts,
                         "Brand Name": best.get("brand", ""), "Tracked Product": prod, "Matched": "Y",
                         "Matched Card": best["name"], "Pack Size": best.get("pack", ""),
                         "Inventory (stock)": best.get("inventory"), "Max Cart": best.get("max_cart"),
                         "Overall Position": best["position"],
                         "Organic Position": org, "Sponsored Position": spo, "Is Sponsored": best["is_sponsored"],
                         "Price (Rs)": best.get("price"), "MRP (Rs)": best.get("mrp"),
                         "Discount (Rs)": best.get("discount"), "Discount % of MRP": best.get("discount_pct"),
                         "Sold Out": best.get("sold_out"), "Total Results": total,
                         "Match Score": round(bs, 2), "Product URL": best.get("url", "")})
        else:
            rw.writerow({"Keyword": kw, "Pincode": pin, "Search Status": status, "Timestamp": ts,
                         "Brand Name": "", "Tracked Product": prod, "Matched": "N", "Matched Card": "",
                         "Pack Size": "", "Inventory (stock)": "", "Max Cart": "", "Overall Position": "",
                         "Organic Position": "", "Sponsored Position": "", "Is Sponsored": "", "Price (Rs)": "",
                         "MRP (Rs)": "", "Discount (Rs)": "", "Discount % of MRP": "",
                         "Sold Out": "", "Total Results": total, "Match Score": round(bs, 2), "Product URL": ""})

    pw = browser = page = None
    if need_search:
        pw = sync_playwright().start()
        browser = pw.chromium.launch(headless=not show)
        ctx = browser.new_context(viewport={"width": 1366, "height": 900},
                                  user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                                             "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
        page = ctx.new_page()
    current_pin, serviceable = None, True
    try:
        for pin, kw, pending in tasks:
            ts = datetime.datetime.now().isoformat(timespec="seconds")
            key = (pin, kw)
            if key in snaps:
                cards, status, src = snaps[key], snap_stat.get(key, "ok"), "cached"
            else:
                if current_pin != pin:
                    serviceable = set_location(page, pin); current_pin = pin
                status, cards = collect_products(page, kw, target=topn, delay_ms=int(delay * 1000) or 1500,
                                                 debug=debug, tag=f" [{pin}] '{kw}'")
                if not serviceable:
                    status = "not-serviceable"
                for c in cards:
                    aw.writerow({"Keyword": kw, "Pincode": pin, "Search Status": status, "Timestamp": ts,
                                 "Position": c["position"], "Is Sponsored": c["is_sponsored"],
                                 "Product ID": c["product_id"], "Brand Name": c["brand"], "Name": c["name"],
                                 "Pack Size": c["pack"], "Price (Rs)": c["price"], "MRP (Rs)": c["mrp"],
                                 "Discount (Rs)": c["discount"], "Discount % of MRP": c["discount_pct"],
                                 "Inventory (stock)": c["inventory"], "Max Cart": c["max_cart"],
                                 "Sold Out": c["sold_out"], "URL": c["url"]})
                fall.flush()
                snaps[key], snap_stat[key], src = cards, status, "search"
                time.sleep(delay)
            total = len(cards)
            for prod in pending:
                write_rank(pin, kw, status, ts, prod, cards, total)
                done.add((pin, kw, prod))
            frank.flush()
            print(f"  [{pin}] '{kw}' ({src}): status={status}, {total} products, +{len(pending)} row(s)")
    finally:
        if browser: browser.close()
        if pw: pw.stop()
    frank.close(); fall.close()
    rank_xlsx_from_csv(rank_csv, out_xlsx)
    print(f"\nDone.\n  Ranks: {out_xlsx}\n  Ranks CSV: {rank_csv}\n  All results: {all_csv}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Blinkit keyword rank tracker (API edition).")
    ap.add_argument("--products", required=True); ap.add_argument("--keywords", required=True)
    ap.add_argument("--pincodes", required=True)
    ap.add_argument("--out", default="blinkit_ranks.xlsx")
    ap.add_argument("--rank-csv", default="blinkit_ranks.csv")
    ap.add_argument("--all-csv", default="blinkit_allresults.csv")
    ap.add_argument("--limit", type=int, default=0); ap.add_argument("--delay", type=float, default=1.5)
    ap.add_argument("--topn", type=int, default=50); ap.add_argument("--threshold", type=float, default=0.7)
    ap.add_argument("--show", action="store_true"); ap.add_argument("--debug", action="store_true")
    a = ap.parse_args()
    run(a.products, a.keywords, a.pincodes, a.out, a.rank_csv, a.all_csv,
        a.limit, a.delay, a.show, a.debug, a.topn, a.threshold)