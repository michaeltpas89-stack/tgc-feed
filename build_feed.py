#!/usr/bin/env python3
"""Build ONE reseller feed file straight from the Shopify Admin API.

Reads every product in a Shopify collection (default handle: reseller-feed),
creates one <item> per variant and writes the result to docs/, which GitHub
Pages serves as a single stable URL. Independent of the theme: publishing a new
theme can no longer break the feed.

Needs three environment variables (GitHub repository secrets):
  SHOPIFY_STORE           e.g. quaq24-ad.myshopify.com
  SHOPIFY_CLIENT_ID       from the Dev Dashboard app
  SHOPIFY_CLIENT_SECRET   from the Dev Dashboard app (keep secret)
App scopes needed: read_products, read_inventory.

Safety: if anything looks wrong (API error, too few items, missing fields),
the script stops with an error and leaves the previous good files untouched.
Standard library only.
"""

import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from decimal import Decimal

# --------------------------------------------------------------------------
# SETTINGS: edit here
# --------------------------------------------------------------------------

COLLECTION_HANDLE = os.environ.get("FEED_COLLECTION", "reseller-feed")
API_VERSION = os.environ.get("SHOPIFY_API_VERSION", "2026-10")  # bump about once a year
IMAGE_WIDTH = 2000

# Output files in docs/. Value None = all fields. Use "exclude" to leave fields out
# of a specific file, e.g. for a reseller who may not see stock levels.
OUTPUTS = {
    "feed.xml": None,
    "feed-no-stock.xml": {"exclude": ["stock_quantity"]},
}
OUTPUT_DIR = "docs"

# Refuse to publish when the feed looks broken.
MIN_ITEMS = int(os.environ.get("FEED_MIN_ITEMS", "200"))
REQUIRED_FIELDS = ["title", "url", "rrp_incl_vat"]
MAX_EMPTY_EAN_SHARE = 0.25

# --------------------------------------------------------------------------

STORE = os.environ.get("SHOPIFY_STORE", "").strip()
CLIENT_ID = os.environ.get("SHOPIFY_CLIENT_ID", "").strip()
CLIENT_SECRET = os.environ.get("SHOPIFY_CLIENT_SECRET", "").strip()
# Only for tests against a mock server:
BASE_URL = os.environ.get("SHOPIFY_BASE_URL", "").strip() or f"https://{STORE}"

VARIANT_FIELDS = """
  id legacyResourceId title price inventoryQuantity
  inventoryItem { tracked }
  barcodes(first: 1) { nodes { value } }
  selectedOptions { name value }
  media(first: 1) { nodes { preview { image { url } } } }
"""

PRODUCTS_QUERY = """
query($handle: String!, $first: Int!, $after: String, $vfirst: Int!) {
  shop { currencyCode }
  collectionByIdentifier(identifier: {handle: $handle}) {
    products(first: $first, after: $after) {
      nodes {
        id title productType onlineStoreUrl status
        seo { description }
        featuredMedia { preview { image { url } } }
        options { name }
        variants(first: $vfirst) {
          nodes { %s }
          pageInfo { hasNextPage endCursor }
        }
      }
      pageInfo { hasNextPage endCursor }
    }
  }
}
""" % VARIANT_FIELDS

MORE_VARIANTS_QUERY = """
query($id: ID!, $after: String, $vfirst: Int!) {
  product(id: $id) {
    variants(first: $vfirst, after: $after) {
      nodes { %s }
      pageInfo { hasNextPage endCursor }
    }
  }
}
""" % VARIANT_FIELDS


class CostTooHigh(Exception):
    pass


def http_json(url, payload, headers, form=False, tries=4):
    """POST and return parsed JSON, with retries on network errors / 5xx / 429."""
    if form:
        body = urllib.parse.urlencode(payload).encode()
        ctype = "application/x-www-form-urlencoded"
    else:
        body = json.dumps(payload).encode()
        ctype = "application/json"
    last = None
    for attempt in range(1, tries + 1):
        req = urllib.request.Request(
            url, data=body, method="POST",
            headers={"Content-Type": ctype, "Accept": "application/json", **headers},
        )
        try:
            with urllib.request.urlopen(req, timeout=90) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:300]
            if exc.code in (429, 500, 502, 503, 504):
                last = f"HTTP {exc.code}"
            else:
                raise RuntimeError(f"HTTP {exc.code} from {url}: {detail}")
        except (urllib.error.URLError, TimeoutError, ConnectionError, json.JSONDecodeError) as exc:
            last = exc
        wait = 4 * attempt
        print(f"  attempt {attempt} failed ({last}); retrying in {wait}s", file=sys.stderr)
        time.sleep(wait)
    raise RuntimeError(f"Could not reach {url}: {last}")


