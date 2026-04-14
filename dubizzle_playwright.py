#!/usr/bin/env python3
"""
Dubizzle Scraper — Playwright Implementation (Primary)
=======================================================
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
    from playwright.async_api import async_playwright, Page, BrowserContext
except ImportError:
    sys.exit("playwright is required.  Install: pip install playwright && playwright install")

from dubizzle_core import (
    BASE_URL, CATEGORIES, ALL_CATEGORY_NAMES,
    USER_AGENTS, VIEWPORTS, FINGERPRINT_JS, NEXT_DATA_JS, DOM_EXTRACT_JS, PAGINATION_JS,
    Listing, CaptchaSolver,
    extract_from_next_data, parse_dom_cards,
    export_json, export_csv, get_env, build_proxy_url,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("dubizzle-pw")


# ---------------------------------------------------------------------------
# Fingerprint
# ---------------------------------------------------------------------------

async def apply_fingerprint(ctx: BrowserContext):
    await ctx.add_init_script(FINGERPRINT_JS)


# ---------------------------------------------------------------------------
# CAPTCHA detection
# ---------------------------------------------------------------------------

async def detect_and_solve_captcha(page: Page, solver: Optional[CaptchaSolver], retries=2) -> bool:
    if not solver:
        return True
    for _ in range(retries):
        ts = await page.query_selector("iframe[src*='challenges.cloudflare.com'],[class*='cf-turnstile'],#cf-turnstile")
        if ts:
            sk = await page.evaluate("""() => {
                const e = document.querySelector('[class*="cf-turnstile"]') || document.querySelector('#cf-turnstile');
                return e ? e.getAttribute('data-sitekey') : null;
            }""")
            if sk:
                t = solver.solve_turnstile(sk, page.url)
                if t:
                    await page.evaluate(f"""() => {{
                        const el = document.querySelector('[name=cf-turnstile-response]');
                        if (el) el.value = '{t}';
                    }}""")
                    await asyncio.sleep(2)
                    return True

        rc = await page.query_selector("iframe[src*='google.com/recaptcha'],.g-recaptcha")
        if rc:
            sk = await page.evaluate("() => { const e = document.querySelector('.g-recaptcha'); return e ? e.getAttribute('data-sitekey') : null; }")
            if sk:
                t = solver.solve_recaptcha_v2(sk, page.url)
                if t:
                    await page.evaluate(f"document.getElementById('g-recaptcha-response').innerHTML = '{t}';")
                    await asyncio.sleep(2)
                    return True

        hc = await page.query_selector("iframe[src*='hcaptcha.com'],.h-captcha")
        if hc:
            sk = await page.evaluate("() => { const e = document.querySelector('.h-captcha'); return e ? e.getAttribute('data-sitekey') : null; }")
            if sk:
                t = solver.solve_hcaptcha(sk, page.url)
                if t:
                    await page.evaluate(f"document.querySelector('[name=h-captcha-response]').value = '{t}';")
                    await asyncio.sleep(2)
                    return True

        if not ts and not rc and not hc:
            return True
        await asyncio.sleep(3)
    return False


# ---------------------------------------------------------------------------
# Scraping
# ---------------------------------------------------------------------------

async def scrape_subcategory(ctx, subcat_path, cat, solver, max_pages=5, scrape_details=False):
    listings = []
    page = await ctx.new_page()

    # dubizzle uses 0-indexed pages: page 0 = default URL, page 1 = ?page=1
    for page_idx in range(0, max_pages):
        url = f"{BASE_URL}{subcat_path}"
        if page_idx > 0:
            sep = "&" if "?" in subcat_path else "?"
            url += f"{sep}page={page_idx}"

        log.info("[%s] page %d/%d → %s", cat, page_idx + 1, max_pages, url)

        try:
            resp = await page.goto(url, wait_until="domcontentloaded", timeout=45_000)
            if resp and resp.status == 404:
                log.info("  → 404 — skipping.")
                break

            await asyncio.sleep(random.uniform(3, 5))
            await detect_and_solve_captcha(page, solver)

            # Strategy 1: __NEXT_DATA__
            nd = await page.evaluate(NEXT_DATA_JS)
            page_lst = []
            pagination = None
            if nd:
                page_lst, pagination = extract_from_next_data(nd, cat, subcat_path)
                if page_lst:
                    log.info("  → %d listings from __NEXT_DATA__", len(page_lst))

            # Strategy 2: DOM fallback
            if not page_lst:
                dom_data = await page.evaluate(DOM_EXTRACT_JS)
                page_lst = parse_dom_cards(dom_data, cat, subcat_path)
                if page_lst:
                    log.info("  → %d listings from DOM", len(page_lst))

            if not page_lst:
                log.info("  → No listings — stopping.")
                break

            listings.extend(page_lst)
            log.info("  → Running total: %d", len(listings))

            # Check if more pages exist using __NEXT_DATA__ pagination
            if pagination:
                current = pagination.get("page", 0)
                total = pagination.get("totalPages", 0)
                if current >= total - 1:
                    log.info("  → Last page reached (%d/%d).", current + 1, total)
                    break
            else:
                # Fallback: try DOM pagination button
                has_next = await page.evaluate(PAGINATION_JS)
                if not has_next:
                    break

        except Exception as exc:
            log.warning("  → Error: %s", exc)
            break

    if scrape_details and listings:
        log.info("Enriching %d detail pages …", len(listings))
        dp = await ctx.new_page()
        for lst in listings:
            await _scrape_detail(dp, lst, solver)
            await asyncio.sleep(random.uniform(1, 2))
        await dp.close()

    await page.close()
    return listings


async def _scrape_detail(page: Page, listing: Listing, solver):
    if not listing.url:
        return
    try:
        await page.goto(listing.url, wait_until="domcontentloaded", timeout=30_000)
        await asyncio.sleep(random.uniform(1, 3))
        await detect_and_solve_captcha(page, solver)

        nd = await page.evaluate(NEXT_DATA_JS)
        if nd:
            try:
                for act in nd["props"]["pageProps"]["reduxWrapperActionsGIPP"]:
                    p = act.get("payload", {})
                    if isinstance(p, dict) and p.get("description") and not listing.description:
                        desc = p["description"]
                        listing.description = desc.get("en", desc) if isinstance(desc, dict) else str(desc)
            except (KeyError, TypeError):
                pass
    except Exception as exc:
        log.debug("Detail error (%s): %s", listing.url, exc)


async def scrape_category(ctx, cat, solver, max_pages=5, scrape_details=False):
    subcats = CATEGORIES.get(cat, [])
    if not subcats:
        log.warning("Unknown category: %s", cat)
        return []
    result = []
    for sp in subcats:
        result.extend(await scrape_subcategory(ctx, sp, cat, solver, max_pages, scrape_details))
    return result


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def main():
    import argparse
    p = argparse.ArgumentParser(description="Dubizzle Scraper — Playwright")
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
        log.warning("No 2captcha API key.  Set CAPTCHA_API_KEY or --captcha-key.  https://2captcha.com")

    proxy_url = build_proxy_url()
    proxy_cfg = None
    if proxy_url:
        parsed = urlparse(proxy_url)
        proxy_cfg = {"server": f"{parsed.scheme}://{parsed.hostname}:{parsed.port}"}
        if parsed.username:
            proxy_cfg["username"] = parsed.username
            proxy_cfg["password"] = parsed.password or ""

    all_listings = []
    async with async_playwright() as pw:
        opts = {"headless": args.headless, "args": ["--disable-blink-features=AutomationControlled", "--no-sandbox"]}
        if proxy_cfg:
            opts["proxy"] = proxy_cfg
        browser = await pw.chromium.launch(**opts)
        ctx = await browser.new_context(
            viewport=random.choice(VIEWPORTS),
            user_agent=random.choice(USER_AGENTS),
            locale="en-US",
            timezone_id="Asia/Dubai",
        )
        await apply_fingerprint(ctx)

        for cat in args.categories:
            all_listings.extend(await scrape_category(ctx, cat, solver, args.max_pages, args.details))
            log.info("Grand total: %d listings", len(all_listings))

        await ctx.close()
        await browser.close()

    if not all_listings:
        log.warning("No listings scraped.")
        return
    if args.format in ("json", "both"):
        export_json(all_listings, f"{args.output}.json")
    if args.format in ("csv", "both"):
        export_csv(all_listings, f"{args.output}.csv")
    log.info("Done! %d total listings.", len(all_listings))


if __name__ == "__main__":
    asyncio.run(main())
