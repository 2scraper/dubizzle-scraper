# Dubizzle Scraper

Open-source web scraper for [dubizzle.com](https://dubizzle.com) (UAE) — the largest classifieds platform in the region. Extract listings from **all categories** into clean JSON or CSV with automatic CAPTCHA solving, proxy rotation, and browser fingerprint evasion.

Three ready-to-use implementations: **Playwright** (recommended), **Selenium**, and **Pyppeteer** (Puppeteer for Python).

> Built and maintained by [2scraper](https://github.com/2scraper) — open-source scraping tools powered by [2captcha.com](https://2captcha.com) and [2prx.com](https://2prx.com).

---

## How It Works

Dubizzle is a Next.js application with MUI components. The scraper uses a dual extraction strategy for maximum reliability:

1. **Primary** — parse the `__NEXT_DATA__` JSON blob embedded in every page. This contains the full Redux state with structured listing data, making it the most reliable method regardless of CSS class changes.

2. **Fallback** — DOM parsing based on the actual MUI component structure (MuiStack-root containers, listing link patterns). Kicks in automatically when `__NEXT_DATA__` doesn't contain listing arrays.

Category pages on dubizzle.com are landing pages (filters, hero sections) — actual listings live under subcategories. The scraper automatically resolves the correct subcategory paths.

---

## Features

- **All dubizzle categories** — Motors, Property, Jobs, Electronics, Fashion, Pets, and more
- **Dual extraction** — `__NEXT_DATA__` JSON (primary) + DOM selectors (fallback)
- **Three browser engines** — pick whichever fits your stack
- **CAPTCHA auto-solving** — Cloudflare Turnstile, reCAPTCHA v2, hCaptcha via [2captcha.com](https://2captcha.com)
- **Proxy support** — native integration with [2prx.com](https://2prx.com) or any HTTP proxy
- **Fingerprint evasion** — randomized UA, viewport, language, timezone, WebDriver flag removal
- **Anti-detect browser** — optional deep stealth via the [2captcha Anti-Detect Browser](https://2captcha.com/anti-detect-browser) (separate license)
- **JSON + CSV export** — structured data ready for analysis, pipelines, or databases
- **Detail page scraping** — enrich listings with descriptions, seller info, and attributes
- **Pagination** — automatically follows next-page links up to a configurable limit
- **Human-like delays** — random pauses between requests to reduce detection risk

---

## Quick Start

### 1. Clone the repository

```bash
git clone https://github.com/2scraper/dubizzle-scraper.git
cd dubizzle-scraper
```

### 2. Install dependencies

```bash
pip install -r requirements.txt
```

**Playwright** requires an extra step:

```bash
playwright install chromium
```

### 3. Run a scraper

```bash
# Playwright (recommended) — scrape motors and property-for-rent
python dubizzle_playwright.py --categories motors property-for-rent --max-pages 3

# Selenium
python dubizzle_selenium.py --categories motors --max-pages 5

# Pyppeteer
python dubizzle_pyppeteer.py --categories jobs --max-pages 2
```

---

## Configuration

### Environment Variables

| Variable | Description | Example |
|---|---|---|
| `CAPTCHA_API_KEY` | Your [2captcha.com](https://2captcha.com) API key | `abc123...` |
| `PROXY_HOST` | Proxy hostname | `gate.2prx.com` |
| `PROXY_PORT` | Proxy port | `9090` |
| `PROXY_USER` | Proxy auth username | `user` |
| `PROXY_PASS` | Proxy auth password | `pass` |
| `PROXY_URL` | Full proxy URL (overrides individual vars) | `http://user:pass@gate.2prx.com:9090` |

### Command-Line Arguments

| Argument | Default | Description |
|---|---|---|
| `--categories` | all | Space-separated list of categories to scrape |
| `--max-pages` | `3` | Maximum number of pages per subcategory |
| `--details` | off | Also scrape individual listing detail pages |
| `--output` | `dubizzle_listings` | Output filename (without extension) |
| `--format` | `both` | Output format: `json`, `csv`, or `both` |
| `--headless` | on | Run browser in headless mode |
| `--no-headless` | — | Show the browser window |
| `--captcha-key` | env | 2captcha API key (overrides `CAPTCHA_API_KEY`) |

### Available Categories

| Category | Subcategories Scraped |
|---|---|
| `motors` | Used Cars, Motorcycles, Auto Parts, Heavy Vehicles, Boats, Number Plates |
| `property-for-rent` | Residential, Commercial, Rooms, Short-term |
| `property-for-sale` | Residential, Commercial, Land, Off-plan |
| `jobs` | All jobs |
| `classifieds` | All classifieds |
| `furniture-home-garden` | Furniture & Home Garden |
| `electronics` | Computers, Phones, TVs, Cameras |
| `fashion` | Clothing & Accessories, Jewelry & Watches |
| `pets` | All pets |
| `community` | Community |
| `business-industrial` | Business & Industrial |

---

## URL Structure

Dubizzle redirects `dubizzle.com/motors` → `uae.dubizzle.com/motors/`. The scraper targets `uae.dubizzle.com` directly. Category landing pages (e.g. `/motors/`) don't show listing grids — actual listings live under subcategories (e.g. `/motors/used-cars/`). The scraper handles this automatically.

---

## CAPTCHA Solving with 2captcha.com

| CAPTCHA Type | Supported |
|---|---|
| Cloudflare Turnstile | ✅ |
| reCAPTCHA v2 | ✅ |
| hCaptcha | ✅ |

```bash
export CAPTCHA_API_KEY="your_key_here"
python dubizzle_playwright.py
```

Sign up at [2captcha.com](https://2captcha.com) · built-in retry logic · graceful fallback if solve fails.

---

## Proxy Setup with 2prx.com

```bash
export PROXY_HOST=gate.2prx.com
export PROXY_PORT=9090
export PROXY_USER=your_username
export PROXY_PASS=your_password

python dubizzle_playwright.py --categories motors
```

Or: `export PROXY_URL="http://user:pass@gate.2prx.com:9090"`

---

## Anti-Detect Browser

For maximum stealth, use the scraper with the [2captcha Anti-Detect Browser](https://2captcha.com/anti-detect-browser) — deep browser fingerprint management including Canvas, WebGL, AudioContext, TLS/JA3, and hardware spoofing.

---

## Output Format

### JSON

```json
[
  {
    "title": "AED 6063/month | 2023 Land Rover Range Rover | GCC Specs",
    "price": "AED 399,000",
    "currency": "AED",
    "location": "Dubai Marina, Dubai",
    "category": "motors",
    "subcategory": "/motors/used-cars/",
    "url": "https://uae.dubizzle.com/motors/used-cars/land-rover/range-rover/...",
    "image_url": "https://...",
    "description": "...",
    "posted_date": "2025-04-10",
    "listing_id": "66966b05b2b746e48835c2b9791ecf7e",
    "seller_name": "AutoDealer UAE",
    "attributes": {
      "year": "2023",
      "kilometers": "500",
      "transmission": "Automatic"
    }
  }
]
```

---

## Requirements

```
playwright>=1.40
selenium>=4.15
pyppeteer>=2.0
2captcha-python>=1.2
```

---

## Project Structure

```
dubizzle-scraper/
├── dubizzle_core.py          # Shared parsing, extraction, export logic
├── dubizzle_playwright.py    # Playwright scraper (recommended)
├── dubizzle_selenium.py      # Selenium scraper
├── dubizzle_pyppeteer.py     # Pyppeteer scraper
├── requirements.txt
├── README.md
└── LICENSE                   # MIT
```

---

## Legal Notice

This tool is provided for educational and research purposes. Users are responsible for ensuring their use complies with dubizzle.com's Terms of Service and applicable laws.

---

## License

MIT — see [LICENSE](LICENSE) for details.

---

**Powered by [2captcha.com](https://2captcha.com) · Proxies by [2prx.com](https://2prx.com) · Open source by [2scraper](https://github.com/2scraper)**
