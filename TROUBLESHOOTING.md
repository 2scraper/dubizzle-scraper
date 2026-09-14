# Troubleshooting

Symptoms first, in the order people actually hit them. Every number here was
measured; where a thing is a property of the site rather than a bug, it says
so, because a good half of what looks broken on a classifieds site is the
site working.

---

## Exit 3, a ~1 KB or ~6 KB dump, "Incapsula incident ID" or "Pardon Our Interruption"

**Your exit is not in the UAE.** This is the first thing to check and it is
not a browser problem.

Measured 2026-09-14 on the same URL:

| Client | Exit | Result |
|---|---|---|
| `curl`, full browser headers | European residential | 403, 1,160 bytes |
| Chrome **headful** | European residential | 403, 885–1,196 bytes |
| `requests`, browser UA | UAE residential | **200**, 6,183 bytes, "Pardon Our Interruption" |
| 2Captcha Scraper API | Finnish datacentre | **200**, 1,152 bytes, same page |
| Chromium **headless** over CDP | UAE residential | **200**, 1.6 MB, full grid |

Two things to take from that. The window is not the variable — every live run
in this repository was headless and was served. And **a refusal here can be
HTTP 200**, which is why the scraper decides "was this served at all" from
the page being built out of `static.dubizzle.com` / `dbz-images.dubizzle.com`
rather than from the status code.

```bash
# A UAE proxy exit
python3 playwright_scraper.py --proxy 'http://user:pass@ae.proxy.2captcha.com:2334' \
    --url "https://uae.dubizzle.com/motors/used-cars/"

# Or the remote browser, with country-ae in its login — the path every live
# run here used
python3 playwright_scraper.py --cdp-endpoint "ws://…-country-ae-…@cb.2captcha.com:9222" --url …
```

`page_flow.RETRY_ON_BLOCKED` is `True` here, against `False` in some sibling
repos, because on this site a different exit genuinely can help — and the
first live run of the Playwright engine had page 1 come back blocked and
cleared it on one re-fetch through the same endpoint. A single block on page
1 is not yet a problem; a run that is blocked on every page is an exit
problem.

---

## `price` is null on some rows

**Check the vertical first.** Measured 2026-09-14, one page of each:

| Vertical | Rows with a price |
|---|---|
| motors | 26/26 |
| property-for-rent | 35/35 |
| property-for-sale | 35/35 |
| classified | 25/25 |
| community | **1/25** |
| jobs | **0/25** |

A job ad publishes no salary and a services ad usually quotes on request. On
those rows `price_source` ends in `:no-price`, which is the scraper saying
"the site published none" rather than "we failed to read one". The
price-coverage floor the engines log is per-vertical for the same reason and
is 0 for jobs and community.

A property ad can also set `is_price_hidden`, which the site honours by
publishing no price at all.

What *is* worth an issue: a null price on **motors, property or classified**,
where every measured page priced every ad.

---

## Every price is null and `price_source` says `dom` everywhere

The payload did not arrive and the DOM fallback ran. That means the Redux
action the grid lives in — `listings/fetchListingDataForQuery/fulfilled` —
has moved or been renamed. Re-run with `--dump-html` and grep the dump for
`__NEXT_DATA__`; if it is there but the action name is not, that is the fix.

The fallback is not a failure mode on its own: stripping the payload out of
three captures left it recovering 26, 35 and 25 ads with the same skus and
**zero price disagreements**. But it cannot recover `listing_id`, `brand`,
`attributes` or the seller, so a run entirely on `dom` is missing two thirds
of each row.

---

## `brand`, `seller_name` and `in_stock` are empty, everything else is fine

The JSON-LD join broke. Those three columns are the only ones that come from
the page's own `ItemList`, and the join is keyed on the ad's
**locale-stripped path**.

This has a known cause worth checking first: an **Arabic** page publishes
`/ar/motors/…` in its JSON-LD while its payload's `absolute_url.ar` is
byte-identical to its `.en` and carries no `/ar` at all. A join keyed on the
full URL matched 25 of 25 ads in English and **0 of 25 in Arabic**. If you
have changed `jsonld_items_by_url` or `sku_from_url`, that is where to look.

On **classified, jobs and community** those three columns are null by design:
those verticals publish no JSON-LD ItemList at all (0 of 25 tiles measured).

---

## Exit 4 and zero rows — broken, or empty?

Three ways to get zero rows that are all correct answers:

* **A hub URL.** `https://uae.dubizzle.com/` and `/motors/` carry category
  tiles and promo rails and no result grid. The run warns you by name.
* **One page past the end.** `?page=401` on a 400-page listing answers HTTP
  200 with `totalPages: 0, totalHits: 0`.
* **A category that genuinely holds nothing**, including a 404 category path.

All three are exit 4. Exit 3 is the different thing — the site did not serve
the request at all.

A detail worth knowing: a page past the end **still renders one ad**, the
promoted Car of the Week, complete with a price node and a JSON-LD item. The
scraper classifies that page `empty` and does not parse it, so an overshooting
run writes no phantom rows. If you see exactly one row where you expected
none, check whether something changed that classification.

---

## The run reports far fewer ads than the listing says it holds

That is the site's own cap, not a bug. A motors listing of 34,619 ads
publishes exactly **400 pages of 25** — 10,000 ads — so two thirds of it
cannot be reached through pagination at all. Both property indexes publish
2,286 pages of 35.

The run logs how many pages the catalogue has beyond the cap. The way through
it is the site's own filters, which are part of the URL and survive
pagination:

```bash
python3 playwright_scraper.py --url \
  "https://uae.dubizzle.com/motors/used-cars/?price_max=50000&year_min=2020" --pages 50
```

Slice by emirate, make, price band or year and run each slice.

