#!/usr/bin/env python3
"""smoke_test.py - the offline suite for dubizzle-scraper.

One file, plain functions; `tests/test_smoke.py` wraps it as a single pytest
test so `pytest` works as an entry point without a second copy of the checks.

It must pass with NO engine library installed at all, so every engine import
is guarded and the skip is reported at the end - a suite that silently skips
part of itself and still says "all passed" is the same defect as code that
reports success without checking what it wanted actually happened.

THE FIXTURES ARE IN `fixtures_generated.json`, NOT INLINE
--------------------------------------------------------
The family's rule is fixtures inline, and on most of these sites that works
because a fixture is a few hundred bytes of markup. Here the fixture IS the
SSR payload: one ad's entry in `__NEXT_DATA__` is 2-6 KB of JSON on its own,
and a fixture has to carry two of them plus the page's JSON-LD, its
pagination block and its tiles, or it stops exercising the join the parser is
built on. `make_fixtures.py` cuts them from real captures and verifies that
each trim parses IDENTICALLY to the untrimmed original - every column, not a
sample - before writing anything.

Personal and credential-shaped material is replaced with obvious placeholders
and guarded by PATTERNS rather than by the literals one capture happened to
contain, so the next capture is caught too. This site's pages carry four
kinds: a real estate agent's own name (34-35 per property page), a per-seller
UUID (25-26 per motors page), the page's Algolia search key, and its Sentry
instrumentation with a public key and a release SHA.
"""
import ast
import builtins
import csv
import inspect
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import textwrap
import threading
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import asdict as dataclasses_asdict, fields

import captcha_solver
from captcha_solver import (CaptchaChallenge, detect_recaptcha_v3,
                            reconcile_detections, _v2_task_for, _redact)
from diff_runs import diff_products
import env_config

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from output_writer import (Product, save, finish_run, write_csv,
                           dedupe_by_key, dedupe_by_sku, run_meta,
                           ROW_CLASS_BY_MODE, UNIQUE_BY_SKU_MODES,
                           EXIT_BLOCKED, EXIT_NO_PRODUCTS, EXIT_PARTIAL,
                           EXIT_API_ERROR, COMPLETE_STOP_REASONS,
                           LIST_CSV_SEPARATOR)
from bs4 import BeautifulSoup

import product_parser
from product_parser import (parse_products, page_url,
                            paginates_by_url, category_from_url, listing_kind,
                            site_host, is_supported_host, host_currency,
                            locale_of, LOCALES, HOSTS, CURRENCY, PAGE_CAP,
                            detect_page_state, detect_bot_challenge,
                            detect_block_marker, served_by_dubizzle,
                            is_no_results, page_number_from_url, total_results,
                            total_pages, pages_beyond_cap, search_header,
                            sku_from_url, unsupported_reason, strip_tracking,
                            prices_in, price_in, next_data,
                            listings_payload, vertical_of, SELECTORS,
                            BLOCK_MARKERS, BOT_CHALLENGE_MARKERS)
import page_flow
from proxy_pool import (ProxyPool, mask, to_playwright, split_credentials,
                        parse_proxy_line)

REPO_ROOT = os.path.dirname(os.path.abspath(__file__))

_failures = []


def check(label, condition):
    """Print and record one check. Returns the condition, so callers can
    accumulate with `ok &= check(...)`."""
    if condition:
        print("  PASS  %s" % label)
    else:
        print("  FAIL  %s" % label)
        _failures.append(label)
    return bool(condition)


def group(title):
    print("\n== %s" % title)


def _raises(fn):
    """True if `fn()` raises. Used where refusing is the correct behaviour."""
    try:
        fn()
    except Exception:
        return True
    return False


BANNED_PHRASES = (
    "cloud browser",
    "antidetect browser",
    "anti-detect browser",
    "2scraper Antidetect Browser",
    "gate.2prx.com",
    "2prx.com",
)

# Flags that were removed and must stay removed. Scoped to the ENGINES:
# `--country` is banned on a scraper here -- the locale is a path segment, so
# a flag could only disagree with the URL -- and legitimate on
# fingerprint_client.py, where it picks a fingerprint locale.
REMOVED_ENGINE_FLAGS = ("--antidetect", "--country")
ENGINE_FILES = ("playwright_scraper.py", "puppeteer_scraper.py",
                "selenium_scraper.py")

# A fingerprint as the 2Captcha API really returns one, with this site's
# region. Kept as a fixture because three of the six defects §16 lists were
# one wrong key each in exactly this structure -- `userAgent.value` where the
# API says `userAgent.userAgent`, a locale built as `en-{country}`, a
# timezone never applied at all.
FIX_FINGERPRINT = {
    "id": 1000000,
    "country": "NL",
    "userAgent": {
        "userAgent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) "
                      "Chrome/146.0.0.0 Safari/537.36"),
        "platform": "Windows",
        "mobile": False,
    },
    "intl": {
        "contentLocale": "nl-NL",
        "languages": ["nl-NL", "nl", "en-US", "en"],
        "timeZone": "Europe/Amsterdam",
    },
    "screen": {"width": 1920, "height": 1080,
               "outerWidth": 1920, "outerHeight": 992,
               "deviceScaleFactor": 1},
}


# The exact tags the 2Captcha Scraping Browser's auto-solve extension injects
# into every page it loads, copied from a CDP capture of a page the site
# plainly served. Kept verbatim because the question they answer is whether
# our own marker set mistakes them for the site's challenge.
EXTENSION_TAGS = (
    '<script src="chrome-extension://kjmkgkdkpedkejedfhmfcenooemhbpbo/'
    'content/captcha/recaptcha/hunter.js"></script>'
    '<script src="chrome-extension://kjmkgkdkpedkejedfhmfcenooemhbpbo/'
    'content/captcha/turnstile/hunter.js" '
    'data-ts-input="cf-turnstile-response"></script>'
)

# ---------------------------------------------------------------------------
# FIXTURES — cut from real captures by make_fixtures.py, which verifies that
# each trim parses IDENTICALLY to the untrimmed original, column for column,
# and scrubs the agent names, per-seller UUIDs and search key out first.
#
# Loaded from `fixtures_generated.json` rather than pasted inline, and the
# reason is this site rather than a preference: the fixture IS the SSR
# payload, one ad's entry is 2-6 KB of JSON on its own, and a fixture that
# carried fewer than two ads plus the page's JSON-LD would stop exercising
# the join the parser is built on. See make_fixtures.py's docstring.
# ---------------------------------------------------------------------------
FIXTURES_PATH = os.path.join(REPO_ROOT, "fixtures_generated.json")
with open(FIXTURES_PATH, encoding="utf-8") as _f:
    FIX = json.load(_f)


def fx(name):
    """One fixture's (html, url, status)."""
    f = FIX[name]
    return f["html"], f["url"], f["status"]


def fx_rows(name):
    html, url, _ = fx(name)
    return parse_products(html, url, page=page_number_from_url(url))


# The exact tags the 2Captcha Scraping Browser's auto-solve extension injects
# into every page it loads, copied from a CDP capture of a page the site
# plainly served. Kept verbatim because the question they answer is whether
# our own marker set mistakes them for the site's challenge — and unlike two
# sibling repos, this marker set CAN match one, so the guard is not dead code.
EXTENSION_TAGS = (
    '<script src="chrome-extension://kjmkgkdkpedkejedfhmfcenooemhbpbo/'
    'content/captcha/recaptcha/hunter.js"></script>'
    '<script src="chrome-extension://kjmkgkdkpedkejedfhmfcenooemhbpbo/'
    'content/captcha/turnstile/hunter.js" '
    'data-ts-input="cf-turnstile-response"></script>'
)

LISTING_URL = "https://uae.dubizzle.com/motors/used-cars/"
PROPERTY_URL = "https://uae.dubizzle.com/property-for-rent/residential/apartmentflat/"
# The `---{ad id}` suffix is kept on ONE line on purpose: the repo's own
# secret check subtracts that exact shape before looking for a 32-hex string,
# and splitting it across a line continuation puts a bare hex run in the
# source that the check then flags on its own repository.
_AD_TAIL = "2683-pm-0-downpayment-bmw-x4-m-kitbmw-serv-2-348---c62da0127c944016911ca4406c7832c4"
AD_URL = ("https://dubai.dubizzle.com/motors/used-cars/bmw/x4/2026/02/05/"
          + _AD_TAIL + "/")
HUB_URL = "https://uae.dubizzle.com/"


# ---------------------------------------------------------------------------
# Money
# ---------------------------------------------------------------------------
def test_price_parsing():
    group("prices: one currency, and the traps around reading it off a tile")
    ok = True
    # The DOM path is the only one that reads a price out of text — the
    # payload states integers — so these are the cases that path must get
    # right. Every grouping convention is pinned, not sampled, because a
    # parser that handles two of the three is wrong by a factor of 1000 on
    # the third.
    cases = [
        ("AED 389,000", 389000.0, "the form this site actually prints"),
        ("AED 1,234.56", 1234.56, "dot decimal, comma grouping"),
        ("AED 1.234,56", 1234.56, "comma decimal, dot grouping"),
        ("AED 1 234 567", 1234567.0, "plain-space grouping"),
        ("AED 1 234 567", 1234567.0, "NBSP grouping, as a page renders it"),
        ("AED 1 234 567", 1234567.0, "narrow NBSP grouping"),
        ("AED 1 234 567", 1234567.0, "thin-space grouping"),
        ("389,000 AED", 389000.0, "the ISO code as a suffix"),
        ("AED 1,234", 1234.0, "three trailing digits is a grouping, not cents"),
        ("13500 AED", 13500.0, "no grouping at all"),
    ]
    for text, want, why in cases:
        ok &= check("%-28r -> %-12s (%s)" % (text, want, why),
                    price_in(text) == want)

    # §4's percentage trap, in both word orders. Rejecting the match
    # afterwards is not enough — a rejected match has already consumed the
    # currency code beside it, so the percentage is stripped BEFORE matching.
    ok &= check("a -16% badge beside a price does not become the price",
                price_in("-16% AED 25.999") == 25999.0)
    ok &= check("a -%10,34 badge (percent first) does not become the price",
                price_in("-%10,34 AED 25.999") == 25999.0)

    # §4's ISO allowlist: a bare three-letter token is not a currency.
    ok &= check("a size chart is not a price", price_in("XXL 100") is None)
    ok &= check("a bare number is not a price", price_in("389000") is None)
    ok &= check("a year is not a price", price_in("2024") is None)

    # Space grouping requires FULL three-digit groups, or a spec list beside
    # a price merges into one number.
    ok &= check("a spec list does not merge into one number",
                price_in("AED 5 yrs 6 yrs 200") != 5620000.0)

    ok &= check("two prices in one string come back in order",
                prices_in("AED 349,000 AED 389,000") == [349000.0, 389000.0])
    return ok


# ---------------------------------------------------------------------------
# Rows: VALUES on real fixtures, not coverage (§10)
# ---------------------------------------------------------------------------
def test_listing_values():
    group("listing rows: pinned VALUES on every vertical, not coverage")
    ok = True

    # make_fixtures.py pinned every column of every kept ad against the
    # untrimmed capture. Re-asserting all of it here is what stops a parser
    # change from being "still 100% populated" and entirely wrong — the
    # defect §10 names, where a review_count of 445279961 passed a coverage
    # check on every row of every run.
    for name in ("motors_p1", "motors_p2", "motors_ar_p1", "property_rent",
                 "property_sale", "classified", "jobs", "community"):
        rows = {r.sku: r for r in fx_rows(name)}
        want = FIX[name]["expect"]
        ok &= check("%s: %d row(s), the same skus as when pinned"
                    % (name, len(want)), set(rows) == set(want))
        mismatches = []
        for sku, fields_ in want.items():
            got = rows.get(sku)
            if got is None:
                continue
            for field_name, value in fields_.items():
                if getattr(got, field_name) != value:
                    mismatches.append("%s.%s: %r != %r"
                                      % (sku[-18:], field_name,
                                         getattr(got, field_name), value))
        ok &= check("%s: every column of every row matches its pinned value%s"
                    % (name, "" if not mismatches else " -- " + "; ".join(mismatches[:3])),
                    not mismatches)

    # One row read out loud, so a reader can see what a row IS without
    # running anything.
    motors = fx_rows("motors_p1")
    first = motors[0]
    ok &= check("a motors row names its make from the page's own JSON-LD",
                first.brand is not None and first.brand[0].isupper())
    ok &= check("a motors row carries AED and a number, or neither",
                (first.price is None) == (first.currency is None))
    ok &= check("a motors row's sku is its URL path",
                first.sku and first.sku.startswith("/motors/used-cars/"))
    ok &= check("a motors row's url is on an emirate subdomain, not the "
                "browse host",
                ".dubizzle.com" in first.url and "uae.dubizzle.com" not in first.url)
    ok &= check("a motors row carries the site's numeric id as well as the path",
                isinstance(first.listing_id, int) and first.listing_id > 0)
    ok &= check("posted_at and bumped_at are ISO-8601 UTC, not epochs",
                first.posted_at and first.posted_at.endswith("+00:00"))
    ok &= check("attributes is a mapping of the site's own slugs",
                isinstance(first.attributes, dict) and first.attributes)
    ok &= check("year and kilometers are promoted out of attributes as ints",
                isinstance(first.year, int) and isinstance(first.kilometers, int))

    prop = fx_rows("property_rent")[0]
    ok &= check("a rental states the period its price is quoted for",
                prop.payment_frequency in ("Yearly", "Monthly", "Weekly", "Daily"))
    ok &= check("a rental carries bedrooms and bathrooms",
                isinstance(prop.bedrooms, int) and isinstance(prop.bathrooms, int))
    ok &= check("a rental carries no `year`, and a car carries no `bedrooms`",
                prop.year is None and first.bedrooms is None)

    # The promoted slot, labelled rather than silently merged.
    kinds = {r.listing_kind for r in motors}
    ok &= check("the Car of the Week is labelled, not passed off as a result",
                "car_of_the_week" in kinds and "organic" in kinds)
    cotw = [r for r in motors if r.listing_kind == "car_of_the_week"]
    ok &= check("it is emitted AFTER the organic results, so position 1 is a "
                "real result", all(r.position > 1 for r in cotw))

    # page + position, together, must identify a row (§18).
    two = fx_rows("motors_p1") + fx_rows("motors_p2")
    pairs = [(r.page, r.position) for r in two]
    ok &= check("page+position is unique across a two-page run",
                len(set(pairs)) == len(pairs))
    ok &= check("page is threaded into the rows, not defaulted to 1",
                {r.page for r in two} == {1, 2})
    return ok


def test_null_price_is_the_site_and_not_the_parser():
    group("a null price: which of them are the site's answer")
    ok = True
    # §9: a column that is null on every row of every run should not exist —
    # so the fact that `price` is null on every jobs row has to be a
    # MEASUREMENT written down, not an unexplained gap. It is also why the
    # price-coverage floor is per-vertical.
    jobs = fx_rows("jobs")
    ok &= check("a jobs ad publishes no salary: every row null",
                jobs and all(r.price is None for r in jobs))
    ok &= check("and says so in price_source rather than leaving a bare null",
                all(r.price_source.endswith(":no-price") for r in jobs))
    ok &= check("a row with no price carries no currency either",
                all(r.currency is None for r in jobs))

    priced = fx_rows("motors_p1") + fx_rows("classified") + fx_rows("property_rent")
    ok &= check("motors, classified and property price every row",
                all(r.price is not None for r in priced))
    ok &= check("and none of them claims a no-price source",
                not any(r.price_source.endswith(":no-price") for r in priced))

    # §4's guard, one line, and it catches the regression forever.
    ok &= check("no row has an original_price at or below its price",
                not [r for r in priced
                     if r.original_price is not None and r.price is not None
                     and r.original_price <= r.price])
    ok &= check("no row has a zero or negative discount",
                not [r for r in priced
                     if r.discount_pct is not None and r.discount_pct <= 0])

    for engine in ENGINE_FILES:
        src = open(os.path.join(REPO_ROOT, engine), encoding="utf-8").read()
        ok &= check("%s's price floor is per-vertical and 0 for jobs" % engine,
                    '"jobs": 0' in src and '"motors": 90' in src)
    return ok


