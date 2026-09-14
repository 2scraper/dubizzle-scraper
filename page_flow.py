"""page_flow.py — what to do with the page dubizzle just gave us.

dubizzle answers a request five ways, and four of them want a different
response, which is why this module exists rather than the same triage being
written three times inside three engines and drifting apart (§1):

    content    the site served its listing payload
    empty      served exactly as asked and holding no listing — a page past
               the end of the catalogue, a 404 category, or a hub URL. It
               must NOT be parsed: a motors page past the end still renders
               one fully-formed Car of the Week ad, and parsing it would
               write one phantom row per exhausted page
    shell      served, built from the site's own assets, payload not there
               yet. Wants a WAIT, not a refetch
    challenge  something solvable was rendered
    blocked    Imperva refused — either the 1,160-byte "Request unsuccessful.
               Incapsula incident ID" iframe or the 6,183-byte "Pardon Our
               Interruption" page, and the second of those is served with
               **HTTP 200**

The policy lives in `STATE_POLICY` as DATA, so an engine cannot quietly
disagree with its twins about whether a page is worth retrying or worth
paying for.

Everything here is pure or driven through small callables, so each engine
passes its own driver's primitives and keeps its browser plumbing to itself:

    count(selector) -> int          how many elements match
    content() -> Optional[str]      current HTML, None if unavailable
    sleep(ms) -> None               wait

No JavaScript crosses that boundary in either direction (§1): Selenium's
`execute_script` takes a function BODY with an explicit `return` where
Playwright and pyppeteer take `() => expr`, so the shared module names the
OPERATION and the engine spells it in its own driver's dialect.

This module has NO scroll loop, and that is measured rather than assumed
-----------------------------------------------------------------------
A listing page holds its whole page of ads in the first response and
paginates by URL. The three captures taken with NO scrolling at all
(`/classified/electronics/televisions/`, `/jobs/accounting-finance/`,
`/community/auto-services/`) carried 25 payload hits and 25 rendered tiles
each — the same counts as the captures taken with four scroll rounds. So a
scroll here would be latency bought for nothing. The opposite of a sibling
repo, where the scroll is the only way any product is ever seen — which is
exactly why this was checked instead of ported.
"""

from __future__ import annotations

import logging
from typing import Callable, Dict, Iterable, List, Optional, Union
from urllib.parse import urljoin, urlsplit

from product_parser import (detect_block_marker, detect_bot_challenge,
                            detect_page_state, listing_kind, listings_payload,
                            next_data, page_url, page_number_from_url,
                            paginates_by_url, served_by_dubizzle,
                            strip_tracking, total_pages, PAGE_CAP)

logger = logging.getLogger("page_flow")


# ---------------------------------------------------------------------------
# Readiness
# ---------------------------------------------------------------------------
# The anchor is a price node WITH SOMETHING IN IT, not a card count, for the
# reason §8 gives: a selector matching the wrong element is worse than one
# matching nothing. A tile's shell and its price node can both exist while
# the amount is still blank, and a wait satisfied by the shell is a wait that
# did not wait.
#
# What a timed-out wait actually costs here is small and worth stating: every
# column but the DOM cross-check comes out of `__NEXT_DATA__`, which is in
# the first response's HTML with no JavaScript run at all. A wait that times
# out costs the `next_data+jsonld` confirmation, not the run. That is
# measured rather than hoped: stripping the payload out of three captures and
# re-parsing left the DOM path recovering 26, 35 and 25 rows with the same
# skus and ZERO price disagreements — the two paths are independent and they
# agree.
READY_SELECTOR_LISTING = '[data-testid="listing-price"]'

# Above 1, per §5: waiting for a single match resolves on an unrelated node
# long before a grid paints. Below the smallest real page, which is 25 ads on
# motors, classified, jobs and community and 35 on both property indexes —
# so 8 leaves room for a page that is genuinely short (a jobs category with
# 148 ads has a last page of 23) without ever being satisfied by one stray
# node.
MIN_CARD_MATCHES = 8

# Generous on purpose: a residential UAE exit and a cold cache are both
# slower than a developer's laptop, and the cost of waiting too long is
# latency where the cost of waiting too little is a run with no DOM
# confirmation in it.
CONTENT_TIMEOUT_MS = 20_000

# One mode. This repo reads listing grids; `ready_selector` and `min_matches`
# keep their signature so the engines and the family's checks bind the same
# way they do in the siblings.
_READY = {"listing": READY_SELECTOR_LISTING}
_MIN = {"listing": MIN_CARD_MATCHES}


def ready_selector(mode: str = "listing") -> str:
    return _READY.get(mode, READY_SELECTOR_LISTING)


