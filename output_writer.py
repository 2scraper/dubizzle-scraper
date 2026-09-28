"""
output_writer.py
-----------------
Shared row models + JSON/CSV writers used by all three scrapers.

One mode, one row shape
-----------------------
    --mode listing   a category listing -> Product

There is deliberately no detail mode. An individual ad's page has not been
captured or measured, and a mode that ships untested would be worse than a
mode that is absent. So there is one dataclass here (a sibling repo needs a
second one for reviews; this one does not).

`Product` keeps the family's first sixteen columns in the family's order,
with this site's own ones appended after `position`, so a consumer
written against another repo in this family still reads the prefix unchanged.

Everything below is row-class-agnostic: pass `row_cls` so an empty CSV still
gets the right header for the mode that produced it.
"""

import csv
import json
from dataclasses import dataclass, asdict, field, fields
from datetime import datetime, timezone
from typing import Optional, List, Set, Sequence, Any, Type


# The hostname a row came from. dubizzle's UAE platform is ONE storefront
# reached through several hostnames — the aggregate `uae.dubizzle.com` a
# listing is browsed on, and the eight emirate subdomains
# (`dubai.`, `abudhabi.`, `sharjah.`, `ajman.`, `rak.`, `uaq.`, `fujairah.`,
# `alain.`) every individual listing URL is published under. They serve one
# catalogue with one currency, so this column is `dubizzle.com` on every row
# of every run rather than the hostname of the moment; the emirate a listing
# sits in is a fact about the AD and is carried in `city`.
#
# It is kept in this position because the family's schema has it here and
# consumers read the columns by name across repos.
SOURCE_DEFAULT = "dubizzle.com"


