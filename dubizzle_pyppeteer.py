#!/usr/bin/env python3
"""
Dubizzle Scraper — Pyppeteer (Puppeteer) Implementation
=========================================================
Repository : https://github.com/2scraper/dubizzle-scraper
License    : MIT
"""

import asyncio
import logging
import random
import sys
from typing import Optional
from urllib.parse import urlparse

try:
    from pyppeteer import launch
except ImportError:
    sys.exit("pyppeteer is required.  Install: pip install pyppeteer")

from dubizzle_core import (
    BASE_URL, CATEGORIES, ALL_CATEGORY_NAMES,
    USER_AGENTS, VIEWPORTS, FINGERPRINT_JS, NEXT_DATA_JS, DOM_EXTRACT_JS, PAGINATION_JS,
    Listing, CaptchaSolver,
    extract_from_next_data, parse_dom_cards,
    export_json, export_csv, get_env, build_proxy_url,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("dubizzle-pp")

# Pyppeteer needs the fingerprint script wrapped in an IIFE
FINGERPRINT_IIFE = f"() => {{ {FINGERPRINT_JS} }}"


async def detect_and_solve_captcha(page, solver, retries=2):
    if not solver:
        return True
    for _ in range(retries):
        ts = await page.querySelector("iframe[src*='challenges.cloudflare.com'],[class*='cf-turnstile']")
        if ts:
            sk = await page.evaluate("()=>{const e=document.querySelector('[class*=\"cf-turnstile\"]')||document.querySelector('#cf-turnstile');return e?e.getAttribute('data-sitekey'):null;}")
            if sk:
                t = solver.solve_turnstile(sk, page.url)
                if t:
                    await page.evaluate(f"document.querySelector('[name=cf-turnstile-response]').value='{t}';")
                    await asyncio.sleep(2)
                    return True
        rc = await page.querySelector("iframe[src*='google.com/recaptcha'],.g-recaptcha")
        if rc:
            sk = await page.evaluate("()=>{const e=document.querySelector('.g-recaptcha');return e?e.getAttribute('data-sitekey'):null;}")
            if sk:
                t = solver.solve_recaptcha_v2(sk, page.url)
                if t:
                    await page.evaluate(f"document.getElementById('g-recaptcha-response').innerHTML='{t}';")
                    await asyncio.sleep(2)
                    return True
        hc = await page.querySelector("iframe[src*='hcaptcha.com'],.h-captcha")
        if hc:
            sk = await page.evaluate("()=>{const e=document.querySelector('.h-captcha');return e?e.getAttribute('data-sitekey'):null;}")
            if sk:
                t = solver.solve_hcaptcha(sk, page.url)
                if t:
                    await page.evaluate(f"document.querySelector('[name=h-captcha-response]').value='{t}';")
                    await asyncio.sleep(2)
                    return True
        if not ts and not rc and not hc:
            return True
        await asyncio.sleep(3)
    return False


async def scrape_subcategory(browser, subcat_path, cat, solver, max_pages=5, scrape_details=False, viewport=None):
    listings = []
    page = await browser.newPage()
    if viewport:
        await page.setViewport(viewport)
    await page.evaluateOnNewDocument(FINGERPRINT_IIFE)

    for page_idx in range(0, max_pages):
        url = f"{BASE_URL}{subcat_path}"
        if page_idx > 0:
            url += f"{'&' if '?' in subcat_path else '?'}page={page_idx}"
        log.info("[%s] page %d/%d → %s", cat, page_idx + 1, max_pages, url)

        try:
            resp = await page.goto(url, {"waitUntil": "domcontentloaded", "timeout": 45000})
            if resp and resp.status == 404:
                log.info("  → 404 — skipping.")
                break

            await asyncio.sleep(random.uniform(3, 5))
            await detect_and_solve_captcha(page, solver)

            nd = await page.evaluate(NEXT_DATA_JS)
            page_lst = []
            pagination = None
            if nd:
                page_lst, pagination = extract_from_next_data(nd, cat, subcat_path)
                if page_lst:
                    log.info("  → %d from __NEXT_DATA__", len(page_lst))
            if not page_lst:
                dom_data = await page.evaluate(DOM_EXTRACT_JS)
                page_lst = parse_dom_cards(dom_data, cat, subcat_path)
                if page_lst:
                    log.info("  → %d from DOM", len(page_lst))
            if not page_lst:
                log.info("  → No listings — stopping.")
                break
            listings.extend(page_lst)
            log.info("  → Total: %d", len(listings))

            if pagination:
                current = pagination.get("page", 0)
                total = pagination.get("totalPages", 0)
                if current >= total - 1:
                    log.info("  → Last page reached (%d/%d).", current + 1, total)
                    break
            else:
                has_next = await page.evaluate(PAGINATION_JS)
                if not has_next:
                    break
        except Exception as e:
            log.warning("  → Error: %s", e)
            break

    if scrape_details and listings:
        log.info("Enriching %d details …", len(listings))
        dp = await browser.newPage()
        if viewport:
            await dp.setViewport(viewport)
        await dp.evaluateOnNewDocument(FINGERPRINT_IIFE)
        for lst in listings:
            await _detail(dp, lst, solver)
            await asyncio.sleep(random.uniform(1, 2))
        await dp.close()

    await page.close()
    return listings


async def _detail(page, listing, solver):
    if not listing.url:
        return
    try:
        await page.goto(listing.url, {"waitUntil": "domcontentloaded", "timeout": 30000})
        await asyncio.sleep(random.uniform(1, 3))
        await detect_and_solve_captcha(page, solver)
        nd = await page.evaluate(NEXT_DATA_JS)
        if nd:
            try:
                for act in nd["props"]["pageProps"]["reduxWrapperActionsGIPP"]:
                    p = act.get("payload", {})
                    if isinstance(p, dict) and p.get("description") and not listing.description:
                        d = p["description"]
                        listing.description = d.get("en", d) if isinstance(d, dict) else str(d)
            except (KeyError, TypeError):
                pass
    except Exception as e:
        log.debug("Detail error: %s", e)


async def scrape_category(browser, cat, solver, max_pages=5, scrape_details=False, viewport=None):
    subcats = CATEGORIES.get(cat, [])
    if not subcats:
        log.warning("Unknown: %s", cat)
        return []
    r = []
    for sp in subcats:
        r.extend(await scrape_subcategory(browser, sp, cat, solver, max_pages, scrape_details, viewport))
    return r


async def main():
    import argparse
    p = argparse.ArgumentParser(description="Dubizzle Scraper — Pyppeteer")
    p.add_argument("--categories", nargs="*", default=ALL_CATEGORY_NAMES)
    p.add_argument("--max-pages", type=int, default=3)
    p.add_argument("--details", action="store_true")
    p.add_argument("--output", default="dubizzle_listings")
    p.add_argument("--format", choices=["json", "csv", "both"], default="both")
    p.add_argument("--headless", action="store_true", default=True)
    p.add_argument("--no-headless", dest="headless", action="store_false")
    p.add_argument("--captcha-key", default=get_env("CAPTCHA_API_KEY"))
    args = p.parse_args()

    solver = CaptchaSolver(args.captcha_key) if args.captcha_key else None
    if not solver:
        log.warning("No 2captcha key.  https://2captcha.com")

    proxy_url = build_proxy_url()
    launch_opts = {"headless": args.headless, "args": ["--disable-blink-features=AutomationControlled",
                    "--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage"], "ignoreHTTPSErrors": True}
    if proxy_url:
        pr = urlparse(proxy_url)
        launch_opts["args"].append(f"--proxy-server={pr.scheme}://{pr.hostname}:{pr.port}")

    viewport = random.choice(VIEWPORTS)
    browser = await launch(**launch_opts)
    all_l = []
    try:
        for c in args.categories:
            all_l.extend(await scrape_category(browser, c, solver, args.max_pages, args.details, viewport))
            log.info("Grand total: %d", len(all_l))
    finally:
        await browser.close()

    if not all_l:
        log.warning("No listings.")
        return
    if args.format in ("json", "both"):
        export_json(all_l, f"{args.output}.json")
    if args.format in ("csv", "both"):
        export_csv(all_l, f"{args.output}.csv")
    log.info("Done! %d total.", len(all_l))


if __name__ == "__main__":
    asyncio.run(main())