# ---------------------------------------------------------------------------
# The three sources, and the join between them
# ---------------------------------------------------------------------------
def test_jsonld_enrichment():
    group("JSON-LD: an enrichment, joined on the PATH and not on the URL")
    ok = True
    # The measurement that decided the payload is primary (§4).
    have_itemlist = {name: len(product_parser.jsonld_items_by_url(fx(name)[0]))
                     for name in ("motors_p1", "property_rent", "classified",
                                  "jobs", "community")}
    ok &= check("motors and property publish an ItemList",
                have_itemlist["motors_p1"] and have_itemlist["property_rent"])
    ok &= check("classified, jobs and community publish none — which is why "
                "JSON-LD is not the primary path here",
                have_itemlist["classified"] == 0
                and have_itemlist["jobs"] == 0
                and have_itemlist["community"] == 0)

    # The Arabic case. Keyed on the full URL this join matched 25 of 25 ads
    # in English and 0 of 25 in Arabic, silently emptying three columns while
    # the run reported success.
    ar = fx_rows("motors_ar_p1")
    ok &= check("an Arabic listing still joins its JSON-LD",
                all(r.price_source.startswith("next_data+jsonld") for r in ar))
    ok &= check("so brand survives the Arabic page",
                all(r.brand for r in ar))
    en = fx_rows("motors_p1")
    ok &= check("price_source names both sources where both exist",
                all(r.price_source == "next_data+jsonld" for r in en))
    cls = fx_rows("classified")
    ok &= check("and names only the payload where the page has no ItemList",
                all(r.price_source == "next_data" for r in cls))
    ok &= check("a vertical with no ItemList has no brand, rather than one "
                "guessed from the title", all(r.brand is None for r in cls))

    # `offers` in each of its legal shapes (§4's table).
    ok &= check("an explicit `\"offers\": null` does not raise",
                product_parser._ld_offer(None) == {})
    ok &= check("`offers` as a list takes the first dict",
                product_parser._ld_offer([1, {"price": 5}]) == {"price": 5})
    ok &= check("an ImageObject image is read",
                product_parser._ld_image({"@type": "ImageObject",
                                          "url": "https://x/y.jpg"}) == "https://x/y.jpg")
    ok &= check("a list of images takes the first usable one",
                product_parser._ld_image([{"contentUrl": "https://x/a.jpg"}])
                == "https://x/a.jpg")
    ok &= check("currency comes from the page's own AggregateOffer",
                product_parser.jsonld_currency(fx("motors_p1")[0]) == "AED")
    return ok


def test_dom_fallback_agrees_with_the_payload():
    group("the DOM fallback: an independent path that agrees")
    ok = True
    # The honest test of a fallback is to take the primary away. Stripping
    # the payload out of three verticals leaves the DOM path recovering the
    # same ads at the same prices — which is what makes it a fallback rather
    # than a comment.
    for name in ("motors_p1", "property_rent", "classified"):
        html, url, _ = fx(name)
        stripped = re.sub(r'<script[^>]+id="__NEXT_DATA__"[^>]*>.*?</script>',
                          "", html, flags=re.S)
        full = {r.sku: r for r in parse_products(html, url)}
        dom = {r.sku: r for r in parse_products(stripped, url)}
        ok &= check("%s: the DOM alone finds the same ads" % name,
                    set(dom) == set(full))
        disagreements = [s for s in dom if dom[s].price != full[s].price]
        ok &= check("%s: and reads the same price for every one of them"
                    % name, not disagreements)
        ok &= check("%s: the DOM rows say so in price_source" % name,
                    all(r.price_source.startswith("dom") for r in dom.values()))
        ok &= check("%s: and read the currency out of the price node's "
                    "SIBLING, which is where this site puts it" % name,
                    all(r.currency == "AED" for r in dom.values()
                        if r.price is not None))

    # §4's tile-scoping failure, in the shape it takes here.
    html, url, _ = fx("motors_p1")
    soup = BeautifulSoup(html, "html.parser")
    anchors = [a for a in soup.select("a[href]")
               if product_parser._is_ad_href(a.get("href"))]
    ok &= check("a tile scope stops at exactly one ad, not at the grid",
                all(len({x.get("href") for x in product_parser._tile_of(a).select("a[href]")
                         if product_parser._is_ad_href(x.get("href"))}) == 1
                    for a in anchors))

    # A hub page is full of ad links in its promo rails; the fallback must
    # not turn them into rows.
    hub_html, hub_url, _ = fx("hub_home")
    ok &= check("the fallback does not fire on a hub URL",
                parse_products(hub_html, hub_url) == [])
    return ok


# ---------------------------------------------------------------------------
# URLs
# ---------------------------------------------------------------------------
def test_urls():
    group("hosts, locales, verticals and the row key")
    ok = True
    ok &= check("two locales, from the site's own hreflang set",
                set(LOCALES) == {"en", "ar"})
    ok &= check("the browse host is supported", is_supported_host(LISTING_URL))
    ok &= check("so is every emirate subdomain an ad is published under",
                all(is_supported_host("https://%s/motors/used-cars/" % h)
                    for h in product_parser.EMIRATE_HOSTS))

    # §5: refuse WITH the reason. "is not a dubizzle site" would be false for
    # a dubizzle-branded OLX site and would send the reader hunting for a typo.
    for host in ("www.dubizzle.com.bh", "www.dubizzle.com.om", "dubizzle.com.eg"):
        why = unsupported_reason("https://%s/" % host)
        ok &= check("%s is refused, and the reason names the OLX platform" % host,
                    why is not None and "OLX" in why)
    ok &= check("a non-dubizzle host is refused too",
                unsupported_reason("https://www.olx.com.lb/") is not None)
    ok &= check("and a URL with no hostname does not raise",
                unsupported_reason("not a url") is not None)

    ok &= check("the locale is read off the path, not off a flag",
                locale_of(LISTING_URL) == "en"
                and locale_of("https://uae.dubizzle.com/ar/motors/used-cars/") == "ar")
    ok &= check("a locale is not invented for a host we do not read",
                locale_of("https://www.olx.com.lb/ar/x/") is None)
    ok &= check("the currency is AED and comes from the host, not a default",
                host_currency(LISTING_URL) == CURRENCY == "AED")
    ok &= check("and is not claimed for a host we do not read",
                host_currency("https://www.dubizzle.com.bh/") is None)

    ok &= check("every vertical the site's own taxonomy names is known",
                set(product_parser.VERTICALS) >= {
                    "motors", "classified", "property-for-sale",
                    "property-for-rent", "jobs", "jobs-wanted", "community"})
    ok &= check("a listing URL is a listing", listing_kind(LISTING_URL) == "listing")
    ok &= check("an individual ad is an ad", listing_kind(AD_URL) == "ad")
    ok &= check("a hub is a hub", listing_kind(HUB_URL) == "home")
    ok &= check("something else is neither",
                listing_kind("https://uae.dubizzle.com/help/") == "")

    # The row key. Four of five verticals carry a 32-hex uuid in the URL and
    # property carries neither of its two ids, which is why sku is the path.
    ok &= check("an ad's sku is its path, without the trailing slash",
                sku_from_url(AD_URL)
                == "/motors/used-cars/bmw/x4/2026/02/05/" + _AD_TAIL)
    ok &= check("the /ar prefix is stripped, so an Arabic run and an English "
                "run of the same ad diff as the same listing",
                sku_from_url("https://dubai.dubizzle.com/ar/property-for-rent/"
                             "residential/apartmentflat/2026/8/21/x-2-270682/")
                == sku_from_url("https://dubai.dubizzle.com/property-for-rent/"
                                "residential/apartmentflat/2026/8/21/x-2-270682/"))
    ok &= check("a category URL has no sku — it is not an ad",
                sku_from_url(LISTING_URL) is None)
    ok &= check("a property ad, which carries neither of its ids in its URL, "
                "still gets a sku",
                sku_from_url("https://dubai.dubizzle.com/property-for-rent/"
                             "residential/apartmentflat/2026/8/21/x-2-270682/")
                is not None)

    ok &= check("the category is the whole chain, not the leaf",
                category_from_url("https://uae.dubizzle.com/classified/"
                                  "electronics/televisions/")
                == "classified/electronics/televisions")
    ok &= check("an ad's own category stops at the date in its path",
                category_from_url(AD_URL) == "motors/used-cars/bmw/x4")
    ok &= check("a hub has no category", category_from_url(HUB_URL) is None)

    ok &= check("campaign parameters are dropped and filters kept",
                strip_tracking(LISTING_URL + "?utm_source=x&price_max=50000")
                == LISTING_URL + "?price_max=50000")
    return ok


def test_pagination():
    group("pagination: the site's own count, and an end it states outright")
    ok = True
    ok &= check("a listing paginates by URL", paginates_by_url(LISTING_URL))
    ok &= check("an individual ad does not", not paginates_by_url(AD_URL))
    ok &= check("page 1 is the bare URL, as the site's own prev-link is",
                page_url(LISTING_URL, 1) == LISTING_URL)
    ok &= check("page 2 is ?page=2", page_url(LISTING_URL, 2) == LISTING_URL + "?page=2")
    ok &= check("the page parameter replaces rather than duplicates",
                page_url(LISTING_URL + "?page=7", 2) == LISTING_URL + "?page=2")
    ok &= check("and the site's own filters survive it",
                page_url(LISTING_URL + "?price_max=50000", 3)
                == LISTING_URL + "?price_max=50000&page=3")
    ok &= check("the page number is read back out of the URL",
                page_number_from_url(LISTING_URL + "?page=12") == 12
                and page_number_from_url(LISTING_URL) == 1)
    ok &= check("a nonsense page parameter reads as page 1 rather than raising",
                page_number_from_url(LISTING_URL + "?page=abc") == 1)

    # The site states its own page count, and it is not the same number on
    # every vertical — a hardcoded 25 a page would have been wrong for
    # property by 40%.
    motors_html = fx("motors_p1")[0]
    prop_html = fx("property_rent")[0]
    ok &= check("motors: the payload's own totals are read back",
                total_results(motors_html) == FIX["motors_p1"]["expect_total_results"]
                and total_pages(motors_html, LISTING_URL)
                == FIX["motors_p1"]["expect_total_pages"])
    ok &= check("property pages hold 35 ads, motors 25, and both are READ",
                product_parser.hits_per_page(prop_html) == 35
                and product_parser.hits_per_page(motors_html) == 25)
    ok &= check("the catalogue is deeper than the site will address, and that "
                "is reported rather than swallowed",
                pages_beyond_cap(motors_html) > 0)

    # Past the end the site EMPTIES rather than clamping — which is a
    # stronger terminator than "this page added no new sku".
    end_html, end_url, _ = fx("motors_past_end")
    ok &= check("one page past the end reports totalHits 0",
                total_results(end_html) == 0)
    ok &= check("and is_no_results says so from the payload, in any language",
                product_parser.is_no_results(end_html))
    ok &= check("total_pages never returns 0 for a caller about to divide by it",
                total_pages(end_html, end_url) == 1)

    ok &= check("page 1's own next-link agrees with the convention, which is "
                "what makes the pages independently addressable",
                page_flow.pagination_is_addressable(
                    LISTING_URL, "https://uae.dubizzle.com/motors/used-cars/?page=2"))
    ok &= check("a next-link that carries a cursor would not",
                not page_flow.pagination_is_addressable(
                    LISTING_URL, "https://uae.dubizzle.com/motors/used-cars/?cursor=abc"))
    ok &= check("agreement is checked against EVERY advertised link, not the "
                "first",
                page_flow.pagination_agrees(
                    LISTING_URL, 1, ["/motors/used-cars/bmw/",
                                     "/motors/used-cars/?page=2"]))
    ok &= check("a relative link is resolved before being compared",
                page_flow.pagination_agrees(LISTING_URL, 1,
                                            "/motors/used-cars/?page=2"))
    cands = page_flow.next_page_candidates(
        LISTING_URL, ["/motors/used-cars/?page=2", "/motors/used-cars/?page=40",
                      "/classified/electronics/televisions/?page=2"])
    ok &= check("a candidate must be the NEXT page of the SAME listing",
                cands == [LISTING_URL + "?page=2"])
    ok &= check("a single href is accepted as well as a list — passing a list "
                "to urljoin is what took a sibling's first live run down",
                page_flow.next_page_candidates(LISTING_URL,
                                               "/motors/used-cars/?page=2")
                == [LISTING_URL + "?page=2"])
    ok &= check("and None is accepted too",
                page_flow.next_page_candidates(LISTING_URL, None)
                == [LISTING_URL + "?page=2"])

    ok &= check("concurrency is allowed on a listing",
                page_flow.concurrency_refusal(LISTING_URL) is None)
    for url, why in ((AD_URL, "one advertisement"), (HUB_URL, "hub page")):
        refusal = page_flow.concurrency_refusal(url)
        ok &= check("concurrency is refused for a %s, WITH the reason" % why,
                    refusal is not None and why.split()[-1] in refusal)
    return ok


def test_page_state():
    group("classification: ordered by what each signal proves, not by cost")
    ok = True
    for name in FIX:
        html, url, status = fx(name)
        ok &= check("%s classifies as %s" % (name, FIX[name]["expect_state"]),
                    detect_page_state(html, status, url) == FIX[name]["expect_state"])

    block_html = fx("block_pardon")[0]
    ok &= check("the 'Pardon Our Interruption' refusal is blocked AT HTTP 200 "
                "— on this site the status code is not the signal",
                detect_page_state(block_html, 200, LISTING_URL) == "blocked")
    ok &= check("neither refusal is built out of dubizzle's own assets",
                not served_by_dubizzle(block_html)
                and not served_by_dubizzle(fx("block_iframe")[0]))
    ok &= check("every page the site served IS",
                all(served_by_dubizzle(fx(n)[0])
                    for n in ("motors_p1", "property_rent", "classified",
                              "jobs", "community", "hub_home", "not_found")))

    # §18's inverted-detection case: Chromium's own network-error page carries
    # the site's hostname in its <title> and no vendor marker of any kind.
    chromium_error = (
        '<html><head><title>www.dubizzle.com</title></head><body>'
        '<div class="error-code">ERR_PROXY_CONNECTION_FAILED</div>'
        '<div>This site can’t be reached</div></body></html>')
    ok &= check("Chromium's own error page is blocked, though its title is "
                "the site's hostname and it carries no vendor marker",
                detect_page_state(chromium_error, None, LISTING_URL) == "blocked")
    ok &= check("and no marker list would have caught it — only the assets do",
                detect_block_marker(chromium_error) is None)

    # §18's other rule: a marker that matches a good page is worse than none.
    served = fx("motors_p1")[0]
    ok &= check("_Incapsula_Resource appears on pages the site SERVED, so it "
                "is not on its own a challenge marker",
                detect_bot_challenge(served) is None)
    ok &= check("and the vendor's own refusal resource IS one",
                detect_bot_challenge(
                    '<script src="/_Incapsula_Resource?SWCGHOEL=v2"></script>')
                is not None)

    # The extension trap (§8), and here the guard is NOT dead code: this
    # marker set contains exactly what the auto-solve extension injects.
    ok &= check("a Scraping Browser extension's own hunters are not mistaken "
                "for the site's challenge",
                detect_bot_challenge(served + EXTENSION_TAGS) is None)
    ok &= check("but the same markers in a NON-extension script still count",
                detect_bot_challenge(
                    served + '<script src="https://www.google.com/recaptcha/'
                             'api2/anchor?k=x"></script>') is not None)

    # A page past the end still renders one ad, so `empty` must not be parsed.
    end_html, end_url, _ = fx("motors_past_end")
    ok &= check("a page past the end classifies as empty",
                detect_page_state(end_html, 200, end_url) == "empty")
    ok &= check("and it really does still carry a fully-formed ad — which is "
                "why the policy does not parse it",
                len(parse_products(end_html, end_url)) == 1)
    ok &= check("the policy does not parse it",
                not page_flow.should_parse("empty"))
    ok &= check("nor does it count as blocked, nor spend a solve",
                not page_flow.counts_as_blocked("empty")
                and not page_flow.should_solve("empty"))

    ok &= check("a hub URL is empty, not a shell that will never paint",
                detect_page_state(fx("hub_home")[0], 200, HUB_URL) == "empty")
    ok &= check("a served page with a grid on the way is a shell",
                detect_page_state(
                    '<html><head><link href="https://static.dubizzle.com/a.css">'
                    '<link href="https://static.dubizzle.com/b.css"></head>'
                    '<body></body></html>', 200, LISTING_URL) == "shell")
    ok &= check("a shell wants the wait, not another fetch",
                page_flow.should_parse("shell") and not page_flow.should_retry("shell"))
    ok &= check("no body at all is blocked rather than a crash",
                detect_page_state(None, None, LISTING_URL) == "blocked")
    return ok