@dataclass
class Product:
    """One row per LISTING.

    The first eighteen fields are the family prefix, byte-identical and in
    the same order as every other repo in this family, so one consumer reads
    a dubizzle run and a mediamarkt run with the same code. Everything after
    `position` is dubizzle's own.

    A classifieds site is not a shop, and two of the prefix's assumptions
    need saying out loud rather than being quietly wrong:

      * A listing is a SINGLE second-hand item, not a stocked product. There
        is no assortment behind it and no restock, so `in_stock` means "the
        ad is still live", which is the only availability this site states.
      * Five verticals share this schema and they do not publish the same
        facts. `bedrooms` is null on every car and `kilometers` is null on
        every flat. That is a property of the vertical and not a parsing
        failure, which is why `vertical` is a column: a consumer can select
        the rows a field is meaningful for instead of guessing from nulls.
    """
    source: str = SOURCE_DEFAULT
    scraped_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    # The listing's own absolute URL, on its emirate subdomain — the address
    # the site itself publishes, not one rebuilt from the browsed host. Two
    # sibling repos lost a whole column by rebuilding a URL that the page
    # then never matched (§5).
    url: str = ""
    # The listing's URL PATH, with a leading `/ar` stripped and no trailing
    # slash.
    #
    # A path rather than an id, and the reason is measured: this site
    # numbers its verticals differently. A motors, classified, jobs or
    # community URL ends in `---{32-hex uuid}`, so an id is recoverable from
    # it; a PROPERTY URL ends in `-{city-id}-{ref}` and carries neither the
    # listing's `id` (27794657) nor its dashed `uuid`
    # (b76a3b0a-ef13-441a-...) anywhere. An id-based sku would therefore be
    # null on every property row the DOM fallback produced, and would differ
    # in kind between two verticals of one site.
    #
    # The path is present on every row of every path — payload, JSON-LD and
    # DOM alike — which is what a dedupe key and a two-run diff need. The
    # `/ar` strip is what makes an Arabic run and an English run of the same
    # category diff as the same listings rather than as a wholesale
    # replacement.
    #
    # `listing_id` and `listing_uuid` below carry the site's own identifiers
    # where it states them.
    sku: Optional[str] = None
    title: Optional[str] = None
    # The manufacturer, and ONLY where the site publishes one as a fact:
    # `brand.name` in a motors page's JSON-LD, which is the make the seller
    # picked from the taxonomy. Null on property, jobs and community, where
    # there is no manufacturer to name, and null on classified, whose
    # JSON-LD carries no ItemList at all (measured: 0 of 25 tiles on
    # /classified/electronics/televisions/, 2026-09-14).
    #
    # Deliberately NOT derived by splitting a title or reading the URL's
    # make segment. Both would be a guess wearing the costume of a fact
    # (§8), and the URL segment is the CATEGORY's make, which for a listing
    # filed under the wrong node is not the car's.
    brand: Optional[str] = None
    # What the seller is asking. An integer in the site's payload — no
    # decimals anywhere in 195 listings across five verticals — and null
    # rather than 0 when the listing states none.
    #
    # Null is COMMON here and is not a failure: a job ad publishes no salary
    # (25 of 25 null on /jobs/accounting-finance/), a services ad usually
    # publishes no fee (24 of 25 null on /community/auto-services/), and a
    # property listing may set `is_price_hidden`. `price_source` says which
    # of those a null is.
    price: Optional[float] = None
    # AED, and on the UAE platform that is a fact rather than a guess: the
    # motors and property JSON-LD state `priceCurrency: "AED"` outright, and
    # the tile prints the ISO code `AED` in its own node beside the amount.
    # Still null rather than defaulted when there is no price — a row with
    # no price has no currency either (§4).
    currency: Optional[str] = None
    # The was-price, from `pre_discount_price`. Sparse ON PURPOSE and worth
    # keeping: only a promoted "Car of the Week" or a classified listing
    # carries the field at all, and it is `0` far more often than not — 0 is
    # the site's way of writing "no discount" and is read as None here, not
    # as a free car.
    original_price: Optional[float] = None
    # Computed from the two prices, never read off the printed badge, and
    # None rather than 0 or a negative when `original_price` is not above
    # `price` — two figures that are not what they were taken for should
    # produce no number at all (§4).
    discount_pct: Optional[float] = None
    # Null on every listing row of every run measured so far, and kept only
    # because it is in the family prefix: this site rates SELLERS on a
    # profile page, never the individual ad, and no listing tile or payload
    # in 195 listings across five verticals carries a rating. Stated here so
    # that nobody re-derives it from the seller's stars, which would put a
    # different quantity in a column the family uses for the item's own.
    rating: Optional[float] = None
    review_count: Optional[int] = None
    # "The ad is still live", the only availability this site states —
    # `offers.availability` in the motors and property JSON-LD, which is
    # `InStock` on every live listing. Null where the vertical publishes no
    # JSON-LD rather than assumed true.
    in_stock: Optional[bool] = None
    image_url: Optional[str] = None
    category: Optional[str] = None
    # Provenance, and it is doing real work here because three sources
    # overlap and do not carry the same columns:
    #
    #   next_data+jsonld  the payload, cross-checked against the page's own
    #                     JSON-LD, which is where `brand`, `seller_name` and
    #                     `in_stock` come from. Motors and property only.
    #   next_data         the payload alone — classified, jobs, community,
    #                     whose pages publish no ItemList.
    #   dom               the fallback: a tile with no payload behind it.
    #                     Carries url, sku, title, price, currency and
    #                     nothing else.
    #
    # A `:no-price` suffix on any of them means the source named this
    # listing and it states no price — which distinguishes "the site
    # published none" from "we failed to read one", where a bare null
    # cannot.
    price_source: Optional[str] = None
    page: Optional[int] = None
    position: Optional[int] = None

    # ---- dubizzle's own ----------------------------------------------
    # Which of the site's seven top-level sections this listing is in, taken
    # from the URL's first path segment: motors, property-for-rent,
    # property-for-sale, classified, jobs, jobs-wanted, community. The
    # column exists so a consumer can filter before reading a field that
    # only one vertical publishes.
    vertical: Optional[str] = None
    # The site's own numeric id for the ad, from the payload. Stable across
    # an edit that changes the slug, which `sku` is not, so this is the
    # column to join on when tracking one ad over time.
    listing_id: Optional[int] = None
    # The payload's `uuid`: 32 hex characters on motors, classified, jobs
    # and community, a dash-separated UUID on property. Both are the site's,
    # copied verbatim rather than normalised into one shape they do not
    # share.
    listing_uuid: Optional[str] = None
    # The site's own short link (`permalink` on motors and classified,
    # `short_url` on property) — `https://dubizzle.com/s/DOBJL9S`. Survives
    # a slug edit and is what the site's own share button copies.
    short_url: Optional[str] = None
    # The emirate, from the payload's `site` (motors, classified, jobs,
    # community) or `city` (property). This is why `source` is not the
    # hostname: a listing browsed on `uae.dubizzle.com` lives in Ajman, and
    # the hostname of the browse would have hidden that.
    city: Optional[str] = None
    # The full place chain the site states, joined with " > " — e.g.
    # "UAE > Ajman > Al Jurf > Al Jurf Industrial Area". Kept as the site
    # writes it rather than split into columns, because the chain is a
    # different depth per emirate and per vertical.
    location: Optional[str] = None
    # Who is selling, where the page names them: `offers.offeredBy.name` in
    # the JSON-LD (the dealership or agency) or the property payload's
    # `agent`. Null for a private seller, who the site deliberately does not
    # name on a listing page.
    seller_name: Optional[str] = None
    # Normalised from two different fields that mean the same thing:
    # motors' `seller_type` ("OW"/"DL") and property's `listed_by`
    # ("AG"/"OW"). Written out as "owner", "dealer" or "agent" so one value
    # means one thing across verticals.
    seller_kind: Optional[str] = None
    seller_id: Optional[int] = None
    # The site's own verification badge on the ad or its seller. Not a
    # quality judgement of ours — just which flag the payload set.
    is_verified: Optional[bool] = None
    # A paid placement flag. Kept separate from `listing_kind` because they
    # answer different questions: this one is "did the seller pay to boost
    # it", that one is "why is this row on this page at all".
    is_premium: Optional[bool] = None
    # Why this row is on this page:
    #
    #   organic          it is a result of the query
    #   car_of_the_week  a single promoted motors listing the site injects
    #                    into every motors page, INCLUDING a page past the
    #                    end of the listing. It is a real ad and is kept,
    #                    but it repeats on every page of a run and is not a
    #                    result — which is why it is labelled rather than
    #                    silently merged.
    listing_kind: Optional[str] = None
    photos_count: Optional[int] = None
    # When the seller first posted, and when the ad was last bumped to the
    # top — the payload's `created_at` and `added`, both Unix seconds,
    # written as UTC ISO-8601. They differ: a car posted in February and
    # bumped this morning reads 2026-02-05 / 2026-09-13.
    posted_at: Optional[str] = None
    bumped_at: Optional[str] = None
    # Property only. A rent is quoted per period and the period is part of
    # the price: 105,000 AED is a year's rent, not a month's. Reading a
    # rent without this column would put a yearly and a monthly figure in
    # one column and call them comparable.
    payment_frequency: Optional[str] = None
    bedrooms: Optional[int] = None
    bathrooms: Optional[int] = None
    # Property only, square feet, as the site states it.
    size_sqft: Optional[float] = None
    # Motors only, promoted out of `attributes` because they are what a car
    # listing is actually filtered and compared on.
    year: Optional[int] = None
    kilometers: Optional[int] = None
    # Everything else the vertical publishes about this listing, as
    # {slug: value} in the site's own slugs and English values — motors'
    # `details_v2` (up to 20 entries: body type, fuel, transmission,
    # regional specs, warranty …) and property's `property_info`.
    #
    # A single column rather than 40 sparse ones, because the key set is
    # per-vertical and grows whenever the site adds a filter. The JSON
    # output keeps the real object; the CSV writes it as compact JSON in one
    # cell, which is lossless and parseable, unlike a Python repr.
    attributes: Optional[dict] = None


