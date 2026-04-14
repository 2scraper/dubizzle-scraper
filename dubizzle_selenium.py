#!/usr/bin/env python3
"""
Dubizzle Scraper — Selenium Implementation
============================================
Repository : https://github.com/2scraper/dubizzle-scraper
License    : MIT
"""

import logging
import random
import sys
import time
from typing import Optional
from urllib.parse import urlparse

try:
    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options
    from selenium.webdriver.common.by import By
    from selenium.common.exceptions import NoSuchElementException, WebDriverException
except ImportError:
    sys.exit("selenium is required.  Install: pip install selenium")

from dubizzle_core import (
    BASE_URL, CATEGORIES, ALL_CATEGORY_NAMES,
    USER_AGENTS, VIEWPORTS, FINGERPRINT_JS, NEXT_DATA_JS, DOM_EXTRACT_JS, PAGINATION_JS,
    Listing, CaptchaSolver,
    extract_from_next_data, parse_dom_cards,
    export_json, export_csv, get_env, build_proxy_url,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("dubizzle-se")


def create_driver(headless=True, proxy_url=None):
    opts = Options()
    ua = random.choice(USER_AGENTS)
    vp = random.choice(VIEWPORTS)
    if headless:
        opts.add_argument("--headless=new")
    opts.add_argument(f"--user-agent={ua}")
    opts.add_argument(f"--window-size={vp['width']},{vp['height']}")
    opts.add_argument("--disable-blink-features=AutomationControlled")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_experimental_option("excludeSwitches", ["enable-automation"])
    opts.add_experimental_option("useAutomationExtension", False)
    if proxy_url:
        p = urlparse(proxy_url)
        opts.add_argument(f"--proxy-server={p.scheme}://{p.hostname}:{p.port}")
    driver = webdriver.Chrome(options=opts)
    driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {"source": FINGERPRINT_JS})
    return driver


def detect_and_solve_captcha(driver, solver, retries=2):
    if not solver:
        return True
    for _ in range(retries):
        try:
            driver.find_element(By.CSS_SELECTOR, "iframe[src*='challenges.cloudflare.com'],[class*='cf-turnstile']")
            sk = driver.execute_script("const e=document.querySelector('[class*=\"cf-turnstile\"]')||document.querySelector('#cf-turnstile');return e?e.getAttribute('data-sitekey'):null;")
            if sk:
                t = solver.solve_turnstile(sk, driver.current_url)
                if t:
                    driver.execute_script(f"document.querySelector('[name=cf-turnstile-response]').value='{t}';")
                    time.sleep(2)
                    return True
        except NoSuchElementException:
            pass
        try:
            driver.find_element(By.CSS_SELECTOR, "iframe[src*='google.com/recaptcha'],.g-recaptcha")
            sk = driver.execute_script("const e=document.querySelector('.g-recaptcha');return e?e.getAttribute('data-sitekey'):null;")
            if sk:
                t = solver.solve_recaptcha_v2(sk, driver.current_url)
                if t:
                    driver.execute_script(f"document.getElementById('g-recaptcha-response').innerHTML='{t}';")
                    time.sleep(2)
                    return True
        except NoSuchElementException:
            pass
        try:
            driver.find_element(By.CSS_SELECTOR, "iframe[src*='hcaptcha.com'],.h-captcha")
            sk = driver.execute_script("const e=document.querySelector('.h-captcha');return e?e.getAttribute('data-sitekey'):null;")
            if sk:
                t = solver.solve_hcaptcha(sk, driver.current_url)
                if t:
                    driver.execute_script(f"document.querySelector('[name=h-captcha-response]').value='{t}';")
                    time.sleep(2)
                    return True
        except NoSuchElementException:
            pass
        return True  # no captcha
    return False


def scrape_subcategory(driver, subcat_path, cat, solver, max_pages=5, scrape_details=False):
    listings = []
    for page_idx in range(0, max_pages):
        url = f"{BASE_URL}{subcat_path}"
        if page_idx > 0:
            url += f"{'&' if '?' in subcat_path else '?'}page={page_idx}"
        log.info("[%s] page %d/%d → %s", cat, page_idx + 1, max_pages, url)
        try:
            driver.get(url)
            time.sleep(random.uniform(3, 5))
            detect_and_solve_captcha(driver, solver)

            nd = driver.execute_script(NEXT_DATA_JS)
            page_lst = []
            pagination = None
            if nd:
                page_lst, pagination = extract_from_next_data(nd, cat, subcat_path)
                if page_lst:
                    log.info("  → %d from __NEXT_DATA__", len(page_lst))
            if not page_lst:
                dom_data = driver.execute_script(DOM_EXTRACT_JS)
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
                has_next = driver.execute_script(PAGINATION_JS)
                if not has_next:
                    break
        except WebDriverException as e:
            log.warning("  → Error: %s", e)
            break

    if scrape_details and listings:
        log.info("Enriching %d details …", len(listings))
        for lst in listings:
            _detail(driver, lst, solver)
            time.sleep(random.uniform(1, 2))
    return listings


def _detail(driver, listing, solver):
    if not listing.url:
        return
    try:
        driver.get(listing.url)
        time.sleep(random.uniform(1, 3))
        detect_and_solve_captcha(driver, solver)
        nd = driver.execute_script(NEXT_DATA_JS)
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


def scrape_category(driver, cat, solver, max_pages=5, scrape_details=False):
    subcats = CATEGORIES.get(cat, [])
    if not subcats:
        log.warning("Unknown: %s", cat)
        return []
    r = []
    for sp in subcats:
        r.extend(scrape_subcategory(driver, sp, cat, solver, max_pages, scrape_details))
    return r


def main():
    import argparse
    p = argparse.ArgumentParser(description="Dubizzle Scraper — Selenium")
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
    driver = create_driver(args.headless, proxy_url)
    all_l = []
    try:
        for c in args.categories:
            all_l.extend(scrape_category(driver, c, solver, args.max_pages, args.details))
            log.info("Grand total: %d", len(all_l))
    finally:
        driver.quit()

    if not all_l:
        log.warning("No listings.")
        return
    if args.format in ("json", "both"):
        export_json(all_l, f"{args.output}.json")
    if args.format in ("csv", "both"):
        export_csv(all_l, f"{args.output}.csv")
    log.info("Done! %d total.", len(all_l))


if __name__ == "__main__":
    main()