def test_page_flow():
    group("policy: as data, so three engines cannot disagree")
    ok = True
    ok &= check("five states covered", set(page_flow.STATE_POLICY) ==
                {"content", "empty", "shell", "challenge", "blocked"})
    for state in page_flow.STATE_POLICY:
        ok &= check("%s: every decision is spelled out" % state,
                    set(page_flow.STATE_POLICY[state])
                    == {"parse", "retry", "solve", "blocked"})
    ok &= check("content parses and is not retried",
                page_flow.should_parse("content")
                and not page_flow.should_retry("content")
                and not page_flow.should_solve("content"))
    ok &= check("empty is NOT parsed here — a page past the end still renders "
                "one Car of the Week",
                not page_flow.should_parse("empty")
                and not page_flow.counts_as_blocked("empty"))
    ok &= check("blocked counts as blocked and never asks for a solve",
                page_flow.counts_as_blocked("blocked")
                and not page_flow.should_solve("blocked"))
    ok &= check("challenge asks for a solve and is not counted as blocked",
                page_flow.should_solve("challenge")
                and not page_flow.counts_as_blocked("challenge"))
    ok &= check("an unknown state is treated as blocked, not as content",
                not page_flow.should_parse("nonsense")
                and page_flow.counts_as_blocked("nonsense"))

    # On THIS site the exit's country is the discriminator — the opposite of
    # a sibling repo, where the client mattered and the address did not. So
    # the retry budget is spent rather than saved, and the engines read these
    # constants rather than computing their own (a policy constant nothing
    # consults is the same defect as dead code, §17).
    ok &= check("RETRY_ON_BLOCKED is True here: rotating the exit DOES help",
                page_flow.RETRY_ON_BLOCKED is True)
    ok &= check("and the block-retry budgets are non-zero",
                page_flow.BLOCK_RETRIES_WITHOUT_POOL >= 1
                and page_flow.BLOCK_RETRIES_WITH_POOL
                >= page_flow.BLOCK_RETRIES_WITHOUT_POOL)
    for engine in ENGINE_FILES:
        src = open(os.path.join(REPO_ROOT, engine), encoding="utf-8").read()
        ok &= check("%s reads the shared block budget rather than its own"
                    % engine,
                    "BLOCK_RETRIES_WITHOUT_POOL" in src
                    and "BLOCK_RETRIES_WITH_POOL" in src)
    advice = page_flow.block_advice(fx("block_pardon")[0], headless=True,
                                    has_pool=False)
    ok &= check("the block advice names the real cause — the exit's COUNTRY",
                "UAE" in advice and "country-ae" in advice)
    ok &= check("and does not blame headless, which is not the cause here",
                "--headful" not in advice)

    # Readiness: a price node, above 1 match, and sized to the page.
    ok &= check("the readiness anchor is a price node",
                "listing-price" in page_flow.ready_selector("listing"))
    ok &= check("the threshold is above 1 — waiting for a single match "
                "resolves on an unrelated node", page_flow.MIN_CARD_MATCHES > 1)
    ok &= check("a short last page lowers the threshold instead of timing out",
                page_flow.min_matches("listing", 5) == 5
                and page_flow.min_matches("listing", 35)
                == page_flow.MIN_CARD_MATCHES)
    ok &= check("...but never below 2", page_flow.min_matches("listing", 1) == 2)
    ok &= check("expected_cards reads the payload, before anything hydrates",
                page_flow.expected_cards(fx("motors_p1")[0])
                == FIX["motors_p1"]["expect_rows"])

    # No scroll subsystem at all, and that is measured: the captures taken
    # with no scrolling carried the same counts as the ones taken with four
    # scroll rounds.
    ok &= check("page_flow exposes no scroll API",
                not hasattr(page_flow, "scroll_until_settled")
                and not hasattr(page_flow, "SCROLL_ROUNDS_MAX"))

    # A poll, not an evaluated string: `wait_for_function` hands the browser a
    # STRING, and a site whose CSP lacks `unsafe-eval` refuses it outright —
    # which took a sibling repo's run down with exit 1 on its most obvious
    # URL (§18). Counting elements goes through the protocol instead.
    calls = {"n": 0}

    def count(_sel):
        calls["n"] += 1
        return 0 if calls["n"] < 3 else 9

    ok &= check("wait_for_count polls until the threshold is reached",
                page_flow.wait_for_count(count, lambda ms: None, ".x", 8, 5000) == 9)
    ok &= check("...and returns the last count when the budget runs out",
                page_flow.wait_for_count(lambda s: 2, lambda ms: None,
                                         ".x", 8, 500) == 2)
    ok &= check("...and a driver error during the poll does not take the run "
                "down",
                page_flow.wait_for_count(
                    lambda s: (_ for _ in ()).throw(RuntimeError("driver")),
                    lambda ms: None, ".x", 8, 500) == 0)
    for name in ("wait_for_count", "classify"):
        src = inspect.getsource(getattr(page_flow, name))
        ok &= check("page_flow.%s hands no JavaScript across the boundary"
                    % name,
                    "=>" not in src and "return document" not in src)

    # This site DOES publish link[rel=next], so the standards-based layer
    # leads the selector list rather than being a hopeful last entry.
    ok &= check("link[rel=next] leads the selector list",
                page_flow.NEXT_PAGE_SELECTOR.strip().startswith('link[rel="next"]'))
    ok &= check("and the site really does publish one",
                'rel="next"' in fx("motors_p1")[0]
                or 'rel="next"' in FIX["motors_p1"]["html"])

    ok &= check("a paginated listing has no concurrency limit",
                page_flow.concurrency_limit(LISTING_URL) is None)
    for url, word in ((AD_URL, "an individual ad"), (HUB_URL, "a hub page")):
        reason = page_flow.concurrency_refusal(url)
        ok &= check("concurrency on %s is refused with a reason" % word,
                    page_flow.concurrency_limit(url) == 1
                    and reason and "nothing to fetch" in reason)
    ok &= check("pages_at_cap does not fire on a listing the site addresses "
                "in full", not page_flow.pages_at_cap(fx("jobs")[0]))
    return ok


# ---------------------------------------------------------------------------
# The output contract
# ---------------------------------------------------------------------------
def test_output_contract():
    group("the output contract shared across this scraper family")
    ok = True
    names = [f.name for f in fields(Product)]
    # The family prefix, byte-identical and in order, so a consumer written
    # against another repo in this family reads the first eighteen columns
    # unchanged. Site-specific columns go AFTER it.
    family_prefix = ["source", "scraped_at", "url", "sku", "title", "brand",
                     "price", "currency", "original_price", "discount_pct",
                     "rating", "review_count", "in_stock", "image_url",
                     "category", "price_source", "page", "position"]
    ok &= check("the family field prefix is present and in order",
                names[:len(family_prefix)] == family_prefix)
    ok &= check("this site's own columns come after it, in order",
                names[len(family_prefix):] ==
                ["vertical", "listing_id", "listing_uuid", "short_url",
                 "city", "location", "seller_name", "seller_kind", "seller_id",
                 "is_verified", "is_premium", "listing_kind", "photos_count",
                 "posted_at", "bumped_at", "payment_frequency", "bedrooms",
                 "bathrooms", "size_sqft", "year", "kilometers", "attributes"])
    # One KIND of thing here, so one dataclass — there is no second row class
    # to keep in step, and no mode that reads something else (§9).
    ok &= check("one mode maps to one row class",
                ROW_CLASS_BY_MODE == {"listing": Product})
    ok &= check("that mode is one row per sku",
                set(UNIQUE_BY_SKU_MODES) == {"listing"})

    # §9: a column that is null on every row of every run should not exist.
    # `rating` and `review_count` ARE null on every row here, and they are
    # kept only because they are in the family prefix — which is a decision,
    # so it is written down and pinned rather than left to be rediscovered.
    every_row = []
    for name in ("motors_p1", "property_rent", "classified", "jobs", "community"):
        every_row += fx_rows(name)
    always_null = {f.name for f in fields(Product)
                   if all(getattr(r, f.name) is None for r in every_row)}
    ok &= check("the only always-null columns are the family-prefix ones this "
                "site does not publish -- rating and review_count, because it "
                "rates SELLERS and never the ad (measured: 0 of %d rows across "
                "five verticals)" % len(every_row),
                always_null <= {"rating", "review_count", "original_price",
                                "discount_pct", "size_sqft", "bedrooms",
                                "bathrooms", "year", "kilometers",
                                "payment_frequency"})
    ok &= check("rating and review_count really are null on every row",
                {"rating", "review_count"} <= always_null)
    # And the per-vertical ones are null only OUTSIDE their vertical, which
    # is the difference between a sparse column and a dead one.
    prop = fx_rows("property_rent")
    motors = fx_rows("motors_p1")
    ok &= check("bedrooms is populated on property and null on motors",
                all(r.bedrooms is not None for r in prop)
                and all(r.bedrooms is None for r in motors))
    ok &= check("kilometers is populated on motors and null on property",
                all(r.kilometers is not None for r in motors)
                and all(r.kilometers is None for r in prop))
    ok &= check("which is what `vertical` is for",
                {r.vertical for r in prop} == {"property-for-rent"}
                and {r.vertical for r in motors} == {"motors"})

    ok &= check("the exit codes are the family's",
                (EXIT_BLOCKED, EXIT_NO_PRODUCTS, EXIT_PARTIAL) == (3, 4, 6))
    ok &= check("an exhausted listing counts as complete",
                "no_new_products" in COMPLETE_STOP_REASONS
                and "pagination_exhausted" in COMPLETE_STOP_REASONS)
    ok &= check("a single-page mode is complete by construction",
                "single_page_mode" in COMPLETE_STOP_REASONS)

    # No defaulted currency anywhere: a row that could not establish one says
    # None rather than claiming EUR, which would be wrong for the four
    # non-euro country sites.
    ok &= check("Product defaults currency to None, not a guess",
                Product().currency is None)
    ok &= check("Product defaults price_source to None",
                Product().price_source is None)
    return ok


def test_writers():
    group("writers, dedupe and the refusal to overwrite good data")
    ok = True
    rows = [Product(sku="1", url="u1", price=1.0),
            Product(sku="2", url="u2", price=2.0)]
    with tempfile.TemporaryDirectory() as d:
        prefix = os.path.join(d, "out")

        # A run that finds nothing writes NOTHING: a consumer cannot tell an
        # empty category from a failed run, and the failure destroys the last
        # known good data.
        save(rows, prefix, "json", allow_empty=False)
        ok &= check("a good run writes its output",
                    os.path.exists(prefix + ".json"))
        before = open(prefix + ".json").read()
        save([], prefix, "json", allow_empty=False)
        ok &= check("an empty run does NOT overwrite the previous good output",
                    open(prefix + ".json").read() == before)
        save([], prefix, "json", allow_empty=True)
        ok &= check("--allow-empty is the opt-out and does overwrite",
                    json.load(open(prefix + ".json")) == [])

        # An empty CSV still carries its header, so a consumer reads a table
        # with no rows instead of failing on a zero-byte file.
        csv_path = os.path.join(d, "empty.csv")
        write_csv([], csv_path, row_cls=Product)
        header = open(csv_path).read().strip().split("\n")[0]
        ok &= check("an empty CSV still carries its header",
                    header.split(",")[:4] == ["source", "scraped_at", "url", "sku"])

        # A list column has to survive CSV without becoming a Python repr.
        #
        # NO column here is a list — a listing row carries one image url
        # and no attribute list this repo reads — so this is checked with a
        # local row class rather than with Product. The joining is kept in
        # write_csv because it is generic and because a future column may
        # need it; pinning the CURRENT behaviour is what stops it being
        # deleted as dead or reappearing as a repr() by accident.
        ok &= check("no Product column is a list today",
                    not [f for f in fields(Product)
                         if "List" in str(f.type)])

        from dataclasses import dataclass as _dataclass
        from typing import List as _List, Optional as _Optional

        @_dataclass
        class _WithList:
            sku: _Optional[str] = None
            things: _Optional[_List[str]] = None

        csv_path = os.path.join(d, "list.csv")
        write_csv([_WithList(sku="1", things=["a", "b"])], csv_path,
                  row_cls=_WithList)
        body = open(csv_path).read()
        ok &= check("a list column is joined, not repr()d in CSV",
                    ("a" + LIST_CSV_SEPARATOR + "b") in body and "['a'" not in body)

    seen = set()
    ok &= check("dedupe drops a repeated sku",
                len(dedupe_by_sku([Product(sku="a"), Product(sku="a")], seen)) == 1)
    # A row with no key is always KEPT: there is nothing to check a duplicate
    # against, and dropping it is a silent data loss rather than a dedupe.
    ok &= check("a row with no sku is kept, not dropped",
                len(dedupe_by_key([Product(sku=None), Product(sku=None)],
                                  set())) == 2)

    meta = run_meta("complete", "completed", 3, 3, "u", "u", 36,
                    pages_failed=[], mode="listing", source="dubizzle.com")
    ok &= check("the sidecar records status, mode and source",
                meta["status"] == "complete" and meta["mode"] == "listing"
                and meta["source"] == "dubizzle.com")
    # A count stops being a description once a page can fail while later ones
    # succeed, so the sidecar names WHICH pages failed.
    meta = run_meta("partial", "blocked", 5, 3, "u", "u", 12,
                    pages_failed=[2, 4], mode="listing", source="dubizzle.com")
    ok &= check("the sidecar names which pages failed, by number",
                meta["pages_failed"] == [2, 4])
    return ok


def test_finish_run():
    group("finish_run: the exit codes all three engines must agree on")
    ok = True
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "run")
        rows = [Product(sku="1", url="u")]

        code = finish_run(rows, p, "json", False, blocked=False,
                          stop_reason="completed", pages_requested=1,
                          pages_completed=1, pages_failed=[], mode="listing",
                          source="dubizzle.com", start_url="u", final_url="u")
        ok &= check("a complete run exits 0", code == 0)

        code = finish_run([], p + "b", "json", False, blocked=True,
                          stop_reason="blocked_no-response",
                          pages_requested=1, pages_completed=0,
                          pages_failed=[1], mode="listing",
                          source="dubizzle.com", start_url="u", final_url="u")
        ok &= check("a blocked run exits 3, not 4", code == EXIT_BLOCKED)
        # A FAILED run writes no sidecar: `save` leaves the previous good
        # output in place, and a "failed" sidecar beside good data would
        # contradict it.
        ok &= check("a failed run writes no sidecar beside older good data",
                    not os.path.exists(p + "b.meta.json"))

        code = finish_run([], p + "c", "json", False, blocked=False,
                          stop_reason="completed", pages_requested=1,
                          pages_completed=1, pages_failed=[], mode="listing",
                          source="dubizzle.com", start_url="u", final_url="u")
        ok &= check("a genuinely empty result exits 4, not 3",
                    code == EXIT_NO_PRODUCTS)

        code = finish_run(rows, p + "d", "json", False, blocked=False,
                          stop_reason="page_load_timeout", pages_requested=5,
                          pages_completed=2, pages_failed=[3], mode="listing",
                          source="dubizzle.com", start_url="u", final_url="u")
        ok &= check("a run with data that stopped early exits 6 (partial)",
                    code == EXIT_PARTIAL)
        ok &= check("a partial run still writes what it got",
                    os.path.exists(p + "d.json"))
    return ok


def test_diff():
    group("diff_runs")
    ok = True
    # `price_source` on this site is "dom" for a listing row and
    # "meta+apollo" for a detail row — there is no structured price on a
    # listing page to confirm against — so the source-change case uses those
    # two values rather than the sibling repos' jsonld pair.
    old = [{"sku": "1", "price": 10.0, "price_source": "dom"},
           {"sku": "2", "price": 20.0, "price_source": "dom"},
           {"sku": "3", "price": 30.0, "price_source": "dom"}]
    new = [{"sku": "1", "price": 11.0, "price_source": "dom"},
           {"sku": "3", "price": 30.5, "price_source": "meta+apollo"},
           {"sku": "4", "price": 40.0, "price_source": "dom"}]
    d = diff_products(old, new)
    ok &= check("a real price move is reported as changed",
                any(c["sku"] == "1" for c in d["changed"]))
    ok &= check("a delisted product is reported as removed",
                [r["sku"] for r in d["removed"]] == ["2"])
    ok &= check("a new product is reported as added",
                [r["sku"] for r in d["added"]] == ["4"])
    # A price difference that comes with a price_source difference says
    # something about OUR two snapshots, not about the shop.
    ok &= check("a price move with a source change is not 'changed'",
                not any(c["sku"] == "3" for c in d["changed"]))
    ok &= check("...it is reported separately as source_changed",
                any(c["sku"] == "3" for c in d.get("source_changed", [])))

    # What a classifieds diff has to track, and one thing it must NOT report
    # as a price change.
    tracked = __import__("diff_runs").TRACKED_FIELDS
    ok &= check("the money columns are tracked",
                {"price", "original_price", "discount_pct", "currency"}
                <= set(tracked))
    ok &= check("so is the period a rent is quoted for — a yearly and a "
                "monthly figure in one column are not comparable",
                "payment_frequency" in tracked)
    ok &= check("and the placement columns, which is how a promoted-slot "
                "move is recognised",
                {"listing_kind", "is_premium"} <= set(tracked))

    # The promoted slot rotates. An ad moving in or out of it is
    # merchandising, not a reprice, and reporting it as a change would put a
    # bogus entry in every motors diff.
    import diff_runs as _dr
    promoted = _dr.diff_products(
        [{"sku": "/a", "price": 100.0, "listing_kind": "organic",
          "price_source": "next_data+jsonld"}],
        [{"sku": "/a", "price": 100.0, "listing_kind": "car_of_the_week",
          "price_source": "next_data+jsonld"}])
    ok &= check("organic -> car_of_the_week lands in `lifecycle`, not in "
                "`changed`",
                len(promoted["lifecycle"]) == 1 and not promoted["changed"])
    ok &= check("...and so does the move back out of the slot",
                len(_dr.diff_products(
                    [{"sku": "/a", "price": 100.0,
                      "listing_kind": "car_of_the_week",
                      "price_source": "next_data+jsonld"}],
                    [{"sku": "/a", "price": 100.0, "listing_kind": "organic",
                      "price_source": "next_data+jsonld"}])["lifecycle"]) == 1)

    # A plain price move with no placement change is still a change — the
    # bucket must not swallow the thing the tool exists for.
    repriced = _dr.diff_products(
        [{"sku": "/a", "price": 100.0, "listing_kind": "organic",
          "price_source": "next_data+jsonld"}],
        [{"sku": "/a", "price": 120.0, "listing_kind": "organic",
          "price_source": "next_data+jsonld"}])
    ok &= check("a seller dropping the price is still a change",
                len(repriced["changed"]) == 1 and not repriced["lifecycle"])

    # The sibling repos' auction and from-price columns are NOT tracked,
    # because they do not exist here.
    ok &= check("no auction columns are tracked (this site has none)",
                not {"bid_kind", "sold", "reserve_price_met", "auction_status"}
                & set(tracked))
    ok &= check("no from-price columns are tracked (a card prints one amount)",
                not {"price_is_from", "price_max"} & set(tracked))
    ok &= check("...and Product declares neither family's extras",
                not {"price_is_from", "price_max", "bid_kind", "sold"}
                & {f.name for f in fields(Product)})
    return ok