# One kind of thing, one dataclass. This repo reads LISTING pages and
# nothing else, so there is no second row class to keep in step.
ROW_CLASS_BY_MODE = {"listing": Product}

# Modes whose rows are one-per-sku, and therefore safe to dedupe on `sku` and
# to hand to diff_runs.py. Both of this repo's modes qualify: a listing page
# names each product once, and a product page IS one product.
UNIQUE_BY_SKU_MODES = ("listing",)


def dedupe_by_key(rows: Sequence[Any], seen: Set[str], key: str = "sku") -> List[Any]:
    """Drop rows whose key already appeared earlier in this same run.

    `seen` is mutated in place, so callers thread the same set across pages —
    a stale or repeating next-page link then re-parses a page without
    duplicating its rows into the final output. On this site this DOES fire
    on healthy runs: page 1 and page 2 of one category listing shared
    exactly 3 products, all three from the "cheaper products" carousel that
    appears on every page of a listing. So a small non-zero drop count here
    is expected and a large one is not.

    A row with no key is always kept: there is nothing to check a duplicate
    against, and dropping it would be a silent data loss rather than a
    duplicate removal.

    Both of this repo's modes are one row per `sku`, so `key` is never
    overridden here — the parameter exists because the rest of the family
    shares this function and one of them needs it.
    """
    fresh = []
    for r in rows:
        val = getattr(r, key, None)
        if val is None or val not in seen:
            if val is not None:
                seen.add(val)
            fresh.append(r)
    return fresh