---

## `rating` and `review_count` are null on every row

Expected. dubizzle rates **sellers**, on their own profile page, and never
the individual ad — no listing tile or payload in 195 ads across five
verticals carries a per-ad rating. The two columns are kept because the
family's schema has them in that position and consumers read the columns by
name across repos.

---

## `bedrooms` is null on a car, `kilometers` on a flat

That is the vertical, not a parse failure. Five verticals share one schema and
they do not publish the same facts, which is exactly why `vertical` is a
column: filter on it before reading a field that only one vertical publishes.
Everything a vertical publishes beyond the promoted columns is in
`attributes`, keyed by the site's own slugs.

---

## An ad's `url` is on a host I did not browse

Expected. Listings are browsed on `uae.dubizzle.com`; individual ads are
published on **emirate subdomains** (`dubai.`, `abudhabi.`, `sharjah.`,
`ajman.`, `rak.`, `uaq.`, `fujairah.`, `alain.`). The `url` column carries
the address the site itself publishes rather than one rebuilt from the browse
host — a sibling repo lost a whole column by rebuilding a URL the page then
never matched. `city` carries the emirate.

---

## `500 Internal Server Error` on connecting, exit 5, `profile_locked`

A Scraping Browser profile allows **one live connection**. Another run still
holds that `pid`. Wait for it, or use a different pid — and give CI its own,
because a canary colliding with a person's run reports a failure that has
nothing to do with the site.

This is exit 5, not exit 1: a remote API refusing a connection is not a bug
in this code, and a harness that lumps them together sends you looking in the
wrong place.

---

## `Browser does not support socks5 proxy authentication`

Chromium cannot authenticate a SOCKS5 proxy, and Selenium cannot authenticate
any proxy at all. 2Captcha's IP-whitelist mode side-steps both: whitelist your
address, then

```bash
curl "https://api.2captcha.com/proxy/generate_white_list_connections\
?key=$TWOCAPTCHA_KEY&ip=YOUR.IP.HERE&protocol=socks5&connection_count=3&country=ae"
```

Each connection is a bare `host:port` with no credentials in it. Save them to
a file and pass `--proxy-file`.

---

## `--proxy` is set and every navigation times out

Check the exit's country before anything else — but if the proxy answers for
other sites and not this one, check that the credential is reaching the
browser rather than the command line. Credentials are passed through the
driver's own fields (`username`/`password` in Playwright,
`page.authenticate` in pyppeteer) and never through `--proxy-server=`, which
would put them in the browser's argv where anything that can run `ps` reads
them.

Selenium strips credentials and warns, because it cannot send them at all.

---

## Selenium refuses my `--cdp-endpoint` with exit 2

Correct behaviour, and the message says why: chromedriver's
`debuggerAddress` takes a bare `host:port` with nowhere to put a password,
while Playwright's `connect_over_cdp` and pyppeteer's `browserWSEndpoint`
take a full `ws://user:pass@host:port` and authenticate on the WebSocket
upgrade. It is not a generic "connect to CDP" option.

Use `playwright_scraper.py` or `puppeteer_scraper.py` for a credentialled
endpoint.

---

## pyppeteer prints a wall of asyncio tracebacks after a successful run

Read the exit code, not the noise. pyppeteer leaves CDP calls in flight when
a browser closes and the interpreter prints "Task was destroyed but it is
pending!" and "Event loop is closed" *after* the output has been written. The
engine suppresses the shapes it can; the rest arrive at shutdown as
`Exception ignored in: <coroutine ...>` lines, printed by the interpreter
past any handler this code could install.

The run succeeded if it printed `[+] Saved N ads` and exited 0. The
tracebacks come after the output is on disk.

---

## `--concurrency 4` is refused

Two legitimate reasons:

* **The URL is not a paginated listing** — an individual ad or a hub page has
  no page 2, so a second worker has nothing to fetch. The refusal says which.
* **You passed `--cdp-endpoint`.** A Scraping Browser profile allows one live
  connection, so N workers collide on `profile_locked`. Use several `pid`s,
  one run each.

With a proxy pool, concurrency is allowed and each worker owns one exit for
its lifetime, so no thread needs a lock. Without a pool it is allowed with a
warning: N workers send N× the traffic from one address, which is a faster
way to get it scored than to gather data.

---

## Two runs differ on almost every row

Expect some churn that is the market rather than the data: sellers bump their
ads to the top, so page membership moves even when nothing about an ad
changed. Compare the **`sku` set**, not the positions.

`diff_runs.py` refuses to compare runs that are not both `complete`, because
a partial run's un-fetched pages otherwise read as delisted ads.

---

## `pytest` or `python3 smoke_test.py` fails after an edit

The suite is one file of plain functions with its fixtures in
`fixtures_generated.json`. It must pass with **no engine library installed at
all** — every engine import is guarded and the skip is reported at the end.

If a value assertion fails after you changed the parser, the fixtures are the
authority: they were cut from real captures by `make_fixtures.py` and
verified to parse identically to the untrimmed original, column for column.
To regenerate them you need your own captures:

```bash
python3 playwright_scraper.py --url … --dump-html captures/uae_usedcars_p1.html
python3 make_fixtures.py ../captures
```

`make_fixtures.py` refuses to write anything if a trim changes any column, or
if the scrub left an agent's name, a per-seller UUID or a key-shaped value in
the output.

---

## Where to look next

* `python3 env_config.py` — what configuration was picked up, without
  printing secrets.
* `python3 .github/ci_checks.py --all` — what CI runs over the working tree.
* `--dump-html PATH` — the exact bytes the parser was given, written on
  success as well as on failure.
* `<out>.meta.json` — the run's own status, stop reason and which pages
  failed by number.
