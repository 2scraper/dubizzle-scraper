# dubizzle-scraper

[![release](https://img.shields.io/github/v/release/2scraper/dubizzle-scraper?sort=semver)](https://github.com/2scraper/dubizzle-scraper/releases)
[![tests](https://github.com/2scraper/dubizzle-scraper/actions/workflows/tests.yml/badge.svg)](https://github.com/2scraper/dubizzle-scraper/actions/workflows/tests.yml)
[![canary](https://github.com/2scraper/dubizzle-scraper/actions/workflows/canary.yml/badge.svg)](https://github.com/2scraper/dubizzle-scraper/actions/workflows/canary.yml)
[![python](https://img.shields.io/badge/python-3.9%20%7C%203.13-blue)](pyproject.toml)
[![licence](https://img.shields.io/badge/licence-MIT-green)](LICENSE)
[![engines](https://img.shields.io/badge/engines-Playwright%20%7C%20Selenium%20%7C%20pyppeteer%20%7C%20CDP-informational)](#engines)
[![needs a UAE exit](https://img.shields.io/badge/needs-a%20UAE%20exit-orange)](#do-you-need-any-of-the-paid-products)

Scrapes [dubizzle](https://uae.dubizzle.com) UAE classifieds — cars, property,
electronics, jobs and community services — to JSON or CSV, with four
interchangeable browser back ends and one row schema shared with the rest of
the [2scraper](https://github.com/2scraper) family.

Everything below that states a number states when it was measured and on what.
Anything not measured is not claimed.

---

## The one thing to know first

**The exit address has to be in the UAE.** Imperva fronts this site and
refuses everything else. Measured 2026-09-14 against
`/motors/used-cars/`:

| Client | Exit address | Result |
|---|---|---|
| `curl` with a full browser header set | European residential | **403**, 1,160 bytes, "Incapsula incident ID" |
| Chrome **headful** | European residential | **403**, 885–1,196 bytes |
| Python `requests`, browser UA | UAE residential | **200**, 6,183 bytes — "Pardon Our Interruption" |
| 2Captcha Scraper API (no browser) | Finnish datacentre | **200**, 1,152 bytes — "Pardon Our Interruption" |
| Chromium **headless** over CDP | UAE residential | **200**, **1.6 MB, the full catalogue** |

Read it by column, not by row: the window makes no difference and the exit's
**country** makes all of it. So:

* `--headless` is the default, as in the rest of this family. Every live run
  in this repository was headless and was served.
* **A refusal is not always an error status.** The "Pardon Our Interruption"
  page is HTTP **200**. This scraper therefore decides "was this served at
  all" from the page being built out of dubizzle's own asset hosts
  (`static.dubizzle.com`, `dbz-images.dubizzle.com`) — 126 to 2,571
  references on every page the site served, **0** on both refusals.
* A red run means the exit, not the browser. The block message says which
  and tells you the two ways to fix it.

---

## Install

Core dependencies are `beautifulsoup4` and `requests`. **Install exactly one
engine**: their pins are mutually unsatisfiable (playwright and pyppeteer
disagree on `pyee`, pyppeteer and selenium on `urllib3`), and while all three
do run side by side in practice, `pip check` reports the conflict and pip may
resolve it by downgrading something you wanted. Use a virtualenv per engine if
you need more than one.

```bash
pip install -r requirements.txt -r requirements-playwright.txt
python -m playwright install chromium         # Playwright only
```

```bash
pip install -r requirements.txt -r requirements-selenium.txt    # or
pip install -r requirements.txt -r requirements-puppeteer.txt
```

Python 3.9 through 3.13; both ends are tested in CI.

---

## Usage

```bash
# Used cars, three pages, JSON and CSV
python3 playwright_scraper.py \
    --url 'https://uae.dubizzle.com/motors/used-cars/' \
    --pages 3 --format both --out cars

# Apartments for rent
python3 playwright_scraper.py \
    --url 'https://uae.dubizzle.com/property-for-rent/residential/apartmentflat/' \
    --pages 2

# Electronics
python3 playwright_scraper.py \
    --url 'https://uae.dubizzle.com/classified/electronics/televisions/'

# Arabic: the language is the URL, not a flag
python3 playwright_scraper.py --url 'https://uae.dubizzle.com/ar/motors/used-cars/'

# The site's own filters are part of the URL and survive pagination
python3 playwright_scraper.py \
    --url 'https://uae.dubizzle.com/motors/used-cars/?price_max=50000&year_min=2020'
```

`puppeteer_scraper.py` and `selenium_scraper.py` take the same flags. See
[engine differences](#engines).

### Pass a listing, not a hub

`https://uae.dubizzle.com/` and `/motors/` are **hub pages**: category tiles
and promo rails, no result grid. A run against one honestly reports 0 rows
and exit 4, and warns you what happened rather than leaving you to guess.
The listings are one or two levels down, and every hub links to its own.

---

## What comes out

One row per advertisement, same field order in JSON and CSV, the family
prefix first (`source, scraped_at, url, sku, title, brand, price, currency,
original_price, discount_pct, rating, review_count, in_stock, image_url,
category, price_source, page, position`) and this site's own columns after it.
`sample_output.json` and `sample_output.csv` are cut from a real run.

The columns that need explaining, because a classifieds site is not a shop:

**`sku` is the ad's URL path, not an id — and that is measured, not
stylistic.** This site numbers its verticals differently. A motors,
classified, jobs or community URL ends in `---{32-hex uuid}`; a **property**
URL ends in `-{city-id}-{ref}` and carries neither the listing's `id`
(27794657) nor its dashed `uuid` (`b76a3b0a-ef13-…`) anywhere at all. An
id-derived key would therefore be null on every property row the DOM fallback
produced. The path is present on every row of every path — payload, JSON-LD
and DOM alike — and a leading `/ar` is stripped, so an Arabic run and an
English run of the same category diff as the same ads rather than as a
wholesale replacement. `listing_id` and `listing_uuid` carry the site's own
identifiers where it states them.

**A null `price` is often the site's answer, not a parse failure.** Measured
2026-09-14, one page of each vertical:

| Vertical | Rows with a price |
|---|---|
| motors | 26/26 |
| property-for-rent | 35/35 |
| property-for-sale | 35/35 |
| classified | 25/25 |
| community | **1/25** — a services ad quotes on request |
| jobs | **0/25** — a job ad publishes no salary at all |

So the price-coverage floor is **per vertical** and is 0 for jobs and
community. `price_source` ends in `:no-price` on those rows, which
distinguishes "the site published none" from "we failed to read one" where a
bare null cannot.

**A rent's price is per period, and `payment_frequency` says which.**
105,000 AED on an apartment is a year's rent. Reading it without that column
would put yearly and monthly figures in one column and call them comparable.

**`listing_kind` marks the promoted slot.** Every motors page carries one
"Car of the Week" that the site injects — including a page past the end of the
listing. It is a real ad and is kept, but it is not a result, and it is
emitted *after* the organic rows so that position 1 of each page is a real
result.

**`brand` is only where the site states one.** It comes from `brand.name` in
a motors page's JSON-LD — the make the seller picked from the taxonomy. Null
on property, jobs and community, where there is no manufacturer to name, and
null on classified, whose pages publish no JSON-LD ItemList at all. It is
deliberately not derived by splitting a title or reading the URL's make
segment: both would be a guess wearing the costume of a fact.

**`attributes` is one column, not forty.** Motors publishes up to 20 details
per ad (body type, fuel, transmission, regional specs, warranty, …) and
property publishes a different set; the key set is per-vertical and grows
whenever the site adds a filter. The JSON output keeps the real object, the
CSV writes it as compact JSON in one cell. `year` and `kilometers` are
promoted out of it because they are what a car listing is actually compared
on.

**`price_source` records which sources agreed.** `next_data+jsonld` where the
page publishes both, `next_data` on the three verticals that publish no
ItemList, `dom` on the fallback path. `diff_runs.py` reports a price
difference that comes with a `price_source` difference as `source_changed`
rather than `changed`.

### Exit codes

`0` ok · `1` crash · `2` bad usage · `3` blocked · `4` zero ads · `5` remote
API error · `6` partial. Every run writes `<out>.meta.json` beside its output
with `status`, `stop_reason` and **which** pages failed by number. A failed
run writes no sidecar and leaves the previous good output in place — a run
that finds nothing does not overwrite last night's data unless you pass
`--allow-empty`.

---

## Where the data actually is

Three sources overlap on a listing page and none is a superset of the others.
Measured across 12 captures and 195 ads, 2026-09-14:

| Vertical | payload hits | JSON-LD ItemList | tile price nodes |
|---|---|---|---|
| motors | 25 | 26 (`Vehicle`) | 26 |
| property-for-rent | 35 | 35 (`RealEstateListing`) | 35 |
| property-for-sale | 35 | 35 (`RealEstateListing`) | 35 |
| classified | 25 | **0** | 25 |
| jobs | 25 | **0** | 0 |
| community | 25 | **0** | 0 |

So **JSON-LD is not the primary path here**, against this family's default.
It is absent on three of the six verticals, and a JSON-LD-primary parser
would work on cars and flats and silently return nothing on half the site.
The primary is the site's own SSR payload — `__NEXT_DATA__`, the Redux action
`listings/fetchListingDataForQuery/fulfilled` — which carries every ad on
every vertical plus the pagination contract, the ids and the attributes.

JSON-LD is read as an **enrichment**, and it earns its place: it is the only
source for `brand`, the dealership's name and `offers.availability`, and its
`priceCurrency` makes AED a stated fact rather than a symbol we recognised.

It is joined on the ad's **locale-stripped path**, not on its URL. On an
Arabic listing the two sources disagree about the address of the same ad: the
JSON-LD publishes `/ar/motors/…` while the payload's own `absolute_url.ar` is
byte-identical to its `.en` and carries no `/ar` at all. Keyed on the full
URL the join matched 25 of 25 ads in English and **0 of 25 in Arabic**,
emptying three columns while the run reported success.

The DOM is the fallback, anchored on the ad-URL pattern rather than on a
class (every class on a tile but `lpv-cards` is a build hash like
`mui-style-1tufyr0`). It is a real path, not a comment: stripping the payload
out of three captures and re-parsing recovered **26, 35 and 25 ads with the
same skus and zero price disagreements**.

---

## Languages, hosts and currency

Two languages, from the site's own `hreflang` set: `en` (no prefix) and `ar`
(an `/ar` path prefix). There is no third.

Listings are browsed on `uae.dubizzle.com`; **individual ads are published on
emirate subdomains** — `dubai.`, `abudhabi.`, `sharjah.`, `ajman.`, `rak.`,
`uaq.`, `fujairah.`, `alain.`. The `url` column carries the address the site
itself publishes rather than one rebuilt from the browsed host, and `city`
carries the emirate as a fact about the ad.

Prices are AED for every visitor. The tile splits them across two sibling
nodes — `AED` in one and `389,000` in the next — so the DOM path reads the
wrapper, not the price node alone.

### The sites this scraper does NOT read

`dubizzle.com.bh`, `dubizzle.com.om` and `dubizzle.com.eg` carry the same
brand and run the **OLX platform**: no `__NEXT_DATA__` anywhere, a different
DOM, a different ad-URL shape, and a `<title>` reading
"دوبيزل (أوليكس)" — dubizzle (OLX). `dubizzle.com.lb` redirects to
`olx.com.lb` outright. Measured 2026-09-14. Passing one of those URLs is
refused **with that reason** rather than with "not a dubizzle site", which
would be false and would send you hunting for a typo.

---

## Pagination

`?page=N`, and three layers of evidence agree on it:

1. Page 1 publishes `<link rel="next">` pointing at exactly
   `…/motors/used-cars/?page=2` — what the convention builds.
2. The fetched page 2 reports `pagination.page: 1`, the 0-based index of URL
   page 2.
3. Page 1's payload states `totalPages` outright.

So every page's address is knowable up front and workers can be handed
independent pages. **Past the end the site does not clamp, it empties:**
`?page=401` and `?page=99999` on a 400-page listing both answered HTTP 200
with `totalPages: 0, totalHits: 0`. That is a cleaner terminator than "this
page added no new sku", because it is the site's own statement.

**The site's page count is a cap, and it is not the same everywhere.** A
motors listing of 34,619 ads publishes exactly 400 pages of 25 — so two
thirds of that catalogue cannot be reached through pagination at all. Both
property indexes publish 2,286 pages of 35. The scraper reports how many
pages the catalogue has beyond the cap rather than swallowing the difference.
The practical answer is the site's own filters: narrow by emirate, make,
price band or year and run each slice.

**One trap worth naming.** A page past the end still renders **one** ad — the
promoted Car of the Week, with a price node, a JSON-LD item and a real listing
URL. Parsing such a page would write a plausible phantom row for every page a
run overshot by, so it is classified `empty` and deliberately not parsed.

---

## Engines

Playwright is primary. All four back ends produce the same rows, the same
columns in the same order, the same exit codes and the same run status.

| | Playwright | pyppeteer | Selenium | `--cdp-endpoint` |
|---|---|---|---|---|
| Live-verified here | yes | yes | block path only¹ | yes |
| Authenticated remote CDP | yes | yes | **no** | — |
| Authenticated proxy | yes | yes | **no** | n/a |
| `--fingerprint`, `--fp-tags`, `--fp-country` | yes | no | yes | ignored |
| `--locale` | yes | no | no | — |
| `--chromium-path` | no | yes | no | — |

There is a fifth entry point, `scraper_api_client.py` — one HTTP request per
page through the 2Captcha Scraper API, no browser at all. **It does not get
into this site**, and that is measured rather than assumed: four requests on
2026-09-14 all came back with the 1,152/6,183-byte "Pardon Our Interruption"
page, including two routed through a `country-ae` Scraping Browser session
with `--cdp-url`. The API's own fetcher reported `x-country-code: FI`. It is
kept for parity and reports the refusal honestly (exit 3, debug dump saved)
rather than writing rows it did not get.

¹ Selenium's content path was **not** live-verified for this release, and
that is stated rather than implied: the machine this was built on could not
give a local chromedriver a UAE exit, and Selenium cannot use the
authenticated Scraping Browser endpoint that the other two engines use. What
*was* exercised live is its refusal path end to end — it declines a
credentialled `--cdp-endpoint` with exit 2 and the reason, and on a refused
address it retries once, reports exit 3, saves the debug dump and refuses to
overwrite a previous good output. Its content path is covered by the offline
suite, which asserts it against the same fixtures and the same shared
`page_flow` policy as the other two.

* **Selenium cannot use an authenticated remote CDP endpoint.** Playwright's
  `connect_over_cdp` and pyppeteer's `browserWSEndpoint` take a full
  `ws://user:pass@host:port` and authenticate on the WebSocket upgrade;
  chromedriver's `debuggerAddress` takes a bare `host:port` with nowhere to
  put a password. It is not a generic "connect to CDP" option.
* **Selenium's `--proxy-server` cannot authenticate at all.** Credentials are
  stripped and a warning is printed. See the whitelist recipe below, which
  makes a proxy work in Selenium anyway.
* **pyppeteer is effectively unmaintained** and its own README points at
  Playwright. It is here for parity, not because it is a good idea. It also
  prints asyncio teardown noise *after* a successful run has written its
  output; the exit code is what to read.
* Credentials never reach a command line or a log in any engine — including
  exception messages, which are logs. They are passed through the driver's own
  fields.

---

## Do you need any of the paid products?

**On this site, you need a UAE exit.** That is the one honest answer, and it
is the opposite of what this family's README usually says. Everything else is
optional.

What the four separately billed [2Captcha](https://2captcha.com) products
actually buy here:

* **The Scraping Browser API** (`--cdp-endpoint`, with `country-ae` in the
  login) — a remote browser in the country the site serves, so you run no
  browser infrastructure and get the exit that matters. This is the path
  every live run in this repository used. One live connection per `pid`, so
  `--concurrency` above 1 is refused with that reason; use several pids.
* **Proxies** (`--proxy`, `--proxy-file`) — the same exit, your own browser.
  Use `region-ae`. A bigger pool of non-UAE addresses buys nothing.
* **Captcha solving** (`--solve-captcha`, default `when-blocked`) — nothing
  to solve here, and that is measured rather than assumed. Across 17 captures
  and 5 live runs this site rendered no challenge at all to an anonymous
  visitor, and Imperva does not challenge either: it refuses outright, with
  no widget and no form on either refusal page.

  It **does** have one, though, and reading the site's own bundles says
  exactly which. **Google reCAPTCHA**, site key
  `6LeubLYqAAAAAK7-X6nc1fW2ggot_vTvQAv0RxdU`, which appears in dubizzle's
  JavaScript as `RECAPTCHA_KEY` in the argument object handed to
  `showAuthPopup(...)` — beside `GOOGLE_APP_ID` and `FACEBOOK_APP_ID` — and
  as `recaptchaSiteKey` in the app config. `showAuthPopup` is called with
  `intent: "login"` or `"phoneverify"`, so the captcha guards **signing in
  and verifying a phone number**, not reading listings.

  Two consequences worth stating. The version is **not** claimed here: none
  of the site's 104 listing chunks carries the `recaptcha/api.js` loader or a
  single `grecaptcha.*` call, so the `render=` parameter that settles v2
  against v3 is not observable from anything this scraper fetches — the
  widget belongs to a separate auth application the listing pages never
  load. And the key itself IS a marker, because it is safe to be one: it
  appears in 0 of 17 captures, so seeing it in a page means the site has
  rendered its own challenge into it.

  The empty mount point it would render into —
  `<captcha-widgets></captcha-widgets>` — is on every page including the good
  ones, so the bare tag is deliberately **not** a marker: one that matches
  every good page is worse than no marker.
* **Fingerprints** (`--fingerprint`) — a consistent device identity. Pass
  **one** OS-family tag to `--fp-tags` (`Windows`, `Microsoft Windows` or
  `Android`); the API rejects a list, and `Chrome`, `Desktop` and `Mobile`
  are each rejected on their own with HTTP 400.

Never set a fingerprint, a user agent or a proxy together with
`--cdp-endpoint`: the remote browser brings its own, and stacking a second
creates a contradiction rather than better cover.

### The proxy recipe that works in every engine

Chromium cannot authenticate a SOCKS5 proxy (Playwright refuses at launch)
and Selenium cannot authenticate any proxy. 2Captcha's IP-whitelist mode
side-steps both: whitelist your address, then

```bash
curl "https://api.2captcha.com/proxy/generate_white_list_connections\
?key=$TWOCAPTCHA_KEY&ip=YOUR.IP.HERE&protocol=socks5&connection_count=3&country=ae"
```

returns one `host:port` per exit with **no credentials in them at all**. Save
them to a file, pass `--proxy-file`, and the credential never enters your
config.

---

## Measured on this site

All 2026-09-14, through a UAE residential exit unless stated.

| | Value |
|---|---|
| Ads per page | 25 (motors, classified, jobs, community) · 35 (both property indexes) |
| Motors listing total / pages the site addresses | 34,619 ads / 400 pages — 985 pages beyond the cap |
| Property listing total / pages | 206,372 ads / 2,286 pages |
| Live run, Playwright, 2 pages of used cars | **52 rows, 52 distinct sku, exit 0, status complete** |
| Live run, Playwright, 2 pages of apartments for rent | **70 rows, exit 0, status complete** |
| Live run, Playwright, 1 page of used cars in **Arabic** | **26 rows, brand 26/26, exit 0** |
| Live run, Playwright, 3 pages of televisions | **75 rows, exit 0, status complete** |
| Live run, pyppeteer, 2 pages of televisions | **50 rows, exit 0, status complete** |
| Across all five live runs | 273 rows, every `sku` unique, every `page`+`position` pair unique, every priced row carrying AED and no unpriced row carrying one |
| Price coverage on those runs | 26/26, 35/35 and 25/25 per page |
| Column coverage on the motors run | price, currency, brand, city, location, listing_id, uuid, short_url, attributes, posted_at, bumped_at, image, year, kilometers: 52/52 · seller_name 43/52 · seller_kind 50/52 |
| JSON-LD ↔ payload agreement (motors, property) | 26/26 and 35/35 ads, no disagreement |
| DOM-only re-parse vs payload | 26, 35 and 25 ads, same skus, **0 price disagreements** |
| Asset-host references, served page vs refusal | 126–2,571 vs **0** |
| Scroll rounds needed | none — captures taken with 0 scrolls matched those taken with 4 |
| Block-page shapes seen | 1,160 B (HTTP 403) and 6,183 B (HTTP **200**) |
| Offline checks | 525 |

Everything except the DOM cross-check comes out of `__NEXT_DATA__`, which is
in the first response and needs no JavaScript, so a readiness wait that times
out costs the confirmation rather than the run.

---

## Traps that look like bugs

* **A jobs run with no prices is correct.** 0 of 25, measured; the site
  publishes no salary. Same for most community ads.
* **A hub URL returns 0 rows and exit 4.** It has no result grid.
  `/motors/` is a hub; `/motors/used-cars/` is a listing.
* **`page=401` is not an error.** The site answers 200 with `totalHits: 0`
  and still draws one promoted ad. That page is not parsed.
* **`rating` and `review_count` are null on every row.** dubizzle rates
  SELLERS on their profile page, never the individual ad. The two family
  columns are kept so one schema works across the family.
* **`bedrooms` is null on every car and `kilometers` on every flat.** That is
  the vertical, not a parse failure — which is why `vertical` is a column.
* **An ad's `url` is on a different host from the one you browsed.** Ads live
  on emirate subdomains; the listing is browsed on `uae.dubizzle.com`.
* **`category` is the ad's own taxonomy path**
  (`motors/used-cars/mercedes-benz/a-class`), not the category you browsed —
  it is a fact about the ad, and the browsed URL is in the run's sidecar.
* **A `profile_locked` error from `--cdp-endpoint`** means another run still
  holds that `pid`. One live connection per profile.
* **A first-fetch block that clears on a retry** is normal here; the first
  live run of the Playwright engine did exactly that.

---

## Diffing two runs

```bash
python3 diff_runs.py old.json new.json
```

Compares by `sku` and refuses to compare runs that are not both `complete`,
because a partial run's un-fetched pages otherwise read as delisted ads.

On a classifieds site, expect some churn between two runs that is the market
rather than the data: ads are bumped to the top by their sellers, so page
membership moves even when nothing about an ad changed. The `sku` set is what
to compare, not the positions.

---

## Testing

```bash
python3 smoke_test.py        # 525 offline checks, no engine library needed
pytest                       # the same checks, wrapped as one test
python3 env_config.py        # what config was picked up, without secrets
python3 .github/ci_checks.py --all          # what CI runs
python3 .github/ci_checks.py --history-check  # before making the repo public
```

Every fixture is cut from a real capture by `make_fixtures.py`, which
verifies that each trim parses **identically** to the untrimmed original —
every column, not a sample — before it is written. Personal and
credential-shaped material is replaced with obvious placeholders and guarded
by PATTERNS rather than by the literals one capture happened to hold, so the
next capture is checked too: this site's pages carry a real estate agent's
own name (34–35 per property page), a per-seller UUID (25–26 per motors
page), the page's Algolia search key and its Sentry instrumentation.

CI runs the offline suite on the oldest and newest supported Python, and
builds **and runs** the Docker image — its entrypoint, a real Chromium launch,
and a check that no `.env`, test suite or fixture was baked in.

**What the canary badge here does and does not mean.** The workflow is a real
three-page run against a real listing URL, with a floor on the rows,
page+position uniqueness and a dozen other assertions. But this repository
deliberately holds **no live credential**, so the live steps are skipped and
the job goes green with a `::notice::` saying why — a check that is always red
teaches everyone to ignore checks, and a check that is always green teaches
the same lesson more quietly. So read the green canary badge as *"the workflow
is wired up"*, not as *"the site was verified today"*.

To make it do real work in your own fork, set one secret and change nothing
else:

```bash
gh secret set DUBIZZLE_CDP_ENDPOINT --repo <your-org>/dubizzle-scraper
```

Use a `pid` reserved for CI, with `country-ae` in the login: a Scraping
Browser profile allows one live connection, so a canary sharing a pid with a
person's run gets `profile_locked` and reports a failure that has nothing to
do with the site.

---

## Contributing, security, licence

See [CONTRIBUTING.md](CONTRIBUTING.md), [SECURITY.md](SECURITY.md) and
[CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md). Issues have templates, including one
for "the site changed". MIT licensed.

This scraper reads public listing pages, obeys a `--delay` between requests
(default on), and is meant for price monitoring and research. It does not log
in, does not post, does not contact sellers, and does not touch anything
behind an account.