def get_token():
    if not (STORE and CLIENT_ID and CLIENT_SECRET) and not os.environ.get("SHOPIFY_BASE_URL"):
        raise RuntimeError(
            "Missing SHOPIFY_STORE / SHOPIFY_CLIENT_ID / SHOPIFY_CLIENT_SECRET "
            "(add them as GitHub repository secrets)."
        )
    data = http_json(
        f"{BASE_URL}/admin/oauth/access_token",
        {"grant_type": "client_credentials", "client_id": CLIENT_ID, "client_secret": CLIENT_SECRET},
        {}, form=True,
    )
    token = data.get("access_token")
    if not token:
        raise RuntimeError(f"No access_token in response: {str(data)[:200]}")
    return token


def graphql(token, query, variables, max_throttle_waits=12):
    url = f"{BASE_URL}/admin/api/{API_VERSION}/graphql.json"
    for _ in range(max_throttle_waits):
        data = http_json(url, {"query": query, "variables": variables},
                         {"X-Shopify-Access-Token": token})
        errors = data.get("errors")
        if not errors:
            return data["data"]
        if isinstance(errors, str):
            raise RuntimeError(f"GraphQL error: {errors}")
        codes = {(e.get("extensions") or {}).get("code") for e in errors}
        if "THROTTLED" in codes:
            print("  throttled by Shopify; waiting 3s", file=sys.stderr)
            time.sleep(3)
            continue
        if "MAX_COST_EXCEEDED" in codes:
            raise CostTooHigh()
        raise RuntimeError("GraphQL error: " + "; ".join(e.get("message", "?") for e in errors))
    raise RuntimeError("Shopify kept throttling the requests; giving up.")


