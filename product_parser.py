"""product_parser.py — dubizzle listing extraction. This module IS the site.

Everything dubizzle-specific lives here: how a listing is recognised, where
its data actually is, how pagination is addressed, and how a served page is
told apart from a refusal. The engines carry a handful of named constants and
nothing else.

Which site this is
------------------
The UAE platform: `uae.dubizzle.com` and the eight emirate subdomains an
individual ad is published under. It is a Next.js application whose listing
grids are served by Algolia and embedded in the page's SSR payload.

`dubizzle.com.bh`, `dubizzle.com.om` and `dubizzle.com.eg` carry the same
brand and are NOT this site: they run the OLX platform (no `__NEXT_DATA__`
anywhere, a different DOM, `<title>` reading "دوبيزل (أوليكس)" — dubizzle
(OLX)), and `dubizzle.com.lb` redirects to `olx.com.lb` outright. Measured
2026-09-14. `unsupported_reason` names that reason rather than saying "not a
dubizzle site", which is false and sends the reader looking for a typo (§5).

Where the data is, and why it is not where you would expect
-----------------------------------------------------------
Three sources overlap on a listing page and none of them is a superset of the
others. Measured across 12 captures and 195 listings, 2026-09-14:

    vertical            payload hits   JSON-LD ItemList   tile price nodes
    motors                    25          26 (Vehicle)          26
    property-for-rent         35          35 (RealEstate)       35
    property-for-sale         35          35 (RealEstate)       35
    classified                25           0                    25
    jobs                      25           0                     0
    community                 25           0                     0

So JSON-LD is NOT the primary path here, against this family's default (§4).
It is absent on three of the six verticals, and a JSON-LD-primary parser
would have produced a scraper that works on cars and flats and silently
returns nothing on everything else. The payload —
`__NEXT_DATA__ → props.pageProps.reduxWrapperActionsGIPP`, the Redux action
`listings/fetchListingDataForQuery/fulfilled` — carries every listing on
every vertical, plus the pagination contract, the ids and the per-vertical
attributes.

JSON-LD is read as an ENRICHMENT, joined on the ad's locale-stripped PATH,
and it is worth reading: it is the only place `brand`, the dealership's name
and `offers.availability` are stated, and its `priceCurrency` makes the
currency a fact rather than a symbol we recognised. On motors and property
the two sources named exactly the same 26 and 35 ads with no disagreement,
which is what `price_source` records.

The PATH rather than the URL, and that is not tidiness: on an ARABIC listing
the two sources disagree about the address of the same ad. Keyed on the full
URL the join matched 25 of 25 in English and 0 of 25 in Arabic — silently
emptying three columns while the run reported success.

The DOM is the fallback, anchored on the listing URL pattern rather than on a
class: every class on a tile except `lpv-cards` is a build hash
(`mui-style-1tufyr0`).

What the DOM knows that the payload does not
---------------------------------------------
The currency. A tile's price is split across two sibling nodes —

    <div class="price-ltr-wrapper">
      <div class="mui-style-1unh6l9">AED</div>
      <div data-testid="listing-price">389,000</div>
    </div>

— so reading the price node alone gets "389,000" with no currency, and
reading the wrapper's text gets "AED 389,000". §4's split-price trap, in the
shape where it is the SYMBOL rather than the cents that lives elsewhere.

The trap on this site
----------------------
A motors page past the end of its listing answers HTTP 200 with
`totalPages: 0, totalHits: 0`, prints "We couldn't find any results matching
your criteria" — and still renders ONE fully-formed Car of the Week ad, with
its own price node, its own JSON-LD item and its own listing URL. Parsing
such a page would write one plausible phantom row per exhausted page, and
nothing downstream could tell it from a real one. `detect_page_state` calls
that page `empty`, and `page_flow.STATE_POLICY` does not parse an `empty`
page.
"""

from __future__ import annotations

import json
import logging
import math
import re
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from bs4 import BeautifulSoup

from output_writer import Product

logger = logging.getLogger("product_parser")


# ---------------------------------------------------------------------------
# Hosts, locales, currency
# ---------------------------------------------------------------------------

# The aggregate host a listing is browsed on, and the bare domain that
# redirects to it.
BROWSE_HOSTS = ("uae.dubizzle.com", "www.dubizzle.com", "dubizzle.com")

# The emirate subdomains every individual ad is published under. Taken from
# the site's OWN links rather than guessed: these are the hosts that appear in
# `absolute_url` across the captures, counted per host. `uae.` never appears
# as an ad's host and no emirate host ever appears as a browse host, so the
# two sets are genuinely different roles rather than aliases.
EMIRATE_HOSTS = ("dubai.dubizzle.com", "abudhabi.dubizzle.com",
                 "sharjah.dubizzle.com", "ajman.dubizzle.com",
                 "rak.dubizzle.com", "uaq.dubizzle.com",
                 "fujairah.dubizzle.com", "alain.dubizzle.com")

HOSTS = BROWSE_HOSTS + EMIRATE_HOSTS

# Same brand, different platform. Refused WITH the reason (§5).
OLX_PLATFORM_HOSTS = ("dubizzle.com.bh", "www.dubizzle.com.bh",
                      "dubizzle.com.om", "www.dubizzle.com.om",
                      "dubizzle.com.eg", "www.dubizzle.com.eg",
                      "dubizzle.com.lb", "www.dubizzle.com.lb")

# The UAE platform ships exactly two, as its own
# `<link rel="alternate" hreflang=...>` set states: `en` (no prefix) and `ar`
# (an `/ar` path prefix). There is no third.
LOCALES = ("en", "ar")

# AED, and a fact rather than a default: the motors and property JSON-LD
# state `offers.priceCurrency: "AED"` outright, and every tile on every
# vertical prints the ISO code in its own node. Still never written onto a
# row that has no price (§4).
CURRENCY = "AED"

# The site's own asset hosts. A page dubizzle SERVED is built out of them —
# 126 to 2,571 references on each of the 12 captures, the lowest being a
# no-results page with the chrome and no grid. Neither Imperva interstitial
# carries one: 0 on both the "Request unsuccessful / Incapsula incident ID"
# iframe page and the "Pardon Our Interruption" page.
#
# This is the primary evidence for "was this served by dubizzle at all",
# because on this site the status code is not: the "Pardon Our Interruption"
# refusal answers **HTTP 200**.
_ASSET_MARKER = re.compile(r"static\.dubizzle\.com|dbz-images\.dubizzle\.com")
_ASSET_MIN_MATCHES = 2


