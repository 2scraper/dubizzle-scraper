"""
dubizzle_core — Shared extraction logic for all three scraper implementations.
================================================================================
Handles __NEXT_DATA__ parsing, DOM fallback, CAPTCHA solving, data export.

Key insight:  dubizzle's Next.js Redux store uses bilingual dicts everywhere:
    name:         {"en": "...", "ar": "..."}
    absolute_url: {"en": "https://...", "ar": "https://..."}
    price:        plain integer (460000)
    details:      {"Key": {"en": {"label": "...", "value": "..."}, "ar": {...}}}
    photos:       dict {micro, main} (motors) OR list [{main, thumb}] (property)
"""

import csv
import json
import logging
import os
import re
from dataclasses import asdict, dataclass, field
from typing import Optional
from urllib.parse import urljoin

log = logging.getLogger("dubizzle")

BASE_URL = "https://uae.dubizzle.com"

# ---------------------------------------------------------------------------
# Category map — top-level name → browsable subcategory paths
# ---------------------------------------------------------------------------

CATEGORIES = {
    "motors": [
        "/motors/used-cars/",
        "/motors/motorcycles/",
        "/motors/auto-accessories-parts/",
        "/motors/heavy-vehicles/",
        "/motors/boats/",
        "/motors/number-plates/",
    ],
    "property-for-rent": [
        "/en/property-for-rent/residential/",
        "/en/property-for-rent/commercial/",
        "/en/property-for-rent/rooms-for-rent/",
        "/en/property-for-rent/short-term/",
    ],
    "property-for-sale": [
        "/en/property-for-sale/residential/",
        "/en/property-for-sale/commercial/",
        "/en/property-for-sale/land/",
        "/en/property-for-sale/off-plan/",
    ],
    "jobs": ["/jobs/"],
    "classifieds": ["/classifieds/"],
    "furniture-home-garden": ["/classifieds/furniture-home-garden/"],
    "electronics": [
        "/classifieds/computers-networking/",
        "/classifieds/mobile-phones-pdas/",
        "/classifieds/tv-dvd-blu-ray/",
        "/classifieds/camera-camcorder/",
    ],
    "fashion": ["/classifieds/clothing-accessories/", "/classifieds/jewelry-watches/"],
    "pets": ["/classifieds/pets/"],
    "community": ["/community/"],
    "business-industrial": ["/classifieds/business-industrial/"],
}

ALL_CATEGORY_NAMES = list(CATEGORIES.keys())


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class Listing:
    title: str = ""
    price: str = ""
    currency: str = "AED"
    location: str = ""
    category: str = ""
    subcategory: str = ""
    url: str = ""
    image_url: str = ""
    description: str = ""
    posted_date: str = ""
    listing_id: str = ""
    seller_name: str = ""
    attributes: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Bilingual dict helper
# ---------------------------------------------------------------------------

def _en(val, fallback: str = "") -> str:
    """
    Extract the English text from a dubizzle bilingual value.
    Handles:  str → str,  {"en": "...", "ar": "..."} → en,  None → fallback
    """
    if val is None:
        return fallback
    if isinstance(val, str):
        return val
    if isinstance(val, dict):
        return val.get("en") or val.get("ar") or fallback
    return str(val)


# ---------------------------------------------------------------------------
# __NEXT_DATA__ extraction  (primary strategy)
# ---------------------------------------------------------------------------