def fetch_products(token):
    """Return (currency, list of product dicts with all variants)."""
    products, after, currency = [], None, None
    first, vfirst = 10, 50
    while True:
        try:
            data = graphql(token, PRODUCTS_QUERY,
                           {"handle": COLLECTION_HANDLE, "first": first, "after": after, "vfirst": vfirst})
        except CostTooHigh:
            if first > 1:
                first = max(1, first // 2)
            elif vfirst > 5:
                vfirst = max(5, vfirst // 2)
            else:
                raise RuntimeError("Query too expensive even at the smallest page size.")
            print(f"  query too expensive; now {first} products x {vfirst} variants per request")
            continue
        currency = data["shop"]["currencyCode"]
        col = data["collectionByIdentifier"]
        if col is None:
            raise RuntimeError(f"Collection '{COLLECTION_HANDLE}' not found.")
        page = col["products"]
        for prod in page["nodes"]:
            variants = prod["variants"]
            vnodes = list(variants["nodes"])
            vinfo = variants["pageInfo"]
            while vinfo["hasNextPage"]:
                more = graphql(token, MORE_VARIANTS_QUERY,
                               {"id": prod["id"], "after": vinfo["endCursor"], "vfirst": vfirst})
                mv = more["product"]["variants"]
                vnodes.extend(mv["nodes"])
                vinfo = mv["pageInfo"]
            prod["variant_list"] = vnodes
            products.append(prod)
        print(f"  {len(products)} products read")
        if not page["pageInfo"]["hasNextPage"]:
            return currency, products
        after = page["pageInfo"]["endCursor"]


def image_url(url):
    if not url:
        return ""
    parts = urllib.parse.urlsplit(url)
    q = [(k, v) for k, v in urllib.parse.parse_qsl(parts.query) if k != "width"]
    q.append(("width", str(IMAGE_WIDTH)))
    return urllib.parse.urlunsplit(parts._replace(query=urllib.parse.urlencode(q)))


def build_items(currency, products):
    """One dict per variant. Dict order = XML field order."""
    items, skipped = [], []
    for prod in products:
        if prod.get("status") != "ACTIVE" or not prod.get("onlineStoreUrl"):
            skipped.append(prod["title"])  # not visible in the online store: no URL to share
            continue
        variants = prod["variant_list"]
        default_only = len(variants) == 1 and all(
            o["value"] == "Default Title" for o in variants[0]["selectedOptions"]
        )
        desc = ((prod.get("seo") or {}).get("description") or "").strip()
        product_image = None
        fm = prod.get("featuredMedia")
        if fm and fm.get("preview") and fm["preview"].get("image"):
            product_image = fm["preview"]["image"]["url"]
        for v in variants:
            item = {}
            barcodes = (v.get("barcodes") or {}).get("nodes") or []
            item["ean"] = (barcodes[0]["value"] or "").strip() if barcodes else ""
            item["title"] = prod["title"] if default_only else f"{prod['title']} - {v['title']}"
            item["product_type"] = prod.get("productType") or ""
            item["rrp_incl_vat"] = f"{Decimal(v['price']):.2f}"
            item["currency"] = currency
            tracked = (v.get("inventoryItem") or {}).get("tracked")
            item["stock_quantity"] = str(max(0, v["inventoryQuantity"])) if tracked else ""
            item["url"] = f"{prod['onlineStoreUrl']}?variant={v['legacyResourceId']}"
            media = (v.get("media") or {}).get("nodes") or []
            vimg = None
            if media and media[0].get("preview") and media[0]["preview"].get("image"):
                vimg = media[0]["preview"]["image"]["url"]
            item["image"] = image_url(vimg or product_image)
            item["description"] = desc
            items.append(item)
    if skipped:
        print(f"Skipped {len(skipped)} products without an online store page: "
              + ", ".join(skipped[:5]) + (" ..." if len(skipped) > 5 else ""))
    return items


def validate(items):
    if len(items) < MIN_ITEMS:
        raise RuntimeError(
            f"Only {len(items)} items found, expected at least {MIN_ITEMS}. "
            "Not overwriting the existing feed."
        )
    for item in items:
        for field in REQUIRED_FIELDS:
            if not (item.get(field) or "").strip():
                raise RuntimeError(f"Item {item.get('title')!r} has no {field}. Not publishing.")
    no_ean = sum(1 for i in items if not i["ean"])
    if no_ean / len(items) > MAX_EMPTY_EAN_SHARE:
        raise RuntimeError(f"{no_ean} of {len(items)} items have no EAN; something is wrong.")
    eans = [i["ean"] for i in items if i["ean"]]
    dupes = sorted({e for e in eans if eans.count(e) > 1})
    print(f"Items: {len(items)} | without EAN: {no_ean} | duplicate EANs: {len(dupes)}")
    if dupes:
        print("  duplicate EAN values: " + ", ".join(dupes[:10]))


def write_feed(items, filename, rule, generated):
    exclude = set((rule or {}).get("exclude", []))
    root = ET.Element("products", {"generated": generated, "count": str(len(items))})
    for item in items:
        node = ET.SubElement(root, "item")
        for tag, text in item.items():
            if tag in exclude:
                continue
            ET.SubElement(node, tag).text = text  # ElementTree escapes & < > itself
    ET.indent(root, space="  ")
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    path = os.path.join(OUTPUT_DIR, filename)
    tmp = path + ".tmp"
    ET.ElementTree(root).write(tmp, encoding="utf-8", xml_declaration=True)
    os.replace(tmp, path)  # swap in only when fully written
    print(f"Wrote {path} ({os.path.getsize(path) // 1024} KB, {len(items)} items)")


def main():
    print("Requesting access token ...")
    token = get_token()
    print(f"Reading collection '{COLLECTION_HANDLE}' ...")
    currency, products = fetch_products(token)
    items = build_items(currency, products)
    validate(items)
    generated = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    for filename, rule in OUTPUTS.items():
        write_feed(items, filename, rule, generated)
    print("Done.")


if __name__ == "__main__":
    try:
        main()
    except RuntimeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