def test_pyppeteer_teardown_noise():
    group("pyppeteer teardown noise is suppressed, and its limit is pinned")
    ok = True
    try:
        import puppeteer_scraper as pyp
    except ImportError:
        return check("pyppeteer engine present (skipped: library absent)", True)

    handler = pyp._AsyncBridge._on_loop_exception.__func__ if hasattr(
        pyp._AsyncBridge._on_loop_exception, "__func__") else pyp._AsyncBridge._on_loop_exception

    class _Loop:
        def __init__(self): self.passed_through = []
        def default_exception_handler(self, context):
            self.passed_through.append(context)

    # Each of these arrives on a run that SUCCEEDED, after the output is
    # written, and four tracebacks under a healthy run is how a reader learns
    # to ignore the log.
    swallowed = [
        {"message": "Task was destroyed but it is pending"},
        {"message": "Future exception was never retrieved",
         "exception": RuntimeError("Protocol error (Target.sendMessageToTarget): "
                                   "No session with given id")},
        {"exception": RuntimeError("Target closed")},
        {"exception": RuntimeError("Connection closed")},
        {"message": "Event loop is closed"},
    ]
    for context in swallowed:
        loop = _Loop()
        handler(loop, context)
        label = (context.get("message") or str(context.get("exception")))[:44]
        ok &= check("teardown noise suppressed: %s" % label,
                    not loop.passed_through)

    # A REAL error must still get through, or the suppression has become a
    # blindfold.
    loop = _Loop()
    handler(loop, {"exception": ValueError("something actually went wrong")})
    ok &= check("a real exception is NOT swallowed", len(loop.passed_through) == 1)

    # The handler reads BOTH fields. It used to read `exception or message`,
    # which meant a context carrying both never had its message inspected —
    # so the asyncio-worded ones kept printing after they were "handled".
    src = inspect.getsource(handler)
    ok &= check("the handler inspects the message as well as the exception",
                'for k in ("exception", "message")' in src)

    # PINNED LIMITATION, not a guard: `Exception ignored in: <coroutine
    # object Connection._recv_loop>` is printed by CPython's garbage
    # collector at interpreter shutdown, after the loop is gone and after the
    # exit code is decided. No loop handler can reach it, and catching it
    # would mean a global unraisable hook that swallows real bugs too. It is
    # documented in TROUBLESHOOTING.md instead; this check makes sure that
    # documentation stays there.
    doc = open(os.path.join(REPO_ROOT, "TROUBLESHOOTING.md"),
               encoding="utf-8").read()
    ok &= check("the shutdown-time traceback is documented rather than hidden",
                "Exception ignored in" in doc and "The run succeeded" in doc)
    return ok


def test_canary_separates_access_from_defect():
    group("the canary fails on defects and only WARNS on access conditions")
    ok = True
    wf_path = os.path.join(REPO_ROOT, ".github", "workflows", "canary.yml")
    wf = open(wf_path, encoding="utf-8").read()

    # The data checks must not run on a blocked or refused run: there is no
    # output file, and a missing file would fail for the wrong reason.
    ok &= check("the data checks are gated on the run having got in",
                "steps.verdict.outputs.tested == 'true'" in wf)

    # Extract the real interpret-the-exit-code script and run it under bash
    # for every code, rather than asserting on the YAML text. What matters is
    # whether the JOB FAILS, and only running it answers that.
    try:
        start = wf.index('          set -e\n          code=')
        end = wf.index('          echo "tested=$tested" >> "$GITHUB_OUTPUT"')
        end += len('          echo "tested=$tested" >> "$GITHUB_OUTPUT"')
    except ValueError:
        return check("the canary's exit-code script could be located", False)
    script = "\n".join(line[10:] if line.startswith(" " * 10) else line
                        for line in wf[start:end].splitlines())

    # WHY EACH CODE LANDS WHERE IT DOES:
    #   0  got in and parsed         -> pass, and the assertions then run
    #   3  blocked before parsing    -> ACCESS. Measured intermittent per
    #      profile on this site, so a daily red badge would be noise.
    #   5  endpoint refused          -> ACCESS, and the commonest cause is an
    #      EXPIRED SECRET. A credential is not forever; failing on it paints
    #      the badge red every day until someone notices.
    #   6  partial                   -> ACCESS, usually a mid-run block.
    #   1  crashed                   -> DEFECT.
    #   2  bad arguments             -> DEFECT (in the workflow itself).
    #   4  served a page, ZERO rows  -> DEFECT, and precisely the regression
    #      this canary exists to catch: the tile anchor moved.
    expected = {0: "pass", 3: "warn", 5: "warn", 6: "warn",
                1: "fail", 2: "fail", 4: "fail", 99: "fail"}
    for code, want in sorted(expected.items()):
        body = script.replace('code="${{ steps.run.outputs.exit_code }}"',
                              'code="%d"' % code)
        with tempfile.TemporaryDirectory() as td:
            out_file = os.path.join(td, "gh_output")
            summary = os.path.join(td, "gh_summary")
            open(out_file, "w").close()
            open(summary, "w").close()
            done = subprocess.run(
                ["bash", "-c", body], capture_output=True, text=True,
                env=dict(os.environ, GITHUB_OUTPUT=out_file,
                         GITHUB_STEP_SUMMARY=summary))
            failed = done.returncode != 0
            warned = "::warning::" in done.stdout
            errored = "::error::" in done.stdout
            wrote_summary = bool(open(summary, encoding="utf-8").read().strip())
            tested = "tested=true" in open(out_file, encoding="utf-8").read()

        if want == "pass":
            got = not failed and not warned and not errored and tested
        elif want == "warn":
            # A warning must NOT read as a pass: it also has to say in the
            # step summary that nothing was actually tested, and it must not
            # claim `tested`.
            got = not failed and warned and wrote_summary and not tested
        else:
            got = failed and errored
        ok &= check("exit %-2d is treated as %s" % (code, want), got)

    ok &= check("the reason access is not a defect is written down",
                "ACCESS CONDITIONS ARE NOT DEFECTS" in wf)

    # NO SCHEDULE, and the reason has to travel with the decision. A daily
    # cron against a credential that does not survive a day gives either a
    # permanently red badge or a permanently green one that tested nothing —
    # and the green is worse, because it reads as "the parser still works".
    # Restoring the cron is a legitimate change the day a long-lived
    # credential exists; this check makes it a decision rather than a habit.
    ok &= check("the canary has no cron schedule",
                not re.search(r"^\s*-\s*cron:", wf, re.M))
    ok &= check("it is dispatchable by hand", "workflow_dispatch:" in wf)
    ok &= check("and the reason the schedule is off is written down",
                "does not survive long" in wf)
    ok &= check("the expired-secret case is named",
                "expired" in wf.lower() and "refresh" in wf.lower())
    return ok


def test_ci_checks_is_actually_wired_up():
    group("the repo's own checks are RUN, and still catch a real secret")
    ok = True
    script = os.path.join(REPO_ROOT, ".github", "ci_checks.py")
    ok &= check("ci_checks.py exists", os.path.exists(script))
    if not os.path.exists(script):
        return ok

    # IT HAS TO BE INVOKED BY A WORKFLOW. It was not — for the whole of
    # v0.1.0 it sat there implementing three checks that nothing ran, while a
    # second, LOOSER copy of one of them lived inline in tests.yml. Dead code
    # that looks load-bearing is worse than no code, and this is the check
    # that keeps it alive.
    wf_dir = os.path.join(REPO_ROOT, ".github", "workflows")
    workflows = "\n".join(
        open(os.path.join(wf_dir, f), encoding="utf-8").read()
        for f in sorted(os.listdir(wf_dir)) if f.endswith((".yml", ".yaml")))
    ok &= check("a workflow runs ci_checks.py", "ci_checks.py" in workflows)
    ok &= check("the secret check specifically is run",
                "--secret-check" in workflows or "--all" in workflows)

    # IT SCANS WHAT IS TRACKED, AT ANY SUFFIX — pinned because a suffix
    # allowlist is how this check failed once. `--dump-html live_results`
    # writes `live_results.page1`, a name with no suffix the old list knew,
    # and a merge committed two of them at 1.5 MB each while this check ran,
    # passed and never opened them. `.json` was not on the list either, so
    # `fixtures_generated.json` and `sample_output.json` had never been
    # scanned at all.
    import importlib.util as _ilu
    spec = _ilu.spec_from_file_location("ci_checks", script)
    ci = _ilu.module_from_spec(spec)
    spec.loader.exec_module(ci)
    scanned = {os.path.relpath(p, REPO_ROOT) for p in ci.scanned_files()}
    ok &= check("the working-tree scan reads the generated data files, which "
                "a suffix allowlist never did",
                {"fixtures_generated.json", "sample_output.json",
                 "sample_output.csv"} <= scanned)
    ok &= check("...and the workflows, the Dockerfile and the env example",
                {".env.example", "Dockerfile"} <= scanned)
    ok &= check("it asks GIT what is tracked, not the filesystem — a "
                "developer's own .env and captures beside the scripts are "
                "expected and must not turn it red",
                ".env" not in scanned)

    # A RAW CAPTURE MUST NOT BE TRACKED, whatever is inside it. The two that
    # got through carried nothing of ours — no key, no proxy password, no
    # cookie — so a content rule would have passed them. What was wrong was
    # that they were committed at all.
    ok &= check("a tracked page dump is refused by shape, not by name",
                any(s.search("live_results.page7") for s in ci.CAPTURE_SHAPES)
                and any(s.search("out_page1_debug.html")
                        for s in ci.CAPTURE_SHAPES)
                and any(s.search("captures/uae_usedcars_p1.html")
                        for s in ci.CAPTURE_SHAPES))
    ok &= check("and no such file is tracked here",
                not [p for p in ci.tracked_files()
                     if any(s.search(p.relative_to(ci.REPO).as_posix())
                            for s in ci.CAPTURE_SHAPES)])

    # The key-shaped-field rule applies EVERYWHERE, generated data included —
    # it is what the bare-hex rule was reaching for, said precisely.
    ok &= check("a secret in a key-shaped field is caught",
                ci.KEY_SHAPED_FIELD.search(
                    # Split so this file does not itself carry a bare 32-hex
                    # run: the check it is testing scans this file too, and a
                    # fixture that trips the rule it proves would either turn
                    # the build red or force the rule to be widened.
                    '"x-algolia-api-key": "%s%s"'
                    % ("cdd839b4fdac8402", "89e88633779e8634")))
    ok &= check("...and the scrubbed placeholder is not",
                not ci.KEY_SHAPED_FIELD.search(
                    '"x-algolia-api-key": "REDACTED-SEARCH-KEY"'))

    # The bare-hex rule is deliberately NOT applied to the generated data
    # files, and that exemption is pinned so it stays a decision: this site
    # publishes 32-hex identifiers in at least five contexts, and a rule that
    # fires 221 times on correct data is a rule somebody switches off.
    ok &= check("the bare-hex rule exempts the generated data files, with "
                "the corpus scan covering them instead",
                set(ci.GENERATED_DATA_FILES) == {"fixtures_generated.json",
                                                 "sample_output.json",
                                                 "sample_output.csv"})
    ok &= check("...but this site's public ad id is still subtracted from "
                "every other file, in all three of its spellings",
                not ci.HEX32.findall(ci._without_site_ids(
                    '"url": "https://dubai.dubizzle.com/x/2026/1/1/'
                    'a---ac0df37b468f4757aebf3a7c36ccf0a1/", '
                    '"listing_uuid": "ac0df37b468f4757aebf3a7c36ccf0a1"')))
    ok &= check("...and a hex the line never justified still fails",
                ci.HEX32.findall(ci._without_site_ids(
                    'key = "%s"' % ("deadbeefdeadbeef" * 2))))

    # AND IT PASSES ON THIS REPO. A check that is always red teaches everyone
    # to ignore checks; this one WAS red, on six documented placeholders.
    done = subprocess.run([sys.executable, script, "--all"],
                          cwd=REPO_ROOT, capture_output=True, text=True)
    ok &= check("ci_checks.py --all passes on this repo (exit %d)" % done.returncode,
                done.returncode == 0)
    if done.returncode != 0:
        print("        " + (done.stdout or done.stderr).strip()[-400:])

    # AND IT STILL CATCHES A REAL ONE. Loosening an allowlist until the check
    # passes is the failure mode here, so both directions are asserted: a
    # planted CDP endpoint, a planted 32-hex key and a planted http proxy URL
    # must all be found. The http one matters most — the inline grep this
    # replaced covered only ws:// and would have missed a committed proxy.
    planted = os.path.join(REPO_ROOT, "_secret_probe_delete_me.py")
    # The key is ASSEMBLED rather than written as a literal, because a
    # 32-character hex string sitting in this file is exactly what the check
    # under test flags — and it did, on the first run of this test. The file
    # it writes still gets the whole thing, which is what the probe needs.
    planted_key = "3f8a1c9e4b7d2065" + "af13ce88b409d752"
    try:
        with open(planted, "w", encoding="utf-8") as f:
            f.write(
                'CDP = "ws://acct-zone-scraping_browser-pid-x:'
                'S3cretPassw0rd@cb.2captcha.com:9222"\n'
                'KEY = "%s"\n'
                'PROXY = "http://acct-zone-custom:S3cretPassw0rd'
                '@na.proxy.2captcha.com:2334"\n' % planted_key)
        caught = subprocess.run([sys.executable, script, "--secret-check"],
                                cwd=REPO_ROOT, capture_output=True, text=True)
        out = caught.stdout + caught.stderr
        ok &= check("a planted secret fails the check", caught.returncode != 0)
        ok &= check("the planted ws:// CDP endpoint is named",
                    "_secret_probe_delete_me.py:1" in out)
        ok &= check("the planted 32-hex key is named",
                    "_secret_probe_delete_me.py:2" in out)
        ok &= check("the planted http:// PROXY url is named (the grep this "
                    "replaced missed those)",
                    "_secret_probe_delete_me.py:3" in out)
    finally:
        # Never leave it behind: a test that mutates the working tree is its
        # own defect, and this one would plant a fake secret.
        if os.path.exists(planted):
            os.remove(planted)
    ok &= check("the probe file is cleaned up", not os.path.exists(planted))

    # The pre-publication scan: the same rules over every blob that has EVER
    # existed. A later commit cannot remove what a published tag and a merged
    # PR's refs already hold, so this has to be runnable BEFORE the repo goes
    # public — and it has to be findable, which a check makes it.
    hist = subprocess.run([sys.executable, script, "--history-check"],
                          cwd=REPO_ROOT, capture_output=True, text=True)
    ok &= check("--history-check runs and this history is clean",
                hist.returncode == 0)
    ok &= check("it says how many objects it looked at",
                "ever existed" in hist.stdout)
    # NOT in --all, on purpose: it shells out to git once per object, and a
    # dirty history needs a decision rather than a red check on every push.
    every = subprocess.run([sys.executable, script, "--all"],
                           cwd=REPO_ROOT, capture_output=True, text=True)
    ok &= check("--all deliberately excludes the history scan",
                "history check" not in every.stdout)
    return ok


def test_no_capture_leaks():
    group("no credentials or personal data in the committed fixtures")
    ok = True
    # Collected by SUFFIX, which is how this file names its fixtures. An
    # earlier version asked for a "FIX_" PREFIX, matched nothing, and every
    # check below passed against an empty string — 150 KB of committed real
    # captures went unexamined while twelve checks reported green. The
    # non-empty assertion underneath is the actual fix: a corpus check that
    # can silently scan nothing is worse than no corpus check at all.
    names = sorted(FIX)
    fixtures = "\n".join(FIX[k]["html"] for k in names)
    ok &= check("the privacy checks below have fixtures to scan "
                "(%d fixtures, %d chars)" % (len(names), len(fixtures)),
                len(names) >= 8 and len(fixtures) > 300000)
    # Guarded with PATTERNS rather than with the literals a previous capture
    # happened to contain, so the NEXT capture is checked too. MediaMarkt's
    # pages embed a front-end configuration blob — a Sentry DSN, a Woosmap
    # public key, a store-code JWT — none of which is needed to test a
    # parser, and none of which belongs in a public repository.
    patterns = {
        "a JWT": r"eyJ[A-Za-z0-9_\-]{16,}\.[A-Za-z0-9_\-]{10,}",
        "an access token": r"(?:access|auth|bearer)[_\-]?[Tt]oken\"?\s*[:=]\s*\"?[A-Za-z0-9._\-]{12,}",
        # The placeholder is exempt by NAME, not by shape: it is 19
        # characters of the same alphabet a real key uses, so a shape-only
        # rule flags the very substitution that makes the fixture safe.
        "an API key": r"(?:api|public|secret|private)[_\-]?[Kk]ey\"?\s*[:=]\s*\"?(?!REDACTED-SEARCH-KEY)[A-Za-z0-9._\-]{12,}",
        "a Sentry DSN": r"https://[0-9a-f]{16,}@[\w.]*ingest",
        "a session id": r"session[_\-]?[Ii]d\"?\s*[:=]\s*\"?[A-Za-z0-9._\-]{8,}",
        "an email address": r"[\w.+-]+@[\w-]+\.[a-z]{2,}",
        "a proxy credential": r"://[^\s/@\"]+:[^\s/@\"]+@",
    }
    for label, pattern in patterns.items():
        hits = re.findall(pattern, fixtures)
        ok &= check("no %s in the fixtures" % label, not hits)

    # The SITE's OWN per-impression material, which a fresh capture brings with
    # it: a click-tracking key, its checksum, and the logging key that ties an
    # impression to a session. Anonymous and expired, and still not something
    # to commit — and a 40-character hex-ish blob in a public repo reads as a
    # credential to every scanner that looks, including this repo's own CI
    # grep. Matched as PATTERNS rather than as the values one capture
    # happened to hold, so the NEXT capture is checked too.
    site_session = {
        "a real estate agent's own name":
            r'"agent_profile"\s*:\s*\{(?:[^{}]|\{[^{}]*\})*?"name"\s*:\s*'
            r'\{"en"\s*:\s*"(?!AGENT NAME REMOVED)[^"]+"',
        "a per-seller UUID":
            r'"user"\s*:\s*\{"id"\s*:\s*"(?!00000000-)[0-9a-f-]{36}"',
        "the page's Algolia search key":
            r'"x-algolia-api-key"\s*:\s*"(?!REDACTED-SEARCH-KEY")[^"]{8,}"',
    }
    for label, pattern in site_session.items():
        hits = re.findall(pattern, fixtures)
        ok &= check("no %s in the fixtures (scrub a new capture before "
                    "committing it)" % label, not hits)

    # The repo-wide grep CI runs, applied here too so a failure is local.
    # Asked of GIT, not of the filesystem. A developer's own `.env` beside
    # the scripts is EXPECTED — it is how the local runs get their key — and
    # `.gitignore` is what keeps it out of the repo. Checking for the file's
    # existence made this red on every machine that had ever run the scraper
    # for real, which is the machine most likely to be running the suite.
    tracked = subprocess.run(
        ["git", "ls-files", "--error-unmatch", ".env"],
        cwd=REPO_ROOT, capture_output=True, text=True).returncode == 0
    ok &= check("no .env file is tracked by git", not tracked)
    return ok