def extract_from_next_data(next_data: dict, category: str, subcategory: str) -> tuple[list[Listing], Optional[dict]]:
    """
    Parse listings from __NEXT_DATA__.
    Returns (listings, pagination) where pagination is e.g.
    {"page": 0, "totalPages": 400, "hitsPerPage": 25, "totalHits": 36777}
    """
    listings: list[Listing] = []
    pagination: Optional[dict] = None

    try:
        actions = next_data["props"]["pageProps"]["reduxWrapperActionsGIPP"]
    except (KeyError, TypeError):
        log.debug("No reduxWrapperActionsGIPP found in __NEXT_DATA__")
        return [], None

    for act in actions:
        atype = act.get("type", "")
        payload = act.get("payload")

        if not isinstance(payload, dict):
            continue

        # Primary: the specific action that holds search results
        if "fetchListingData" in atype or "fulfilled" in atype:
            hits = payload.get("hits", [])
            if isinstance(hits, list) and hits:
                log.debug("Found %d hits in action '%s'", len(hits), atype)
                for item in hits:
                    if isinstance(item, dict):
                        listing = _parse_hit(item, category, subcategory)
                        if listing:
                            listings.append(listing)

            # Grab pagination metadata
            pag = payload.get("pagination")
            if isinstance(pag, dict) and "totalPages" in pag:
                pagination = pag

            # Also grab cotwListings (Cars of the Week etc.)
            cotw = payload.get("cotwListings", [])
            if isinstance(cotw, list):
                for item in cotw:
                    if isinstance(item, dict):
                        listing = _parse_hit(item, category, subcategory)
                        if listing:
                            listings.append(listing)

        # Fallback: any action payload containing a 'hits' list
        if not listings:
            for key in ("hits", "listings", "results", "items"):
                val = payload.get(key)
                if isinstance(val, list) and val and isinstance(val[0], dict):
                    for item in val:
                        listing = _parse_hit(item, category, subcategory)
                        if listing:
                            listings.append(listing)
                    if listings:
                        break

    # Deduplicate
    seen = set()
    unique = []
    for l in listings:
        key = l.listing_id or l.url
        if key and key not in seen:
            seen.add(key)
            unique.append(l)
    return unique, pagination


def _parse_hit(item: dict, category: str, subcategory: str) -> Optional[Listing]:
    """Parse a single listing from the dubizzle Redux store hit object."""
    listing = Listing(category=category, subcategory=subcategory)

    # ---- Title ----
    listing.title = _en(item.get("name") or item.get("title") or item.get("heading"))

    # ---- Price ----
    price_raw = item.get("price")
    if isinstance(price_raw, (int, float)):
        listing.price = f"AED {price_raw:,.0f}"
        listing.currency = "AED"
    elif isinstance(price_raw, str):
        listing.price = price_raw
        listing.currency = "AED" if "AED" in price_raw else ""
    elif isinstance(price_raw, dict):
        listing.price = str(price_raw.get("value", price_raw.get("amount", "")))
        listing.currency = price_raw.get("currency", "AED")

    # ---- URL ----
    abs_url = item.get("absolute_url")
    if isinstance(abs_url, dict):
        listing.url = abs_url.get("en") or abs_url.get("ar") or ""
    elif isinstance(abs_url, str):
        listing.url = urljoin(BASE_URL, abs_url)
    if not listing.url:
        permalink = item.get("permalink") or item.get("short_url") or ""
        if permalink:
            listing.url = permalink

    # ---- ID ----
    listing.listing_id = str(item.get("id") or item.get("external_id") or item.get("uuid") or "")

    # ---- Location ----
    # Motors: places = {"en": ["UAE", "Dubai", "Al Quoz"], ...}
    places = item.get("places")
    if isinstance(places, dict):
        en_places = places.get("en", [])
        if isinstance(en_places, list) and len(en_places) > 1:
            listing.location = ", ".join(en_places[1:])  # skip "UAE"
    # Property: city + neighborhoods
    if not listing.location:
        city = item.get("city")
        if isinstance(city, dict):
            city_name = _en(city.get("name", city))
            neighborhoods = item.get("neighborhoods", {})
            if isinstance(neighborhoods, dict):
                nh_names = neighborhoods.get("name", {})
                nh_en = _en(nh_names) if isinstance(nh_names, str) else ""
                if isinstance(nh_names, dict):
                    nh_list = nh_names.get("en", [])
                    nh_en = ", ".join(nh_list) if isinstance(nh_list, list) else ""
                listing.location = f"{nh_en}, {city_name}" if nh_en else city_name
            else:
                listing.location = city_name
    # Generic fallback
    if not listing.location:
        site = item.get("site")
        if isinstance(site, dict):
            listing.location = _en(site)
        loc = item.get("location_list")
        if isinstance(loc, dict):
            en_locs = loc.get("en", [])
            if isinstance(en_locs, list) and len(en_locs) > 1:
                listing.location = ", ".join(en_locs[1:])

    # ---- Image ----
    photos = item.get("photos")
    if isinstance(photos, dict):
        # Motors: {micro: url, main: url}
        listing.image_url = photos.get("main") or photos.get("micro") or ""
    elif isinstance(photos, list) and photos:
        # Property: [{main: url, thumb: url}, ...]
        first = photos[0]
        if isinstance(first, dict):
            listing.image_url = first.get("main") or first.get("thumb") or ""
        elif isinstance(first, str):
            listing.image_url = first
    if not listing.image_url:
        thumbs = item.get("photo_thumbnails", [])
        if isinstance(thumbs, list) and thumbs:
            listing.image_url = thumbs[0] if isinstance(thumbs[0], str) else ""

    # ---- Description ----
    listing.description = _en(item.get("description_short") or item.get("description") or "")

    # ---- Date ----
    created = item.get("created_at") or item.get("added")
    if isinstance(created, (int, float)):
        import datetime
        try:
            listing.posted_date = datetime.datetime.fromtimestamp(created).strftime("%Y-%m-%d")
        except (OSError, ValueError):
            listing.posted_date = str(created)
    elif created:
        listing.posted_date = str(created)

    # ---- Seller ----
    agent = item.get("agent") or item.get("agent_profile")
    if isinstance(agent, dict):
        listing.seller_name = _en(agent.get("name", ""))
    seller_type = item.get("seller_type") or item.get("listed_by")
    if isinstance(seller_type, dict):
        listing.attributes["seller_type"] = _en(seller_type)
    elif isinstance(seller_type, str) and seller_type:
        listing.attributes["seller_type"] = seller_type

    # ---- Attributes ----
    # Motors: details = {"Key": {"en": {"label": "...", "value": "..."}, ...}}
    details = item.get("details")
    if isinstance(details, dict):
        for key, val in details.items():
            if isinstance(val, dict):
                en_detail = val.get("en", {})
                if isinstance(en_detail, dict):
                    label = en_detail.get("label", key)
                    value = en_detail.get("value", "")
                    if value:
                        listing.attributes[label] = value

    # Property: property_info = [{"label": {"en": "Type"}, "value": {"en": "Apartment"}}]
    prop_info = item.get("property_info")
    if isinstance(prop_info, list):
        for info in prop_info:
            if isinstance(info, dict):
                label = _en(info.get("label", ""))
                value = _en(info.get("value", ""))
                if label and value:
                    listing.attributes[label] = value

    # Property-specific fields
    for field_name in ("bedrooms", "bathrooms", "size", "furnished", "completion_status",
                       "payment_frequency", "property_reference"):
        val = item.get(field_name)
        if val is not None and val != "" and val is not False:
            listing.attributes[field_name] = _en(val) if isinstance(val, dict) else str(val)

    # Category info
    cat_data = item.get("category") or item.get("categories")
    if isinstance(cat_data, dict):
        cat_en = cat_data.get("en") or cat_data.get("name", {}).get("en")
        if isinstance(cat_en, list):
            listing.attributes["category_path"] = " > ".join(cat_en)

    if listing.title or listing.url:
        return listing
    return None