def site_host(url: str) -> Optional[str]:
    host = (urlsplit(url or "").hostname or "").lower()
    return host or None


def unsupported_reason(url: str) -> Optional[str]:
    """Why this URL cannot be scraped, or None when it can.

    The reason matters as much as the refusal. A dubizzle-branded OLX site is
    not a typo and not an outage, and saying "is not a dubizzle site" would
    send the reader hunting for one.
    """
    host = site_host(url)
    if not host:
        return "no hostname in URL: %r" % (url,)
    if host in HOSTS:
        return None
    if host in OLX_PLATFORM_HOSTS:
        return ("%s carries the dubizzle brand but runs the OLX platform: no "
                "__NEXT_DATA__ payload, a different DOM and a different "
                "listing URL shape. This scraper reads the UAE platform "
                "(uae.dubizzle.com and the emirate subdomains) and would "
                "return nothing here. Measured 2026-09-14." % host)
    if host.endswith(".dubizzle.com"):
        return ("%s is a dubizzle host this scraper does not read. Listing "
                "pages live on uae.dubizzle.com and individual ads on the "
                "emirate subdomains (%s)."
                % (host, ", ".join(EMIRATE_HOSTS[:3]) + ", ..."))
    return "%s is not a dubizzle host. Supported: %s" % (host, ", ".join(HOSTS))


def is_supported_host(url: str) -> bool:
    return unsupported_reason(url) is None


def locale_of(url: str) -> Optional[str]:
    """"ar" for an /ar-prefixed path, "en" otherwise — but only for a URL on
    a host this scraper reads, so a stray path on another site does not come
    back as a dubizzle locale."""
    if not is_supported_host(url):
        return None
    parts = [p for p in urlsplit(url or "").path.split("/") if p]
    return "ar" if parts and parts[0].lower() == "ar" else "en"


def host_currency(url: str) -> Optional[str]:
    """The currency this host quotes in.

    One platform, one country, one currency. Kept as a function rather than
    read as the constant so that adding a second country site later is a
    change in one place — and so the caller cannot accidentally stamp AED on
    a row from a host that does not quote it.
    """
    return CURRENCY if is_supported_host(url) else None


def served_by_dubizzle(html: Optional[str]) -> bool:
    """Whether this response was built out of dubizzle's own assets.

    Positive evidence, which is what an inverted detector needs: an
    interstitial, a network-error page and an empty body all fail it, and
    none of them has to be enumerated.
    """
    if not html:
        return False
    return len(_ASSET_MARKER.findall(html)) >= _ASSET_MIN_MATCHES


# ---------------------------------------------------------------------------
# Verticals, URL shapes, pagination
# ---------------------------------------------------------------------------

# The site's own top-level sections, from the `taxonomy/taxonomyRequest`
# action in the page's payload rather than from a hand-written list.
VERTICALS = ("motors", "classified", "property-for-sale", "property-for-rent",
             "jobs", "jobs-wanted", "community")

# Path segments that are a listing's own address rather than a category.
# `/motors/used-cars/` is a category; `/motors/used-cars/bmw/x4/2026/02/05/
# 2683-pm-...---c62da01.../` is one ad. The discriminator that works on every
# vertical is the DATE: every ad's path carries `/{yyyy}/{m}/{d}/` and no
# category path does.
_AD_PATH_RE = re.compile(r"/\d{4}/\d{1,2}/\d{1,2}/[^/]+/?$")

# How a listing link is recognised. A URL pattern, never a class: every class
# on a tile but `lpv-cards` is a build hash (`mui-style-1tufyr0`), and the
# hashes change with the next deploy while the URL shape is a contract with
# search engines.
SELECTORS = {
    # Anchors whose href is an ad. Kept broad — the href test below is what
    # actually decides — because the site renders the same shape for organic
    # results, the Car of the Week and the "similar ads" rail.
    "item_link": 'a[href*="/20"]',
    # The tile's price, split across two nodes; the WRAPPER is what carries
    # both the ISO code and the amount.
    "tile_price": '[data-testid="listing-price"]',
    "tile_title": '[data-testid="subheading-text"]',
    "tile_location": '[data-testid="listing-location"]',
    # The grid. `lpv-cards` is the one semantic class on a tile and is used
    # only as a readiness hint, never as the extraction anchor.
    "grid_card": ".lpv-cards",
}

PAGE_PARAM = "page"

# A safety rail, not the real limit. The site states its own page count in
# every payload (`pagination.totalPages`) and the two verticals measured cap
# at different numbers — 400 on motors (10,000 ads at 25 a page) and 2,286 on
# both property indexes (80,010 at 35 a page) — so a single constant would be
# wrong for one of them. `total_pages()` reads the payload and falls back to
# this only when there is no payload to read.
#
# Past the cap the site does NOT clamp, it empties: `?page=401` and
# `?page=99999` on a 400-page motors listing both answered HTTP 200 with
# `totalPages: 0, totalHits: 0`. That is a cleaner terminator than any
# selector, and it is why `is_no_results` reads the payload first.
PAGE_CAP = 2286

TRACKING_PARAMS = frozenset("""
utm_source utm_medium utm_campaign utm_term utm_content utm_id
gclid fbclid msclkid ttclid twclid igshid
_ga _gl mc_cid mc_eid ref referrer
""".split())


def _path_parts(url: str) -> List[str]:
    parts = [p for p in urlsplit(url or "").path.split("/") if p]
    if parts and parts[0].lower() == "ar":
        parts = parts[1:]
    return parts


def vertical_of(url: str) -> Optional[str]:
    """Which top-level section this URL belongs to."""
    parts = _path_parts(url)
    if parts and parts[0] in VERTICALS:
        return parts[0]
    return None


def listing_kind(url: str) -> str:
    """Which kind of page this URL is: "listing", "ad", "home", or "" when it
    is none of them.

    "listing" is a browsable grid, the only kind this repo fetches. "ad" is
    one advertisement's own page, recognised so that a user who pastes one
    gets told what it is rather than an empty run.
    """
    if not is_supported_host(url):
        return ""
    path = urlsplit(url or "").path
    parts = _path_parts(url)
    if not parts:
        return "home"
    if parts[0] not in VERTICALS:
        return ""
    if _AD_PATH_RE.search(path):
        return "ad"
    return "listing"


PAGINATED_KINDS = ("listing",)


