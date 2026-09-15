"""End-to-end check of the dashboard + live capture API.

    python app/e2e_test.py http://127.0.0.1:8000/ "green tea" 110001 24

Opens the page, waits for the API chip to go online, runs a capture through the
form, waits for the charts to switch to the new keyword, then checks every panel
rendered without NaN/undefined and saves screenshots.
"""
import sys, time
from playwright.sync_api import sync_playwright

url, kw, pin, top = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
out = "app/"
errors = []
with sync_playwright() as pw:
    b = pw.chromium.launch()
    p = b.new_page(viewport={"width": 1440, "height": 900})
    p.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
    p.on("pageerror", lambda e: errors.append(str(e)))
    p.goto(url)
    p.wait_for_function("document.querySelector('#apiText').textContent.includes('online')", timeout=30000)
    print("api chip:", p.inner_text("#apiText"))
    p.screenshot(path=out + "shot_hero.png")
    p.fill("#inKw", kw); p.fill("#inPin", pin); p.select_option("#inTop", top)
    t0 = time.time()
    p.click("#runBtn")
    p.wait_for_function("/charts updated|error:/.test(document.querySelector('#log').textContent)", timeout=240000)
    assert p.input_value("#fKw").lower() == kw.lower() and p.input_value("#fPin") == pin, "charts did not switch to the capture"
    print(f"capture loaded in {time.time() - t0:.1f}s")
    p.wait_for_timeout(800)
    print("log tail:", p.inner_text("#log").strip().splitlines()[-3:])
    print("kpis:", p.inner_text("#kpis").replace("\n", " | "))
    brand = p.eval_on_selector_all("#brandList option", "o => o.map(x => x.value)")[:1]
    if brand:
        p.fill("#fBrand", brand[0]); p.wait_for_timeout(600)
        print("highlight", brand[0], "->", p.inner_text("#skuMeta"))
    body = p.inner_text("body")
    bad = [w for w in ("NaN", "undefined", "[object Object]") if w in body]
    print("bad tokens:", bad or "none")
    print("slot rows:", p.eval_on_selector_all("#slots .srow", "r => r.length"), "cells:", p.eval_on_selector_all("#slots .cell.org, #slots .cell.ad", "c => c.length"))
    p.screenshot(path=out + "shot_full.png", full_page=True)
    b.close()
print("console errors:", errors or "none")