# ---------------------------------------------------------------------------
# DOM extraction  (fallback)
# ---------------------------------------------------------------------------

PRICE_RE = re.compile(r"(AED|USD|EUR)\s*[\d,]+")

DOM_EXTRACT_JS = """() => {
    const res = [];
    for (const a of document.querySelectorAll('a[href]')) {
        const h = a.getAttribute('href');
        if (!h) continue;
        if (h.split('/').filter(Boolean).length < 4) continue;
        if (a.closest('#header-container') || a.closest('footer')
            || a.closest('[data-testid="footer_links"]') || a.closest('nav')) continue;
        const t = a.innerText.trim();
        if (!t || t.length < 10) continue;
        const c = { href: h, full_text: t };
        const img = a.querySelector('img[src]');
        if (img) c.image = img.src;
        res.push(c);
    }
    return res;
}"""


def parse_dom_cards(card_data: list, category: str, subcategory: str) -> list[Listing]:
    listings = []
    for card in card_data:
        l = _parse_dom_card(card, category, subcategory)
        if l:
            listings.append(l)
    return listings


def _parse_dom_card(card: dict, cat: str, sub: str) -> Optional[Listing]:
    l = Listing(category=cat, subcategory=sub)
    href = card.get("href", "")
    l.url = urljoin(BASE_URL, href) if href else ""
    if href:
        l.listing_id = href.rstrip("/").split("/")[-1]
    l.image_url = card.get("image", "")

    lines = [x.strip() for x in card.get("full_text", "").split("\n") if x.strip()]
    if not lines:
        return None

    for ln in lines:
        m = PRICE_RE.search(ln)
        if m:
            l.price = ln
            l.currency = m.group(1)
            break

    for ln in lines:
        if not PRICE_RE.search(ln) and len(ln) > 15:
            l.title = ln
            break

    for ln in lines:
        if "•" in ln:
            for p in ln.split("•"):
                p = p.strip()
                if ":" in p:
                    k, v = p.split(":", 1)
                    l.attributes[k.strip()] = v.strip()
        elif ":" in ln and ln != l.title and not PRICE_RE.search(ln):
            k, v = ln.split(":", 1)
            l.attributes[k.strip()] = v.strip()

    cities = ("Dubai", "Abu Dhabi", "Sharjah", "Ajman", "RAK", "Fujairah", "Al Ain")
    for ln in lines:
        if any(c in ln for c in cities) and ln != l.title:
            l.location = ln
            break

    return l if (l.title or l.url) else None