def strip_tracking(url: str) -> str:
    """Drop campaign parameters, keep everything else in its original order.

    Two URLs that differ only by a `utm_source` are the same page, and a
    dedupe or a "did page 1's next-link agree with the convention" check that
    does not know it will answer no to an identical address.
    """
    s = urlsplit(url or "")
    kept = [(k, v) for k, v in parse_qsl(s.query, keep_blank_values=True)
            if k not in TRACKING_PARAMS]
    return urlunsplit((s.scheme, s.netloc, s.path,
                       urlencode(kept, doseq=True), ""))


def paginates_by_url(url: str) -> bool:
    """Whether page N of this URL has an address of its own.

    True for every listing grid on this site, and that is measured rather
    than assumed: page 1's own `<link rel="next">` is
    `https://uae.dubizzle.com/motors/used-cars/?page=2` — exactly what the
    convention below builds — and page 2's payload reports `pagination.page:
    1` for it. So every page's address is knowable up front and workers can
    be handed independent pages (§7).
    """
    return listing_kind(url) in PAGINATED_KINDS


def page_url(url: str, page_num: int) -> Optional[str]:
    """`?page=N`, replacing rather than duplicating, preserving the filters.

    Page 1 is the bare URL with no `page` parameter — which is what the
    site's own `<link rel="prev">` on page 2 points at, so the convention
    agrees with the site rather than merely working.
    """
    if page_num < 1 or not paginates_by_url(url):
        return None
    s = urlsplit(url or "")
    params = [(k, v) for k, v in parse_qsl(s.query, keep_blank_values=True)
              if k != PAGE_PARAM]
    if page_num > 1:
        params.append((PAGE_PARAM, str(page_num)))
    return urlunsplit((s.scheme, s.netloc, s.path,
                       urlencode(params, doseq=True), ""))


def page_number_from_url(url: str) -> int:
    """The 1-based page an URL addresses. The payload counts from 0 and the
    URL counts from 1; this returns the URL's convention, and `parse_products`
    is given the same number so `page` on a row matches the address the
    operator asked for."""
    for k, v in parse_qsl(urlsplit(url or "").query, keep_blank_values=True):
        if k == PAGE_PARAM:
            try:
                return max(1, int(v))
            except (TypeError, ValueError):
                return 1
    return 1


# Segments that are a vertical or a routing artefact rather than a category
# anyone browses.
_NOT_A_CATEGORY = frozenset(("ar", "en", "search", "s"))


def category_from_url(url: str) -> Optional[str]:
    """The browsed category path, e.g. "motors/used-cars" or
    "classified/electronics/televisions".

    The whole chain rather than the leaf: "televisions" alone loses which
    vertical it was under, and two verticals can and do use the same leaf
    word.
    """
    parts = [p for p in _path_parts(url) if p not in _NOT_A_CATEGORY]
    if not parts or parts[0] not in VERTICALS:
        return None
    if _AD_PATH_RE.search(urlsplit(url or "").path):
        # An ad's path carries its category too, up to the date.
        cut = next((i for i, p in enumerate(parts) if re.fullmatch(r"\d{4}", p)),
                   len(parts))
        parts = parts[:cut]
    return "/".join(parts) or None


def sku_from_url(url: str) -> Optional[str]:
    """The row key: the ad's URL path, locale-stripped, no trailing slash.

    Why a path and not an id is argued in `output_writer.Product.sku`. The
    short version: four of this site's five verticals carry a 32-hex uuid in
    the URL and property carries neither of its two ids, so an id-derived key
    would be null on every property row the DOM path produced.
    """
    if not url:
        return None
    path = urlsplit(url).path
    if not _AD_PATH_RE.search(path):
        return None
    parts = _path_parts(url)
    if not parts:
        return None
    return "/" + "/".join(parts)


# ---------------------------------------------------------------------------
# The SSR payload
# ---------------------------------------------------------------------------