def min_matches(mode: str = "listing", expected: Optional[int] = None) -> int:
    """How many matches mean "this page painted".

    `expected` is the page's own hit count where the caller knows it, because
    this site states it: a last page of a jobs category holds 23 ads and
    waiting for 8 of them is right, while waiting for 8 on a page that holds
    3 would time out on a page that is fully painted. Never above the floor
    and never below 2 (§5).
    """
    floor = _MIN.get(mode, MIN_CARD_MATCHES)
    if expected is None:
        return floor
    return max(2, min(floor, expected))


def content_timeout_ms(mode: str = "listing") -> int:
    return CONTENT_TIMEOUT_MS


def expected_cards(html: Optional[str]) -> Optional[int]:
    """How many ads this page's own payload says it holds.

    Used to size the readiness wait rather than to build rows — the rows come
    from the payload itself.
    """
    hits = listings_payload(next_data(html)).get("hits")
    if not isinstance(hits, list):
        return None
    promoted = listings_payload(next_data(html)).get("cotwListings")
    extra = len(promoted) if isinstance(promoted, list) else 0
    return len(hits) + extra


def wait_for_count(count: Callable[[str], int], sleep: Callable[[int], None],
                   selector: str, minimum: int, timeout_ms: int,
                   poll_ms: int = 250) -> int:
    """Poll a selector's match count until it reaches `minimum`.

    A COUNT through the driver's own query, never a string handed to the page
    to evaluate. A sibling repo's readiness wait died with
    `EvalError: Evaluating a string as JavaScript violates the following
    Content Security Policy directive` on a site whose CSP has no
    `unsafe-eval`, and took the run down with exit 1 on the site's most
    obvious URL (§18). Counting elements goes through the protocol and works
    under any CSP, and spells the same in all three drivers.
    """
    waited = 0
    seen = 0
    while waited <= timeout_ms:
        try:
            seen = count(selector)
        except Exception as exc:               # a driver-specific failure
            logger.debug("readiness count failed: %s", exc)
            seen = 0
        if seen >= minimum:
            return seen
        sleep(poll_ms)
        waited += poll_ms
    return seen


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------
def classify(html: Optional[str], status: Optional[int] = None,
             url: str = "") -> str:
    """Which of the five states this response is.

    `status` is positional and comes SECOND, matching `detect_page_state`.
    Getting that wrong is not a style question: a sibling repo shipped two of
    three engines calling this as `classify(html, url=...)`, both crashed on
    their first fetch, and nothing short of a live run or a signature-binding
    check saw it (§17).
    """
    if html is None:
        return "blocked"
    return detect_page_state(html, status, url)


# The retry/solve/blocked decision as DATA rather than as three copies of an
# if-chain in three engines (§1).
#
#   parse    is there anything on this page worth writing down?
#   retry    would fetching it again, from a different exit, plausibly help?
#   solve    is there something to pay a solver for?
#   blocked  does this count towards exit 3?
STATE_POLICY: Dict[str, Dict[str, bool]] = {
    "content":   {"parse": True,  "retry": False, "solve": False, "blocked": False},
    # NOT parsed, and on this site that is load-bearing rather than tidy. A
    # motors listing one page past its end answers HTTP 200 with
    # `totalPages: 0, totalHits: 0` and the site's own "We couldn't find any
    # results" sentence — and STILL renders one Car of the Week ad, with a
    # price node, a JSON-LD item and a real listing URL. Parsing an `empty`
    # page would write one plausible, well-formed phantom row for every page
    # a run overshot by, and nothing downstream could tell them from
    # results. A hub URL is the same story with 45 rail links instead of one.
    "empty":     {"parse": False, "retry": False, "solve": False, "blocked": False},
    # Served, and still painting. Wants the readiness wait, not another
    # fetch: refetching a shell just buys another shell (§18).
    "shell":     {"parse": True,  "retry": False, "solve": False, "blocked": False},
    "challenge": {"parse": False, "retry": True,  "solve": True,  "blocked": False},
    "blocked":   {"parse": False, "retry": True,  "solve": False, "blocked": True},
}


def should_parse(state: str) -> bool:
    return STATE_POLICY.get(state, STATE_POLICY["blocked"])["parse"]


def should_retry(state: str) -> bool:
    return STATE_POLICY.get(state, STATE_POLICY["blocked"])["retry"]


def should_solve(state: str) -> bool:
    return STATE_POLICY.get(state, STATE_POLICY["blocked"])["solve"]


def counts_as_blocked(state: str) -> bool:
    return STATE_POLICY.get(state, STATE_POLICY["blocked"])["blocked"]


def is_unpainted(state: str, html: Optional[str]) -> bool:
    """Whether this page is served but has not filled its grid in yet.

    Distinguished from a refusal by the site's own assets, which an
    interstitial does not carry.
    """
    if state != "shell":
        return False
    return served_by_dubizzle(html or "")