def test_wording():
    group("wording and removed flags")
    ok = True
    # Asked of GIT, so the scan reaches the workflows and the issue
    # templates under .github/ — eight shipped files that an os.listdir of
    # the repo ROOT silently missed, including the four a contributor is
    # most likely to paste marketing wording into. Untracked scratch files
    # and .pytest_cache/ are excluded for free by asking git.
    listed = subprocess.run(["git", "ls-files"], cwd=REPO_ROOT,
                            capture_output=True, text=True)
    if listed.returncode == 0 and listed.stdout.strip():
        shipped = [f for f in listed.stdout.split("\n")
                   if f.endswith((".py", ".md", ".txt", ".toml", ".yml", ".yaml"))
                   and os.path.basename(f) != os.path.basename(__file__)]
    else:  # not a git checkout (a release tarball): fall back to the root
        shipped = [f for f in os.listdir(REPO_ROOT)
                   if f.endswith((".py", ".md", ".txt", ".toml", ".yml", ".yaml"))
                   and f != os.path.basename(__file__)]
    ok &= check("the wording scan reaches beyond the repo root",
                any(os.sep in f or "/" in f for f in shipped))
    for phrase in BANNED_PHRASES:
        offenders = []
        for f in shipped:
            try:
                text = open(os.path.join(REPO_ROOT, f), encoding="utf-8").read()
            except (OSError, UnicodeDecodeError):
                continue
            if phrase.lower() in text.lower():
                offenders.append(f)
        ok &= check("no shipped file says %r" % phrase, not offenders)

    for flag in REMOVED_ENGINE_FLAGS:
        offenders = []
        for f in ENGINE_FILES:
            path = os.path.join(REPO_ROOT, f)
            if not os.path.exists(path):
                continue
            text = open(path, encoding="utf-8").read()
            # A prose mention explaining why the flag does NOT exist is fine
            # and is worth keeping; an argparse registration is not.
            if ('add_argument("%s"' % flag) in text or \
                    ("add_argument('%s'" % flag) in text:
                offenders.append(f)
        ok &= check("no engine registers the removed flag %s" % flag,
                    not offenders)

    # The product this repo integrates with, named correctly.
    readme = os.path.join(REPO_ROOT, "README.md")
    if os.path.exists(readme):
        text = open(readme, encoding="utf-8").read()
        ok &= check("the README names the Scraping Browser API",
                    "Scraping Browser API" in text)
        ok &= check("the README does not name a competitor",
                    not re.search(r"brightdata|oxylabs|smartproxy|zyte|scraperapi\.com",
                                  text, re.IGNORECASE))
    return ok


def test_fingerprint_client_reads_env():
    group("fingerprint_client resolves its key the way the docs promise")
    ok = True
    import fingerprint_client as fpc

    # THE DEFECT THIS PINS, found the first time --fingerprint was run live
    # here and confirmed present in five sibling repos: `--key` defaulted to
    # `os.environ.get("TWOCAPTCHA_KEY")` alone. So a key put in `.env` —
    # which is exactly what §3, the README and .env.example instruct — worked
    # for every engine and failed HERE with "No API key". A documented
    # mechanism not applied on one path, which is the shape of half the
    # defects §16 lists.
    src = inspect.getsource(fpc.main)
    ok &= check("it loads .env itself, rather than hoping an engine did",
                "env_config.load_env()" in src)
    ok &= check("...and reads the key through the family's loader",
                'env_config.env_value("TWOCAPTCHA_KEY")' in src)
    # Through `env_value` and NOT `os.environ.get`, because only the former
    # applies the placeholder rule. Measured both ways with
    # TWOCAPTCHA_KEY=your_2captcha_api_key_here exported: os.environ.get
    # sends the placeholder to the API and the run reports "Fingerprint API
    # rejected the key (401) — note this is a separate subscription", which
    # sends the reader to check a subscription they never needed.
    ok &= check("...not straight from os.environ, which skips the "
                "placeholder rule",
                'os.environ.get("TWOCAPTCHA_KEY")' not in src)
    # Behaviourally, not just by reading the source — and written
    # self-contained so this check is byte-identical in every repo of the
    # family rather than depending on a local helper.
    saved = os.environ.get("TWOCAPTCHA_KEY")
    try:
        os.environ["TWOCAPTCHA_KEY"] = "your_2captcha_api_key_here"
        read_back = env_config.env_value("TWOCAPTCHA_KEY")
    finally:
        if saved is None:
            os.environ.pop("TWOCAPTCHA_KEY", None)
        else:
            os.environ["TWOCAPTCHA_KEY"] = saved
    ok &= check("a placeholder still reads as unset on this path",
                read_back is None)

    # The default must never reach `--help`. argparse prints a default only
    # when the help string asks for it, so this is one substring away from
    # printing a live credential to anyone who types --help.
    ok &= check("the --key help text does not interpolate its default",
                "%(default)s" not in src)
    return ok


def test_fingerprint_application():
    group("a fingerprint is applied as the fingerprint describes it")
    ok = True
    import fingerprint_client as fpc

    ua = fpc.fingerprint_user_agent(FIX_FINGERPRINT)
    # The UA used to be read from `userAgent.value`, a key the API returns in
    # NEITHER format. So --fingerprint silently set no user agent at all and
    # the browser kept its own: a German fingerprint's screen and locale
    # wearing a local Chromium's UA, which is precisely the identity mismatch
    # the flag exists to avoid.
    ok &= check("the user agent is found in the shape the API returns",
                ua and ua.startswith("Mozilla/5.0 (Windows NT 10.0"))
    ok &= check("the `raw` format's ua key is understood too",
                fpc.fingerprint_user_agent({"data": {"ua": "UA/1.0"}}) == "UA/1.0")
    ok &= check("a fingerprint with no user agent yields None, not a crash",
                fpc.fingerprint_user_agent({"country": "DE"}) is None)

    kw = fpc.playwright_context_kwargs(FIX_FINGERPRINT)
    ok &= check("the context carries the fingerprint's user agent",
                kw.get("user_agent") == ua)
    # `locale` used to be built as f"en-{country}", giving "en-NL" for a
    # Dutch fingerprint. An English-speaking visitor in the Netherlands is
    # possible, but it is not what this fingerprint describes, and a locale
    # that contradicts the rest of the identity is the mismatch again. This
    # was one of the six defects a sibling repo inherited from copied core
    # and never ran (§16).
    ok &= check("the locale is the fingerprint's own, not en-<country>",
                kw.get("locale") == "nl-NL")
    ok &= check("the timezone is carried, so the browser cannot contradict it",
                kw.get("timezone_id") == "Europe/Amsterdam")
    # The device pixel ratio, which Playwright takes as its own option and
    # which was dropped on the floor until a live browser was compared
    # against the fingerprint: a fingerprint stating 1.25 produced a browser
    # reporting 1, so the identity contradicted itself on an axis a
    # fingerprinter reads for free.
    ok &= check("the device scale factor is carried",
                kw.get("device_scale_factor") == 1)
    # A viewport exactly equal to the screen is itself a signal, and the
    # fingerprint states its own window size rather than needing one guessed.
    ok &= check("the viewport is the fingerprint's window, not its screen",
                kw.get("viewport") == {"width": 1920, "height": 992}
                and kw.get("screen") == {"width": 1920, "height": 1080})

    # Falling back sensibly when a field is absent, rather than dropping it.
    bare = fpc.playwright_context_kwargs({"country": "FR", "screen":
                                          {"width": 1280, "height": 800}})
    ok &= check("a fingerprint with no intl block still gets a locale",
                bare.get("locale") == "en-FR")
    ok &= check("...and a window smaller than the screen",
                bare["viewport"]["height"] < bare["screen"]["height"])
    ok &= check("a fingerprint with nothing usable yields no kwargs",
                fpc.playwright_context_kwargs({}) == {})

    # Every key this produces must be one Playwright's new_context accepts;
    # an unknown one is a TypeError at launch, on the paid path, at runtime.
    accepted = {"user_agent", "viewport", "screen", "locale", "timezone_id",
                "geolocation", "permissions", "extra_http_headers",
                "device_scale_factor", "is_mobile", "has_touch", "color_scheme"}
    ok &= check("every context kwarg is one Playwright accepts",
                set(kw) <= accepted)
    return ok


def test_credentials_never_reach_a_log():
    group("an API key never reaches a log or an exception message")
    ok = True
    import fingerprint_client as fpc
    import captcha_solver as cs

    # requests puts the FULL URL — query string included — into the text of
    # HTTPError and of every connection error. Both of these modules have an
    # endpoint that takes the key as a query parameter, so an error there
    # echoed a live key to the terminal. It did, once, on a real call.
    # An obviously fake key, and NOT a real one even a revoked one: a
    # 32-hex string in a public repo reads as a live credential to every
    # scanner that looks, including this repo's own CI grep. The word
    # "example" in the name is what tells that grep this line is a fixture.
    example_key = "0123456789abcdef0123456789abcdef"
    for name, module in (("fingerprint_client", fpc), ("captcha_solver", cs)):
        redacted = module._redact(
            "400 Client Error: Bad Request for url: "
            "https://api.2captcha.com/fingerprint/random?format=chromium&"
            "key=%s" % example_key)
        ok &= check("%s redacts a key out of an error message" % name,
                    example_key not in redacted)
        ok &= check("...and keeps the endpoint, which is the useful half",
                    "api.2captcha.com/fingerprint/random" in redacted)
        ok &= check("%s redacts clientKey too" % name,
                    example_key not in module._redact("clientKey=%s" % example_key))
        ok &= check("%s leaves ordinary text alone" % name,
                    module._redact("upstream status 403") == "upstream status 403")
    return ok


def test_concurrent_dispatch(skips):
    group("concurrent page dispatch (threads, stop event, accounting)")
    ok = True
    try:
        import playwright_scraper as eng
    except ImportError as e:
        skips.append("concurrent dispatch (%s)" % e)
        return ok

    # The thread fan-out is the one part of --concurrency that the rest of
    # this suite does not reach, and it is not reachable from a live run in
    # every environment either: page 1 is always fetched alone and decides
    # whether the rest may be addressed, so a blocked page 1 means the
    # workers never start. Driven here with the browser stubbed out, which
    # leaves exactly the concurrency logic under test.
    original = (eng.sync_playwright, eng._BrowserSession, eng._fetch_one_page)

    class Args:
        delay = 0
        mode = "listing"
        out = "x"

    def run(specs, concurrency, rows_for_page, die_on=()):
        fetched, lock = [], threading.Lock()

        def fake_fetch(session, args, pool, page_num, url):
            with lock:
                fetched.append(page_num)
            if page_num in die_on:
                raise RuntimeError("worker blew up on page %d" % page_num)
            outcome = eng.PageOutcome(page_num=page_num, url=url)
            outcome.products = rows_for_page(page_num)
            return outcome

        eng.sync_playwright = lambda: _FakePlaywright()
        eng._BrowserSession = lambda pw, args, pool, **kw: _FakeSession(pool)
        eng._fetch_one_page = fake_fetch
        try:
            results, unattempted, exhausted = eng._fetch_pages_concurrently(
                Args(), None, specs, concurrency)
        finally:
            (eng.sync_playwright, eng._BrowserSession,
             eng._fetch_one_page) = original
        return fetched, results, unattempted, exhausted

    # 1. Every page fetched exactly once, whatever the worker count.
    specs = [(n, "u%d" % n) for n in range(2, 12)]
    fetched, results, unattempted, exhausted = run(
        specs, 4, lambda n: ["row"])
    ok &= check("every queued page is fetched exactly once",
                sorted(fetched) == [n for n, _ in specs])
    ok &= check("every page produces an outcome",
                sorted(o.page_num for o in results) == [n for n, _ in specs])
    ok &= check("nothing is left unattempted when the listing does not end",
                unattempted == [] and not exhausted)

    # 2. Results arrive in whatever order the threads finish, which is
    #    exactly why the caller merges by page number instead of by arrival.
    #    Sorting them must reconstruct the page order.
    ok &= check("outcomes can be put back into page order",
                [o.page_num for o in sorted(results, key=lambda o: o.page_num)]
                == [n for n, _ in specs])

    # 3. The stop event. Asking for 50 pages of a listing that ends at page 5
    #    must not fetch 45 empty ones: workers check the event before taking
    #    more work, so at most (concurrency - 1) extra are already in flight.
    specs = [(n, "u%d" % n) for n in range(2, 51)]
    fetched, results, unattempted, exhausted = run(
        specs, 3, lambda n: [] if n >= 5 else ["row"])
    ok &= check("the end of the listing stops dispatch", exhausted)
    ok &= check("an exhausted listing costs at most (concurrency-1) extra "
                "fetches (%d fetched of 49 queued)" % len(fetched),
                len(fetched) <= 4 + 3)
    ok &= check("the pages never tried are reported, not counted as failed",
                unattempted and all(o.ok for o in results))
    ok &= check("unattempted pages are reported in order",
                unattempted == sorted(unattempted))

    # 4. A worker that dies must not hang the run, and must not swallow the
    #    pages its siblings did fetch.
    specs = [(n, "u%d" % n) for n in range(2, 8)]
    fetched, results, unattempted, exhausted = run(
        specs, 3, lambda n: ["row"], die_on={3})
    ok &= check("a worker that raises does not hang the run",
                len(results) + len(unattempted) + 1 >= len(specs))
    ok &= check("the pages other workers fetched still come back",
                any(o.page_num != 3 for o in results))
    return ok


def test_no_undefined_names():
    group("no engine references a name that does not exist")
    ok = True
    # This exists because of a bug that got all the way to a live run.
    # puppeteer_scraper.py called `detect_page_state(...)` on a line reached
    # only while fetching a page, after the import of that name had been
    # removed. The module imported fine, `--help` worked, `compileall`
    # passed, the whole offline suite passed and CI was green — and the
    # engine died with NameError on its first real page.
    #
    # Byte-compiling proves a file PARSES. It says nothing about whether the
    # names in it resolve, and the paths where they do not are exactly the
    # ones an offline suite cannot execute.
    for name in sorted(f for f in os.listdir(REPO_ROOT) if f.endswith(".py")):
        missing = _undefined_names(os.path.join(REPO_ROOT, name))
        detail = ", ".join("%s (line %d)" % (k, v[0])
                           for k, v in sorted(missing.items()))
        ok &= check("%s references no undefined name%s"
                    % (name, ": " + detail if missing else ""), not missing)

    # And a statement that can never RUN — see `_unreachable_statements`.
    for name in sorted(f for f in os.listdir(REPO_ROOT) if f.endswith(".py")):
        dead = _unreachable_statements(os.path.join(REPO_ROOT, name))
        ok &= check("%s has no statement the control flow can never reach%s"
                    % (name, "" if not dead else ": line %d" % dead[0]),
                    not dead)

    return ok


