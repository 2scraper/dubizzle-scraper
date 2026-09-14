import re, sys, pathlib, collections
sys.path.insert(0, ".")
from product_parser import parse_products, detect_page_state

cap = pathlib.Path("/Users/jehr/Work/2scraper/dubizzle/captures")
CASES = [("uae_usedcars_p1.html", "https://uae.dubizzle.com/motors/used-cars/"),
         ("uae_property_rent_p1.html", "https://uae.dubizzle.com/property-for-rent/residential/apartmentflat/"),
         ("uae_classified_tv_p1.html", "https://uae.dubizzle.com/classified/electronics/televisions/")]

for f, u in CASES:
    h = (cap / f).read_text()
    full = parse_products(h, u)
    # strip the payload: what the DOM path alone recovers
    stripped = re.sub(r'<script[^>]+id="__NEXT_DATA__"[^>]*>.*?</script>', "", h, flags=re.S)
    dom = parse_products(stripped, u)
    n = lambda rows, k: sum(1 for r in rows if getattr(r, k) is not None)
    print("== %s   payload=%d  dom-only=%d   state(dom)=%s"
          % (f, len(full), len(dom), detect_page_state(stripped, 200, u)))
    print("   dom: price %d  currency %d  title %d  sku %d   %s"
          % (n(dom, "price"), n(dom, "currency"), n(dom, "title"), n(dom, "sku"),
             dict(collections.Counter(r.price_source for r in dom))))
    same = {r.sku for r in full} & {r.sku for r in dom}
    print("   sku overlap payload vs dom: %d of %d" % (len(same), len(full)))
    if dom:
        r = dom[0]
        print("   [0]", (r.title or "")[:46], "|", r.price, r.currency)
    # do the two paths agree on the price of the ads they share?
    fp = {r.sku: r.price for r in full}
    diff = [(s, fp[s], r.price) for r in dom if (s := r.sku) in fp and fp[s] != r.price]
    print("   price disagreements:", len(diff), diff[:3])