# On this site rotating the exit DOES help, and that is measured rather than
# assumed — it is the opposite of a sibling repo, where the client mattered
# and the address did not:
#
#   a European residential address, headful Chrome  -> HTTP 403, 1,160 bytes
#   the same address, plain HTTP with a browser UA  -> the Incapsula iframe
#   a Finnish datacentre exit (the Scraper API)     -> "Pardon Our
#                                                      Interruption", HTTP 200
#   a UAE residential exit, headless Chromium       -> HTTP 200, 1.6 MB,
#                                                      the full catalogue
#
# So the discriminator here is the ADDRESS, and specifically whether it is in
# the country the site serves. A retry from a different exit is worth
# spending, and the engines read these constants rather than computing their
# own budget — a policy constant nothing consults is the same defect as dead
# code (§17).
RETRY_ON_BLOCKED = True
BLOCK_RETRIES_WITHOUT_POOL = 1
BLOCK_RETRIES_WITH_POOL = 3

# One solve per page. Detection is broad on purpose, but a second solve on
# the same page has never been the answer to the first one failing.
SOLVES_PER_PAGE = 1


def block_advice(html: Optional[str], headless: bool,
                 has_pool: bool) -> str:
    """What a reader should actually DO about this block.

    Exists because the honest answer differs per site and a generic "try a
    proxy" wastes an afternoon where it is wrong. Here it happens to be
    right — and the reason is specific enough to be worth printing: the exit
    has to be IN THE UAE.
    """
    marker = detect_block_marker(html or "") or "no marker"
    country = ("Imperva fronts this site and refuses addresses outside the "
               "UAE: a European residential exit and a Finnish datacentre "
               "exit were both refused, headful and headless alike, while a "
               "UAE residential exit was served the full catalogue.")
    if not has_pool:
        return (f"blocked ({marker}) with no proxy pool. {country} "
                f"Use --proxy with a UAE exit, or --cdp-endpoint with "
                f"`country-ae` in the Scraping Browser login.")
    return (f"blocked ({marker}) with a proxy pool. {country} "
            f"Check that the pool's exits are UAE ones — rotation between "
            f"non-UAE addresses will not clear this. "
            f"{'Running headless is not the cause here.' if headless else ''}")


# ---------------------------------------------------------------------------
# Pagination
# ---------------------------------------------------------------------------
# Three layers, weakest last (§7), and on this site every one of them is
# available and they agree:
#
#   1. `<link rel="next">` in <head> — a standards-based signal, present on
#      every listing page captured, spelling exactly `?page=N`.
#   2. `?page=N` rebuilt by `product_parser.page_url`.
#   3. A data terminator: the payload's own `totalPages`, and past the end
#      `totalHits: 0`.
#
# Layer 3 is the one to trust. Past its last page this site does not clamp,
# it EMPTIES: `?page=401` and `?page=99999` on a 400-page motors listing both
# answered HTTP 200 with `totalPages: 0, totalHits: 0`. That is a cleaner
# terminator than "this page added no new sku", because it is the site's own
# statement rather than an inference from what we happened to receive.
NEXT_PAGE_SELECTOR = (
    'link[rel="next"], '
    'a[rel="next"][href*="page="], '
    'nav a[href*="page="], '
    'a[href*="page="]'
)


def next_page_selector(page_num: int = 1) -> str:
    return NEXT_PAGE_SELECTOR


def pagination_is_addressable(page1_url: str,
                              next_href: Optional[str] = None) -> bool:
    """Whether page N can be fetched without walking pages 2..N-1.

    True for every listing grid here, and verified the way §7 asks rather
    than assumed: page 1's own `<link rel="next">` is
    `https://uae.dubizzle.com/motors/used-cars/?page=2`, which is exactly
    what the convention builds, and the fetched page 2 reported
    `pagination.page: 1` — the 0-based index of URL page 2. The site and the
    convention agree, so page URLs can be planned up front and handed to
    independent workers.
    """
    if not paginates_by_url(page1_url):
        return False
    if next_href and "page=" not in next_href:
        # The site's own next link does not spell what the convention builds.
        # Chain link-to-link rather than guessing.
        return False
    return True


def _resolve(base_url: str, href: str) -> str:
    """A possibly-relative href as an absolute URL.

    Load-bearing, not tidiness: this site's in-page links are relative
    (`/motors/used-cars/?page=2`) while its `<link rel="next">` is absolute,
    so a comparison that does not resolve first fails for half the links it
    is given. That reads as "the site's link disagrees with the convention",
    which sends the caller down the link-chaining path and quietly costs it
    `--concurrency` (§7) — while looking like a deliberate safety decision.
    """
    return urljoin(base_url, href) if href else href