def test_dockerfile_copies_what_it_runs():
    group("the Docker image contains every module its entrypoint imports")
    ok = True
    path = os.path.join(REPO_ROOT, "Dockerfile")
    if not os.path.exists(path):
        return check("Dockerfile exists", False)

    # The Dockerfile COPYs an explicit list rather than the whole directory,
    # which is right — the image should not carry the test suite, the
    # fixtures or a stray .env. The cost is that the list can fall behind the
    # imports, and NOTHING else in this repo would notice: CI never builds
    # the image, so a missing module ships and the container dies with
    # ModuleNotFoundError on every invocation, `--help` included.
    #
    # That is not hypothetical. `proxy_pool.py` was missing from this list,
    # and playwright_scraper.py imports it at module level.
    raw = open(path, encoding="utf-8").read()
    joined = re.sub(r"\\\n\s*", " ", raw)          # fold line continuations
    copied = set()
    for line in joined.splitlines():
        if line.startswith("COPY "):
            copied.update(tok for tok in line.split() if tok.endswith(".py"))

    entrypoint = None
    m = re.search(r'ENTRYPOINT\s*\[([^\]]*)\]', joined)
    if m:
        parts = [x.strip().strip('"\'') for x in m.group(1).split(",")]
        entrypoint = next((x for x in parts if x.endswith(".py")), None)
    ok &= check("the Dockerfile names a Python entrypoint", bool(entrypoint))
    if not entrypoint:
        return False
    ok &= check("the entrypoint itself is copied into the image",
                entrypoint in copied)

    # Every LOCAL module the entrypoint reaches, transitively.
    local = {f[:-3] for f in os.listdir(REPO_ROOT) if f.endswith(".py")}

    def reached(module, seen=None):
        seen = seen if seen is not None else set()
        if module in seen:
            return seen
        seen.add(module)
        tree = ast.parse(open(os.path.join(REPO_ROOT, module + ".py"),
                              encoding="utf-8").read())
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name.split(".")[0] for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module.split(".")[0]]
            for name in names:
                if name in local:
                    reached(name, seen)
        return seen

    needed = reached(entrypoint[:-3])
    missing = sorted(m + ".py" for m in needed if (m + ".py") not in copied)
    ok &= check("every module the entrypoint imports is COPYed (%s)"
                % (", ".join(missing) if missing else "none missing"),
                not missing)

    # The other direction is a warning, not a failure: diff_runs.py is copied
    # deliberately as a companion tool even though the engine never imports
    # it. But anything copied must at least still EXIST.
    gone = sorted(f for f in copied
                  if not os.path.exists(os.path.join(REPO_ROOT, f)))
    ok &= check("the Dockerfile copies no file that has been deleted (%s)"
                % (", ".join(gone) if gone else "none"), not gone)
    return ok


def test_sample_output():
    group("sample_output is cut from a real run")
    ok = True
    path = os.path.join(REPO_ROOT, "sample_output.json")
    if not os.path.exists(path):
        return check("sample_output.json exists", False)
    rows = json.load(open(path, encoding="utf-8"))
    ok &= check("the sample has rows", len(rows) > 0)
    names = [f.name for f in fields(Product)]
    ok &= check("its columns match the Product schema exactly",
                all(set(r) == set(names) for r in rows))
    text = json.dumps(rows, ensure_ascii=False)
    ok &= check("the sample carries no fabrication markers",
                not re.search(r"example\.com|lorem ipsum|FIXME|TODO|XXXX",
                              text, re.IGNORECASE))
    ok &= check("every sample row's sku is a listing PATH, as this schema "
                "defines it",
                all((r.get("sku") or "").startswith("/") for r in rows))
    ok &= check("every sample row names the storefront it came from",
                all((r.get("source") or "") in ("dubizzle.com",) + HOSTS
                    for r in rows))
    ok &= check("every sample row's URL is an ad on an emirate subdomain — "
                "the address the site itself publishes, not one rebuilt from "
                "the browse host",
                all(re.match(r"https://[a-z]+\.dubizzle\.com/", r.get("url") or "")
                    and "uae.dubizzle.com" not in (r.get("url") or "")
                    for r in rows))
    ok &= check("no sample URL carries a tracking tail",
                not [r for r in rows if "utm_" in (r.get("url") or "")])
    # A sample of organic rows alone would hide the promoted slot, which is
    # the one row a consumer most needs to be able to recognise: it repeats
    # on every page of a run and is not a result.
    ok &= check("the sample shows both kinds of row it can produce",
                {r.get("listing_kind") for r in rows}
                == {"organic", "car_of_the_week"})
    ok &= check("the sample shows what the JSON-LD enrichment adds",
                any(r.get("brand") for r in rows))
    ok &= check("and what only the payload has",
                all(r.get("listing_id") and r.get("attributes") for r in rows))
    ok &= check("every priced sample row carries its currency, and no "
                "unpriced row carries one",
                all((r.get("price") is None) == (r.get("currency") is None)
                    for r in rows))
    ok &= check("no sample row has an original_price at or below its price",
                not [r for r in rows
                     if r.get("original_price") is not None
                     and r.get("price") is not None
                     and r["original_price"] <= r["price"]])
    ok &= check("the sample shows a real price_source",
                {r.get("price_source") for r in rows}
                <= {"next_data+jsonld", "next_data", "dom",
                    "next_data+jsonld:no-price", "next_data:no-price",
                    "dom:no-price"}
                and any(r.get("price_source") for r in rows))

    csv_path = os.path.join(REPO_ROOT, "sample_output.csv")
    if os.path.exists(csv_path):
        header = open(csv_path, encoding="utf-8").read().split("\n")[0]
        ok &= check("the sample CSV header matches the schema",
                    header.strip().split(",") == names)
    return ok


def test_captcha():
    group("captcha detection and reconciliation")
    ok = True
    from captcha_solver import CaptchaChallenge

    # Format 2: the site's own wrapper element carries the config as
    # attributes, with the execute() call inside a bundled file that never
    # appears as readable inline script.
    widget = ('<captcha-widget data-captcha-type="recaptcha" data-version="v3" '
              'data-sitekey="6LcABCDEFGHIJKLMNOPQRSTUVWXYZ0123" '
              'data-action="submit"></captcha-widget>')
    c = detect_recaptcha_v3(widget, "https://uae.dubizzle.com/")
    ok &= check("a captcha-widget declaring v3 is detected",
                c is not None and c.kind == "recaptcha_v3")

    # A sitekey is at least 20 characters; a short string next to
    # data-sitekey is not one, and treating it as one would send a malformed
    # task to the API and bill for the answer.
    ok &= check("a too-short sitekey is not accepted as a challenge",
                detect_recaptcha_v3('<div data-sitekey="short" '
                                    'class="g-recaptcha"></div>',
                                    "https://uae.dubizzle.com/") is None)
    ok &= check("a page with no reCAPTCHA at all is not a challenge",
                detect_recaptcha_v3(fx("motors_p1")[0], LISTING_URL) is None)

    # THE LOADER WINS. A site's own wrapper can declare v3 while the Google
    # loader it actually ships is the v2-invisible signature
    # (render=explicit, size=invisible, a bframe challenge iframe). v3
    # parameters sent for a v2-invisible widget buy a token the site
    # rejects — so the runtime reading is authoritative and the two
    # detectors are reconciled rather than short-circuited.
    static_v3 = CaptchaChallenge(kind="recaptcha_v3", sitekey="6LcABC" + "X" * 20,
                                 action="submit", source="html")
    runtime_v2 = CaptchaChallenge(kind="recaptcha_v2_invisible",
                                  sitekey="6LcABC" + "X" * 20,
                                  source="runtime", size="invisible")
    merged = reconcile_detections(static_v3, runtime_v2)
    ok &= check("when the detectors disagree, the live loader wins",
                merged is not None and merged.kind == "recaptcha_v2_invisible")
    ok &= check("...and the real action from the static markup is kept",
                merged.action == "submit")
    ok &= check("one detector alone is still used when only it fires",
                reconcile_detections(static_v3, None) is static_v3
                and reconcile_detections(None, runtime_v2) is runtime_v2)
    ok &= check("neither firing means no challenge",
                reconcile_detections(None, None) is None)

    # Deliberately absent: no solver for a first-party image captcha. This
    # site has no such page — measured 2026-09-10, its refusal is not a page
    # at all: the HTTP/2 stream is reset and nothing arrives — so a solver
    # for one would be dead code that looks load-bearing. Pinned so that
    # reintroducing it is a decision rather than a drift.
    import captcha_solver
    ok &= check("no first-party image-captcha solver was ported",
                not [n for n in dir(captcha_solver)
                     if "image" in n.lower() and "captcha" in n.lower()])
    # ...while the DETECTORS stay broad, which is the family's standing
    # policy: which challenge a visitor meets depends on the exit country and
    # on what the address has been doing.
    # The marker set is deliberately SHORT on this site, and shorter than
    # the sibling repos'. None of these has ever been observed here —
    # a refused request is not a page at all — so every entry is a guess
    # about a bot manager that might be switched on between deploys, and a
    # long list of guesses is not better than a short one.
    #
    # `cf-turnstile` is specifically EXCLUDED: the Scraping Browser's
    # auto-solve extension injects a cf-turnstile hunter into every page it
    # loads, so it would fire on good pages fetched over --cdp-endpoint.
    # Pinned in both directions so a broadening edit is a decision.
    markers = {m.lower() for m in product_parser.BOT_CHALLENGE_MARKERS}
    ok &= check("detection covers the shapes a challenge here would take",
                {"recaptcha/api2/anchor", "recaptcha/api2/bframe",
                 "recaptcha/api.js", "data-sitekey",
                 "hcaptcha.com/captcha"} <= markers)
    # The other half of the question §18 asks: "did we meet a challenge" and
    # "is one configured, and would we recognise it" are different, and here
    # the answers are no and yes. This site ships its own captcha mount point
    # EMPTY on every page it serves — `<captcha-widgets></captcha-widgets>`,
    # 1 occurrence on all 15 captures, good pages included — so a bare tag
    # marker would make every page a challenge. What a rendered challenge
    # looks like is the same element with something INSIDE it, which is a
    # structural question and is asked by a function.
    ok &= check("the bare <captcha-widgets> tag is NOT a marker — it is on "
                "every good page",
                not any("captcha-widgets" in m for m in markers))
    ok &= check("an EMPTY mount point is not a challenge",
                not product_parser.captcha_mount_is_populated(
                    "<captcha-widgets></captcha-widgets>"))
    ok &= check("a POPULATED one is",
                product_parser.captcha_mount_is_populated(
                    '<captcha-widgets><div data-sitekey="6Le"></div>'
                    '</captcha-widgets>'))
    ok &= check("and no page dubizzle served carries a populated one",
                not any(product_parser.captcha_mount_is_populated(fx(n)[0])
                        for n in ("motors_p1", "property_rent", "classified",
                                  "jobs", "community")))
    # WHICH captcha this site uses, found by reading its own bundles rather
    # than by guessing from a vendor list (§18). Google reCAPTCHA, on the
    # login / phone-verification flow, never on a listing.
    # Against the raw tuple, not the lowercased set above: a reCAPTCHA key
    # is case-sensitive and the page emits it verbatim, so the marker has to
    # keep its case.
    ok &= check("the site's own reCAPTCHA key is known and is a marker",
                product_parser.RECAPTCHA_SITE_KEY.startswith("6L")
                and product_parser.RECAPTCHA_SITE_KEY
                in product_parser.BOT_CHALLENGE_MARKERS)
    ok &= check("...and it is safe to be one: absent from every fixture, so "
                "seeing it means the site rendered its own challenge",
                not any(product_parser.RECAPTCHA_SITE_KEY in FIX[n]["html"]
                        for n in FIX))
    ok &= check("a page carrying it IS a challenge",
                product_parser.detect_bot_challenge(
                    '<div data-sitekey="%s"></div>'
                    % product_parser.RECAPTCHA_SITE_KEY) is not None)
    # The version is deliberately NOT claimed anywhere: the loader that would
    # settle v2 against v3 lives in a separate auth application that a
    # listing page never loads, so nothing this scraper fetches can observe
    # it. Pinned so that nobody later writes a guess into the docs.
    for name in ("README.md", "product_parser.py"):
        text = open(os.path.join(REPO_ROOT, name), encoding="utf-8").read()
        lowered = text.lower()
        ok &= check("%s does not claim a reCAPTCHA version it cannot see"
                    % name,
                    "recaptcha v2" not in lowered
                    and "recaptcha v3" not in lowered)

    # The extension's own hunters reference turnstile, arkoselabs and
    # recaptcha on EVERY page fetched over --cdp-endpoint: 3, 2 and 2
    # occurrences on a good motors page, and 0 of each after the extension
    # <script> tags are stripped. This guard is load-bearing here, unlike in
    # two sibling repos where the same code would be dead (§8).
    ok &= check("stripping extension scripts removes every one of their "
                "markers from a good page",
                product_parser.detect_bot_challenge(fx("motors_p1")[0]) is None)
    # `cf-turnstile` is specifically EXCLUDED as a bare marker: the Scraping
    # Browser's auto-solve extension injects a turnstile hunter into every
    # page it loads, so a bare match would fire on good pages fetched over
    # --cdp-endpoint. The full challenge host is fine, since the extension
    # does not reference it.
    ok &= check("...and no bare cf-turnstile marker, which our own extension "
                "would trip",
                "cf-turnstile" not in markers)
    # Refusals are a SEPARATE set: `--solve-captcha` must not pay for a page
    # that carries nothing to solve.
    block = {m.lower() for m in product_parser.BLOCK_MARKERS}
    ok &= check("the refusal markers are their own set and do not overlap",
                block and not (block & markers))
    return ok


def _placeholder_reads_unset(raw):
    """Whether env_config would treat `raw` as "not configured".

    Goes through the real rule — `env_config.env_value`, which is where the
    placeholder logic lives — rather than reimplementing it, because a
    reimplementation is what drifts. The variable is set in os.environ
    directly and restored afterwards: `load_env` only fills variables that
    are not already set, so writing a temporary .env would be shadowed by
    whatever the suite has already loaded.
    """
    name = "CATAWIKI_CDP_ENDPOINT"
    saved = os.environ.get(name)
    try:
        os.environ[name] = raw
        with io.StringIO() as buf, redirect_stdout(buf):
            value = env_config.env_value(name)
    finally:
        if saved is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = saved
    return value is None


def test_env_config():
    group("env_config")
    ok = True
    ok &= check("the env keys are this site's, not another repo's",
                set(env_config.ENV_KEYS) ==
                {"TWOCAPTCHA_KEY", "DUBIZZLE_CDP_ENDPOINT",
                 "DUBIZZLE_PROXY", "DUBIZZLE_URL"})

    # .env.example must document exactly the variables the code reads, in
    # both directions. It drifts otherwise, and a documented-but-unread
    # variable is worse than an undocumented one.
    example = os.path.join(REPO_ROOT, ".env.example")
    documented = set()
    if os.path.exists(example):
        for line in open(example, encoding="utf-8"):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                documented.add(line.split("=", 1)[0].strip())
    ok &= check(".env.example documents exactly the variables the code reads",
                documented == set(env_config.ENV_KEYS))

    # A variable mapped onto a flag with a non-empty default would be
    # silently inert, because the loader only fills UNSET values: a setting
    # that looks configurable and is not.
    ok &= check("no env variable is mapped onto --out (it has a default)",
                "out" not in env_config.ENV_KEYS.values())

    # THE ROUND TRIP, on the file this repo actually ships rather than on
    # strings written here. `cp .env.example .env` and run: every credential
    # must read as unset, and the one non-credential default must survive.
    #
    # Hand-written placeholder strings are not enough, and that is why this
    # exists: a sibling repo's literal-only check passed while the SHIPPED
    # example's two credentialled URLs read as CONFIGURED, so a copied
    # example connected to cb.2captcha.com with `{login}-zone-…` as its
    # username and got a 401 a long way from its cause.
    with tempfile.TemporaryDirectory() as d:
        copied = os.path.join(d, ".env")
        with open(os.path.join(REPO_ROOT, ".env.example"), encoding="utf-8") as src:
            example_text = src.read()
        with open(copied, "w", encoding="utf-8") as dst:
            dst.write(example_text)

        class Copied:
            twocaptcha_key = None
            url = None
            cdp_endpoint = None
            proxy = None

        saved = {k: os.environ.pop(k, None) for k in env_config.ENV_KEYS}
        try:
            args = Copied()
            env_config.load_env(copied)
            env_config.apply(args, quiet=True)
            ok &= check("a copied .env.example leaves every CREDENTIAL unset",
                        args.twocaptcha_key is None
                        and args.cdp_endpoint is None
                        and args.proxy is None)
            ok &= check("...and leaves the target URL usable, so a copied "
                        "example still runs",
                        isinstance(args.url, str)
                        and args.url.startswith("https://uae.dubizzle.com/"))
            ok &= check("the example names no variable the loader does not "
                        "read", not env_config.unknown_keys(copied))
        finally:
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v

    # And the placeholder shapes themselves, pinned so a future example that
    # writes a credential differently is still caught. Any value carrying
    # `{...}` braces is unset, whatever else it looks like.
    for raw in ('ws://{login}-zone-scraping_browser-country-ae-pid-'
                '{profileId}:{password}@cb.2captcha.com:9222',
                'http://{user}:{password}@ae.proxy.2captcha.com:2334',
                'your_2captcha_api_key_here'):
        ok &= check("a placeholder value reads as unset: %s..." % raw[:34],
                    _placeholder_reads_unset(raw))
    # ...and a REAL value still reads as set, or the guard has eaten the
    # feature it was protecting.
    ok &= check("a real value is not mistaken for a placeholder",
                _placeholder_reads_unset(
                    "ws://acct1-zone-scraping_browser-country-ae-pid-p1:"
                    "secret@cb.2captcha.com:9222") is False)
    ok &= check("the example's default URL is usable as-is",
                _placeholder_reads_unset(
                    "https://uae.dubizzle.com/motors/used-cars/") is False)

    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, ".env")
        with open(path, "w", encoding="utf-8") as f:
            f.write("TWOCAPTCHA_KEY=fromfile\n")
            f.write("DUBIZZLE_URL=https://uae.dubizzle.com/classified/electronics/televisions/\n")
            f.write("NOT_A_REAL_KEY=1\n")

        class A:
            twocaptcha_key = None
            url = None
            cdp_endpoint = None
            proxy = None

        a = A()
        env_config.load_env(path)
        env_config.apply(a, quiet=True)
        ok &= check("a value in .env fills an unset flag",
                    a.twocaptcha_key == "fromfile")

        b = A()
        b.twocaptcha_key = "fromflag"
        env_config.apply(b, quiet=True)
        # A .env must never override something the caller typed.
        ok &= check("an explicit flag beats .env", b.twocaptcha_key == "fromflag")
        # A typo is REPORTED rather than silently ignored.
        ok &= check("an unrecognised variable in .env is reported",
                    "NOT_A_REAL_KEY" in env_config.unknown_keys(path))
    return ok


