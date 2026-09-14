# Changelog

All notable changes to this project are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and the project follows [Semantic Versioning](https://semver.org/) as closely
as a CLI toolkit can. A **patch** release means fixes — it does not mean every
flag and every default is frozen. Where a patch changes behaviour an existing
user would notice, the release notes say so first.

## [0.1.0] — 2026-09-14

The first release of this repository as a member of the
[2scraper](https://github.com/2scraper) family. It replaces the previous
four-file scraper entirely: nothing of the old `dubizzle_core.py` /
`dubizzle_playwright.py` / `dubizzle_selenium.py` / `dubizzle_pyppeteer.py`
layout survives.

> **If you used the previous version, every command changes.** The old flags
> (`--categories`, `--max-pages`, `--output`, `--details`) are gone; the new
> ones are the family's (`--url`, `--pages`, `--out`, `--format`, …) and are
> the same in all three engines. The old output had a different schema and no
> run metadata. See the README's Usage section.

### Added

- **The family's row schema and output contract.** One row per ad, the
  eighteen-field family prefix first so a consumer reads a dubizzle run and a
  mediamarkt run with the same code, then twenty-two dubizzle columns.
  JSON and CSV in the same field order, a `<out>.meta.json` sidecar per run,
  and the family's exit codes (`0` ok · `1` crash · `2` bad usage · `3`
  blocked · `4` zero ads · `5` remote API error · `6` partial).
- **Five verticals, one parser**: motors, property-for-rent,
  property-for-sale, classified, jobs and community, in English and Arabic.
- **Three browser engines plus a browserless client** —
  `playwright_scraper.py` (primary), `puppeteer_scraper.py`,
  `selenium_scraper.py` and `scraper_api_client.py` — sharing one parser, one
  page-state policy (`page_flow.py`) and one writer, so they cannot drift on
  exit codes or run status.
- **Proxy pooling, rotation and credential masking** (`proxy_pool.py`),
  `.env` loading with documented precedence (`env_config.py`), captcha
  detection and solving (`captcha_solver.py`), 2Captcha fingerprints
  (`fingerprint_client.py`) and a two-run differ (`diff_runs.py`).
- **510 offline checks** (`smoke_test.py`, wrapped for `pytest`), with every
  fixture cut from a real capture by `make_fixtures.py` and verified to parse
  identically to the untrimmed original before being committed.
- **CI**: the offline suite on the oldest and newest supported Python, a
  per-engine `pip check`, a Docker build that runs the image and launches
  Chromium, a credential grep over the working tree and over every blob that
  has ever existed, and a manually-dispatched live canary.

### What was measured, and what it changed

Everything below is from captures and live runs taken 2026-09-14 through a
UAE residential exit.

- **The payload is the primary source, not JSON-LD** — against this family's
  default. A JSON-LD ItemList is published on motors and property and on
  none of classified, jobs or community, so a JSON-LD-primary parser would
  have worked on cars and flats and silently returned nothing on half the
  site. JSON-LD is read as an enrichment instead, and is the only source for
  `brand`, the seller's name and `in_stock`.
- **That enrichment is joined on the ad's locale-stripped PATH.** Keyed on
  the full URL it matched 25 of 25 ads in English and **0 of 25 in Arabic**,
  because an Arabic page's JSON-LD publishes `/ar/…` while its payload does
  not. Caught before release by parsing an Arabic capture.
- **The exit's country is what the site refuses on.** A European residential
  exit and a Finnish datacentre exit were both refused — headful and
  headless alike — and a UAE residential exit was served the full catalogue
  by a headless browser. So `--headless` stays the family default here and
  the block advice talks about `region-ae` / `country-ae` rather than about
  the browser.
- **A refusal can be HTTP 200.** The "Pardon Our Interruption" page is served
  with a 200, so "was this served at all" is decided from the page being
  built out of `static.dubizzle.com` / `dbz-images.dubizzle.com` — 126 to
  2,571 references on every served page, 0 on both refusals.
- **A page past the end of a listing still renders one ad.** `?page=401`
  answers HTTP 200 with `totalPages: 0, totalHits: 0` and draws the promoted
  Car of the Week anyway. Such a page is classified `empty` and deliberately
  not parsed, so a run that overshoots writes no phantom rows.
- **`price` is legitimately null on two verticals** — 0 of 25 jobs ads and 1
  of 25 community ads carry one — so the price-coverage floor is per-vertical
  and is 0 for both, and `price_source` ends in `:no-price` on those rows.
- **`sku` is the ad's URL path, not an id.** Four of five verticals carry a
  32-hex uuid in the URL; a property URL carries neither of that ad's two
  identifiers, so an id-derived key would have been null on every property
  row the DOM fallback produced.
- **No scroll loop**, measured: captures taken with no scrolling at all
  carried the same counts as captures taken with four scroll rounds.
- **`dubizzle.com.bh`, `.om` and `.eg` are refused with the reason** — they
  carry the same brand and run the OLX platform, which publishes no
  `__NEXT_DATA__` and a different DOM. `dubizzle.com.lb` redirects to
  `olx.com.lb`.

### Fixed in inherited code

These were in files copied verbatim from sibling repos, and only a live run
found them:

- The pyppeteer engine crashed with a 40-line asyncio traceback when the
  Scraping Browser refused a connection, where the Playwright engine printed
  one sentence and exited 5. It now reports the same way.
- The pyppeteer and Selenium engines crashed with
  `AttributeError: 'NoneType' object has no attribute 'current'` on a blocked
  page with no proxy pool. The branch was unreachable in the repo they came
  from, where the no-pool block budget was 0; it is 1 here, so it ran on the
  first try.
- `--locale` defaulted to `id-ID`, a value inherited through two repos from a
  site in Indonesia.
- `page_flow.BLOCK_RETRIES_WITH_POOL` was documented policy that nothing
  read. It is now the default for `--proxy-block-retries`.
- A `logger.warning` whose format string had one placeholder and two
  arguments printed its raw template on a real property run — Python's
  logging swallows that TypeError and carries on. The suite now walks every
  module's AST and counts placeholders against arguments, which needs no
  branch to execute.
- The repo's own secret check flagged this site's public ad identifiers — a
  32-hex string is both an API key's shape and every dubizzle ad's id — so it
  failed on its own repository. It now subtracts the `---{ad id}` shape
  before scanning.

[0.1.0]: https://github.com/2scraper/dubizzle-scraper/releases/tag/v0.1.0