def _as_hrefs(next_href: Union[str, Iterable[str], None]) -> List[str]:
    """One href or many, always as a list.

    Both spellings are real: an engine hands over EVERY advertised link so
    the filtering rule lives here instead of in three engines, while a test
    or a caller holding one link passes the string. Accepting only the string
    is what took a sibling repo's FIRST live run down — `TypeError: Cannot
    mix str and non-str arguments`, from `urljoin` being handed a list — and
    nothing short of running it saw that, because the offline checks called
    it the way its author was thinking (§17).
    """
    if next_href is None:
        return []
    if isinstance(next_href, str):
        return [next_href] if next_href else []
    return [h for h in next_href if isinstance(h, str) and h]


def pagination_agrees(current_url: str, page_num: int,
                      next_href: Union[str, Iterable[str], None]) -> bool:
    """Whether the site's own next link matches what the convention builds.

    True when ANY advertised link agrees. A listing page carries links to
    several of its own pages at once, and the question here is whether the
    CONVENTION is the site's, not whether every link happens to be page N+1.
    """
    hrefs = _as_hrefs(next_href)
    if not hrefs:
        return True
    want = page_url(current_url, page_num + 1)
    if not want:
        return False
    target = comparable(want)
    return any(comparable(_resolve(current_url, h)) == target for h in hrefs)


def next_page_candidates(current_url: str,
                         next_href: Union[str, Iterable[str], None] = None
                         ) -> List[str]:
    """Addresses worth trying for the page after `current_url`, best first.

    Takes one href or the whole advertised set, and FILTERS it: a listing
    page links to plenty of OTHER listings — subcategories, neighbouring
    makes, the SEO chip rail — and chaining onto one of those returns rows
    from the wrong catalogue while reporting success.
    """
    page_num = page_number_from_url(current_url)
    out: List[str] = []
    built = page_url(current_url, page_num + 1)
    if built:
        out.append(built)
    for href in _as_hrefs(next_href):
        resolved = _resolve(current_url, href)
        known = {comparable(u) for u in out}
        # The number has to be the NEXT one, not merely a legal one: a
        # listing page advertises links to several of its own pages at once,
        # so accepting any of them as "the page after this" would let page
        # 40 be fetched as page 2 the moment the built URL failed.
        if (comparable(resolved) not in known
                and _same_listing(current_url, resolved)
                and page_number_from_url(resolved) == page_num + 1
                and page_num + 1 <= PAGE_CAP):
            out.append(resolved)
    return out


def _same_listing(current_url: str, candidate: str) -> bool:
    """Whether a candidate is another page of the SAME listing."""
    a, b = urlsplit(current_url), urlsplit(candidate)
    if b.netloc and a.netloc and a.netloc != b.netloc:
        return False
    return a.path.rstrip("/") == b.path.rstrip("/")


def pages_at_cap(html: Optional[str]) -> bool:
    """Whether this listing is deeper than the site will address.

    Reported rather than swallowed: a motors listing of 34,619 ads publishes
    exactly 400 pages of 25, so 24,619 of them cannot be reached through
    pagination at all. A run that stops at 400 is complete as far as the site
    is concerned and a third of the way through as far as the catalogue is,
    and only saying so lets a consumer tell the two apart.
    """
    pages = total_pages(html or "")
    return pages is not None and pages >= PAGE_CAP


def comparable(url: str) -> str:
    """A URL reduced to what makes two spellings the same page."""
    parts = urlsplit(strip_tracking(url))
    host = (parts.hostname or "").lower()
    host = host[4:] if host.startswith("www.") else host
    return "%s%s?%s" % (host, parts.path.rstrip("/"), parts.query)


# ---------------------------------------------------------------------------
# Concurrency
# ---------------------------------------------------------------------------
def concurrency_limit(url: str) -> Optional[int]:
    """The highest `--concurrency` this URL can honestly support."""
    return None if paginates_by_url(url) else 1


def concurrency_refusal(url: str) -> Optional[str]:
    """Why concurrency above 1 is refused for this URL, or None.

    Refused WITH the reason (§18): an individual ad and a hub page have no
    page 2 at all, so there is nothing for a second worker to fetch, and
    silently running one worker would look like the flag did something.
    """
    kind = listing_kind(url)
    if kind == "ad":
        return ("that URL is one advertisement, not a listing; --concurrency "
                "above 1 has nothing to fetch. Pass the category it sits in.")
    if kind == "home":
        return ("a hub page carries no result grid, only category tiles and "
                "promo rails; --concurrency above 1 has nothing to fetch")
    if not paginates_by_url(url):
        return "%r is not a paginated dubizzle listing" % (url,)
    return None