# ---------------------------------------------------------------------------
# CAPTCHA solver (2captcha.com)
# ---------------------------------------------------------------------------

try:
    from twocaptcha import TwoCaptcha
except ImportError:
    TwoCaptcha = None


class CaptchaSolver:
    def __init__(self, api_key: str):
        if TwoCaptcha is None:
            raise RuntimeError("Install: pip install 2captcha-python")
        self.solver = TwoCaptcha(api_key)
        log.info("2captcha solver ready (key: %s…)", api_key[:8])

    def solve_turnstile(self, sitekey, url):
        try:
            log.info("Solving Turnstile via 2captcha …")
            r = self.solver.turnstile(sitekey=sitekey, url=url)
            return r.get("code") if isinstance(r, dict) else r
        except Exception as e:
            log.warning("Turnstile failed: %s", e)
            return None

    def solve_recaptcha_v2(self, sitekey, url):
        try:
            log.info("Solving reCAPTCHA v2 via 2captcha …")
            r = self.solver.recaptcha(sitekey=sitekey, url=url)
            return r.get("code") if isinstance(r, dict) else r
        except Exception as e:
            log.warning("reCAPTCHA failed: %s", e)
            return None

    def solve_hcaptcha(self, sitekey, url):
        try:
            log.info("Solving hCaptcha via 2captcha …")
            r = self.solver.hcaptcha(sitekey=sitekey, url=url)
            return r.get("code") if isinstance(r, dict) else r
        except Exception as e:
            log.warning("hCaptcha failed: %s", e)
            return None


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------

def export_json(listings: list[Listing], filepath: str):
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump([asdict(l) for l in listings], f, ensure_ascii=False, indent=2)
    log.info("Exported %d listings → %s", len(listings), filepath)


def export_csv(listings: list[Listing], filepath: str):
    if not listings:
        return
    data = [asdict(l) for l in listings]
    for row in data:
        row["attributes"] = json.dumps(row.get("attributes", {}), ensure_ascii=False)
    with open(filepath, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(data[0].keys()))
        w.writeheader()
        w.writerows(data)
    log.info("Exported %d listings → %s", len(listings), filepath)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def get_env(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


def build_proxy_url() -> Optional[str]:
    full = get_env("PROXY_URL")
    if full:
        return full
    host = get_env("PROXY_HOST")
    port = get_env("PROXY_PORT")
    if not host:
        return None
    user = get_env("PROXY_USER")
    pwd = get_env("PROXY_PASS")
    if user and pwd:
        return f"http://{user}:{pwd}@{host}:{port}"
    return f"http://{host}:{port}"


# Fingerprint evasion constants
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:126.0) Gecko/20100101 Firefox/126.0",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Safari/605.1.15",
]

VIEWPORTS = [
    {"width": 1920, "height": 1080},
    {"width": 1366, "height": 768},
    {"width": 1536, "height": 864},
    {"width": 1440, "height": 900},
    {"width": 1280, "height": 720},
]

FINGERPRINT_JS = """
    // Hide the webdriver flag (safe, doesn't break page APIs)
    Object.defineProperty(navigator, 'webdriver', { get: () => false });

    // Patch permissions API (safe)
    if (navigator.permissions && navigator.permissions.query) {
        const origQuery = navigator.permissions.query.bind(navigator.permissions);
        navigator.permissions.query = (params) =>
            params.name === 'notifications'
                ? Promise.resolve({ state: Notification.permission })
                : origQuery(params);
    }
"""

NEXT_DATA_JS = """() => {
    const el = document.getElementById('__NEXT_DATA__');
    if (!el) return null;
    try { return JSON.parse(el.textContent); } catch(e) { return null; }
}"""

PAGINATION_JS = """() => {
    return !!document.querySelector(
        'a[aria-label="Next"], a[aria-label="next"], '
        + '[data-testid="pagination-next"], a[rel="next"]'
    );
}"""
