# Contributing

Bug reports, site-change reports and pull requests are all welcome. This file
covers the few things specific to a scraper, which are not the usual ones.

## Before you open anything

Run the offline suite. It needs no network, no browser and no API key, and takes
about a second:

```bash
pip install -r requirements.txt
python3 smoke_test.py
```

It prints its own check count, and lists any group it had to skip because an
engine library is absent.

**The suite must pass with no engine installed at all.** CI installs only
`beautifulsoup4` and `requests`, so any import of `playwright_scraper`,
`puppeteer_scraper` or `selenium_scraper` in a test has to sit inside
`try/except ImportError` with the skip recorded. This is easy to get wrong
locally, where you almost certainly have an engine installed and an unguarded
import passes.

If the suite fails on a clean clone, that is itself the bug — say so.

## Never commit a credential

`.env` is in `.gitignore`. Keep it there.

The scrapers mask `user:pass@` in their own log lines, but three things are **not**
masked: raw HTML dumps, the Scraper API's `x-debug` response header, and your
shell history. Before pasting any output into an issue or a PR, replace keys,
proxy passwords and full `ws://user:pass@host:9222` endpoints with `***`.

CI fails the build if something that looks like a credential is committed. That
check is a backstop, not a review — a leaked key has to be rotated whether or
not the check caught it.

## Reporting a site change

dubizzle changing its markup is the normal way this stops working, and it
has its own issue template. The detail that saves the most time is WHICH
source broke, because a row here is read from more than one:

1. **The SSR payload** — `<script id="__NEXT_DATA__">`, the Redux action
   `listings/fetchListingDataForQuery/fulfilled`. Every ad on every vertical,
   with its ids, url, price, location, attributes and the pagination
   contract. If this moves the run reports 0 rows and exit 4, which is loud.
2. **The JSON-LD ItemList**, on motors and property only — the one source
   for `brand`, `seller_name` and `in_stock`, joined to the payload on the
   ad's locale-stripped path.
3. **The DOM fallback**, anchored on the ad-URL pattern (every ad path
   carries `/{yyyy}/{m}/{d}/`), with the tile's price read from the wrapper
   around `[data-testid="listing-price"]`. Every class on a tile but
   `lpv-cards` is a build hash, so nothing is anchored on one.

A third thing can break without any path failing: the **join** between the
payload and the JSON-LD. When it breaks, the row count and the prices stay
healthy while `brand`, `seller_name` and `in_stock` quietly empty out. If
you are reporting a change, say which vertical and which language (`/ar/`
or none) — the Arabic join is the one that broke before.

`--dump-html PATH` writes the exact bytes the parser was given, on success as
well as failure, and a run that finds nothing writes a dump and a screenshot
next to the output on its own.

## Before this repository goes public

One item cannot be undone later, so it belongs on a checklist rather than in
someone's head. **A commit on top cannot reach what a published tag and a
merged PR's refs already hold** — those stay attached to the PR and cannot be
deleted from it. Afterwards, only a fresh repository removes anything.

```bash
python3 .github/ci_checks.py --history-check
```

That applies the same credential rules CI enforces to **every blob that has
ever existed**, not just the working tree. It is deliberately not part of
`--all` and not run by CI: it shells out to git once per object, and a dirty
history needs a decision, not a red check on every push.

Then the rest of the presentation, in the order that matters:

1. `python3 smoke_test.py` green, and the canary dispatched at least once —
   including its SKIP branch, which is what runs when the
   `DUBIZZLE_CDP_ENDPOINT` secret is absent. The canary has **no schedule**: a
   Scraping Browser credential on this account does not survive a day, so it
   runs on demand with a fresh secret rather than painting a badge that has
   tested nothing. Put a schedule back the day a long-lived credential
   exists.
2. The repo description, homepage and topics set (see the family notes on
   what those should say).
3. Only then the row in the org profile README — and check it with an
   ANONYMOUS request rather than your own logged-in browser. A row pointing
   at a private repo is a 404 for every visitor, which costs more trust than
   the missing row.

## Pull requests

**Add a test for the behaviour you are changing.** `smoke_test.py` is a single
file of plain functions; its fixtures live beside it in
`fixtures_generated.json` because on this site a fixture IS the SSR payload
and one ad's entry is kilobytes of JSON (see `make_fixtures.py`). Copy the
nearest existing check and edit it.

The properties below exist because they were once absent, or were wrong in
the repo this one was ported from, and cost real time. Tests pin all of them,
so a PR that breaks one will fail rather than silently regress:

- **The SSR payload is the primary source, not JSON-LD.** A JSON-LD ItemList
  is published on motors and property and on NONE of classified, jobs or
  community, so a JSON-LD-primary parser works on cars and flats and silently
  returns nothing on half the site. JSON-LD is the enrichment, and is the
  only source for `brand`, `seller_name` and `in_stock`.
- **That enrichment is joined on the ad's locale-stripped PATH, not its
  URL.** An Arabic page's JSON-LD publishes `/ar/motors/…` while its payload's
  `absolute_url.ar` is byte-identical to its `.en` and carries no `/ar`.
  Keyed on the URL the join matched 25 of 25 ads in English and **0 of 25 in
  Arabic**, emptying three columns while the run reported success.
- **`sku` is the ad's URL path.** Four of five verticals carry a 32-hex uuid
  in the URL; a PROPERTY URL carries neither that ad's `id` nor its dashed
  `uuid`, so an id-derived key would be null on every property row the DOM
  fallback produced. `listing_id` and `listing_uuid` carry the site's own
  identifiers where it states them.