_NEXT_DATA_RE = re.compile(
    r'<script[^>]+id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.S)

# The Redux action whose payload holds the grid. Named rather than
# positional: the action list is 7 entries long on a hub page and 33 on a
# property listing, and its order moves between verticals.
_LISTINGS_ACTION = "listings/fetchListingDataForQuery/fulfilled"


def next_data(html: Optional[str]) -> Optional[dict]:
    """The page's `__NEXT_DATA__`, or None when there is none to parse."""
    if not html:
        return None
    m = _NEXT_DATA_RE.search(html)
    if not m:
        return None
    try:
        return json.loads(m.group(1))
    except (ValueError, TypeError):
        # Loud on purpose: a payload that is present and unparseable is a
        # site change, not an empty page, and the two want different
        # responses.
        logger.warning("__NEXT_DATA__ present but not parseable as JSON")
        return None


def _redux_actions(props: Optional[dict]) -> List[dict]:
    page_props = (((props or {}).get("props") or {}).get("pageProps") or {})
    actions = page_props.get("reduxWrapperActionsGIPP")
    return [a for a in actions if isinstance(a, dict)] if isinstance(actions, list) else []


def listings_payload(props: Optional[dict]) -> dict:
    """The grid payload: `hits`, `pagination`, and the promoted rails.

    Returns `{}` rather than raising when the page has no grid — a hub page,
    a 404 and a challenge all legitimately have none, and telling them apart
    is `detect_page_state`'s job, not this one's.
    """
    for action in _redux_actions(props):
        if action.get("type") == _LISTINGS_ACTION:
            payload = action.get("payload")
            return payload if isinstance(payload, dict) else {}
    return {}


def _pagination(html: Optional[str]) -> dict:
    pag = listings_payload(next_data(html)).get("pagination")
    return pag if isinstance(pag, dict) else {}


def total_results(html: Optional[str]) -> Optional[int]:
    """How many ads the listing says it holds, in total."""
    total = _pagination(html).get("totalHits")
    return total if isinstance(total, int) else None


def hits_per_page(html: Optional[str]) -> int:
    """How many ads a page of this vertical holds. 25 on motors, classified,
    jobs and community; 35 on both property indexes. Read rather than
    assumed — a hardcoded 25 would have mis-computed every property page
    count by 40%."""
    value = _pagination(html).get("hitsPerPage")
    return value if isinstance(value, int) and value > 0 else 25


def total_pages(html: Optional[str], url: str = "") -> Optional[int]:
    """Pages this listing has, from the site's OWN count.

    The strongest form §7 asks for, and stronger than the arithmetic a
    sibling repo needs: page 1's payload states `totalPages` directly, so no
    selector is followed and no page count is inferred. The site applies its
    own result cap before publishing this number, which is why it is 400 on a
    34,619-ad motors listing.
    """
    pages = _pagination(html).get("totalPages")
    if not isinstance(pages, int):
        return None
    if pages <= 0:
        # Past the end of the listing. One page, and it holds nothing —
        # reported as 1 rather than 0 so a caller asking "how many pages"
        # never divides by it.
        return 1
    return min(pages, PAGE_CAP)


def pages_beyond_cap(html: Optional[str]) -> int:
    """How many pages the catalogue has that the site will not address.

    Reported rather than swallowed: a motors listing of 34,619 ads publishes
    400 pages of 25, so 24,619 ads — 985 pages — are simply not reachable
    through pagination. A run that stops at 400 is complete as far as the
    site is concerned and a third of the way through as far as the catalogue
    is, and only saying so lets a consumer tell the two apart.
    """
    total = total_results(html)
    stated = _pagination(html).get("totalPages")
    if total is None or not isinstance(stated, int) or stated <= 0:
        return 0
    needed = max(1, math.ceil(total / hits_per_page(html)))
    return max(0, needed - stated)


def search_header(html: Optional[str]) -> Optional[str]:
    """The Algolia index a listing page queried — "motors.com",
    "classified.com", "by_verification_feature_asc_property-for-rent-
    residential.com".

    Not a search box's text: this site filters by PATH, not by query string,
    so the index name is the closest thing to "what was asked for" that the
    page states about itself. Logged by the engines, which is what the
    family uses this for.
    """
    name = listings_payload(next_data(html)).get("algoliaIndexName")
    return name if isinstance(name, str) and name else None


# ---------------------------------------------------------------------------
# Bilingual values
# ---------------------------------------------------------------------------

_ZERO_WIDTH = "\u200b\u200c\u200d\u2060\ufeff"


def _clean(text: Optional[str]) -> str:
    if not isinstance(text, str):
        return ""
    for ch in _ZERO_WIDTH:
        text = text.replace(ch, "")
    return re.sub(r"\s+", " ", text.replace("\xa0", " ")).strip()


def _en(value: Any) -> Optional[str]:
    """The English side of one of this site's bilingual values.

    Every human-readable string in the payload is `{"en": ..., "ar": ...}`,
    and both halves are sometimes the same string: a motors ad's
    `absolute_url.ar` is byte-identical to its `.en` (the site publishes no
    Arabic motors URL at all), while a property ad's carries an `/ar` prefix.
    Falls through to the Arabic side rather than returning nothing, because a
    row with an Arabic title is worth more than a row with none.
    """
    if value is None:
        return None
    if isinstance(value, str):
        return _clean(value) or None
    if isinstance(value, dict):
        got = _clean(value.get("en")) or _clean(value.get("ar"))
        if got:
            return got
        # `{"id": 2, "name": {"en": "Dubai", "ar": "..."}}` — property wraps
        # what motors states flat. Both are the site's own, and a reader that
        # knows only the flat shape leaves `city` null on a third of the
        # property rows while looking like it worked.
        if isinstance(value.get("name"), (dict, str)):
            return _en(value.get("name"))
        return None
    if isinstance(value, (int, float)):
        return str(value)
    return None


def _en_list(value: Any) -> List[str]:
    """The English side of a bilingual LIST — `location_list`, `places`,
    `neighborhoods` and `categories` all take this shape."""
    if isinstance(value, dict):
        for key in ("en", "ar"):
            got = value.get(key)
            if isinstance(got, list):
                return [_clean(x) for x in got if _clean(x)]
        name = value.get("name")
        if isinstance(name, dict):
            return _en_list(name)
    if isinstance(value, list):
        return [_clean(x) for x in value if _clean(x)]
    return []


def _int(value: Any) -> Optional[int]:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        digits = re.sub(r"[^\d]", "", value)
        return int(digits) if digits else None
    return None


def _number(value: Any) -> Optional[float]:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        return price_in(value)
    return None


def _epoch_iso(value: Any) -> Optional[str]:
    """A Unix second count as a UTC ISO-8601 string.

    Guarded rather than trusted: the payload's `added` and `created_at` are
    seconds, but a field that is occasionally milliseconds would otherwise
    silently produce a year in the 56th century.
    """
    from datetime import datetime, timezone
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    seconds = float(value)
    if seconds > 1e11:          # milliseconds
        seconds /= 1000.0
    if not (0 < seconds < 4e9):  # before 1970 or after 2096: not a date
        return None
    return datetime.fromtimestamp(seconds, tz=timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Price text (the DOM fallback path only — the payload states numbers)
# ---------------------------------------------------------------------------

# A rendered page uses a no-break variant so the number cannot wrap, so the
# plain space is not enough (§4).
_GROUP_SPACES = " \u00a0\u202f\u2009"
_AMOUNT = (r"\d{1,3}(?:[.,%s]\d{3})+(?:[.,]\d{1,2})?" % _GROUP_SPACES
           + r"|\d+(?:[.,]\d{1,2})?")
# The UAE writes AED as a prefix, as an ISO code and occasionally as "Dhs".
# Matched longest-first so "AED" is not swallowed by a bare "D".
_PRICE_RE = re.compile(r"(?:AED|DHS|Dhs|د\.إ)\s*(" + _AMOUNT + r")"
                       r"|(" + _AMOUNT + r")\s*(?:AED|DHS|Dhs|د\.إ)")
# Stripped BEFORE matching, not rejected after: a rejected match has already
# consumed the currency symbol beside it (§4).
_PCT_RE = re.compile(r"-?\s*\d{1,3}(?:[.,]\d+)?\s*%|-?\s*%\s*\d{1,3}(?:[.,]\d+)?")


def _normalize_amount(raw: str) -> Optional[float]:
    """One number out of one of the three grouping conventions."""
    text = raw.strip()
    for space in _GROUP_SPACES[1:]:
        text = text.replace(space, " ")
    has_dot, has_comma = "." in text, "," in text
    if has_dot and has_comma:
        # Whichever comes last is the decimal point.
        if text.rfind(".") > text.rfind(","):
            text = text.replace(",", "")
        else:
            text = text.replace(".", "").replace(",", ".")
    elif has_dot or has_comma:
        sep = "." if has_dot else ","
        tail = text.rsplit(sep, 1)[1]
        if len(tail) == 3 and text.count(sep) >= 1:
            # Exactly three trailing digits is a thousands grouping: no
            # currency has a three-digit subunit, so "AED 1,234" is 1234.
            text = text.replace(sep, "")
        else:
            text = text.replace(sep, ".") if sep == "," else text
    if " " in text:
        groups = text.split(" ")
        # Space grouping needs FULL three-digit groups, or a spec list beside
        # a price merges into one number.
        if all(re.fullmatch(r"\d{3}", g) for g in groups[1:]) and groups[0].isdigit():
            text = "".join(groups)
        else:
            return None
    try:
        return float(text)
    except ValueError:
        return None


def prices_in(text: str) -> List[float]:
    """Every AED amount in a piece of text, in order."""
    if not text:
        return []
    cleaned = _PCT_RE.sub(" ", _clean(text))
    out: List[float] = []
    for m in _PRICE_RE.finditer(cleaned):
        amount = _normalize_amount(m.group(1) or m.group(2) or "")
        if amount is not None:
            out.append(amount)
    return out


def price_in(text: str) -> Optional[float]:
    """The first AED amount in a piece of text, or None."""
    found = prices_in(text)
    return found[0] if found else None


# ---------------------------------------------------------------------------
# Blocks and challenges
# ---------------------------------------------------------------------------

# Imperva fronts this site, and its refusal takes two shapes. Both were
# captured from a real refusal rather than transcribed from a vendor's docs:
#
#   1,160 bytes  an iframe page whose only visible text is
#                "Request unsuccessful. Incapsula incident ID: ..."
#   6,183 bytes  "Pardon Our Interruption" — served with **HTTP 200**
#
# The status code is therefore NOT the signal here, which is why
# `served_by_dubizzle` leads the classification.
BLOCK_MARKERS = (
    "Request unsuccessful. Incapsula incident",
    "Pardon Our Interruption",
    "/_Incapsula_Resource?SWUDNSAI=",
)

# What a challenge looks like — a page asking the visitor to prove something,
# as opposed to one refusing outright.
#
# `_Incapsula_Resource` on its OWN is deliberately NOT in this list, and that
# is the §18 rule applied before it could cost anything: the marker appears
# once on pages dubizzle plainly served (1 occurrence on the motors page 1
# capture and 1 on the property capture, against 3 and 4 on the two refusals).
# A marker that matches a good page is worse than no marker, so the entries
# below are the ones a served page cannot carry — the challenge widget
# itself, not the vendor's ordinary instrumentation.
BOT_CHALLENGE_MARKERS = (
    "g-recaptcha",
    "grecaptcha.render",
    "recaptcha/api.js",
    "recaptcha/api2/anchor",
    "recaptcha/api2/bframe",
    "data-sitekey",
    "hcaptcha.com/1/api.js",
    "hcaptcha.com/captcha",
    "challenges.cloudflare.com/turnstile",
    "_Incapsula_Resource?SWCGHOEL",
    "Incapsula incident ID",
    # dubizzle's OWN reCAPTCHA key (see RECAPTCHA_SITE_KEY below). Absent
    # from 17 of 17 captures, so its appearance means the site rendered its
    # own challenge into the page rather than that we recognised a vendor.
    "6LeubLYqAAAAAK7-X6nc1fW2ggot_vTvQAv0RxdU",
)

# WHICH CAPTCHA THIS SITE ACTUALLY USES, found by reading its own bundles
# rather than by guessing from a vendor list (§18).
#
# Google reCAPTCHA. The key below is dubizzle's, and it is in the site's own
# JavaScript twice — as `RECAPTCHA_KEY` in the argument object handed to
# `showAuthPopup(...)`, beside `GOOGLE_APP_ID` and `FACEBOOK_APP_ID`, and as
# `recaptchaSiteKey` in the app config. `showAuthPopup` is called with
# `intent: "login"` or `"phoneverify"`, which is the whole story: the captcha
# guards SIGNING IN and VERIFYING A PHONE NUMBER, not reading listings.
#
# That is why an anonymous listing scrape never meets it — 15 captures and 5
# live runs, zero rendered challenges — and it is also why the version (v2 vs
# v3) is NOT claimed here: none of the site's 104 listing chunks contains the
# `recaptcha/api.js` loader or a single `grecaptcha.*` call. The widget is
# rendered by a separate auth application that the listing pages never load,
# so the loader's `render=` parameter, which is what settles v2 against v3
# (§8), is not observable from anything this scraper fetches.
#
# Kept as a MARKER because it is safe to be one: the key appears in 0 of 17
# captures, so its presence in a page's HTML means the site has rendered its
# own challenge into the page we were given. Checked before adding, which is
# the rule that keeps a marker from matching every good page.
RECAPTCHA_SITE_KEY = "6LeubLYqAAAAAK7-X6nc1fW2ggot_vTvQAv0RxdU"

# The site also ships an EMPTY mount point on every page it serves —
# `<captcha-widgets></captcha-widgets>`, 1 occurrence on all 17 captures,
# good pages included. So the bare tag is a fact about the site and not a
# marker (§18), and it is deliberately absent from the list above. What a
# rendered challenge would look like is the same element with something
# INSIDE it, which is a structural question and therefore a function rather
# than a substring.
_CAPTCHA_MOUNT_RE = re.compile(
    r"<captcha-widgets[^>]*>(?P<inner>.*?)</captcha-widgets>", re.S | re.I)


def captcha_mount_is_populated(html: Optional[str]) -> bool:
    """Whether the site's own captcha mount point has rendered anything.

    Asked rather than assumed: "did we meet a challenge" and "is one
    configured" are different questions (§18), and on this site the answer to
    the second is yes while the answer to the first has been no on every
    capture taken.
    """
    if not html:
        return False
    for m in _CAPTCHA_MOUNT_RE.finditer(_without_extension_scripts(html)):
        if m.group("inner").strip():
            return True
    return False

# The Scraping Browser's auto-solve extension injects its own hunters into
# every page it loads, so a challenge marker can be OURS rather than the
# site's (§8). Stripped before the scan because two of the markers above
# (`recaptcha/api.js`, `challenges.cloudflare.com/turnstile`) are exactly
# what that extension injects — unlike two sibling repos, where the same
# guard would have been dead code, this marker set can genuinely match one.
_EXTENSION_SCRIPT_RE = re.compile(
    r"<script[^>]+src=\"(?:chrome|moz)-extension://[^\"]*\"[^>]*>\s*</script>",
    re.I)


def _without_extension_scripts(html: str) -> str:
    return _EXTENSION_SCRIPT_RE.sub("", html or "")


def detect_block_marker(html: Optional[str]) -> Optional[str]:
    """Which refusal this is, or None."""
    if not html:
        return None
    for marker in BLOCK_MARKERS:
        if marker in html:
            return marker
    return None


def detect_bot_challenge(html: Optional[str], url: str = "") -> Optional[str]:
    """Which challenge is on this page, or None.

    Broad on purpose (§8): different geos and scenarios surface different
    challenges, and a detection that only knows the one we happened to meet
    is a detection that fails the first time the site changes vendor.
    """
    if not html:
        return None
    stripped = _without_extension_scripts(html)
    for marker in BOT_CHALLENGE_MARKERS:
        if marker in stripped:
            return marker
    if captcha_mount_is_populated(html):
        return "<captcha-widgets> rendered a challenge"
    return None


# The site's own no-results sentence, in both its languages. A SECONDARY
# signal: the payload's own `totalHits: 0` is unambiguous and language-
# independent, and is checked first. This exists for the case where the
# payload is absent but the page plainly says it found nothing.
#
# The Arabic sentence is NOT pinned here, because no Arabic no-results page
# was captured and transcribing one from a translator would be inventing
# evidence. If you capture one, add it.
NO_RESULTS_MARKERS = (
    "We couldn\u2019t find any results matching your criteria",
    "We couldn't find any results matching your criteria",
)


def is_no_results(html: Optional[str]) -> bool:
    """Whether the site served this page and told us it holds nothing.

    Ordered by what each signal PROVES, not by what is cheap to check (§17):
    the payload stating `totalHits: 0` is the site's own answer and settles
    it; the sentence is a fallback for a page with no payload.
    """
    if not html:
        return False
    pag = _pagination(html)
    if pag:
        if pag.get("totalHits") == 0 or pag.get("totalPages") == 0:
            return True
        return False
    return any(marker in html for marker in NO_RESULTS_MARKERS)


def detect_page_state(html: Optional[str], status: Optional[int] = None,
                      url: str = "") -> str:
    """Which of the five states this response is.

    Signals are ordered by how much each one PROVES, which on this site is
    not the order they are cheapest in (§17):

      1. No body at all              -> blocked. Nothing to read.
      2. An explicit refusal marker  -> blocked, even at HTTP 200, because
                                        "Pardon Our Interruption" IS a 200.
      3. Not built out of dubizzle's assets -> blocked. This catches an
                                        interstitial nobody has enumerated
                                        and Chromium's own network-error
                                        page, whose <title> is the site's
                                        hostname and which carries no vendor
                                        marker at all (§18).
      4. A challenge widget          -> challenge.
      5. The payload names hits      -> content. Positive and unambiguous.
      6. The payload says zero       -> empty. Checked AFTER content so a
                                        page with results is never talked out
                                        of them.
      7. Served, no payload yet      -> shell. Wants the readiness wait, not
                                        another fetch.

    `status` is positional and comes SECOND, matching `page_flow.classify`.
    """
    if html is None:
        return "blocked"
    if detect_block_marker(html):
        return "blocked"
    if not served_by_dubizzle(html):
        return "blocked"
    if detect_bot_challenge(html, url):
        return "challenge"
    payload = listings_payload(next_data(html))
    hits = payload.get("hits")
    if isinstance(hits, list) and hits:
        return "content"
    if is_no_results(html):
        return "empty"
    if status is not None and status >= 400:
        # Served by dubizzle, no grid, and the site said so with a status.
        # A 404 category is an empty answer, not a refusal.
        return "empty"
    if url and listing_kind(url) != "listing":
        # A HUB, not a grid: `uae.dubizzle.com/` and `/jobs/` are landing
        # pages of category tiles and promo rails, and they never paint a
        # result grid however long you wait. Calling them `shell` would send
        # the engine into a readiness wait that cannot succeed and then into
        # the DOM fallback, which on the home page finds 45 ad links in the
        # promo rails and writes 45 rows of carousel text — a complete-looking
        # run of data nobody asked for. They are `empty`: served exactly as
        # requested, holding no listing.
        return "empty"
    return "shell"


# ---------------------------------------------------------------------------
# JSON-LD enrichment
# ---------------------------------------------------------------------------

_LD_RE = re.compile(
    r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>', re.S)


def jsonld_blocks(html: Optional[str]) -> List[Any]:
    out: List[Any] = []
    for raw in _LD_RE.findall(html or ""):
        try:
            out.append(json.loads(raw))
        except (ValueError, TypeError):
            continue
    return out


def _ld_image(value: Any) -> Optional[str]:
    """A schema.org `image` in any of its four legal shapes (§4)."""
    if isinstance(value, str):
        return value or None
    if isinstance(value, dict):
        return value.get("url") or value.get("contentUrl") or None
    if isinstance(value, list):
        for item in value:
            got = _ld_image(item)
            if got:
                return got
    return None


def _ld_offer(value: Any) -> dict:
    """`offers` as a dict, a list, or an explicit null.

    `.get("offers", {})` is not enough: a JSON null is a PRESENT key, so the
    default never applies and the caller gets None where it expected a dict
    (§4's first row).
    """
    if isinstance(value, dict):
        return value
    if isinstance(value, list):
        for item in value:
            if isinstance(item, dict):
                return item
    return {}


def jsonld_items_by_url(html: Optional[str]) -> Dict[str, dict]:
    """The page's JSON-LD ItemList, keyed by the ad's locale-stripped PATH.

    The path rather than the URL, and that is not tidiness: on an ARABIC
    listing the two sources disagree about the address of the same ad. The
    JSON-LD publishes `/ar/motors/used-cars/...` while the payload's
    `absolute_url.ar` is byte-identical to its `.en` and carries no `/ar` at
    all. Keyed on the full URL the join matched 25 of 25 ads in English and
    **0 of 25 in Arabic**, silently emptying `brand`, `seller_name` and
    `in_stock` on every Arabic row while the run reported success — the
    exact shape of this codebase's most common bug class.

    Empty on classified, jobs and community, which publish no ItemList at
    all — that is the measurement that decided the payload is primary, and
    an empty dict here is the normal case rather than a failure.
    """
    out: Dict[str, dict] = {}
    for block in jsonld_blocks(html):
        if not isinstance(block, dict):
            continue
        main = block.get("mainEntity")
        if not isinstance(main, dict):
            continue
        for element in main.get("itemListElement") or []:
            item = element.get("item") if isinstance(element, dict) else None
            if isinstance(item, dict) and isinstance(item.get("url"), str):
                key = sku_from_url(item["url"])
                if key:
                    out[key] = item
    return out


def jsonld_currency(html: Optional[str]) -> Optional[str]:
    """`priceCurrency` from the page's AggregateOffer — the site naming its
    own currency, which outranks anything read off the DOM (§4)."""
    for block in jsonld_blocks(html):
        if isinstance(block, dict):
            code = _ld_offer(block.get("offers")).get("priceCurrency")
            if isinstance(code, str) and re.fullmatch(r"[A-Z]{3}", code):
                return code
    return None


# ---------------------------------------------------------------------------
# Rows
# ---------------------------------------------------------------------------

_SELLER_KIND = {"OW": "owner", "DL": "dealer", "AG": "agent"}


def _seller_kind(hit: dict) -> Optional[str]:
    """One word for what the seller is, out of two fields that mean the same
    thing on two different verticals."""
    code = hit.get("seller_type")
    if isinstance(code, str) and code in _SELLER_KIND:
        return _SELLER_KIND[code]
    listed_by = hit.get("listed_by")
    if isinstance(listed_by, dict):
        code = listed_by.get("value")
        if isinstance(code, str) and code in _SELLER_KIND:
            return _SELLER_KIND[code]
        label = _en(listed_by)
        if label:
            return label.lower()
    return None


def _image(hit: dict) -> Optional[str]:
    """The tile's photo. `photos` is a DICT on motors, classified, jobs and
    community and a LIST of dicts on property — both legal, both real, and a
    parser that knows only one returns null on a fifth of the site."""
    photos = hit.get("photos")
    if isinstance(photos, dict):
        for key in ("main", "thumb", "micro"):
            got = photos.get(key)
            if isinstance(got, str) and got:
                return got
    if isinstance(photos, list):
        for entry in photos:
            if isinstance(entry, dict):
                for key in ("main", "thumb", "micro"):
                    got = entry.get(key)
                    if isinstance(got, str) and got:
                        return got
            elif isinstance(entry, str) and entry:
                return entry
    thumbs = hit.get("photo_thumbnails")
    if isinstance(thumbs, list) and thumbs and isinstance(thumbs[0], str):
        return thumbs[0]
    return None


def _attributes(hit: dict) -> Optional[dict]:
    """Everything the vertical publishes about this ad, as {slug: value}.

    Two shapes, both flattened into one mapping:

      motors     `details_v2` — groups (`primary`, `secondary`, ...) each
                 holding a list of {label, value, slug}
      property   `property_info` — a flat list of {label, value, id}

    English values, because the slug is already the stable key and an Arabic
    run should still produce a column a consumer can compare across runs.
    """
    out: Dict[str, str] = {}
    details = hit.get("details_v2")
    if isinstance(details, dict):
        for group in details.values():
            if not isinstance(group, list):
                continue
            for entry in group:
                if not isinstance(entry, dict):
                    continue
                slug = entry.get("slug") or entry.get("id")
                value = _en(entry.get("value"))
                if isinstance(slug, str) and slug and value:
                    out[slug] = value
    info = hit.get("property_info")
    if isinstance(info, list):
        for entry in info:
            if not isinstance(entry, dict):
                continue
            slug = entry.get("id") or entry.get("slug")
            value = _en(entry.get("value"))
            if isinstance(slug, str) and slug and value:
                out[slug] = value
    return out or None


def _discount(price: Optional[float],
              original: Optional[float]) -> Tuple[Optional[float], Optional[float]]:
    """`(original_price, discount_pct)` from the payload's two figures.

    `pre_discount_price: 0` is how this site writes "no discount", and a
    naive read turns it into a 100%-off car. Anything not strictly above the
    price produces None for BOTH columns — two figures that are not what they
    were taken for should produce no number at all (§4).
    """
    if price is None or original is None:
        return None, None
    if original <= 0 or original <= price:
        return None, None
    return original, round((original - price) / original * 100.0, 2)


def _row_from_hit(hit: dict, page: int, position: int, url: str,
                  category: Optional[str], ld: Dict[str, dict],
                  ld_currency: Optional[str], kind: str) -> Optional[Product]:
    absolute = _en(hit.get("absolute_url"))
    if not absolute:
        return None
    sku = sku_from_url(absolute)
    item = (ld.get(sku) or {}) if sku else {}
    offer = _ld_offer(item.get("offers"))

    price = _number(hit.get("price"))
    if price is None:
        price = _number(offer.get("price"))
    original, discount = _discount(price, _number(hit.get("pre_discount_price")))

    currency = None
    if price is not None:
        currency = (offer.get("priceCurrency") if isinstance(offer.get("priceCurrency"), str)
                    else None) or ld_currency or host_currency(url)

    source = "next_data+jsonld" if item else "next_data"
    if price is None:
        source += ":no-price"

    brand = None
    brand_node = item.get("brand")
    if isinstance(brand_node, dict):
        brand = _clean(brand_node.get("name")) or None
    elif isinstance(brand_node, str):
        brand = _clean(brand_node) or None

    seller_name = _en((offer.get("offeredBy") or {}).get("name")) if isinstance(
        offer.get("offeredBy"), dict) else None
    if not seller_name:
        agent = hit.get("agent")
        if isinstance(agent, dict):
            seller_name = _en(agent.get("name"))

    availability = offer.get("availability")
    in_stock = None
    if isinstance(availability, str) and availability:
        in_stock = availability.rstrip("/").rsplit("/", 1)[-1].lower() == "instock"

    places = _en_list(hit.get("location_list")) or _en_list(hit.get("places"))
    if not places:
        city_node = hit.get("city")
        hood = _en_list(hit.get("neighborhoods"))
        places = [p for p in ([_en(city_node)] if isinstance(city_node, dict) else []) + hood if p]
    city = _en(hit.get("site")) or _en(hit.get("city"))
    if not city and len(places) > 1:
        city = places[1]

    attributes = _attributes(hit)
    year = _int((attributes or {}).get("year"))
    kilometers = _int((attributes or {}).get("kilometers"))

    verified = hit.get("is_verified")
    if verified is None:
        verified = hit.get("is_verified_user") or hit.get("is_verified_business") or None

    return Product(
        url=absolute,
        sku=sku,
        title=_en(hit.get("name")) or _clean(item.get("name")) or None,
        brand=brand,
        price=price,
        currency=currency,
        original_price=original,
        discount_pct=discount,
        # Both left null deliberately: this site rates sellers, never ads.
        rating=None,
        review_count=None,
        in_stock=in_stock,
        image_url=_image(hit) or _ld_image(item.get("image")),
        category=category or category_from_url(absolute) or category_from_url(url),
        price_source=source,
        page=page,
        position=position,
        vertical=vertical_of(absolute) or vertical_of(url),
        listing_id=_int(hit.get("id")),
        listing_uuid=hit.get("uuid") if isinstance(hit.get("uuid"), str) else None,
        short_url=(hit.get("permalink") or hit.get("short_url")
                   if isinstance(hit.get("permalink") or hit.get("short_url"), str) else None),
        city=city,
        location=" > ".join(places) if places else None,
        seller_name=seller_name,
        seller_kind=_seller_kind(hit),
        seller_id=_int(hit.get("user_id")) or _int((hit.get("agent") or {}).get("id")
                                                   if isinstance(hit.get("agent"), dict) else None),
        is_verified=bool(verified) if verified is not None else None,
        is_premium=bool(hit["is_premium"]) if isinstance(hit.get("is_premium"), bool)
        else (bool(hit["is_premium_ad"]) if isinstance(hit.get("is_premium_ad"), bool) else None),
        listing_kind=kind,
        photos_count=_int(hit.get("photos_count")),
        posted_at=_epoch_iso(hit.get("created_at")),
        bumped_at=_epoch_iso(hit.get("added")),
        payment_frequency=_en(hit.get("payment_frequency")),
        bedrooms=_int(hit.get("bedrooms")),
        bathrooms=_int(hit.get("bathrooms")),
        size_sqft=_number(hit.get("size")),
        year=year,
        kilometers=kilometers,
        attributes=attributes,
    )


def parse_products(html: str, url: str, page: int = 1,
                   position_offset: int = 0,
                   category: Optional[str] = None) -> List[Product]:
    """Rows for one listing page, in the payload's own order.

    Payload order is page order, which is what §8 requires: a dedupe that
    mutates a running set inside a fetch loop makes the output depend on
    which page finished first.

    The promoted Car of the Week is emitted AFTER the organic results and
    labelled, even though the site renders it first. It is the same ad on
    every page of a run, so leaving it in position 1 of each page would make
    the first organic result of every page look like position 2 — and a
    consumer sorting by position would read the same car 400 times.
    """
    props = next_data(html)
    payload = listings_payload(props)
    hits = payload.get("hits")
    hits = hits if isinstance(hits, list) else []
    promoted = payload.get("cotwListings")
    promoted = promoted if isinstance(promoted, list) else []

    soup = BeautifulSoup(html or "", "html.parser")
    if not hits and not promoted:
        # The fallback runs only where a grid was ASKED for. Every page on
        # this site carries ad links in its promo rails — the home page has
        # 45 — so a fallback that fired on any URL would turn a hub into a
        # page of rows built out of carousel text.
        if listing_kind(url) != "listing":
            return []
        return _dom_only_rows(soup, url, page, position_offset)

    ld = jsonld_items_by_url(html)
    ld_currency = jsonld_currency(html)

    rows: List[Product] = []
    position = position_offset
    for group, kind in ((hits, "organic"), (promoted, "car_of_the_week")):
        for hit in group:
            if not isinstance(hit, dict):
                continue
            position += 1
            row = _row_from_hit(hit, page, position, url, category, ld,
                                ld_currency, kind)
            if row is None:
                position -= 1
                continue
            rows.append(row)
    return rows


def _tile_of(anchor: Any) -> Any:
    """The smallest ancestor that still covers exactly ONE ad.

    Counts distinct ad URLs, not links (§4). Capped at 8 levels so a
    malformed document cannot walk to <body>: on this site the tile is one
    level up and the grid — 26 ads — is two, so stopping late would give
    every row its neighbours' price.
    """
    node = anchor
    best = anchor
    for _ in range(8):
        parent = getattr(node, "parent", None)
        if parent is None or getattr(parent, "name", None) in (None, "body", "html"):
            break
        hrefs = {a.get("href") for a in parent.select("a[href]")
                 if _is_ad_href(a.get("href"))}
        if len(hrefs) > 1:
            break
        best = parent
        node = parent
    return best


def _is_ad_href(href: Optional[str]) -> bool:
    if not isinstance(href, str) or not href:
        return False
    return bool(_AD_PATH_RE.search(urlsplit(href).path))


def _absolutise(href: str, url: str) -> str:
    """A tile's href against the BROWSED host.

    The site renders listing links as absolute paths on the browse host and
    the payload publishes them on the ad's emirate subdomain, so a DOM-only
    row's URL is not byte-identical to a payload row's. Said here rather than
    silently: the two agree on `sku`, which is the path, and that is what
    dedupe and diff use.
    """
    if href.startswith("http://") or href.startswith("https://"):
        return href
    s = urlsplit(url or "")
    base = "%s://%s" % (s.scheme or "https", s.netloc or "uae.dubizzle.com")
    return base + (href if href.startswith("/") else "/" + href)


def _dom_only_rows(soup: BeautifulSoup, url: str, page: int,
                   position_offset: int) -> List[Product]:
    """The fallback path: tiles with no payload behind them.

    Kept because the payload is one Redux action name away from a redesign
    and a URL shape is not. It cannot recover the payload-only columns and
    says so in `price_source` rather than leaving a consumer to guess why
    two thirds of the row is null.
    """
    rows: List[Product] = []
    seen: set = set()
    position = position_offset
    for anchor in soup.select("a[href]"):
        href = anchor.get("href")
        if not _is_ad_href(href):
            continue
        absolute = _absolutise(href, url)
        sku = sku_from_url(absolute)
        if not sku or sku in seen:
            continue
        seen.add(sku)
        tile = _tile_of(anchor)
        # The tile's own title node, not the anchor's text: an anchor wraps
        # the whole card here, so its text is the price, the badges, the
        # specs and the location run together.
        heading = tile.select_one(SELECTORS["tile_title"])
        image = tile.select_one("img[alt]")
        title = (_clean(heading.get_text(" ", strip=True)) if heading is not None else "")
        if not title and image is not None:
            # The site alt-texts a tile photo with the ad's title and a
            # "-0" index suffix.
            title = re.sub(r"-\d+$", "", _clean(image.get("alt") or ""))
        price_node = tile.select_one(SELECTORS["tile_price"])
        # The WRAPPER, because the ISO code lives in the price node's sibling
        # and the node itself holds only the digits.
        price_text = ""
        if price_node is not None:
            holder = price_node.parent if price_node.parent is not None else price_node
            price_text = holder.get_text(" ", strip=True)
        price = price_in(price_text)
        position += 1
        rows.append(Product(
            url=absolute,
            sku=sku,
            title=title or None,
            price=price,
            currency=host_currency(url) if price is not None else None,
            image_url=(tile.select_one("img[src]") or {}).get("src")
            if tile.select_one("img[src]") is not None else None,
            category=category_from_url(absolute) or category_from_url(url),
            price_source="dom" if price is not None else "dom:no-price",
            page=page,
            position=position,
            vertical=vertical_of(absolute) or vertical_of(url),
            listing_kind="organic",
        ))
    return rows
