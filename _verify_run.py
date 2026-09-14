import json, sys, collections

rows = json.load(open(sys.argv[1] if len(sys.argv) > 1 else "live_p1.json"))
meta = json.load(open((sys.argv[1] if len(sys.argv) > 1 else "live_p1") .replace(".json", "") + ".meta.json"))
print("rows:", len(rows), " meta status:", meta.get("status"), meta.get("stop_reason"),
      "pages:", meta.get("pages_completed"), "failed:", meta.get("pages_failed"))

skus = [r["sku"] for r in rows]
print("distinct sku:", len(set(skus)), "of", len(skus))
dupes = [s for s, c in collections.Counter(skus).items() if c > 1]
print("duplicate skus:", len(dupes), dupes[:3])

pp = [(r["page"], r["position"]) for r in rows]
print("page+position unique:", len(set(pp)) == len(pp), " pages:", sorted({r["page"] for r in rows}))

for k in ("price", "currency", "title", "url", "brand", "seller_name", "city",
          "location", "listing_id", "listing_uuid", "short_url", "attributes",
          "posted_at", "bumped_at", "image_url", "year", "kilometers",
          "seller_kind", "is_premium", "photos_count", "in_stock"):
    n = sum(1 for r in rows if r.get(k) is not None)
    print("  %-14s %3d/%d" % (k, n, len(rows)))

print("price_source:", dict(collections.Counter(r["price_source"] for r in rows)))
print("listing_kind:", dict(collections.Counter(r["listing_kind"] for r in rows)))
print("vertical    :", dict(collections.Counter(r["vertical"] for r in rows)))
print("currency    :", dict(collections.Counter(r["currency"] for r in rows)))
bad = [r["sku"] for r in rows if r.get("original_price") is not None
       and r.get("price") is not None and r["original_price"] <= r["price"]]
print("original_price <= price:", len(bad))
print("negative/zero discount:", sum(1 for r in rows if (r.get("discount_pct") or 1) <= 0))
print("\nsample row:")
print(json.dumps(rows[0], ensure_ascii=False, indent=1)[:1200])