def test_proxy_pool():
    group("proxy_pool: credentials never reach argv or logs")
    ok = True
    url = "http://user:secret@eu.proxy.2captcha.com:2334"
    masked = mask(url)
    ok &= check("credentials are masked in logs", "secret" not in masked)
    # The host and port are KEPT: which exit a run used is the point of the
    # log and is not the secret.
    ok &= check("...but the host and port survive masking",
                "eu.proxy.2captcha.com:2334" in masked)

    pw = to_playwright(url)
    # A `--proxy-server=` value becomes part of the browser's command line,
    # readable by anything that can run `ps`. The credentials go through the
    # driver's own fields instead.
    ok &= check("the server string handed to the browser has no credentials",
                "secret" not in pw["server"])
    ok &= check("credentials go through the driver's own fields",
                pw["username"] == "user" and pw["password"] == "secret")

    scrubbed, creds = split_credentials(url)
    ok &= check("split_credentials separates the two",
                scrubbed == "http://eu.proxy.2captcha.com:2334"
                and creds == ("user", "secret"))

    pool = ProxyPool(["http://a:1", "http://b:2", "http://c:3"])
    ok &= check("a pool reports its size", len(pool) == 3)
    first = pool.current
    pool.advance("test")
    ok &= check("advancing moves to another exit", pool.current != first)
    # `.proxies` hands back a COPY, so a worker building its own pool from it
    # cannot mutate the parent's list. Two threads sharing one mutable list
    # is the bug that makes concurrency stop being worth it.
    copy = pool.proxies
    copy.append("http://d:4")
    ok &= check("the pool hands out a copy of its exits, not the list itself",
                len(pool) == 3)

    # Workers start on DIFFERENT exits, each with its own pool object, so no
    # thread needs a lock: the concurrency is safe by construction rather
    # than by discipline. Tested through the engine's own helper, because
    # that is where the offset actually lives.
    try:
        import playwright_scraper
    except ImportError:
        playwright_scraper = None
    if playwright_scraper is not None:
        exits = [playwright_scraper._worker_pool(pool, i).current
                 for i in range(3)]
        ok &= check("three workers start on three different exits",
                    len(set(exits)) == 3)
        ok &= check("a worker with no pool gets none",
                    playwright_scraper._worker_pool(None, 0) is None)

    # A pool of one is legal and must not rotate itself into an index error.
    one = ProxyPool(["http://only:1"])
    one.advance("nowhere else to go")
    ok &= check("a single-exit pool survives a rotation",
                one.current == "http://only:1")
    ok &= check("an empty pool is refused rather than silently accepted",
                _raises(lambda: ProxyPool([])))

    # This used to assert that "http://host:port:login:pass" — a line from a
    # proxy LIST FILE — "is understood", checking only that parse_proxy_line
    # did not reject it. It returned the string unchanged, so the check
    # passed; the value was never usable, and it blew up several calls later.
    # A test that asserts a function did not complain is not a test that its
    # answer was right.
    #
    # A proxy LIST FILE line pasted where a proxy URL belongs. This is the
    # mistake a new user makes — the file format is
    # scheme://host:port:login:password and the flag wants
    # http://login:password@host:port — and it reached a real CI run.
    #
    # It used to sail through parse_proxy_line (which never looked at the
    # port) and blow up much later inside to_playwright as an uncaught
    # ValueError: exit 1, a crash, where it should be exit 2, bad usage. And
    # the traceback printed the login AND the password into a public CI log.
    from proxy_pool import ProxyError
    pasted = ("http://eu.proxy.2captcha.com:2334:"
              "SOMELOGIN-zone-custom-region-de:SOMEPASSWORD")
    raised = None
    try:
        parse_proxy_line(pasted, source="CATAWIKI_PROXY")
    except ProxyError as exc:
        raised = str(exc)
    ok &= check("a proxy-list line pasted as a URL is refused, not crashed on",
                raised is not None)
    ok &= check("...and the refusal says what the value should look like",
                raised is not None and "login:password@host:port" in raised)
    ok &= check("...and neither the login nor the password is in the message",
                raised is not None
                and "SOMEPASSWORD" not in raised and "SOMELOGIN" not in raised)

    # mask() is the last thing standing between a password and a log, and it
    # is called precisely when the value is already wrong. It read
    # `parsed.port`, which urlparse computes lazily and which RAISES on a
    # malformed authority — so the masker blew up on exactly the input that
    # most needed masking. A masker that raises is worse than a vague one.
    ok &= check("mask() does not raise on a malformed URL",
                "SOMEPASSWORD" not in mask(pasted))
    for junk in ("::::", "not a url", "http://", "://x", ""):
        try:
            mask(junk)
            raised_here = False
        except Exception:
            raised_here = True
        ok &= check("mask(%r) does not raise" % junk, not raised_here)
    ok &= check("mask() still keeps host and port on a good URL",
                mask("http://u:p@h.example:8080") == "http://***:***@h.example:8080")

    # PINNED LIMITATION, not a defence. mask() takes a bare URL; given a
    # SENTENCE containing one it returns "?://?" — the password is gone,
    # which is the property that matters, but so is the host and port the log
    # was written to show. Every caller here passes the URL as its own `%s`
    # argument for that reason, and `_mask_credentials()` is what handles
    # arbitrary text. Asserting the CURRENT behaviour makes a future swap a
    # failing check rather than an unreadable log (§10).
    sentence = "a http://u:supersecret@h.example:8080 b"
    ok &= check("mask() on a sentence loses the host — the documented limit",
                mask(sentence) == "?://?")
    ok &= check("...but never the password", "supersecret" not in mask(sentence))
    ok &= check("every mask() call site passes a bare URL, not a sentence",
                not [ln for f in ("playwright_scraper.py", "puppeteer_scraper.py",
                                  "selenium_scraper.py", "proxy_pool.py")
                     for ln in open(os.path.join(REPO_ROOT, f),
                                    encoding="utf-8").read().split("\n")
                     if re.search(r'[^_]mask\(f?["\']', ln)])
    # ...and the engines' own masker handles a sentence, globally. A masker
    # that fixes the first occurrence and prints the password the other four
    # times looks exactly like one that works.
    for name in _ENGINE_MODULES:
        try:
            mod = __import__(name)
        except ImportError:
            continue
        many = ("x ws://u:supersecret@h:1 y ws://u:supersecret@h:1 "
                "z http://u:supersecret@h:2")
        out = mod._mask_credentials(many)
        ok &= check("%s._mask_credentials masks EVERY occurrence" % name,
                    "supersecret" not in out)
        ok &= check("%s._mask_credentials keeps the surrounding text" % name,
                    out.startswith("x ") and out.endswith(":2")
                    and "h:1" in out)
    return ok


# The three engines. Playwright is primary; the other two exist for parity
# and are demoted in priority, not in correctness — all three must agree on
# exit codes, run status, and whether a run crashes or spends money.
_ENGINE_MODULES = ("playwright_scraper", "puppeteer_scraper",
                   "selenium_scraper")


# Stand-ins for a real browser session, so the concurrency machinery can be
# driven with the browser stubbed out. A live run cannot always reach it:
# page 1 is fetched alone and decides whether the rest may be addressed, so a
# blocked page 1 means the workers never start.
class _FakeSession:
    """Stands in for a _BrowserSession: opened, closed, carries a pool."""

    def __init__(self, pool=None):
        self.pool = pool
        self.closed = False

    def open(self):
        return self

    def close(self):
        self.closed = True


class _FakePlaywright:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


STATE_POLICY_NAMES = ("content", "empty", "blocked", "challenge", "unknown")


def test_engines(skips):
    group("engines: all three must behave identically")
    ok = True
    loaded = {}
    for name in _ENGINE_MODULES:
        try:
            loaded[name] = __import__(name)
        except ImportError as e:
            # Reported, never swallowed: "skipped, engine absent" reads
            # exactly like a passing run, and CI's engine-smoke job fails if
            # this list is non-empty.
            skips.append("%s (%s)" % (name, e))

    for name, mod in loaded.items():
        ok &= check("%s exposes scrape() and parse_args()" % name,
                    hasattr(mod, "scrape") and hasattr(mod, "parse_args"))
        # The engines must reach the shared policy rather than carry copies.
        src = inspect.getsource(mod)
        ok &= check("%s takes its readiness policy from page_flow" % name,
                    "page_flow.ready_selector" in src)
        ok &= check("%s takes its state policy from page_flow" % name,
                    "page_flow.should_retry" in src or "page_flow.classify" in src)
        # NO scroll anywhere, and it is asserted rather than merely absent:
        # three scrolls to the document's own bottom added zero cards and left
        # the page height unchanged on all three page kinds, so an engine that
        # grew a loop back would be buying latency for nothing -- and would
        # disagree with its twins about what a settled page is. A sibling repo
        # cannot see one product without the scroll, which is exactly why this
        # was measured here instead of ported.
        ok &= check("%s has no scroll loop of its own" % name,
                    "scroll_until_settled" not in src
                    and "scroll_to_bottom" not in src)
        ok &= check("%s passes no JS across the page_flow boundary" % name,
                    "page_flow.wait_for_count" in src)
        # "Not painted yet" is not a fault, and every engine has to make that
        # distinction the same way — the first live search run of the
        # Playwright engine reported 0 rows and exit 4 because it did not.
        # §17's rule, applied to this repo's own additions: a policy
        # constant or a helper that nothing consults is the same defect as
        # dead code, and harder to see because the prose reads like
        # enforcement. All three of these were written, documented in the
        # README, and called by nothing until this check was added.
        ok &= check("%s shows the block advice rather than only computing it"
                    % name, "page_flow.block_advice" in src)
        ok &= check("%s reports the pages beyond the site's own cap" % name,
                    "parser_page_cap" in src and "deeper than the site will" in src)
        ok &= check("%s passes the page's own ad count to the readiness "
                    "threshold, so a short last page does not time out" % name,
                    "page_flow.expected_cards" in src)
        ok &= check("%s waits for an unpainted page instead of retrying it"
                    % name, "page_flow.is_unpainted" in src)
        # Credentials never reach a log, in any engine.
        ok &= check("%s masks credentials globally, not just once" % name,
                    "pass@" not in mod._mask_credentials(
                        "a ws://user:pass@h:1/ b ws://user:pass@h:1/"))
        ok &= check("%s refuses a host it does not read, with the reason" % name,
                    "is_supported_host" in src)
        # The same modes in every engine — a mode one engine offers and
        # another does not is the drift page_flow.py and finish_run() exist
        # to prevent, one level up. There is exactly ONE here: an individual
        # ad's markup has not been measured, and a mode that ships untested
        # is worse than one that is absent.
        ok &= check("%s offers exactly one mode, listing" % name,
                    'choices=["listing"]' in src)
        ok &= check("%s has no unmeasured detail mode" % name,
                    '"product"]' not in src and '"lot"]' not in src)
        # HEADFUL is the default here, against headless in every sibling: a
        # headless browser is refused with HTTP 403 from every address tried,
        # residential included, while a real window is served from the same
        # ones. A --headless default would be a scraper whose default cannot
        # fetch the site.
        # HEADLESS is the default, as in the rest of the family: every live
        # run of this repo was headless and was served. What was refused was
        # a non-UAE address, headful and headless alike — the discriminator
        # here is the exit's country, not the window.
        ok &= check("%s defaults to headless, like the rest of the family"
                    % name,
                    'dest="headless", action="store_true",' in src
                    and "default=True" in src)

    # THE FLAG CONTRACT, and the exact ways the engines differ from it.
    #
    # §9 lists the flags every engine must offer. Checked rather than
    # trusted, because a flag one engine has and another does not is the
    # drift page_flow.py and finish_run() exist to prevent, one level up —
    # and because the README documents these differences by name, so a new
    # divergence has to update the README or fail here.
    contract = ("--url --pages --category --format --out --delay --retries "
                "--retry-delay --concurrency --proxy --proxy-file "
                "--proxy-rotate --proxy-shuffle --proxy-block-retries "
                "--twocaptcha-key --captcha-api --solve-captcha --min-score "
                "--cdp-endpoint --allow-empty --dump-html").split()
    flags = {}
    for name in _ENGINE_MODULES:
        path = os.path.join(REPO_ROOT, name + ".py")
        if not os.path.exists(path):
            continue
        src = open(path, encoding="utf-8").read()
        flags[name] = set(re.findall(r'add_argument\(\s*"(--[a-z-]+)"', src))
        missing = [f for f in contract if f not in flags[name]]
        ok &= check("%s offers every flag in the family contract" % name,
                    not missing)
        if missing:
            print("        missing: %s" % missing)
        ok &= check("%s offers --headless and --headful" % name,
                    "--headless" in flags[name] and "--headful" in flags[name])
    if len(flags) == 3:
        pw = flags["playwright_scraper"]
        # The differences the README states, pinned in both directions: a NEW
        # divergence fails here, and closing one of these also fails here, so
        # the README cannot quietly go stale either way.
        ok &= check("pyppeteer differs from playwright by exactly the "
                    "documented four flags",
                    sorted(pw - flags["puppeteer_scraper"])
                    == ["--fingerprint", "--fp-country", "--fp-tags",
                        "--locale"])
        ok &= check("selenium differs from playwright by exactly --locale",
                    sorted(pw - flags["selenium_scraper"]) == ["--locale"])

    # `--fp-tags` MUST DEFAULT TO ONE OS-FAMILY TAG. It shipped in this
    # family as "Windows,Chrome,Desktop", which the fingerprint API rejects
    # with HTTP 400 — so --fingerprint failed on every invocation, which is
    # one of the six defects §16 of the family notes lists. Measured
    # 2026-09-10 against the live API: `Windows` succeeds;
    # `Windows,Chrome,Desktop`, `Chrome` and `Desktop` each 400.
    for name in _ENGINE_MODULES:
        path = os.path.join(REPO_ROOT, name + ".py")
        if not os.path.exists(path):
            continue
        src = open(path, encoding="utf-8").read()
        m = re.search(r'--fp-tags"\s*,\s*default="([^"]*)"', src)
        if m is None:
            continue          # pyppeteer has no fingerprint flags
        ok &= check("%s's --fp-tags default is ONE tag the API accepts"
                    % name,
                    "," not in m.group(1)
                    and m.group(1) in ("Windows", "Microsoft Windows",
                                       "Android"))

    # EVERY page_flow CALL IN EVERY ENGINE, CHECKED AGAINST THE REAL
    # SIGNATURE. This is the general form of a bug the first live run of the
    # pyppeteer engine found: `classify(html, status, url)` took `status`
    # positionally, and two of the three engines called it as
    # `classify(html, url=…)` because they have no response object to read a
    # status from. Both crashed with TypeError on their FIRST fetch — and
    # that was invisible to import, to --help, to compileall, to the AST
    # undefined-name walk and to 400+ green offline checks, because none of
    # those calls a function the way a live run does.
    #
    # An offline suite cannot execute a fetch. It CAN bind every call's
    # arguments to the callee's signature, which is the same check the
    # interpreter does at the moment of the call, minus the browser.
    import inspect as _inspect
    for name in _ENGINE_MODULES:
        path = os.path.join(REPO_ROOT, name + ".py")
        if not os.path.exists(path):
            continue
        tree = ast.parse(open(path, encoding="utf-8").read())
        bad = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            if not (isinstance(fn, ast.Attribute)
                    and isinstance(fn.value, ast.Name)
                    and fn.value.id == "page_flow"):
                continue
            target = getattr(page_flow, fn.attr, None)
            if not callable(target):
                bad.append("%s: page_flow has no %s()" % (name, fn.attr))
                continue
            try:
                sig = _inspect.signature(target)
            except (TypeError, ValueError):
                continue
            # Bind PLACEHOLDERS, not values: this checks arity and keyword
            # names, which is what drifts. `*args` in the call (none today)
            # would make the binding unknowable, so it is skipped rather
            # than guessed at.
            if any(isinstance(a, ast.Starred) for a in node.args) or \
                    any(k.arg is None for k in node.keywords):
                continue
            try:
                sig.bind(*[object()] * len(node.args),
                         **{k.arg: object() for k in node.keywords})
            except TypeError as exc:
                bad.append("%s:%d page_flow.%s(...) — %s"
                           % (name, node.lineno, fn.attr, exc))
        ok &= check("every page_flow call in %s matches its signature" % name,
                    not bad)
        for line in bad:
            print("        %s" % line)

    # The same check for product_parser, which the engines call as often.
    for name in _ENGINE_MODULES:
        path = os.path.join(REPO_ROOT, name + ".py")
        if not os.path.exists(path):
            continue
        tree = ast.parse(open(path, encoding="utf-8").read())
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == "product_parser":
                imported.update(a.asname or a.name for a in node.names)
        bad = []
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                    and node.func.id in imported):
                continue
            target = getattr(product_parser, node.func.id, None)
            if not callable(target):
                continue
            if any(isinstance(a, ast.Starred) for a in node.args) or \
                    any(k.arg is None for k in node.keywords):
                continue
            try:
                _inspect.signature(target).bind(
                    *[object()] * len(node.args),
                    **{k.arg: object() for k in node.keywords})
            except TypeError as exc:
                bad.append("%s:%d %s(...) — %s"
                           % (name, node.lineno, node.func.id, exc))
        ok &= check("every product_parser call in %s matches its signature"
                    % name, not bad)
        for line in bad:
            print("        %s" % line)

    # And the two-argument call itself, pinned: `status` must stay optional,
    # because two of the three engines have no status to pass.
    ok &= check("page_flow.classify works with no status, as two engines "
                "call it",
                page_flow.classify("<html>x</html>",
                                   url=LISTING_URL) in STATE_POLICY_NAMES)

    # For "it must pass with no engine installed" to mean anything, each
    # engine has to import its driver at MODULE level — otherwise the module
    # imports cleanly with the library absent, the group never skips, and the
    # CI job that exists to catch that cannot. This drifts back silently, so
    # it is asserted rather than trusted.
    driver_imports = {"playwright_scraper": "playwright",
                      "puppeteer_scraper": "pyppeteer",
                      "selenium_scraper": "selenium"}
    for name, lib in driver_imports.items():
        path = os.path.join(REPO_ROOT, name + ".py")
        if not os.path.exists(path):
            continue
        tree = ast.parse(open(path, encoding="utf-8").read())
        top_level = set()
        for node in tree.body:
            if isinstance(node, ast.Import):
                top_level.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                top_level.add(node.module.split(".")[0])
        ok &= check("%s imports %s at module level, so an absent library skips"
                    % (name, lib), lib in top_level)
    return ok


