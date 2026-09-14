import sys, pathlib, collections
sys.path.insert(0, ".")
from product_parser import (detect_page_state, parse_products, total_pages,
                            total_results, pages_beyond_cap, search_header,
                            page_number_from_url)

cap = pathlib.Path("/Users/jehr/Work/2scraper/dubizzle/captures")
FILES = [
    ("uae_usedcars_p1.html", "https://uae.dubizzle.com/motors/used-cars/", 200),
    ("uae_usedcars_p2.html", "https://uae.dubizzle.com/motors/used-cars/?page=2", 200),
    ("uae_usedcars_p400.html", "https://uae.dubizzle.com/motors/used-cars/?page=400", 200),
    ("uae_usedcars_p401.html", "https://uae.dubizzle.com/motors/used-cars/?page=401", 200),
    ("uae_usedcars_p99999.html", "https://uae.dubizzle.com/motors/used-cars/?page=99999", 200),
    ("uae_usedcars_ar_p1.html", "https://uae.dubizzle.com/ar/motors/used-cars/", 200),
    ("uae_property_rent_p1.html", "https://uae.dubizzle.com/property-for-rent/residential/apartmentflat/", 200),
    ("uae_property_sale_apt_p1.html", "https://uae.dubizzle.com/property-for-sale/residential/apartment/", 200),
    ("uae_classified_tv_p1.html", "https://uae.dubizzle.com/classified/electronics/televisions/", 200),
    ("uae_jobs_accounting_p1.html", "https://uae.dubizzle.com/jobs/accounting-finance/", 200),
    ("uae_community_auto_p1.html", "https://uae.dubizzle.com/community/auto-services/", 200),
    ("uae_home.html", "https://uae.dubizzle.com/", 200),
    ("uae_property_sale_p1.html", "https://uae.dubizzle.com/property-for-sale/residential/apartmentflat/", 404),
    ("block_incapsula_iframe.html", "https://uae.dubizzle.com/motors/used-cars/", 403),
    ("block_pardon_interruption.html", "https://uae.dubizzle.com/motors/used-cars/", 200),
]

for f, u, st in FILES:
    p = cap / f
    if not p.exists():
        print("MISSING", f)
        continue
    h = p.read_text()
    state = detect_page_state(h, st, u)
    rows = parse_products(h, u, page=page_number_from_url(u))
    n = lambda k: sum(1 for r in rows if getattr(r, k) is not None)
    print("%-32s state=%-9s rows=%-4s tp=%-6s tot=%-7s beyond=%-5s idx=%s" % (
        f, state, len(rows), total_pages(h, u), total_results(h),
        pages_beyond_cap(h), search_header(h)))
    if rows:
        print("      price %d/%d  cur %d  brand %d  seller %d  city %d  sku %d  attrs %d  %s" % (
            n("price"), len(rows), n("currency"), n("brand"), n("seller_name"),
            n("city"), n("sku"), n("attributes"),
            dict(collections.Counter(r.price_source for r in rows))))
        r = rows[0]
        print("      [0]", (r.sku or "")[:58], "|", (r.title or "")[:32], "|",
              r.price, r.currency, "|", r.vertical, "|", r.listing_kind)