# Kept under its old name: the engines and smoke tests in this family all
# call it, and a listing run does dedupe by sku.
def dedupe_by_sku(rows: Sequence[Any], seen: Set[str]) -> List[Any]:
    return dedupe_by_key(rows, seen, key="sku")


# CSV cannot hold a list. Joining with " | " keeps the cell readable in a
# spreadsheet and round-trippable by splitting on the same separator; the
# JSON output keeps the real list, so nothing is lost for a consumer that
# wants structure. `repr()` of a Python list (the default if this is not
# handled) is neither readable nor parseable by anything but Python.
LIST_CSV_SEPARATOR = " | "


def _csv_value(v: Any) -> Any:
    if isinstance(v, (list, tuple)):
        return LIST_CSV_SEPARATOR.join(str(x) for x in v)
    # `attributes` is a mapping, and a Python repr of one is neither
    # readable in a spreadsheet nor parseable by anything but Python.
    # Compact JSON is both, and round-trips through json.loads.
    if isinstance(v, dict):
        return json.dumps(v, ensure_ascii=False, separators=(",", ":"))
    return v


def write_json(rows: Sequence[Any], path: str) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump([asdict(r) for r in rows], f, ensure_ascii=False, indent=2)


def write_csv(rows: Sequence[Any], path: str, row_cls: Type = Product) -> None:
    # An empty result still gets the header row. A zero-byte file makes a
    # consumer fail on read (no columns to parse) instead of reading a valid
    # table with zero rows — and "an empty result is still a well-formed
    # result" is the same principle as `save` refusing to overwrite good data.
    #
    # The header comes from `row_cls`, not from the first row, so an empty
    # run still writes the columns of the mode that produced it.
    fieldnames = [f.name for f in fields(row_cls)]
    with open(path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for r in rows:
            writer.writerow({k: _csv_value(v) for k, v in asdict(r).items()})


# Exit code used when a run completes but produced nothing. Distinct from 1
# (crash) so a caller can tell "ran, found nothing" from "blew up".
EXIT_NO_PRODUCTS = 4

# Exit code for a run blocked by a bot-check/challenge page before parsing
# even started — distinct from EXIT_NO_PRODUCTS so a caller can tell "the
# search genuinely matched nothing" from "something stood between us and the
# content". See product_parser.detect_bot_challenge.
#
# On this site this code specifically does NOT cover the two ways to get a
# real page with no ads on it: a hub page (`/`, `/motors/`), which is
# category tiles and promo rails with no result grid; and one page past the
# end of a listing, which answers HTTP 200 with `totalHits: 0`. Both are
# EXIT_NO_PRODUCTS — the request was served exactly as asked and simply has
# no ads on it. Reporting either as blocked would send a user hunting for a
# proxy problem that does not exist.
#
# What EXIT_BLOCKED means here: Imperva refused the exit — an HTTP 403, or
# the "Pardon Our Interruption" page, which arrives as HTTP 200. Both are
# recognised by carrying none of the site's own asset hosts (see the README).
EXIT_BLOCKED = 3

# Exit code for a run that gathered SOME rows and then stopped early — a
# page-load timeout, a 503 throttle, or a challenge on page 3 of 10. The
# output file is still written (throwing away three good pages would be
# worse), but it is not a complete picture, and a consumer that cannot tell
# the difference will read the pages that were never fetched as products that
# disappeared from the catalogue. See write_run_meta.
# A REMOTE service failed — the Scraping Browser refusing the connection
# (`profile_locked` is the common one: a profile allows a single live
# connection), or the Scraper API answering an error. Distinct from 1 (a
# crash in this code) and from 2 (bad usage) because it means "try again, or
# use a different profile", not "there is a bug here". Defined once, here,
# because the browser engines and scraper_api_client.py both return it and
# two definitions of the same code is exactly how a family's exit contract
# drifts.
EXIT_API_ERROR = 5

EXIT_PARTIAL = 6


# Exit code for a run that never GOT its pages: a navigation timeout, a dead
# or unauthenticated proxy, a DNS failure, or an edge answering with
# something that is not the page that was asked for.
#
# Distinct from EXIT_NO_PRODUCTS because those are opposite facts. Exit 4 is
# a statement about the CATALOGUE — "we asked, and the answer was nothing" —
# so handing it to a run that never reached the site tells a pipeline the
# listing is empty when nothing was read at all.
#
# 5 rather than a new number, and 5 rather than EXIT_PARTIAL:
#
#   * this family's contract already reserves 5 for a transport failure
#     (scraper_api_client has used it for a remote API error since it was
#     written), so this needs no new code and no per-repo table for a caller
#     driving more than one of these scrapers;
#   * EXIT_PARTIAL (6) means "some rows were gathered and the output is
#     incomplete". A run holding nothing writes no output at all, so a
#     consumer that reads the file on a 6 finds either nothing or the
#     PREVIOUS run's good data, which `save` deliberately does not
#     overwrite. Exit 5 promises no file.
#
# Deliberately NOT applied when rows WERE gathered: a timeout on page 7 of
# 10 is a partial run (exit 6, output written), which is already right. This
# decides only what a run holding nothing reports.
EXIT_FETCH_FAILED = 5


def write_run_meta(out_prefix: str, meta: dict) -> str:
    """Write a run-metadata sidecar next to the output, return its path.

    Deliberately a separate `<out>.meta.json` rather than columns on every
    row: this describes the RUN, not the product, and repeating it across
    every row would both bloat the output and change the schema every
    consumer of this project already parses.

    diff_runs.py reads it to refuse a comparison between runs that are not
    both complete, and between runs of different `mode`.
    """
    path = f"{out_prefix}.meta.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)
    print(f"[+] Wrote run metadata -> {path} (status={meta.get('status')})")
    return path


def run_meta(status: str, stop_reason: str, pages_requested: int,
             pages_completed: int, start_url: str, final_url: str,
             products: int, pages_failed: Optional[List[int]] = None,
             mode: str = "listing", source: str = SOURCE_DEFAULT,
             extra: Optional[dict] = None) -> dict:
    """Build the metadata dict for a finished run.

    `status` is the field a consumer branches on:
      complete — every requested page was fetched, or the site's own
                 pagination genuinely ran out (nothing more existed to get)
      partial  — rows were gathered, then the run stopped early
      failed   — nothing was gathered at all

    `mode` and `source` are recorded because the family's sidecar carries
    them: in a sibling repo with several modes the output prefix alone does
    not say which one produced a file, and diff_runs.py refuses a pair whose
    modes or sources differ. Here `mode` is always "listing". `source` is `dubizzle.com` on every row of every run
    here, since the site has one storefront and one currency; it is kept
    because consumers read these columns by name across the family.

    `extra` carries facts about the run that are not about any single row,
    so they are not repeated down a column.

    `pages_failed` lists the pages that did not yield data, by number.
    `pages_completed` alone was enough only while pages were fetched strictly
    in order, where "3 of 10 completed" could only mean 1-2-3: a count is not
    a description once pages can be fetched independently and page 3 can fail
    while 4 and 5 succeed. Recording the numbers keeps the sidecar honest
    about WHICH part of the catalogue is missing, not just how much.
    """
    meta = {
        "source": source,
        "mode": mode,
        "status": status,
        "stop_reason": stop_reason,
        "pages_requested": pages_requested,
        "pages_completed": pages_completed,
        "pages_failed": pages_failed or [],
        "products": products,
        "start_url": start_url,
        "final_url": final_url,
        "finished_at": datetime.now(timezone.utc).isoformat(),
    }
    if extra:
        # Merged rather than nested under a key, so a consumer reads
        # `shop_rating` at the top level beside `products`. Run fields win a
        # name collision: a caller cannot accidentally overwrite `status`.
        meta.update({k: v for k, v in extra.items() if k not in meta})
    return meta


def save(rows: Sequence[Any], out_prefix: str, fmt: str,
         allow_empty: bool = False, row_cls: Type = Product) -> int:
    """Write JSON/CSV and return a process exit code.

    Returns 0 when rows were written, EXIT_NO_PRODUCTS when there were none.
    Callers are expected to exit with it.

    On zero rows, nothing is written at all unless `allow_empty`. Two reasons,
    and a live run demonstrated both. A page-load timeout produced
    `Saved 0 products -> out.json` and exit 0: a two-byte `[]` that a
    consuming pipeline reads as a successful run with no stock. Worse, if the
    file already held a good result from an earlier run, that result is now
    gone — the failure destroyed the last known good data. So an empty result
    leaves the previous file intact and says why.

    `allow_empty=True` is for the legitimate case: a filter that genuinely
    matches nothing, where an empty file is the answer.
    """
    if not rows and not allow_empty:
        print(f"[!] 0 products — refusing to write {out_prefix}.json/.csv, so an "
              f"earlier good result isn't overwritten with an empty one. "
              f"Pass --allow-empty if an empty result is the expected answer.")
        return EXIT_NO_PRODUCTS

    if fmt in ("json", "both"):
        write_json(rows, f"{out_prefix}.json")
        print(f"[+] Saved {len(rows)} products -> {out_prefix}.json")
    if fmt in ("csv", "both"):
        write_csv(rows, f"{out_prefix}.csv", row_cls=row_cls)
        print(f"[+] Saved {len(rows)} products -> {out_prefix}.csv")
    return 0 if rows else EXIT_NO_PRODUCTS


# Stop reasons that mean the run saw everything there was to see. Anything
# else ended the page loop early, so the result is only a partial view.
#
# "no_new_products" belongs here and "pagination_exhausted" is kept for the
# engines that still stop on a missing next-link: the first is a property of
# the DATA (a page contributed nothing not already seen, so the listing is
# over), while the second is a property of a CSS SELECTOR and is therefore
# the weaker signal — a renamed attribute looks identical to a short
# catalogue. On this site the listing states its own end: `?page=N` past
# `totalPages` answers with `totalHits: 0` (see page_flow's pagination notes).
#
# "single_page_mode" is the family's name for a mode that reads one page by
# construction. This repo has no such mode; the reason is kept in the set so
# the family's status mapping stays identical.
COMPLETE_STOP_REASONS = ("completed", "pagination_exhausted", "no_new_products",
                         "single_page_mode")


def finish_run(rows: Sequence[Any], out_prefix: str, fmt: str,
               allow_empty: bool, *, blocked: bool, stop_reason: str,
               pages_requested: int, pages_completed: int,
               start_url: str, final_url: str,
               pages_failed: Optional[List[int]] = None,
               mode: str = "listing", source: str = SOURCE_DEFAULT,
               extra: Optional[dict] = None) -> int:
    """Write output + the run-metadata sidecar; return the exit code.

    Shared by all three browser engines so the status/exit-code mapping
    cannot drift between them.

    The metadata sidecar is written ONLY when the row file was written.
    Otherwise a failed run would leave a "status": "failed" sidecar next to
    the previous run's still-intact good output (which `save` deliberately
    does not overwrite) — the two files would contradict each other, and
    diff_runs.py would refuse to compare data that is in fact fine.
    """
    # Completeness is decided by the reason AND by the evidence. A named
    # list of stop reasons cannot cover a failure recorded somewhere else,
    # and `pages_failed` is somewhere else: a run whose loop ended for a
    # COMPLETE reason while individual pages failed reported exit 0 and
    # `status: complete` with a non-empty `pages_failed` in the same
    # sidecar — a file that contradicts itself, and a pipeline branching
    # on `status` reading a short run as a whole one.
    #
    # Found by a third-party audit of a sibling repo and measured across
    # the family by CALLING each `finish_run` rather than grepping for the
    # fix: 28 of 32 repos behaved this way. Same shape as the exit-code
    # unification this file already carries — a rule keyed on a list of
    # names has a hole for every name nobody added to it.
    complete = stop_reason in COMPLETE_STOP_REASONS and not pages_failed
    row_cls = ROW_CLASS_BY_MODE.get(mode, Product)
    rc = save(rows, out_prefix, fmt, allow_empty=allow_empty, row_cls=row_cls)
    wrote_output = bool(rows) or allow_empty

    if wrote_output:
        status = "complete" if (rows and complete) else (
            "partial" if rows else "failed")
        write_run_meta(out_prefix, run_meta(
            status=status, stop_reason=stop_reason,
            pages_requested=pages_requested, pages_completed=pages_completed,
            pages_failed=pages_failed, mode=mode, source=source,
            start_url=start_url, final_url=final_url, products=len(rows),
            extra=extra))

    if not rows:
        # Nothing gathered at all, and WHY decides the code. The three
        # outcomes are different facts and a pipeline branches on them
        # (blocked is not empty is not "never reached"):
        #
        #   blocked            something stood between the run and the content
        #   did not complete   we never got the pages — a dead proxy, a load
        #                      timeout, an edge serving something else
        #   completed          we asked, and the answer was nothing
        #
        # Keyed on `not complete` rather than on a list of stop reasons, on
        # purpose: a list cannot cover a reason nobody has added to it yet,
        # so a new one falls silently through to "the catalogue is empty" —
        # which is the defect this branch exists to prevent.
        if blocked:
            return EXIT_BLOCKED
        if not complete:
            print(f"[!] Nothing was gathered and the run did not finish "
                  f"({stop_reason}) — exit {EXIT_FETCH_FAILED}, NOT an empty "
                  f"result (exit {EXIT_NO_PRODUCTS}). Nothing can be "
                  f"concluded about the catalogue from this run.")
            return EXIT_FETCH_FAILED
        return rc
    if not complete:
        print(f"[!] Partial run: stopped after {pages_completed} of "
              f"{pages_requested} page(s) ({stop_reason}). The output holds "
              f"what was gathered, but it is NOT a complete view — see "
              f"{out_prefix}.meta.json.")
        return EXIT_PARTIAL
    return rc