- **A null `price` is per-vertical, not per-bug.** 0 of 25 jobs ads and 1 of
  25 community ads carry one; motors, property and classified priced every
  row measured. So the price-coverage floor is a per-vertical table and is 0
  for jobs and community, and those rows say so with a `price_source` ending
  in `:no-price`. A single global threshold would either miss a real break or
  fail on every healthy jobs run.
- **A tile's price is split across two sibling nodes** — `AED` in one and the
  digits in `[data-testid="listing-price"]` — so the DOM path reads the
  WRAPPER. Reading the price node alone gets a number with no currency.
- **A page past the end of a listing still renders ONE ad.** `?page=401`
  answers HTTP 200 with `totalPages: 0, totalHits: 0` and draws the promoted
  Car of the Week anyway, with a price node and a JSON-LD item. That page is
  classified `empty` and NOT parsed, or a run that overshoots writes one
  plausible phantom row per page.
- **The promoted slot is labelled, not merged.** `listing_kind` is
  `car_of_the_week` for it, and it is emitted AFTER the organic rows so that
  position 1 of every page is a real result. It is the same ad on every page
  of a run.
- **Pagination is the site's own count**, read from `pagination.totalPages`,
  and it differs per vertical: 400 pages of 25 on motors, 2,286 of 35 on both
  property indexes. A hardcoded page size would mis-count property by 40%.
  How many pages the catalogue has BEYOND that cap is reported rather than
  swallowed.
- **A block here is the exit's COUNTRY, not the browser.** A European
  residential exit and a Finnish datacentre exit were both refused, headful
  and headless alike; a UAE residential exit was served the full catalogue by
  a headless browser. So `--headless` stays the default, `RETRY_ON_BLOCKED`
  is True, and the block message talks about `region-ae` / `country-ae`.
- **A refusal can be HTTP 200.** The "Pardon Our Interruption" page is served
  with one, so block detection is INVERTED: a served page is recognised by
  the site's own asset hosts (126–2,571 references, against 0 on both
  refusals). That also catches Chromium's own network-error page, which
  carries the site's hostname in its `<title>` and would pass any title
  check.
- **A marker that matches every page is not a marker.** `_Incapsula_Resource`
  appears on pages dubizzle plainly served, and the site ships its own
  `<captcha-widgets></captcha-widgets>` EMPTY on every page — so neither is
  in the challenge set. What a rendered challenge looks like is the mount
  point with something inside it, which is a function rather than a
  substring. The extension-stripping guard IS load-bearing here, unlike in
  two sibling repos: the Scraping Browser's auto-solve extension injects
  turnstile, arkoselabs and recaptcha hunters into every page it loads, and
  this repo's marker set can match them.
- **A challenge marker is only consulted for a state already counted as
  blocked.** An EMPTY page is a correct answer, and refining it into a block
  is how a sibling repo reported exit 3 on a page the site had served.
- **A run that finds nothing writes nothing.** It must not replace a good
  output file with `[]`. `--allow-empty` is the opt-out.
- **Exit codes are a contract**, not decoration: `0` ok, `1` crash, `2` bad
  usage, `3` blocked, `4` zero rows, `5` remote API error, `6` partial. A
  pipeline branches on these.
- **An EMPTY page is never retried and never counted as blocked.**
  `page_flow.STATE_POLICY` holds that for all three engines so they cannot
  disagree about it.
- **`page` and `position` together identify a row.** `position` restarts at 1
  on each page, so a run that does not thread the page number through the
  parser produces rows that collide silently.
- **A sku already written by an earlier page of the same run is dropped, not
  duplicated.** See `dedupe_by_key` in `output_writer.py`.

There is also a naming check: certain phrases are banned repo-wide and the suite
fails naming them. If it trips, read the message — the phrase is wrong for a
reason, not merely unfashionable.

### Style

- **Match the file you are editing.** No formatter is enforced.
- **Comments explain *why*.** What the code does is visible; why it does it that
  way, especially where the obvious version is wrong, is not.
- **A timeout on every remote call.** Every browser library used here has needed
  an explicit timeout its own API does not provide, and each has needed its own
  route out of the runtime — reporting a timeout is not the same as exiting on
  one. If you add a call to a remote browser or API, bound it.
- **Fail loudly.** A function that returns an empty list on error, or logs
  success without checking that the thing it wanted actually happened, is the
  single most common bug class in this codebase's history. A selector that
  matches the *wrong* element is worse than one that matches nothing, because
  the second one tells you.

### If your change needs a live run

Most do not — the suite covers the parser, the writers, the captcha classifier
and the CLI contract against inline fixtures. If yours genuinely needs
dubizzle.com, say in the PR what you ran, which URL and page kind, from
which exit, and what you got — including the price and image coverage
percentages the run prints. Note that a run from an exit outside the UAE
is refused by Imperva — a 403, or a "Pardon Our Interruption" page under
HTTP 200 — so "it returned nothing" from a non-UAE address is not a
finding. Ad counts differ by vertical and by URL, so a bare "worked for me"
is not reproducible.

**Run more than the primary engine.** "Mirror them exactly" is a design rule,
not a verification: in a sibling repo (tokopedia-scraper) the first live run
of the pyppeteer engine crashed on its FIRST fetch on a signature mismatch
that four separate offline checks and 400 green assertions had not caught.

Do not add anything that submits a form on the site. This project
deliberately never does.

## Scope

This repo scrapes **public pages** on dubizzle: category listings on
`uae.dubizzle.com`, exactly as an anonymous visitor is served them.
Out of scope: anything behind a login, anything that submits a form, and
anything that defeats a protection rather than passing it the way an ordinary
browser does.

## Licence

MIT. By opening a pull request you agree your contribution ships under it.