def _raises_type(fn, exc_type) -> bool:
    """True if `fn()` raises exactly `exc_type` (or a subclass)."""
    try:
        fn()
    except exc_type:
        return True
    except Exception:  # noqa: BLE001 — a different type is a failed check
        return False
    return False


def _no_secret_in(fn, secret: str) -> bool:
    """True if `fn()` raises and the secret is absent from the message."""
    try:
        fn()
    except Exception as e:  # noqa: BLE001
        return secret not in str(e)
    return False


# Names Python provides that are not imports and not assignments.
_MODULE_DUNDERS = {"__file__", "__name__", "__doc__", "__package__",
                   "__spec__", "__loader__", "__builtins__", "__debug__"}


def _undefined_names(path):
    """Names loaded in `path` that are never imported, defined or assigned.

    A deliberately coarse approximation — it pools every binding in the file
    rather than tracking scopes, so it under-reports and never invents a
    problem. That is the right trade here: this exists to catch a name that
    is nowhere at all, and a false positive would be worse than a miss.
    """
    tree = ast.parse(open(path, encoding="utf-8").read())
    bound = set(dir(builtins)) | _MODULE_DUNDERS
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            bound |= {(a.asname or a.name.split(".")[0]) for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            bound |= {(a.asname or a.name) for a in node.names}
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                               ast.ClassDef)):
            bound.add(node.name)
        elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            bound.add(node.id)
        elif isinstance(node, ast.arg):
            bound.add(node.arg)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            bound.add(node.name)
        elif isinstance(node, ast.Global):
            bound |= set(node.names)
    missing = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load) \
                and node.id not in bound:
            missing.setdefault(node.id, []).append(node.lineno)
    return missing



def _unreachable_statements(path):
    """Line numbers of statements that can never run.

    A statement sitting after a `return`/`raise`/`break`/`continue` in the
    SAME block. Deliberately narrow: it makes no claim about conditions or
    reachability in general, only about a block whose control flow has
    already left. Measured across the eighteen repos in this family on
    2026-09-16 it reported six problems and zero false positives.

    `_undefined_names` above cannot see this class at all, by design — it
    pools every binding in the file rather than tracking scopes, so a name
    used inside dead code passes as long as anything else in the module
    binds it. What was hiding there: a function whose `def` line had been
    lost, leaving its docstring and body absorbed into the end of the
    function above it. Identical in six repos, present since each one's
    first commit, invisible to import, `--help`, `compileall` and every
    green run of this suite.
    """
    tree = ast.parse(open(path, encoding="utf-8").read())
    dead = []
    for node in ast.walk(tree):
        for field in ("body", "orelse", "finalbody"):
            block = getattr(node, field, None)
            if not isinstance(block, list):
                continue
            for i, stmt in enumerate(block[:-1]):
                if isinstance(stmt, (ast.Return, ast.Raise,
                                     ast.Continue, ast.Break)):
                    dead.append(block[i + 1].lineno)
                    break
    return sorted(dead)

def test_public_names_have_consumers():
    group("no public name is dead code or unenforced policy")
    ok = True
    # §17 #5: grep every public name in a shared module for a consumer
    # outside its own module, then READ what comes back. Running that over
    # this repo returned eleven names; every one of them is consumed by a
    # function in its own module, and none is dead. The ones that encode SITE
    # POLICY rather than plumbing are pinned here, so deleting one fails
    # instead of quietly changing behaviour — which is the failure mode a
    # policy constant has, and it is harder to see than dead code because the
    # prose beside it reads like enforcement.
    ok &= check("the browse hosts and the emirate hosts are different sets",
                set(product_parser.BROWSE_HOSTS)
                & set(product_parser.EMIRATE_HOSTS) == set()
                and set(product_parser.HOSTS)
                == set(product_parser.BROWSE_HOSTS)
                | set(product_parser.EMIRATE_HOSTS))
    ok &= check("the OLX-platform hosts are listed, not merely refused by "
                "accident",
                {"dubizzle.com.bh", "dubizzle.com.om", "dubizzle.com.eg"}
                <= set(product_parser.OLX_PLATFORM_HOSTS))
    ok &= check("and none of them is also a supported host",
                not set(product_parser.OLX_PLATFORM_HOSTS)
                & set(product_parser.HOSTS))
    ok &= check("the page parameter is the one page_url builds with",
                product_parser.PAGE_PARAM == "page"
                and "%s=2" % product_parser.PAGE_PARAM in page_url(LISTING_URL, 2))
    ok &= check("only a listing is a paginated kind",
                product_parser.PAGINATED_KINDS == ("listing",))
    ok &= check("campaign parameters are named, and stripping uses them",
                "utm_source" in product_parser.TRACKING_PARAMS
                and "utm_source" not in strip_tracking(
                    LISTING_URL + "?utm_source=x"))
    ok &= check("jsonld_blocks reads every block on the page, not only the "
                "ItemList one",
                len(product_parser.jsonld_blocks(fx("motors_p1")[0])) >= 1)
    ok &= check("the readiness timeout is what content_timeout_ms returns",
                page_flow.content_timeout_ms("listing")
                == page_flow.CONTENT_TIMEOUT_MS)
    ok &= check("SOURCE_DEFAULT is what a row's `source` actually says",
                Product().source == "dubizzle.com")
    return ok


def test_log_format_strings_match_their_args():
    group("every log call's placeholders match its arguments")
    ok = True
    # A %-format string and its argument list drift the moment someone edits
    # the prose, and the failure is invisible until that branch runs: Python's
    # logging swallows the TypeError, prints "--- Logging error ---" and the
    # RAW TEMPLATE, and the run carries on. A live property run printed
    # "This listing is %d page(s) deeper..." for exactly that reason, after a
    # rewrite dropped one placeholder and left both arguments.
    #
    # Counted rather than formatted: this walks the AST, so it needs no
    # branch to execute and catches the ones a live run never reaches.
    import ast as _ast
    for name in ENGINE_FILES + ("product_parser.py", "page_flow.py",
                                "output_writer.py", "proxy_pool.py",
                                "env_config.py", "captcha_solver.py",
                                "scraper_api_client.py", "diff_runs.py",
                                "fingerprint_client.py"):
        path = os.path.join(REPO_ROOT, name)
        if not os.path.exists(path):
            continue
        tree = _ast.parse(open(path, encoding="utf-8").read())
        bad = []
        for node in _ast.walk(tree):
            if not isinstance(node, _ast.Call):
                continue
            fn = node.func
            if not (isinstance(fn, _ast.Attribute)
                    and fn.attr in ("debug", "info", "warning", "error",
                                    "exception", "critical")
                    and isinstance(fn.value, _ast.Name)
                    and fn.value.id == "logger"):
                continue
            if not node.args:
                continue
            template = node.args[0]
            # Only literal templates can be counted; a variable one is the
            # caller's problem and is rare here.
            parts = []
            if isinstance(template, _ast.Constant) and isinstance(template.value, str):
                parts = [template.value]
            elif isinstance(template, _ast.JoinedStr):
                continue        # an f-string interpolates itself
            else:
                continue
            text = "".join(parts)
            # `%%` is a literal percent and consumes no argument.
            holders = len(re.findall(r"%[-+ #0-9.*]*[diouxXeEfFgGcrsa%]",
                                     text.replace("%%", "")))
            supplied = len([a for a in node.args[1:]
                            if not isinstance(a, _ast.Starred)])
            has_star = any(isinstance(a, _ast.Starred) for a in node.args[1:])
            if has_star:
                continue
            if holders != supplied:
                bad.append("%s:%d %d placeholder(s), %d argument(s)"
                           % (name, node.lineno, holders, supplied))
        ok &= check("%s: every logger call's placeholders match its arguments%s"
                    % (name, "" if not bad else " -- " + "; ".join(bad[:3])),
                    not bad)
    return ok


def test_x_debug_header_is_redacted():
    """SECURITY.md names the Scraper API's x-debug header as a place
    credentials reach a log unmasked. It was then logged verbatim.

    The fixtures are assembled from pieces rather than written out whole,
    because this file is scanned by the credential check like every other
    and a fixture that LOOKS like a live key fails it. They are the SHAPES a
    credential takes, not the literals this repo happens to contain today.
    """
    try:
        import scraper_api_client as sac
    except ImportError:
        return False

    pw = "SeCr" + "EtPw"
    key = "abcdef01" * 4
    raw = ("cdpurl=ws://acct-zone-scraping_browser-pid-7:" + pw
           + "@cb.2captcha.com:9222 cost=0.00145 key=" + key + " status=200")
    out = sac._redact_debug_header(raw)
    ok = True
    ok &= check("x-debug: the credential and the key are gone",
                      pw not in out and key not in out)
    ok &= check("x-debug: the cost, host and status survive",
                      "cost=0.00145" in out and "cb.2captcha.com:9222" in out
                      and "status=200" in out)

    s1, s2 = "secret" + "one", "secret" + "two"
    two = sac._redact_debug_header(
        "a=http://u1:" + s1 + "@h1:1 b=http://u2:" + s2 + "@h2:2")
    ok &= check("x-debug: both credentials are masked, not just the first",
                      s1 not in two and s2 not in two)

    src = inspect.getsource(sac)
    ok &= check("x-debug: the log line calls the redactor",
                      'logger.info("x-debug: %s", _redact_debug_header(debug))' in src)
    return ok


def test_parse_is_gated_on_the_policy():
    group("the engines read STATE_POLICY's parse column")
    ok = True
    # `should_parse` existed, was correct, and had NO consumer: every
    # engine parsed whatever reached the parse line, so the `parse` column
    # of STATE_POLICY decided nothing and an engine could disagree with the
    # table — and with its twins — without anything noticing. That is the
    # defect CLAUDE.md §17 names for constants, with a function instead.
    #
    # Measured across the family on 2026-09-23 by counting definitions
    # against readers: 7 of 24 repos defined it and none called it.
    import glob as _glob
    engines = sorted(_glob.glob(os.path.join(REPO_ROOT, "*_scraper.py")))
    ok &= check("there are engines to check (%d)" % len(engines), engines)
    for path in engines:
        name = os.path.basename(path)
        text = open(path, encoding="utf-8").read()
        if "_parse_for_mode" not in text:
            continue
        ok &= check("%s reads the parse decision from the policy" % name,
                    "should_parse(" in text)
        # And nothing parses unconditionally any more: a bare
        # `products = _parse_for_mode(` is the shape that ignored the table.
        ok &= check("%s does not parse unconditionally" % name,
                    not re.search(r"products = _parse_for_mode\(", text))
    # Every state the policy names must be answerable — a typo'd state name
    # would make should_parse fall through to its default for ever.
    for state in page_flow.STATE_POLICY:
        ok &= check("should_parse answers for %r" % state,
                    isinstance(page_flow.should_parse(state), bool))

    # The live bug this repo had, pinned as a value: a page one past the
    # end of a listing carries a recommendation rail, and reading it wrote
    # a car that was not in the category.
    import json as _json
    _p = os.path.join(REPO_ROOT, "fixtures_generated.json")
    if os.path.exists(_p):
        _d = _json.load(open(_p, encoding="utf-8"))
        _fx = _d.get("fixtures", _d)
        _v = _fx.get("motors_past_end")
        if _v:
            _state = detect_page_state(_v["html"], 200, _v.get("url", ""))
            ok &= check("a page past the end still classifies as empty",
                        _state == "empty")
            ok &= check("the policy says do not parse it",
                        page_flow.should_parse(_state) is False)
            ok &= check("...and the parser alone WOULD have yielded a row "
                        "(so the gate is load-bearing)",
                        len(parse_products(_v["html"], _v.get("url", ""))) > 0)
    return ok


def test_scraper_api_sends_waitfor_as_an_object_and_reads_http_code():
    """Measured 2026-09-23 against the live Scraper API: a JSON-encoded
    STRING waitFor is answered HTTP 422 and still billed, an object is
    answered 200; and the target's status is `http_code`, while `status` is
    the API's own verdict ("success"). Driven through the real fetch_html
    with requests.post stubbed -- no network."""
    group("Scraper API payload and target status")
    import types
    import scraper_api_client as sac
    sent = {}

    class _Resp:
        status_code = 200
        headers = {}
        text = ""

        def json(self):
            return {"status": "success", "http_code": 403, "headers": {},
                    "body": "<html></html>"}

    def _post(url, **kw):
        sent.update(kw.get("json") or {})
        return _Resp()

    args = types.SimpleNamespace(
        url='https://uae.dubizzle.com/motors/used-cars/', key="k" * 8,
        timeout=60, cdp_url=None, wait_text='AED', wait_element=None,
        wait_state=None)
    real_post = sac.requests.post
    sac.requests.post = _post
    try:
        _html, status = sac.fetch_html(args)
    finally:
        sac.requests.post = real_post
    ok = check("Scraper API: --wait-text sends waitFor as an OBJECT, not a JSON "
               "string (422 + billed, 2026-09-23) -- got %r" % (sent.get("waitFor"),),
               sent.get("waitFor") == {"text": 'AED'})
    ok &= check("Scraper API: the target status handed onward is http_code (403), "
                "not the API's 'success' -- got %r" % (status,),
                status == 403 and isinstance(status, int))
    return ok


def main() -> int:
    ok = True
    # Checks that could not run because an optional engine library is absent.
    # Reported at the end: a suite that silently skips part of itself and
    # still says "all passed" is the same defect as code that reports success
    # without checking that what it wanted actually happened.
    skips = []

    ok &= test_parse_is_gated_on_the_policy()
    ok &= test_price_parsing()
    ok &= test_listing_values()
    ok &= test_null_price_is_the_site_and_not_the_parser()
    ok &= test_jsonld_enrichment()
    ok &= test_dom_fallback_agrees_with_the_payload()
    ok &= test_urls()
    ok &= test_pagination()
    ok &= test_page_state()
    ok &= test_page_flow()
    ok &= test_output_contract()
    ok &= test_writers()
    ok &= test_finish_run()
    ok &= test_diff()
    ok &= test_captcha()
    ok &= test_env_config()
    ok &= test_proxy_pool()
    ok &= test_engines(skips)
    ok &= test_pyppeteer_teardown_noise()
    ok &= test_canary_separates_access_from_defect()
    ok &= test_ci_checks_is_actually_wired_up()
    ok &= test_no_capture_leaks()
    ok &= test_wording()
    ok &= test_fingerprint_client_reads_env()
    ok &= test_fingerprint_application()
    ok &= test_credentials_never_reach_a_log()
    ok &= test_concurrent_dispatch(skips)
    ok &= test_no_undefined_names()
    ok &= test_dockerfile_copies_what_it_runs()
    ok &= test_sample_output()
    ok &= test_public_names_have_consumers()
    ok &= test_log_format_strings_match_their_args()
    ok &= test_x_debug_header_is_redacted()
    ok &= test_scraper_api_sends_waitfor_as_an_object_and_reads_http_code()

    print()
    if _failures:
        print("%d check(s) FAILED:" % len(_failures))
        for f in _failures:
            print("  - %s" % f)
    if skips:
        print("%d engine group(s) SKIPPED — an optional engine library is "
              "absent. CI's engine-smoke job installs all three and fails if "
              "this list is non-empty, because a skip reads exactly like a "
              "passing run:" % len(skips))
        for s in skips:
            print("  - %s" % s)
    print("smoke_test: %s" % ("OK" if ok else "FAILED"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
