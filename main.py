
import csv, io, os, time, re, math
from typing import Any
from urllib.parse import urljoin
import httpx
from bs4 import BeautifulSoup
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse

APP_NAME = "PawVida Southern Automation"
VERSION = "12.0.0"

SHOPIFY_API_VERSION = os.getenv("SHOPIFY_API_VERSION", "2026-07")
SHOPIFY_SHOP = os.getenv("SHOPIFY_SHOP", "pawvida-6")
SHOPIFY_CLIENT_ID = os.getenv("SHOPIFY_CLIENT_ID", "")
SHOPIFY_CLIENT_SECRET = os.getenv("SHOPIFY_CLIENT_SECRET", "")

SOUTHERN_STOCK_URL = os.getenv("SOUTHERN_STOCK_URL", "https://www.agline.com/stock-level-csv/")
SOUTHERN_GTIN_URL = os.getenv("SOUTHERN_GTIN_URL", "https://www.southernpetsupplies.com.au/resources/product-barcode-gtin-list/")
SOUTHERN_LOGIN_URL = os.getenv("SOUTHERN_LOGIN_URL", "https://www.southernpetsupplies.com.au/my-account/")
SOUTHERN_PRICING_SAMPLE_URL = os.getenv(
    "SOUTHERN_PRICING_SAMPLE_URL",
    "https://www.southernpetsupplies.com.au/category/dog-products/dog-food/"
)
SOUTHERN_USERNAME = os.getenv("SOUTHERN_USERNAME", "")
SOUTHERN_PASSWORD = os.getenv("SOUTHERN_PASSWORD", "")

# Mixed-category test sources. Override any URL in Render if Southern changes its taxonomy.
MIXED_CATEGORY_URLS = {
    "food": os.getenv("SOUTHERN_FOOD_URL", "https://www.southernpetsupplies.com.au/category/dog-products/dog-food/"),
    "toys": os.getenv("SOUTHERN_TOYS_URL", "https://www.southernpetsupplies.com.au/category/dog-products/dog-toys/"),
    "bedding": os.getenv("SOUTHERN_BEDDING_URL", "https://www.southernpetsupplies.com.au/category/dog-products/dog-beds-bedding/"),
    "grooming": os.getenv("SOUTHERN_GROOMING_URL", "https://www.southernpetsupplies.com.au/category/dog-products/grooming-products/"),
    "health": os.getenv("SOUTHERN_HEALTH_URL", "https://www.southernpetsupplies.com.au/category/dog-health-products/?orderby=popularity"),
}

DRY_RUN = os.getenv("DRY_RUN", "true").lower() in {"1","true","yes","on"}

# Commercial policy defaults. These remain reviewable before any live writes.
DEFAULT_GST = 0.10
PAYMENT_FEE_RATE = 0.024
PAYMENT_FEE_FIXED = 0.30
RETURNS_ALLOWANCE_RATE = 0.005

# PawVida shipping policy:
# - customer pays shipping below FREE_SHIPPING_THRESHOLD
# - PawVida allows a capped freight subsidy in product economics
# - free shipping above threshold is funded from basket contribution, not fully embedded into every SKU
FREE_SHIPPING_THRESHOLD = float(os.getenv("FREE_SHIPPING_THRESHOLD", "99"))
MAX_FREIGHT_SUBSIDY = float(os.getenv("MAX_FREIGHT_SUBSIDY", "6"))
DEFAULT_CUSTOMER_SHIPPING = float(os.getenv("DEFAULT_CUSTOMER_SHIPPING", "9.95"))
MINIMUM_ORDER_VALUE = float(os.getenv("MINIMUM_ORDER_VALUE", "39"))
TARGET_BASKET_CONTRIBUTION = float(os.getenv("TARGET_BASKET_CONTRIBUTION", "12"))
LAUNCH_TARGET_APPROVED = int(os.getenv("LAUNCH_TARGET_APPROVED", "100"))
LAUNCH_MAX_CANDIDATES = int(os.getenv("LAUNCH_MAX_CANDIDATES", "300"))
MIN_RESILIENCE_FOR_PRIORITY = float(os.getenv("MIN_RESILIENCE_FOR_PRIORITY", "0.05"))
MAX_CATEGORY_PAGES = int(os.getenv("MAX_CATEGORY_PAGES", "10"))
LAUNCH_PER_CATEGORY = int(os.getenv("LAUNCH_PER_CATEGORY", "60"))
SOUTHERN_WALKING_URL = os.getenv(
    "SOUTHERN_WALKING_URL",
    "https://www.southernpetsupplies.com.au/category/dog-products/dog-collars-leads-harnesses/"
)




# Competitor benchmark source.
# Expected CSV columns:
# sku,gtin,product,retailer,price,in_stock,url,checked_at,match_type
MARKET_BENCHMARK_CSV_URL = os.getenv("MARKET_BENCHMARK_CSV_URL", "")
MARKET_CEILING_MULTIPLIER = float(os.getenv("MARKET_CEILING_MULTIPLIER", "1.03"))
MIN_BENCHMARK_OFFERS = int(os.getenv("MIN_BENCHMARK_OFFERS", "3"))

# Live Google Shopping benchmark via SerpApi.
SERPAPI_API_KEY = os.getenv("SERPAPI_API_KEY", "")
SERPAPI_LOCATION = os.getenv("SERPAPI_LOCATION", "Sydney, New South Wales, Australia")
SERPAPI_GL = os.getenv("SERPAPI_GL", "au")
SERPAPI_HL = os.getenv("SERPAPI_HL", "en")

# Exclude marketplace-style sources from automatic commercial approval by default.
MARKETPLACE_BLOCKLIST = {
    x.strip().lower() for x in os.getenv(
        "MARKETPLACE_BLOCKLIST",
        "ebay,temu,aliexpress,catch"
    ).split(",") if x.strip()
}

_serp_cache = {}

app = FastAPI(title=APP_NAME, version=VERSION)
_token_cache: dict[str, Any] = {"token": None, "expires_at": 0}

def shop_domain() -> str:
    s = SHOPIFY_SHOP.strip().replace("https://", "").replace("http://", "").rstrip("/")
    if not s.endswith(".myshopify.com"):
        s += ".myshopify.com"
    return s

async def get_shopify_token() -> str:
    if not SHOPIFY_CLIENT_ID or not SHOPIFY_CLIENT_SECRET:
        raise HTTPException(500, "SHOPIFY_CLIENT_ID / SHOPIFY_CLIENT_SECRET are not configured")
    if _token_cache["token"] and time.time() < _token_cache["expires_at"] - 300:
        return _token_cache["token"]
    url = f"https://{shop_domain()}/admin/oauth/access_token"
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(url, data={
            "grant_type": "client_credentials",
            "client_id": SHOPIFY_CLIENT_ID,
            "client_secret": SHOPIFY_CLIENT_SECRET,
        })
    if r.status_code >= 400:
        raise HTTPException(r.status_code, f"Shopify token request failed: {r.text[:500]}")
    data = r.json()
    token = data.get("access_token")
    if not token:
        raise HTTPException(502, "Shopify returned no access_token")
    _token_cache["token"] = token
    _token_cache["expires_at"] = time.time() + int(data.get("expires_in", 86399))
    return token

async def shopify_graphql(query: str, variables: dict | None = None):
    token = await get_shopify_token()
    url = f"https://{shop_domain()}/admin/api/{SHOPIFY_API_VERSION}/graphql.json"
    async with httpx.AsyncClient(timeout=45) as client:
        r = await client.post(url, headers={
            "X-Shopify-Access-Token": token,
            "Content-Type": "application/json",
        }, json={"query": query, "variables": variables or {}})
    if r.status_code >= 400:
        raise HTTPException(r.status_code, f"Shopify GraphQL request failed: {r.text[:800]}")
    data = r.json()
    if data.get("errors"):
        raise HTTPException(502, f"Shopify GraphQL errors: {data['errors']}")
    return data.get("data")

async def fetch_csv_or_discover(source_url: str):
    headers = {"User-Agent": "PawVidaAutomation/3.0"}
    async with httpx.AsyncClient(timeout=45, follow_redirects=True, headers=headers) as client:
        r = await client.get(source_url)
        r.raise_for_status()
        ctype = r.headers.get("content-type", "").lower()
        text = r.text
        if "csv" in ctype or source_url.lower().endswith(".csv"):
            return str(r.url), text
        first = text[:1000]
        if "<html" not in first.lower() and ("," in first or "\t" in first):
            return str(r.url), text
        soup = BeautifulSoup(text, "html.parser")
        candidates = []
        for a in soup.find_all("a", href=True):
            href = a["href"]
            label = " ".join(a.stripped_strings).lower()
            if ".csv" in href.lower() or "csv" in label or "spreadsheet" in label:
                candidates.append(urljoin(str(r.url), href))
        for href in candidates:
            rr = await client.get(href)
            if rr.status_code < 400:
                c2 = rr.headers.get("content-type", "").lower()
                if "csv" in c2 or href.lower().endswith(".csv") or "<html" not in rr.text[:500].lower():
                    return str(rr.url), rr.text
        raise HTTPException(502, f"Could not discover a CSV at {source_url}")

def parse_delimited(text: str):
    sample = text[:10000]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
    except Exception:
        dialect = csv.excel
    reader = csv.DictReader(io.StringIO(text), dialect=dialect)
    return reader.fieldnames or [], list(reader)

def money_values(text: str):
    vals = []
    for m in re.finditer(r"\$\s*([0-9][0-9,]*\.?[0-9]{0,2})", text):
        try:
            vals.append(float(m.group(1).replace(",", "")))
        except Exception:
            pass
    return vals

SKU_RE = re.compile(r"^(?=.*\d)[A-Z0-9][A-Z0-9.\-_/]{3,24}$", re.I)

def valid_sku(sku: str | None) -> bool:
    if not sku:
        return False
    sku = sku.strip()
    if sku.upper() in {"SKU", "INFO", "DESCRIPTION", "PRICE", "WEIGHT", "QUANTITY"}:
        return False
    return bool(SKU_RE.match(sku))

def clean_title(raw: str, sku: str) -> str:
    t = re.sub(r"\s+", " ", raw or "").strip()
    t = re.sub(r"\bSKU\b.*$", "", t, flags=re.I)
    t = re.sub(r"\bPrice\b.*$", "", t, flags=re.I)
    t = re.sub(r"\$\s*\d[\d,.]*", "", t)
    t = re.sub(r"\b\d+(?:\.\d+)?\s*kg\b", lambda m: m.group(0), t, flags=re.I)
    t = re.sub(r"\bAdd to cart\b.*$", "", t, flags=re.I)
    t = re.sub(r"\bProduct Info\b.*$", "", t, flags=re.I)
    t = t.strip(" -|:")
    if t.upper().startswith(sku.upper()):
        t = t[len(sku):].strip(" -|:")
    return t[:180]

def first_match(patterns, text):
    for p in patterns:
        m = re.search(p, text, re.I)
        if m:
            return m.group(1).strip()
    return None

async def southern_authenticated_client():
    if not SOUTHERN_USERNAME or not SOUTHERN_PASSWORD:
        raise HTTPException(500, "SOUTHERN_USERNAME / SOUTHERN_PASSWORD are not configured")
    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; PawVidaAutomation/3.0)",
        "Accept-Language": "en-AU,en;q=0.9",
    }
    client = httpx.AsyncClient(timeout=45, follow_redirects=True, headers=headers)
    login_page = await client.get(SOUTHERN_LOGIN_URL)
    if login_page.status_code >= 400:
        await client.aclose()
        raise HTTPException(login_page.status_code, "Could not open Southern login page")

    soup = BeautifulSoup(login_page.text, "html.parser")
    form = None
    for f in soup.find_all("form"):
        if f.find("input", {"name": "username"}) and f.find("input", {"name": "password"}):
            form = f
            break
    if not form:
        await client.aclose()
        raise HTTPException(502, "Could not identify Southern login form")

    payload = {}
    for inp in form.find_all("input"):
        name = inp.get("name")
        if name:
            payload[name] = inp.get("value", "")
    payload["username"] = SOUTHERN_USERNAME
    payload["password"] = SOUTHERN_PASSWORD
    payload["login"] = payload.get("login") or "Log in"
    payload["rememberme"] = "forever"

    action = urljoin(str(login_page.url), form.get("action") or SOUTHERN_LOGIN_URL)
    await client.post(action, data=payload)

    test = await client.get(SOUTHERN_PRICING_SAMPLE_URL)
    if "login to view prices" in test.text.lower():
        await client.aclose()
        raise HTTPException(401, "Southern login was not accepted")
    return client

def parse_southern_products(html: str, limit: int = 100):
    soup = BeautifulSoup(html, "html.parser")
    product_nodes = soup.select("li.product, tr, .product, .products > *")
    by_sku = {}

    for node in product_nodes:
        text = " ".join(node.stripped_strings)
        if "$" not in text or len(text) < 20:
            continue

        sku = first_match([
            r"\bSKU\b\s*[:\-]?\s*([A-Z0-9][A-Z0-9.\-_/]+)",
            r"\b([A-Z]{1,8}\d[A-Z0-9.\-_/]{3,})\b",
        ], text)
        if not valid_sku(sku):
            continue
        sku = sku.strip()

        prices = money_values(text)
        if not prices:
            continue
        # Southern sale display normally has old price first and current price second.
        active_cost = prices[-1]
        former_cost = prices[0] if len(prices) > 1 and prices[0] != prices[-1] else None

        weight = first_match([r"\bWeight\b\s*[:\-]?\s*([0-9.]+\s*kg)", r"\b([0-9.]+\s*kg)\b"], text)

        heading = node.find(["h2","h3","h4"])
        title = " ".join(heading.stripped_strings) if heading else ""
        if not title:
            # pick a useful link label
            labels = []
            for a in node.find_all("a", href=True):
                label = " ".join(a.stripped_strings)
                if len(label) > 8 and "product info" not in label.lower() and "add to cart" not in label.lower():
                    labels.append(label)
            title = labels[0] if labels else text[:220]
        title = clean_title(title, sku)

        img = node.find("img")
        image = (img.get("data-src") or img.get("src")) if img else None

        product_url = None
        for a in node.find_all("a", href=True):
            href = a["href"]
            if "/product/" in href:
                product_url = urljoin(SOUTHERN_PRICING_SAMPLE_URL, href)
                break

        candidate = {
            "sku": sku,
            "title": title,
            "active_cost_ex_gst": round(active_cost, 2),
            "former_cost_ex_gst": round(former_cost, 2) if former_cost else None,
            "page_weight": weight,
            "image": image,
            "product_url": product_url,
        }

        # Keep the most informative row per SKU.
        prev = by_sku.get(sku)
        score = (1 if title else 0) + (1 if image else 0) + (1 if product_url else 0) + (1 if weight else 0)
        prev_score = 0 if not prev else sum(1 for k in ["title","image","product_url","page_weight"] if prev.get(k))
        if prev is None or score > prev_score:
            by_sku[sku] = candidate

        if len(by_sku) >= limit:
            break

    return list(by_sku.values())

def stock_map(rows):
    out = {}
    for r in rows:
        sku = str(r.get("SKU") or r.get("Sku") or r.get("sku") or "").strip()
        if valid_sku(sku):
            raw = r.get("Available") or r.get("available") or r.get("Stock") or "0"
            try:
                qty = int(float(str(raw).strip() or "0"))
            except Exception:
                qty = 0
            out[sku] = qty
    return out

def gtin_map(rows):
    out = {}
    for r in rows:
        sku = str(r.get("SKU") or r.get("Sku") or "").strip()
        if not valid_sku(sku):
            continue
        out[sku] = {
            "product_name": (r.get("Product Name") or "").strip(),
            "gtin": (r.get("GTIN") or "").strip() or None,
            "weight": (r.get("Weight") or "").strip() or None,
        }
    return out

def parse_weight_kg(value):
    if value is None:
        return None
    s = str(value).lower().strip()
    m = re.search(r"([0-9.]+)", s)
    if not m:
        return None
    try:
        return float(m.group(1))
    except Exception:
        return None

def category_rule(product_name: str):
    n = product_name.lower()
    if any(x in n for x in ["coat", "jacket", "clothing", "jumper"]):
        return {"name":"clothing", "category_margin_floor":0.24, "returns_rate":0.03}
    if any(x in n for x in ["bed", "bedding", "mattress"]):
        return {"name":"bedding", "category_margin_floor":0.24, "returns_rate":0.0075}
    if any(x in n for x in ["toy", "kong", "ball", "chew", "benebone", "chuckit"]):
        return {"name":"toys", "category_margin_floor":0.20, "returns_rate":0.005}
    if any(x in n for x in ["lead", "leash", "harness", "collar", "walking"]):
        return {"name":"walking", "category_margin_floor":0.22, "returns_rate":0.0075}
    if any(x in n for x in ["food", "kibble", "adult dog", "puppy", "cat food", "diet"]):
        return {"name":"mainstream_food", "category_margin_floor":0.16, "returns_rate":0.005}
    if any(x in n for x in ["groom", "clipper", "comb", "brush", "shampoo", "conditioner", "blade care", "clipper oil"]):
        return {"name":"grooming", "category_margin_floor":0.18, "returns_rate":0.005}
    if any(x in n for x in ["supplement", "health", "immune", "vitamin", "joint", "skin", "care"]):
        return {"name":"health", "category_margin_floor":0.20, "returns_rate":0.0075}
    return {"name":"general", "category_margin_floor":0.20, "returns_rate":0.005}

def retail_band_rule(retail_price: float):
    if retail_price < 15:
        return {"min_contribution":2.00, "min_margin_rate":0.18}
    if retail_price < 30:
        return {"min_contribution":3.50, "min_margin_rate":0.20}
    if retail_price < 60:
        return {"min_contribution":6.00, "min_margin_rate":0.22}
    if retail_price < 100:
        return {"min_contribution":10.00, "min_margin_rate":0.24}
    return {"min_contribution":15.00, "min_margin_rate":0.18}

def estimate_freight_subsidy(weight_kg):
    # Avoid embedding full freight in unit price. Customer pays shipping below threshold.
    if weight_kg is None:
        return 3.0
    if weight_kg <= 1:
        return 2.0
    if weight_kg <= 5:
        return 3.5
    if weight_kg <= 10:
        return 4.5
    return MAX_FREIGHT_SUBSIDY

def round_95(x: float) -> float:
    # Round up to the next .95 retail point.
    whole = math.floor(x)
    candidate = whole + 0.95
    if candidate + 1e-9 < x:
        candidate = whole + 1.95
    return round(candidate, 2)

def commercial_calc(cost_ex_gst, weight_kg, product_name):
    rule = category_rule(product_name)
    cost_inc_gst = cost_ex_gst * (1 + DEFAULT_GST)
    freight_subsidy = estimate_freight_subsidy(weight_kg)

    test_price = max(cost_inc_gst + 0.50, 4.95)
    found = None
    upper = max(test_price * 3.0, test_price + 120)
    cents = int(test_price * 100)
    max_cents = int(upper * 100)

    while cents <= max_cents:
        p = cents / 100.0
        band = retail_band_rule(p)
        min_margin = max(band["min_margin_rate"], rule["category_margin_floor"])
        payment_fee = p * PAYMENT_FEE_RATE + PAYMENT_FEE_FIXED
        returns_allowance = p * rule["returns_rate"]
        contribution = p - cost_inc_gst - freight_subsidy - payment_fee - returns_allowance
        margin_rate = contribution / p if p else 0

        if contribution >= band["min_contribution"] and margin_rate >= min_margin:
            found = p
            break
        cents += 5

    if found is None:
        found = cost_inc_gst * 1.4

    proposed = round_95(found)
    band = retail_band_rule(proposed)
    min_margin = max(band["min_margin_rate"], rule["category_margin_floor"])
    payment_fee = proposed * PAYMENT_FEE_RATE + PAYMENT_FEE_FIXED
    returns_allowance = proposed * rule["returns_rate"]
    contribution = proposed - cost_inc_gst - freight_subsidy - payment_fee - returns_allowance
    margin_rate = contribution / proposed if proposed else 0
    passes = contribution >= band["min_contribution"] and margin_rate >= min_margin and contribution > 0

    return {
        "category_rule": rule["name"],
        "category_margin_floor": rule["category_margin_floor"],
        "price_band_min_margin": band["min_margin_rate"],
        "min_margin_rate": min_margin,
        "min_contribution": band["min_contribution"],
        "southern_cost_ex_gst": round(cost_ex_gst,2),
        "cost_inc_gst": round(cost_inc_gst,2),
        "freight_subsidy": round(freight_subsidy,2),
        "payment_fee_allowance": round(payment_fee,2),
        "returns_allowance": round(returns_allowance,2),
        "proposed_price": proposed,
        "contribution": round(contribution,2),
        "margin_rate": round(margin_rate,4),
        "passes_margin": passes,
        "basket_model": True,
    }

async def load_market_benchmarks():
    """
    Loads an externally maintained competitor benchmark CSV.
    This keeps competitor acquisition separate from pricing decisions and makes every
    benchmark auditable by retailer, URL, timestamp and match type.
    """
    if not MARKET_BENCHMARK_CSV_URL:
        return []
    _, text = await fetch_csv_or_discover(MARKET_BENCHMARK_CSV_URL)
    _, rows = parse_delimited(text)
    cleaned = []
    for r in rows:
        sku = str(r.get("sku") or r.get("SKU") or "").strip()
        gtin = str(r.get("gtin") or r.get("GTIN") or "").strip()
        retailer = str(r.get("retailer") or r.get("Retailer") or "").strip()
        product = str(r.get("product") or r.get("Product") or "").strip()
        url = str(r.get("url") or r.get("URL") or "").strip()
        checked_at = str(r.get("checked_at") or r.get("Checked At") or "").strip()
        match_type = str(r.get("match_type") or r.get("Match Type") or "").strip() or None
        raw_price = str(r.get("price") or r.get("Price") or "").replace("$","").replace(",","").strip()
        raw_stock = str(r.get("in_stock") or r.get("In Stock") or "true").strip().lower()
        try:
            price = float(raw_price)
        except Exception:
            continue
        in_stock = raw_stock in {"1","true","yes","y","in stock","available"}
        if price <= 0 or not in_stock:
            continue
        cleaned.append({
            "sku": sku or None,
            "gtin": gtin or None,
            "product": product or None,
            "retailer": retailer or None,
            "price": round(price,2),
            "url": url or None,
            "checked_at": checked_at or None,
            "match_type": match_type,
            "in_stock": True,
        })
    return cleaned

def market_for_product(product_row, offers):
    """
    Match benchmark rows by exact GTIN first, then exact SKU.
    Fuzzy title matching is deliberately excluded from automatic approval.
    """
    gtin = str(product_row.get("gtin") or "").strip()
    sku = str(product_row.get("sku") or "").strip()
    matches = []
    for o in offers:
        if gtin and o.get("gtin") and o["gtin"] == gtin:
            matches.append({**o, "effective_match":"GTIN"})
        elif sku and o.get("sku") and o["sku"].upper() == sku.upper():
            matches.append({**o, "effective_match":"SKU"})
    # dedupe same retailer + price + URL
    dedup = {}
    for o in matches:
        key=(o.get("retailer"),o.get("price"),o.get("url"))
        dedup[key]=o
    matches=list(dedup.values())
    matches.sort(key=lambda x:x["price"])
    credible = matches[:max(MIN_BENCHMARK_OFFERS,3)]
    if len(credible) < MIN_BENCHMARK_OFFERS:
        return {
            "verified": False,
            "offer_count": len(matches),
            "offers": matches,
            "benchmark_price": None,
            "market_ceiling": None,
        }
    # median of the three lowest credible offers
    sample = credible[:3]
    prices = sorted([x["price"] for x in sample])
    median = prices[len(prices)//2]
    ceiling = round(median * MARKET_CEILING_MULTIPLIER, 2)
    return {
        "verified": True,
        "offer_count": len(matches),
        "offers": matches,
        "benchmark_price": round(median,2),
        "market_ceiling": ceiling,
    }

async def final_commercial_rows(limit:int=50):
    rows = await commercial_rows(limit)
    offers = await load_market_benchmarks()
    final=[]
    for r in rows:
        market = market_for_product(r, offers)
        required = r["proposed_price"]
        final_status = None
        hold = list(r.get("hold_reason") or [])
        if r["stock"] <= 0:
            final_status = "TEMP_OUT_OF_STOCK"
        elif not r["passes_margin"]:
            final_status = "COMMERCIAL_HOLD"
            if "margin" not in hold:
                hold.append("margin")
        elif not market["verified"]:
            final_status = "REVIEW_MARKET"
            hold.append("market_unverified")
        elif required > market["market_ceiling"]:
            final_status = "COMMERCIAL_HOLD"
            hold.append("market_price")
        else:
            final_status = "APPROVED_DRAFT"
        final.append({
            **r,
            "market_verified": market["verified"],
            "market_offer_count": market["offer_count"],
            "market_benchmark": market["benchmark_price"],
            "market_ceiling": market["market_ceiling"],
            "market_offers": market["offers"],
            "final_status": final_status,
            "hold_reason": hold,
        })
    return final



def normalize_product_text(value: str | None) -> str:
    s = (value or "").lower()
    s = s.replace("&", " and ")
    s = re.sub(r"[^a-z0-9.]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s

def pack_tokens(value: str | None):
    s = normalize_product_text(value)
    # capture sizes such as 13kg, 2.5kg, 500g, 250ml
    return set(re.findall(r"\b\d+(?:\.\d+)?\s*(?:kg|g|ml|l)\b", s))

def token_similarity(a: str, b: str) -> float:
    aa = {x for x in normalize_product_text(a).split() if len(x) > 1}
    bb = {x for x in normalize_product_text(b).split() if len(x) > 1}
    if not aa or not bb:
        return 0.0
    return len(aa & bb) / max(1, len(aa | bb))

def source_blocked(source: str | None) -> bool:
    s = normalize_product_text(source)
    return any(block in s for block in MARKETPLACE_BLOCKLIST)

async def serpapi_shopping_search(query: str):
    if not SERPAPI_API_KEY:
        raise HTTPException(500, "SERPAPI_API_KEY is not configured")
    cache_key = (query, SERPAPI_LOCATION, SERPAPI_GL)
    cached = _serp_cache.get(cache_key)
    if cached and time.time() - cached["ts"] < 3600:
        return cached["data"]

    params = {
        "engine": "google_shopping",
        "q": query,
        "gl": SERPAPI_GL,
        "hl": SERPAPI_HL,
        "location": SERPAPI_LOCATION,
        "api_key": SERPAPI_API_KEY,
        "output": "json",
    }
    async with httpx.AsyncClient(timeout=60) as client:
        r = await client.get("https://serpapi.com/search", params=params)
    if r.status_code >= 400:
        raise HTTPException(r.status_code, f"SerpApi request failed: {r.text[:500]}")
    data = r.json()
    if data.get("error"):
        err = str(data.get("error") or "")
        # Google Shopping sometimes returns a valid "no results" response for niche SKUs.
        # Treat that as an empty search result, not a fatal batch error.
        if "hasn't returned any results" in err.lower() or "no results" in err.lower():
            data = {"shopping_results": [], "search_metadata": {"status": "No results"}}
        else:
            raise HTTPException(502, f"SerpApi error: {err}")
    _serp_cache[cache_key] = {"ts": time.time(), "data": data}
    return data


def numeric_pack_signature(value: str | None):
    s = normalize_product_text(value)
    sig = []
    for num, unit in re.findall(r"\b(\d+(?:\.\d+)?)\s*(kg|g|ml|l)\b", s):
        try:
            n = float(num)
        except Exception:
            continue
        # normalize kg/g and l/ml
        if unit == "kg":
            sig.append(("mass_g", round(n*1000,1)))
        elif unit == "g":
            sig.append(("mass_g", round(n,1)))
        elif unit == "l":
            sig.append(("volume_ml", round(n*1000,1)))
        elif unit == "ml":
            sig.append(("volume_ml", round(n,1)))
    return sig

def pack_match(product_name: str, result_title: str) -> bool:
    wanted = numeric_pack_signature(product_name)
    found = numeric_pack_signature(result_title)
    if not wanted:
        return True
    if not found:
        # Missing pack size is not enough for auto-approval
        return False
    for wu, wv in wanted:
        for fu, fv in found:
            if wu == fu and abs(wv-fv) <= max(1.0, wv*0.02):
                return True
    return False

def brand_tokens(value: str | None):
    s = normalize_product_text(value)
    # first meaningful token is usually brand on these catalogue titles
    toks = [x for x in s.split() if len(x) > 2 and not x.isdigit()]
    return toks[:2]

def core_title_tokens(value: str | None):
    stop = {
        "adult","dog","dogs","cat","cats","food","dry","with","and","the","for",
        "kg","g","ml","l","all","breed","breeds","original","current","price","was"
    }
    toks = []
    for x in normalize_product_text(value).split():
        if x in stop or re.fullmatch(r"\d+(?:\.\d+)?", x):
            continue
        if len(x) > 2:
            toks.append(x)
    return set(toks)

def offer_plausible(product_row, item) -> tuple[bool, str]:
    title = item.get("title") or ""
    source = item.get("source") or ""
    if source_blocked(source):
        return False, "blocked_source"

    if not pack_match(product_row.get("product") or "", title):
        return False, "pack_mismatch"

    p_tokens = core_title_tokens(product_row.get("product") or "")
    r_tokens = core_title_tokens(title)
    if not p_tokens or not r_tokens:
        return False, "weak_title"

    overlap = len(p_tokens & r_tokens)
    # Require at least 2 meaningful shared tokens, or 1 if very short title.
    if overlap < (1 if len(p_tokens) <= 2 else 2):
        return False, "title_mismatch"

    # Brand consistency: first meaningful token should appear in result.
    brands = brand_tokens(product_row.get("product") or "")
    if brands and brands[0] not in normalize_product_text(title).split():
        return False, "brand_mismatch"

    return True, "accepted"

def filter_serp_offers(product_row, shopping_results):
    product_name = product_row.get("product") or ""
    gtin = str(product_row.get("gtin") or "").strip()
    offers = []

    for item in shopping_results or []:
        ok, reason = offer_plausible(product_row, item)
        if not ok:
            continue

        raw_price = item.get("extracted_price")
        if raw_price is None:
            raw = str(item.get("price") or "").replace("$","").replace(",","").strip()
            try:
                raw_price = float(raw)
            except Exception:
                continue
        try:
            price = float(raw_price)
        except Exception:
            continue
        if price <= 0:
            continue

        title = item.get("title") or ""
        similarity = token_similarity(product_name, title)

        # Economic sanity guard: an "exact" retail offer far below Southern wholesale
        # is likely a wrong size/accessory/sample unless proven by GTIN.
        southern_cost_inc = float(product_row.get("southern_cost_ex_gst") or 0) * 1.10
        if southern_cost_inc > 0 and price < southern_cost_inc * 0.55 and not gtin:
            continue

        offers.append({
            "retailer": item.get("source"),
            "price": round(price,2),
            "title": title,
            "url": item.get("product_link"),
            "position": item.get("position"),
            "match_type": "GTIN_QUERY" if gtin else "TITLE_QUERY",
            "similarity": round(similarity,3),
            "delivery": item.get("delivery"),
        })

    dedup={}
    for o in offers:
        dedup[(o["retailer"],o["price"],o["title"])]=o
    offers=list(dedup.values())
    offers.sort(key=lambda x:x["price"])
    return offers


def strip_operational_suffix(value: str | None) -> str:
    """
    Remove Southern page artefacts such as:
    - trailing duplicate shipping weight + stock: '13.72 kg 5'
    - trailing weight + stock: '0.2 kg 22'
    - trailing stock counts
    while preserving legitimate pack-size text earlier in the title.
    """
    s = re.sub(r"\s+", " ", value or "").strip()

    # Remove common Southern UI phrases.
    s = re.sub(r"\b(?:Add to cart|Product Info)\b.*$", "", s, flags=re.I).strip()

    # Remove repeated trailing operational values.
    patterns = [
        r"\s+\d+(?:\.\d+)?\s*kg\s+\d+\s*$",
        r"\s+\d+(?:\.\d+)?\s*g\s+\d+\s*$",
        r"\s+\d+(?:\.\d+)?\s*ml\s+\d+\s*$",
        r"\s+\d+(?:\.\d+)?\s*l\s+\d+\s*$",
        r"\s+\d+(?:\.\d+)?\s+kg\s+\d+\s*$",
        r"\s+\d+\s*$",
    ]
    for p in patterns:
        s = re.sub(p, "", s, flags=re.I).strip()

    return s.strip(" -|:")

def clean_search_title(product: str) -> str:
    s = strip_operational_suffix(product)
    # Remove duplicated adjacent pack data such as "13kg 13.72 kg".
    s = re.sub(
        r"(\b\d+(?:\.\d+)?\s*(?:kg|g|ml|l)\b)\s+\d+(?:\.\d+)?\s*(?:kg|g|ml|l)\b",
        r"\1",
        s,
        flags=re.I
    )
    return re.sub(r"\s+", " ", s).strip()

def meaningful_query_tokens(value: str | None):
    stop = {
        "adult","dog","dogs","cat","cats","all","with","and","the","for",
        "original","current","price","was","small","medium","large"
    }
    toks = []
    for t in normalize_product_text(value).split():
        if t in stop:
            continue
        if re.fullmatch(r"\d+(?:\.\d+)?", t):
            continue
        toks.append(t)
    return toks

def build_market_queries(product_row):
    """
    Search strategy:
    1. exact GTIN
    2. clean full product title
    3. brand + distinctive product/model terms + pack size
    4. simplified product title + pack size
    """
    product = clean_search_title(product_row.get("product") or product_row.get("sku") or "")
    gtin = str(product_row.get("gtin") or "").strip()
    queries = []

    if gtin:
        queries.append(gtin)

    if product:
        queries.append(product)

    packs = re.findall(r"\b\d+(?:\.\d+)?\s*(?:kg|g|ml|l)\b", product, flags=re.I)
    pack = packs[-1] if packs else ""

    tokens = meaningful_query_tokens(product)
    if tokens:
        # retain first token (usually brand) + up to 6 distinctive terms
        compact = " ".join(tokens[:7])
        if pack and pack.lower() not in compact.lower():
            compact += " " + pack
        compact = compact.strip()
        if compact and compact not in queries:
            queries.append(compact)

    # Broader fallback: first 5 title words + size.
    words = product.split()
    if words:
        broad = " ".join(words[:5])
        if pack and pack.lower() not in broad.lower():
            broad += " " + pack
        broad = broad.strip()
        if broad and broad not in queries:
            queries.append(broad)

    # Return max 4 distinct queries.
    out = []
    for q in queries:
        q = re.sub(r"\s+", " ", q).strip()
        if q and q not in out:
            out.append(q)
    return out[:4]


async def live_market_for_product(product_row):
    queries = build_market_queries(product_row)
    all_offers = []
    used_queries = []

    for q in queries:
        data = await serpapi_shopping_search(q)
        accepted = filter_serp_offers(product_row, data.get("shopping_results") or [])
        all_offers.extend(accepted)
        used_queries.append(q)

        # stop as soon as enough distinct retailers are available
        if len({(o.get("retailer") or "").lower() for o in all_offers}) >= MIN_BENCHMARK_OFFERS:
            break

    # De-duplicate across fallback queries.
    dedup = {}
    for o in all_offers:
        key = ((o.get("retailer") or "").lower(), o["price"], o["title"])
        dedup[key] = o
    offers = list(dedup.values())
    offers.sort(key=lambda x: x["price"])

    # Keep one accepted offer per retailer for benchmark purposes.
    distinct = []
    seen = set()
    for o in offers:
        retailer = (o.get("retailer") or "").lower()
        if not retailer or retailer in seen:
            continue
        seen.add(retailer)
        distinct.append(o)

    if len(distinct) < MIN_BENCHMARK_OFFERS:
        return {
            "verified": False,
            "queries": used_queries,
            "offer_count": len(distinct),
            "offers": distinct[:10],
            "benchmark_price": None,
            "market_ceiling": None,
        }

    lowest = distinct[:3]
    prices = sorted(o["price"] for o in lowest)
    benchmark = prices[1]
    ceiling = round(benchmark * MARKET_CEILING_MULTIPLIER, 2)

    return {
        "verified": True,
        "queries": used_queries,
        "offer_count": len(distinct),
        "offers": distinct[:10],
        "benchmark_price": round(benchmark, 2),
        "market_ceiling": ceiling,
    }

async def serp_final_rows(limit:int=5):
    rows = await commercial_rows(limit)
    final=[]
    for r in rows:
        market = await live_market_for_product(r)
        required=r["proposed_price"]
        hold=list(r.get("hold_reason") or [])
        if r["stock"] <= 0:
            status="TEMP_OUT_OF_STOCK"
        elif not r["passes_margin"]:
            status="COMMERCIAL_HOLD"
            hold.append("margin")
        elif not market["verified"]:
            status="REVIEW_MARKET"
            hold.append("market_unverified")
        elif required > market["market_ceiling"]:
            status="COMMERCIAL_HOLD"
            hold.append("market_price")
        else:
            status="APPROVED_DRAFT"
        final.append({
            **r,
            "market_queries": market.get("queries") or [market.get("query")],
            "market_verified": market["verified"],
            "market_offer_count": market["offer_count"],
            "market_benchmark": market["benchmark_price"],
            "market_ceiling": market["market_ceiling"],
            "market_offers": market["offers"],
            "final_status": status,
            "hold_reason": list(dict.fromkeys(hold)),
        })
    return final



async def mixed_price_rows(per_category:int=4):
    """
    Read a small balanced sample from five Southern categories.
    This keeps the commercial test representative without consuming excessive SerpApi quota.
    """
    per_category = min(max(per_category,1),6)
    client = await southern_authenticated_client()
    out = []
    try:
        for category, url in MIXED_CATEGORY_URLS.items():
            r = await client.get(url)
            if r.status_code >= 400:
                continue
            rows = parse_southern_products(r.text, per_category)
            for row in rows:
                out.append({**row, "source_category": category, "source_url": str(r.url)})
    finally:
        await client.aclose()
    return out

async def commercial_rows_from_price_rows(price_rows):
    _, stock_text = await fetch_csv_or_discover(SOUTHERN_STOCK_URL)
    _, stock_rows = parse_delimited(stock_text)
    smap = stock_map(stock_rows)

    _, gtin_text = await fetch_csv_or_discover(SOUTHERN_GTIN_URL)
    _, gtin_rows = parse_delimited(gtin_text)
    gmap = gtin_map(gtin_rows)

    out = []
    for p in price_rows:
        sku = p["sku"]
        g = gmap.get(sku, {})
        product = g.get("product_name") or p.get("title") or sku
        product = strip_operational_suffix(clean_title(product, sku))
        weight = g.get("weight") or p.get("page_weight")
        weight_kg = parse_weight_kg(weight)
        stock = smap.get(sku, 0)
        calc = commercial_calc(float(p["active_cost_ex_gst"]), weight_kg, product)

        hold=[]
        if stock <= 0:
            hold.append("supplier_stock")
        if not calc["passes_margin"]:
            hold.append("margin")

        out.append({
            "sku": sku,
            "product": product,
            "source_category": p.get("source_category"),
            "southern_cost_ex_gst": p["active_cost_ex_gst"],
            "former_cost_ex_gst": p.get("former_cost_ex_gst"),
            "stock": stock,
            "gtin": g.get("gtin"),
            "weight": weight,
            **calc,
            "status": "LIVE" if not hold else "OUT_OF_STOCK",
            "hold_reason": hold,
        })
    return out

async def mixed_serp_final_rows(per_category:int=4):
    price_rows = await mixed_price_rows(per_category)
    commercial = await commercial_rows_from_price_rows(price_rows)
    final = []

    # One SerpApi benchmark per product. Caller controls sample size.
    for r in commercial:
        market = await live_market_for_product(r)
        required = r["proposed_price"]
        hold = list(r.get("hold_reason") or [])

        if r["stock"] <= 0:
            final_status = "TEMP_OUT_OF_STOCK"
        elif not r["passes_margin"]:
            final_status = "COMMERCIAL_HOLD"
            hold.append("margin")
        elif not market["verified"]:
            final_status = "REVIEW_MARKET"
            hold.append("market_unverified")
        elif required > market["market_ceiling"]:
            final_status = "COMMERCIAL_HOLD"
            hold.append("market_price")
        else:
            final_status = "APPROVED_DRAFT"

        final.append({
            **r,
            "market_queries": market.get("queries") or [market.get("query")],
            "market_verified": market["verified"],
            "market_offer_count": market["offer_count"],
            "market_benchmark": market["benchmark_price"],
            "market_ceiling": market["market_ceiling"],
            "market_offers": market["offers"],
            "final_status": final_status,
            "hold_reason": list(dict.fromkeys(hold)),
        })
    return final



def candidate_score(row):
    """
    Cheap Southern-only pre-screen. Higher score = better candidate for paid market lookup.
    Favors margin, contribution, stock depth, lighter shipping and commercially attractive categories.
    """
    category_bonus = {
        "bedding": 22,
        "grooming": 20,
        "walking": 18,
        "toys": 16,
        "general": 10,
        "clothing": 6,
        "mainstream_food": -12,
    }.get(row.get("category_rule"), 8)

    margin = float(row.get("margin_rate") or 0)
    contribution = float(row.get("contribution") or 0)
    stock = int(row.get("stock") or 0)
    weight_kg = parse_weight_kg(row.get("weight"))

    margin_score = min(50, margin * 100)
    contribution_score = min(25, contribution / 2)
    stock_score = min(20, stock * 1.5)

    if weight_kg is None:
        freight_score = 2
    elif weight_kg <= 0.5:
        freight_score = 18
    elif weight_kg <= 2:
        freight_score = 14
    elif weight_kg <= 5:
        freight_score = 9
    elif weight_kg <= 10:
        freight_score = 3
    else:
        freight_score = -10

    low_stock_penalty = -15 if stock <= 2 else 0
    no_margin_penalty = -50 if not row.get("passes_margin") else 0

    return round(
        category_bonus + margin_score + contribution_score +
        stock_score + freight_score + low_stock_penalty + no_margin_penalty,
        1
    )

async def candidate_pool(per_category:int=20, limit:int=50):
    """
    Build a Southern-only shortlist without SerpApi calls.
    """
    per_category = min(max(per_category, 1), 30)
    price_rows = await mixed_price_rows(per_category)
    commercial = await commercial_rows_from_price_rows(price_rows)

    # Remove duplicates across category pages.
    dedup = {}
    for r in commercial:
        sku = r["sku"]
        score = candidate_score(r)
        candidate = {**r, "candidate_score": score}
        old = dedup.get(sku)
        if old is None or score > old["candidate_score"]:
            dedup[sku] = candidate

    rows = list(dedup.values())
    rows.sort(key=lambda x: (-x["candidate_score"], -x["stock"], x["proposed_price"]))
    return rows[:min(max(limit,1),100)]

async def top_candidate_market_rows(limit:int=25, per_category:int=20):
    """
    Spend SerpApi calls only on the strongest Southern-only candidates.
    """
    limit = min(max(limit,1),25)
    candidates = await candidate_pool(per_category=per_category, limit=max(limit,50))
    chosen = candidates[:limit]
    final = []

    for r in chosen:
        market = await live_market_for_product(r)
        hold = list(r.get("hold_reason") or [])

        if r["stock"] <= 0:
            status = "TEMP_OUT_OF_STOCK"
        elif not r["passes_margin"]:
            status = "COMMERCIAL_HOLD"
            hold.append("margin")
        elif not market["verified"]:
            status = "REVIEW_MARKET"
            hold.append("market_unverified")
        elif r["proposed_price"] > market["market_ceiling"]:
            status = "COMMERCIAL_HOLD"
            hold.append("market_price")
        else:
            status = "APPROVED_DRAFT"

        final.append({
            **r,
            "market_queries": market.get("queries") or [market.get("query")],
            "market_verified": market["verified"],
            "market_offer_count": market["offer_count"],
            "market_benchmark": market["benchmark_price"],
            "market_ceiling": market["market_ceiling"],
            "market_offers": market["offers"],
            "final_status": status,
            "hold_reason": list(dict.fromkeys(hold)),
        })
    return final




def paged_category_url(base_url: str, page: int) -> str:
    if page <= 1:
        return base_url
    if "?" in base_url:
        path, query = base_url.split("?", 1)
        return path.rstrip("/") + f"/page/{page}/?" + query
    return base_url.rstrip("/") + f"/page/{page}/"

async def paginated_launch_price_rows(per_category:int=60):
    """
    Crawl multiple pages per Southern category, collecting unique SKUs before
    any SerpApi calls are made.
    """
    per_category = min(max(per_category,1),100)
    categories = dict(MIXED_CATEGORY_URLS)
    categories["walking"] = SOUTHERN_WALKING_URL

    client = await southern_authenticated_client()
    collected = []
    try:
        for category, base_url in categories.items():
            seen = set()
            for page in range(1, MAX_CATEGORY_PAGES + 1):
                url = paged_category_url(base_url, page)
                r = await client.get(url)
                if r.status_code >= 400:
                    break
                rows = parse_southern_products(r.text, 100)
                new_count = 0
                for row in rows:
                    sku = row.get("sku")
                    if not sku or sku in seen:
                        continue
                    seen.add(sku)
                    collected.append({
                        **row,
                        "source_category": category,
                        "source_url": str(r.url),
                        "source_page": page,
                    })
                    new_count += 1
                    if len(seen) >= per_category:
                        break
                if len(seen) >= per_category:
                    break
                # End pagination when a page yields no new products.
                if new_count == 0:
                    break
    finally:
        await client.aclose()
    return collected

async def paginated_candidate_pool(per_category:int=60, limit:int=300):
    price_rows = await paginated_launch_price_rows(per_category=per_category)
    commercial = await commercial_rows_from_price_rows(price_rows)

    dedup = {}
    for r in commercial:
        sku = r["sku"]
        score = candidate_score(r)
        candidate = {**r, "candidate_score": score}
        old = dedup.get(sku)
        if old is None or score > old["candidate_score"]:
            dedup[sku] = candidate

    rows = list(dedup.values())
    rows.sort(key=lambda x: (-x["candidate_score"], -x["stock"], x["proposed_price"]))
    return rows[:min(max(limit,1),LAUNCH_MAX_CANDIDATES)]

def resilience_score(required_price, market_ceiling):
    if not market_ceiling or market_ceiling <= 0:
        return None
    return round((market_ceiling - required_price) / market_ceiling, 4)

def launch_priority(row):
    resilience = row.get("resilience")
    base = float(row.get("candidate_score") or 0)
    if resilience is None:
        return round(base - 20, 1)
    return round(base + max(-40, min(60, resilience * 200)), 1)

async def launch_candidate_pool(per_category:int=60, max_candidates:int=300):
    return await paginated_candidate_pool(
        per_category=min(max(per_category,1),100),
        limit=min(max(max_candidates,1),LAUNCH_MAX_CANDIDATES)
    )

async def benchmark_candidate_batch(candidates, max_products:int=50):
    final = []
    for r in candidates[:max_products]:
        try:
            market = await live_market_for_product(r)
        except Exception as exc:
            market = {
                "verified": False,
                "offer_count": 0,
                "offers": [],
                "benchmark_price": None,
                "market_ceiling": None,
                "queries": [],
                "lookup_error": str(exc)[:300],
            }
        hold = list(r.get("hold_reason") or [])
        if r["stock"] <= 0:
            status = "TEMP_OUT_OF_STOCK"
        elif not r["passes_margin"]:
            status = "COMMERCIAL_HOLD"
            hold.append("margin")
        elif not market["verified"]:
            status = "REVIEW_MARKET"
            hold.append("market_unverified")
        elif r["proposed_price"] > market["market_ceiling"]:
            status = "COMMERCIAL_HOLD"
            hold.append("market_price")
        else:
            status = "APPROVED_DRAFT"
        resilience = resilience_score(r["proposed_price"], market["market_ceiling"])
        row = {
            **r,
            "market_verified": market["verified"],
            "market_offer_count": market["offer_count"],
            "market_benchmark": market["benchmark_price"],
            "market_ceiling": market["market_ceiling"],
            "market_offers": market["offers"],
            "market_lookup_error": market.get("lookup_error"),
            "resilience": resilience,
            "strong_launch_candidate": bool(status == "APPROVED_DRAFT" and resilience is not None and resilience >= MIN_RESILIENCE_FOR_PRIORITY),
            "final_status": status,
            "hold_reason": list(dict.fromkeys(hold)),
        }
        row["launch_priority"] = launch_priority(row)
        final.append(row)
    final.sort(key=lambda x: (
        x["final_status"] != "APPROVED_DRAFT",
        -(x["launch_priority"] or 0),
        -(x.get("resilience") or -999),
        -x.get("stock",0)
    ))
    return final

async def build_launch_shortlist(per_category:int=60, benchmark_limit:int=50, approved_target:int=100):
    candidates = await launch_candidate_pool(
        per_category=per_category,
        max_candidates=LAUNCH_MAX_CANDIDATES
    )
    benchmarked = await benchmark_candidate_batch(
        candidates,
        max_products=min(max(benchmark_limit,1),50)
    )
    approved = [r for r in benchmarked if r["final_status"] == "APPROVED_DRAFT"]
    approved.sort(key=lambda x: (-(x.get("launch_priority") or 0), -(x.get("resilience") or 0)))
    return {
        "candidates_scanned": len(candidates),
        "benchmarked": len(benchmarked),
        "approved_count": len(approved),
        "approved_target": approved_target,
        "approved": approved[:approved_target],
        "all_benchmarked": benchmarked,
    }


@app.get("/")
async def root():
    return {
        "service": APP_NAME, "status":"running", "version":VERSION, "dry_run":DRY_RUN,
        "next":["/health","/shopify/test","/southern/stock-preview","/southern/gtin-preview",
                "/southern/login-test","/southern/pricing-preview","/southern/commercial-preview",
                "/southern/commercial-table","/market/benchmark-preview","/southern/final-commercial-table","/market/serpapi-test","/southern/serp-commercial-table","/southern/mixed-market-table","/southern/candidate-table","/southern/top-candidates-market-table"]
    }

@app.get("/health")
async def health():
    return {"ok":True,"dry_run":DRY_RUN,"version":VERSION}

@app.get("/shopify/test")
async def shopify_test():
    q = """
    query { shop { name myshopifyDomain } locations(first:10){nodes{id name isActive}}
            products(first:3){nodes{id title handle status}} }
    """
    return {"ok":True,"shopify":await shopify_graphql(q)}

@app.get("/southern/stock-preview")
async def stock_preview(limit:int=10):
    url,text = await fetch_csv_or_discover(SOUTHERN_STOCK_URL)
    fields,rows = parse_delimited(text)
    return {"ok":True,"resolved_url":url,"fields":fields,"rows":rows[:min(max(limit,1),50)],
            "note":"Preview only; no Shopify changes are made."}

@app.get("/southern/gtin-preview")
async def gtin_preview(limit:int=10):
    url,text = await fetch_csv_or_discover(SOUTHERN_GTIN_URL)
    fields,rows = parse_delimited(text)
    return {"ok":True,"resolved_url":url,"fields":fields,"rows":rows[:min(max(limit,1),50)],
            "note":"Preview only; no Shopify changes are made."}

@app.get("/southern/login-test")
async def southern_login_test():
    client = await southern_authenticated_client()
    try:
        r = await client.get(SOUTHERN_PRICING_SAMPLE_URL)
        return {"ok":True,"logged_in":True,"url":str(r.url),"credentials_present":True,
                "note":"No credentials are returned. Read-only test."}
    finally:
        await client.aclose()

@app.get("/southern/pricing-preview")
async def pricing_preview(limit:int=20):
    client = await southern_authenticated_client()
    try:
        r = await client.get(SOUTHERN_PRICING_SAMPLE_URL)
        rows = parse_southern_products(r.text, min(max(limit,1),100))
        return {"ok":True,"dry_run":DRY_RUN,"count":len(rows),"rows":rows,
                "note":"Cleaned read-only pricing preview."}
    finally:
        await client.aclose()

async def commercial_rows(limit:int=50):
    # Price page
    client = await southern_authenticated_client()
    try:
        pr = await client.get(SOUTHERN_PRICING_SAMPLE_URL)
        price_rows = parse_southern_products(pr.text, min(max(limit,1),100))
    finally:
        await client.aclose()

    # Stock
    _, stock_text = await fetch_csv_or_discover(SOUTHERN_STOCK_URL)
    _, stock_rows = parse_delimited(stock_text)
    smap = stock_map(stock_rows)

    # GTIN/weight
    _, gtin_text = await fetch_csv_or_discover(SOUTHERN_GTIN_URL)
    _, gtin_rows = parse_delimited(gtin_text)
    gmap = gtin_map(gtin_rows)

    out=[]
    for p in price_rows:
        sku=p["sku"]
        g=gmap.get(sku,{})
        product = g.get("product_name") or p.get("title") or sku
        product = strip_operational_suffix(clean_title(product, sku))
        weight = g.get("weight") or p.get("page_weight")
        weight_kg = parse_weight_kg(weight)
        stock = smap.get(sku,0)
        calc = commercial_calc(float(p["active_cost_ex_gst"]), weight_kg, product)
        hold=[]
        if stock <= 0:
            hold.append("supplier_stock")
        if not calc["passes_margin"]:
            hold.append("margin")
        status = "LIVE" if not hold else "OUT_OF_STOCK"
        out.append({
            "sku":sku,
            "product":product,
            "southern_cost_ex_gst":p["active_cost_ex_gst"],
            "former_cost_ex_gst":p.get("former_cost_ex_gst"),
            "stock":stock,
            "gtin":g.get("gtin"),
            "weight":weight,
            **calc,
            "status":status,
            "hold_reason":hold,
        })
    return out

@app.get("/southern/commercial-preview")
async def commercial_preview(limit:int=50):
    rows = await commercial_rows(min(max(limit,1),100))
    return {"ok":True,"dry_run":DRY_RUN,"count":len(rows),"rows":rows,
            "shipping_policy":{
                "customer_shipping_below_threshold":DEFAULT_CUSTOMER_SHIPPING,
                "free_shipping_threshold":FREE_SHIPPING_THRESHOLD,
                "max_product_freight_subsidy":MAX_FREIGHT_SUBSIDY,
            },
            "note":"No Shopify changes are made."}

@app.get("/southern/commercial-table", response_class=HTMLResponse)
async def commercial_table(limit:int=50):
    rows = await commercial_rows(min(max(limit,1),100))
    trs=[]
    for r in rows:
        margin=f"{r['margin_rate']*100:.1f}%"
        status=r["status"]
        trs.append(
            f"<tr><td>{r['sku']}</td><td>{r['product']}</td>"
            f"<td>${r['southern_cost_ex_gst']:.2f}</td><td>{r['stock']}</td>"
            f"<td>{r['weight'] or ''}</td><td>${r['proposed_price']:.2f}</td>"
            f"<td>${r['contribution']:.2f}</td><td>{margin}</td><td>{status}</td></tr>"
        )
    html=f"""
    <html><head><title>PawVida Commercial Preview</title>
    <style>
    body{{font-family:Arial,sans-serif;margin:30px;color:#1f2d22}}
    h1{{margin-bottom:4px}} p{{color:#555}}
    table{{border-collapse:collapse;width:100%;font-size:14px}}
    th,td{{border-bottom:1px solid #ddd;padding:10px;text-align:left}}
    th{{background:#eef4ed;position:sticky;top:0}}
    tr:hover{{background:#fafafa}}
    .note{{padding:12px;background:#f3f6ef;border-radius:8px;margin:16px 0}}
    </style></head><body>
    <h1>PawVida — Commercial Preview</h1>
    <p>Dry run. No Shopify changes. Customer shipping: ${DEFAULT_CUSTOMER_SHIPPING:.2f} below
    ${FREE_SHIPPING_THRESHOLD:.0f}; product freight subsidy capped at ${MAX_FREIGHT_SUBSIDY:.2f}.</p>
    <div class="note">Products failing stock or margin are shown as OUT_OF_STOCK.</div>
    <table><thead><tr><th>SKU</th><th>Product</th><th>Southern Cost ex GST</th>
    <th>Stock</th><th>Weight</th><th>PawVida Price</th><th>Contribution</th>
    <th>Margin</th><th>Status</th></tr></thead><tbody>{''.join(trs)}</tbody></table>
    </body></html>
    """
    return HTMLResponse(html)


@app.get("/market/benchmark-preview")
async def market_benchmark_preview(limit:int=50):
    offers = await load_market_benchmarks()
    return {
        "ok": True,
        "configured": bool(MARKET_BENCHMARK_CSV_URL),
        "count": len(offers),
        "rows": offers[:min(max(limit,1),100)],
        "policy": {
            "minimum_offers": MIN_BENCHMARK_OFFERS,
            "benchmark": "median of 3 lowest exact-match in-stock credible offers",
            "market_ceiling_multiplier": MARKET_CEILING_MULTIPLIER,
            "automatic_match": "GTIN first, SKU second; fuzzy title requires manual review",
        }
    }

@app.get("/southern/final-commercial-preview")
async def final_commercial_preview(limit:int=50):
    rows = await final_commercial_rows(min(max(limit,1),100))
    return {
        "ok": True,
        "dry_run": DRY_RUN,
        "count": len(rows),
        "rows": rows,
        "note": "APPROVED_DRAFT requires stock + margin + verified market benchmark. No Shopify writes."
    }

@app.get("/southern/final-commercial-table", response_class=HTMLResponse)
async def final_commercial_table(limit:int=50):
    rows = await final_commercial_rows(min(max(limit,1),100))
    trs=[]
    for r in rows:
        bp = "—" if r["market_benchmark"] is None else f"${r['market_benchmark']:.2f}"
        mc = "—" if r["market_ceiling"] is None else f"${r['market_ceiling']:.2f}"
        trs.append(
            f"<tr><td>{r['sku']}</td><td>{r['product']}</td>"
            f"<td>${r['southern_cost_ex_gst']:.2f}</td><td>{r['stock']}</td>"
            f"<td>{r['weight'] or ''}</td><td>${r['proposed_price']:.2f}</td>"
            f"<td>{bp}</td><td>{mc}</td><td>${r['contribution']:.2f}</td>"
            f"<td>{r['margin_rate']*100:.1f}%</td><td>{r['final_status']}</td></tr>"
        )
    html=f"""
    <html><head><title>PawVida Final Commercial Preview</title>
    <style>
    body{{font-family:Arial,sans-serif;margin:30px;color:#1f2d22}}
    h1{{margin-bottom:4px}} p{{color:#555}}
    table{{border-collapse:collapse;width:100%;font-size:13px}}
    th,td{{border-bottom:1px solid #ddd;padding:9px;text-align:left}}
    th{{background:#eef4ed;position:sticky;top:0}}
    tr:hover{{background:#fafafa}}
    .note{{padding:12px;background:#f3f6ef;border-radius:8px;margin:16px 0}}
    </style></head><body>
    <h1>PawVida — Final Commercial Preview</h1>
    <p>Dry run. No Shopify changes.</p>
    <div class="note">APPROVED_DRAFT requires stock + margin + verified market benchmark.
    REVIEW_MARKET means the economics pass, but no market benchmark has been supplied yet.</div>
    <table><thead><tr><th>SKU</th><th>Product</th><th>Southern Cost ex GST</th>
    <th>Stock</th><th>Weight</th><th>Required PawVida Price</th><th>Market Benchmark</th>
    <th>Market Ceiling</th><th>Contribution</th><th>Margin</th><th>Status</th></tr></thead>
    <tbody>{''.join(trs)}</tbody></table>
    </body></html>
    """
    return HTMLResponse(html)



@app.get("/market/serpapi-test")
async def market_serpapi_test(q:str="Advance Adult Dog All Breed Active 13kg"):
    data = await serpapi_shopping_search(q)
    results = data.get("shopping_results") or []
    return {
        "ok": True,
        "query": q,
        "result_count": len(results),
        "sample": [{
            "title": x.get("title"),
            "source": x.get("source"),
            "price": x.get("extracted_price") or x.get("price"),
            "product_link": x.get("product_link"),
        } for x in results[:10]],
        "note": "One SerpApi Google Shopping Australia search. No Shopify changes."
    }

@app.get("/southern/serp-commercial-preview")
async def serp_commercial_preview(limit:int=5):
    # Limit is intentionally capped during testing to protect API quota.
    limit=min(max(limit,1),10)
    rows=await serp_final_rows(limit)
    return {
        "ok":True,
        "dry_run":DRY_RUN,
        "count":len(rows),
        "rows":rows,
        "policy":{
            "country":SERPAPI_GL,
            "location":SERPAPI_LOCATION,
            "minimum_distinct_retailers":MIN_BENCHMARK_OFFERS,
            "benchmark":"median of 3 lowest accepted distinct-retailer Google Shopping offers",
            "market_ceiling_multiplier":MARKET_CEILING_MULTIPLIER,
            "marketplace_blocklist":sorted(MARKETPLACE_BLOCKLIST),
        },
        "note":"Testing cap: 10 products per request. No Shopify changes."
    }

@app.get("/southern/serp-commercial-table", response_class=HTMLResponse)
async def serp_commercial_table(limit:int=5):
    limit=min(max(limit,1),10)
    rows=await serp_final_rows(limit)
    trs=[]
    for r in rows:
        bp="—" if r["market_benchmark"] is None else f"${r['market_benchmark']:.2f}"
        mc="—" if r["market_ceiling"] is None else f"${r['market_ceiling']:.2f}"
        offer_text="<br>".join(
            f"{o['retailer']}: ${o['price']:.2f}"
            for o in r.get("market_offers",[])[:3]
        ) or "—"
        trs.append(
            f"<tr><td>{r['sku']}</td><td>{r['product']}</td>"
            f"<td>${r['southern_cost_ex_gst']:.2f}</td><td>{r['stock']}</td>"
            f"<td>${r['proposed_price']:.2f}</td><td>{offer_text}</td>"
            f"<td>{bp}</td><td>{mc}</td><td>{r['margin_rate']*100:.1f}%</td>"
            f"<td><b>{r['final_status']}</b></td></tr>"
        )
    html=f"""
    <html><head><title>PawVida Live Market Test</title>
    <style>
    body{{font-family:Arial,sans-serif;margin:28px;color:#1f2d22}}
    table{{border-collapse:collapse;width:100%;font-size:13px}}
    th,td{{border-bottom:1px solid #ddd;padding:9px;text-align:left;vertical-align:top}}
    th{{background:#eef4ed;position:sticky;top:0}}
    .note{{padding:12px;background:#f3f6ef;border-radius:8px;margin:16px 0}}
    </style></head><body>
    <h1>PawVida — Live Market Benchmark Test</h1>
    <div class="note">Dry run. Limited to {limit} products to protect SerpApi quota.
    No Shopify changes.</div>
    <table><thead><tr><th>SKU</th><th>Product</th><th>Southern Cost ex GST</th>
    <th>Stock</th><th>Required Price</th><th>Lowest accepted offers</th>
    <th>Benchmark</th><th>Ceiling</th><th>Margin</th><th>Status</th></tr></thead>
    <tbody>{''.join(trs)}</tbody></table></body></html>
    """
    return HTMLResponse(html)



@app.get("/southern/mixed-market-preview")
async def mixed_market_preview(per_category:int=4):
    # Maximum 20 products by default; hard cap 30.
    per_category = min(max(per_category,1),6)
    rows = await mixed_serp_final_rows(per_category)
    summary = {}
    for r in rows:
        cat = r.get("source_category") or "unknown"
        summary.setdefault(cat, {"APPROVED_DRAFT":0,"COMMERCIAL_HOLD":0,"TEMP_OUT_OF_STOCK":0,"REVIEW_MARKET":0})
        summary[cat][r["final_status"]] = summary[cat].get(r["final_status"],0) + 1
    return {
        "ok": True,
        "dry_run": DRY_RUN,
        "count": len(rows),
        "summary_by_category": summary,
        "rows": rows,
        "note": "Mixed-category SerpApi dry run. No Shopify changes."
    }

@app.get("/southern/mixed-market-table", response_class=HTMLResponse)
async def mixed_market_table(per_category:int=4):
    per_category = min(max(per_category,1),6)
    rows = await mixed_serp_final_rows(per_category)

    summary = {}
    for r in rows:
        cat = r.get("source_category") or "unknown"
        summary.setdefault(cat, {"APPROVED_DRAFT":0,"COMMERCIAL_HOLD":0,"TEMP_OUT_OF_STOCK":0,"REVIEW_MARKET":0})
        summary[cat][r["final_status"]] = summary[cat].get(r["final_status"],0) + 1

    summary_html = "".join(
        f"<div class='card'><b>{cat.title()}</b><br>"
        f"Approved: {vals.get('APPROVED_DRAFT',0)} &nbsp; "
        f"Hold: {vals.get('COMMERCIAL_HOLD',0)} &nbsp; "
        f"Stockout: {vals.get('TEMP_OUT_OF_STOCK',0)} &nbsp; "
        f"Review: {vals.get('REVIEW_MARKET',0)}</div>"
        for cat, vals in summary.items()
    )

    trs=[]
    for r in rows:
        offers="<br>".join(
            f"{o.get('retailer')}: ${o.get('price'):.2f}"
            for o in r.get("market_offers",[])[:3]
        ) or "—"
        bench = "—" if r["market_benchmark"] is None else f"${r['market_benchmark']:.2f}"
        ceiling = "—" if r["market_ceiling"] is None else f"${r['market_ceiling']:.2f}"
        trs.append(
            f"<tr><td>{r['source_category']}</td><td>{r['sku']}</td><td>{r['product']}</td>"
            f"<td>${r['southern_cost_ex_gst']:.2f}</td><td>{r['stock']}</td>"
            f"<td>${r['proposed_price']:.2f}</td><td>{offers}</td>"
            f"<td>{bench}</td><td>{ceiling}</td><td>{r['margin_rate']*100:.1f}%</td>"
            f"<td><b>{r['final_status']}</b></td></tr>"
        )

    html=f"""
    <html><head><title>PawVida Mixed Category Market Test</title>
    <style>
    body{{font-family:Arial,sans-serif;margin:26px;color:#1f2d22}}
    h1{{margin-bottom:4px}}
    .note{{padding:12px;background:#f3f6ef;border-radius:8px;margin:14px 0}}
    .summary{{display:flex;gap:10px;flex-wrap:wrap;margin:15px 0}}
    .card{{background:#eef4ed;border-radius:8px;padding:12px 14px;min-width:190px}}
    table{{border-collapse:collapse;width:100%;font-size:12px}}
    th,td{{border-bottom:1px solid #ddd;padding:8px;text-align:left;vertical-align:top}}
    th{{background:#eef4ed;position:sticky;top:0}}
    </style></head><body>
    <h1>PawVida — Mixed Category Market Test</h1>
    <div class="note">Dry run. {len(rows)} products across food, toys, bedding, grooming and health.
    No Shopify changes.</div>
    <div class="summary">{summary_html}</div>
    <table><thead><tr>
    <th>Category</th><th>SKU</th><th>Product</th><th>Southern Cost ex GST</th>
    <th>Stock</th><th>Required Price</th><th>Lowest accepted offers</th>
    <th>Benchmark</th><th>Ceiling</th><th>Margin</th><th>Status</th>
    </tr></thead><tbody>{''.join(trs)}</tbody></table>
    </body></html>
    """
    return HTMLResponse(html)



@app.get("/southern/candidate-pool")
async def candidate_pool_preview(per_category:int=20, limit:int=50):
    rows = await candidate_pool(per_category=per_category, limit=limit)
    return {
        "ok": True,
        "dry_run": DRY_RUN,
        "count": len(rows),
        "rows": rows,
        "note": "Southern-only ranking. No SerpApi calls and no Shopify changes."
    }

@app.get("/southern/candidate-table", response_class=HTMLResponse)
async def candidate_table(per_category:int=20, limit:int=50):
    rows = await candidate_pool(per_category=per_category, limit=limit)
    trs = []
    for r in rows:
        trs.append(
            f"<tr><td>{r['candidate_score']:.1f}</td><td>{r.get('source_category','')}</td>"
            f"<td>{r['sku']}</td><td>{r['product']}</td>"
            f"<td>${r['southern_cost_ex_gst']:.2f}</td><td>{r['stock']}</td>"
            f"<td>{r['weight'] or ''}</td><td>${r['proposed_price']:.2f}</td>"
            f"<td>{r['margin_rate']*100:.1f}%</td></tr>"
        )
    html = f"""
    <html><head><title>PawVida Candidate Pool</title>
    <style>
    body{{font-family:Arial,sans-serif;margin:26px;color:#1f2d22}}
    table{{border-collapse:collapse;width:100%;font-size:12px}}
    th,td{{border-bottom:1px solid #ddd;padding:8px;text-align:left;vertical-align:top}}
    th{{background:#eef4ed;position:sticky;top:0}}
    </style></head><body>
    <h1>PawVida — Candidate Pool</h1>
    <p>Southern-only ranking. No SerpApi calls. No Shopify changes.</p>
    <table><thead><tr><th>Score</th><th>Category</th><th>SKU</th><th>Product</th>
    <th>Southern Cost ex GST</th><th>Stock</th><th>Weight</th><th>Required Price</th><th>Margin</th>
    </tr></thead><tbody>{''.join(trs)}</tbody></table>
    </body></html>
    """
    return HTMLResponse(html)

@app.get("/southern/top-candidates-market-preview")
async def top_candidates_market_preview(limit:int=25, per_category:int=20):
    rows = await top_candidate_market_rows(limit=limit, per_category=per_category)
    counts = {"APPROVED_DRAFT":0,"COMMERCIAL_HOLD":0,"TEMP_OUT_OF_STOCK":0,"REVIEW_MARKET":0}
    by_category = {}
    for r in rows:
        counts[r["final_status"]] = counts.get(r["final_status"],0) + 1
        cat = r.get("source_category") or "unknown"
        by_category.setdefault(cat, {"APPROVED_DRAFT":0,"COMMERCIAL_HOLD":0,"TEMP_OUT_OF_STOCK":0,"REVIEW_MARKET":0})
        by_category[cat][r["final_status"]] = by_category[cat].get(r["final_status"],0) + 1
    return {
        "ok": True,
        "dry_run": DRY_RUN,
        "count": len(rows),
        "summary": counts,
        "summary_by_category": by_category,
        "rows": rows,
        "note": "Top Southern candidates only. No Shopify changes."
    }

@app.get("/southern/top-candidates-market-table", response_class=HTMLResponse)
async def top_candidates_market_table(limit:int=25, per_category:int=20):
    rows = await top_candidate_market_rows(limit=limit, per_category=per_category)

    counts = {"APPROVED_DRAFT":0,"COMMERCIAL_HOLD":0,"TEMP_OUT_OF_STOCK":0,"REVIEW_MARKET":0}
    for r in rows:
        counts[r["final_status"]] = counts.get(r["final_status"],0) + 1

    summary = (
        f"Approved: {counts['APPROVED_DRAFT']} &nbsp; "
        f"Hold: {counts['COMMERCIAL_HOLD']} &nbsp; "
        f"Stockout: {counts['TEMP_OUT_OF_STOCK']} &nbsp; "
        f"Review: {counts['REVIEW_MARKET']}"
    )

    trs=[]
    for r in rows:
        offers = "<br>".join(
            f"{o.get('retailer')}: ${o.get('price'):.2f}"
            for o in r.get("market_offers",[])[:3]
        ) or "—"
        benchmark = "—" if r["market_benchmark"] is None else f"${r['market_benchmark']:.2f}"
        ceiling = "—" if r["market_ceiling"] is None else f"${r['market_ceiling']:.2f}"

        trs.append(
            f"<tr><td>{r['candidate_score']:.1f}</td><td>{r.get('source_category','')}</td>"
            f"<td>{r['sku']}</td><td>{r['product']}</td>"
            f"<td>${r['southern_cost_ex_gst']:.2f}</td><td>{r['stock']}</td>"
            f"<td>${r['proposed_price']:.2f}</td><td>{offers}</td>"
            f"<td>{benchmark}</td><td>{ceiling}</td>"
            f"<td>{r['margin_rate']*100:.1f}%</td><td><b>{r['final_status']}</b></td></tr>"
        )

    html=f"""
    <html><head><title>PawVida Top Candidate Market Test</title>
    <style>
    body{{font-family:Arial,sans-serif;margin:26px;color:#1f2d22}}
    .note{{padding:12px;background:#f3f6ef;border-radius:8px;margin:14px 0}}
    table{{border-collapse:collapse;width:100%;font-size:12px}}
    th,td{{border-bottom:1px solid #ddd;padding:8px;text-align:left;vertical-align:top}}
    th{{background:#eef4ed;position:sticky;top:0}}
    </style></head><body>
    <h1>PawVida — Top Candidate Market Test</h1>
    <div class="note">Dry run. {len(rows)} highest-ranked Southern candidates. No Shopify changes.
    Do not repeatedly refresh: uncached products can consume SerpApi searches.</div>
    <h3>{summary}</h3>
    <table><thead><tr>
    <th>Score</th><th>Category</th><th>SKU</th><th>Product</th>
    <th>Southern Cost ex GST</th><th>Stock</th><th>Required Price</th>
    <th>Lowest accepted offers</th><th>Benchmark</th><th>Ceiling</th><th>Margin</th><th>Status</th>
    </tr></thead><tbody>{''.join(trs)}</tbody></table>
    </body></html>
    """
    return HTMLResponse(html)


@app.get("/economics/policy")
async def economics_policy():
    return {
        "ok": True,
        "model": "basket_level_profitability",
        "minimum_order_value": MINIMUM_ORDER_VALUE,
        "target_basket_contribution": TARGET_BASKET_CONTRIBUTION,
        "customer_shipping_below_free_threshold": DEFAULT_CUSTOMER_SHIPPING,
        "free_shipping_threshold": FREE_SHIPPING_THRESHOLD,
        "max_product_freight_subsidy": MAX_FREIGHT_SUBSIDY,
        "retail_bands": [
            {"retail":"under $15","min_contribution":2.00,"min_margin":0.18},
            {"retail":"$15-$29.99","min_contribution":3.50,"min_margin":0.20},
            {"retail":"$30-$59.99","min_contribution":6.00,"min_margin":0.22},
            {"retail":"$60-$99.99","min_contribution":10.00,"min_margin":0.24},
            {"retail":"$100+","min_contribution":15.00,"min_margin":0.18},
        ],
    }


@app.get("/southern/launch-candidate-pool")
async def launch_candidate_pool_preview(per_category:int=60, limit:int=300):
    rows = await launch_candidate_pool(per_category=per_category, max_candidates=limit)
    return {
        "ok": True,
        "dry_run": DRY_RUN,
        "count": len(rows),
        "rows": rows,
        "note": "Large Southern-only pre-screen. No SerpApi calls and no Shopify changes."
    }

@app.get("/southern/launch-shortlist-preview")
async def launch_shortlist_preview(per_category:int=60, benchmark_limit:int=25, approved_target:int=100):
    benchmark_limit = min(max(benchmark_limit,1),50)
    result = await build_launch_shortlist(
        per_category=per_category,
        benchmark_limit=benchmark_limit,
        approved_target=approved_target
    )
    return {"ok": True, "dry_run": DRY_RUN, **result}

@app.get("/southern/launch-shortlist-table", response_class=HTMLResponse)
async def launch_shortlist_table(per_category:int=60, benchmark_limit:int=25, approved_target:int=100):
    benchmark_limit = min(max(benchmark_limit,1),50)
    result = await build_launch_shortlist(
        per_category=per_category,
        benchmark_limit=benchmark_limit,
        approved_target=approved_target
    )
    rows = result["all_benchmarked"]
    trs = []
    for r in rows:
        offers = "<br>".join(
            f"{o.get('retailer')}: ${o.get('price'):.2f}"
            for o in r.get("market_offers",[])[:3]
        ) or "—"
        benchmark = "—" if r["market_benchmark"] is None else f"${r['market_benchmark']:.2f}"
        ceiling = "—" if r["market_ceiling"] is None else f"${r['market_ceiling']:.2f}"
        resilience = "—" if r["resilience"] is None else f"{r['resilience']*100:.1f}%"
        trs.append(
            f"<tr><td>{r['launch_priority']:.1f}</td>"
            f"<td>{r.get('source_category','')}</td><td>{r['sku']}</td>"
            f"<td>{r['product']}</td><td>${r['southern_cost_ex_gst']:.2f}</td>"
            f"<td>{r['stock']}</td><td>${r['proposed_price']:.2f}</td>"
            f"<td>{offers}</td><td>{benchmark}</td><td>{ceiling}</td>"
            f"<td>{r['margin_rate']*100:.1f}%</td><td>{resilience}</td>"
            f"<td><b>{r['final_status']}</b></td></tr>"
        )
    html = f"""
    <html><head><title>PawVida Launch Shortlist</title>
    <style>
    body{{font-family:Arial,sans-serif;margin:26px;color:#1f2d22}}
    .note{{padding:12px;background:#f3f6ef;border-radius:8px;margin:14px 0}}
    table{{border-collapse:collapse;width:100%;font-size:12px}}
    th,td{{border-bottom:1px solid #ddd;padding:8px;text-align:left;vertical-align:top}}
    th{{background:#eef4ed;position:sticky;top:0}}
    </style></head><body>
    <h1>PawVida — Launch Shortlist</h1>
    <div class="note">
      Candidates scanned: {result['candidates_scanned']} |
      Benchmarked: {result['benchmarked']} |
      Approved: {result['approved_count']} |
      Launch target: {approved_target}<br>
      Dry run. No Shopify changes.
    </div>
    <table><thead><tr>
    <th>Priority</th><th>Category</th><th>SKU</th><th>Product</th>
    <th>Southern Cost ex GST</th><th>Stock</th><th>Required Price</th>
    <th>Accepted Offers</th><th>Benchmark</th><th>Ceiling</th>
    <th>Margin</th><th>Resilience</th><th>Status</th>
    </tr></thead><tbody>{''.join(trs)}</tbody></table>
    </body></html>
    """
    return HTMLResponse(html)



@app.get("/market/query-preview")
async def market_query_preview(per_category:int=60, limit:int=25):
    candidates = await launch_candidate_pool(
        per_category=min(max(per_category,1),100),
        max_candidates=min(max(limit,1),100)
    )
    return {
        "ok": True,
        "count": len(candidates),
        "rows": [{
            "sku": r["sku"],
            "product": r["product"],
            "gtin": r.get("gtin"),
            "queries": build_market_queries(r),
        } for r in candidates],
        "note": "No SerpApi calls. Query-quality diagnostic only."
    }

@app.post("/sync/stock")
async def sync_stock():
    if DRY_RUN:
        return JSONResponse({"ok":True,"dry_run":True,
            "message":"Live writes remain disabled while DRY_RUN=true."})
    raise HTTPException(501,"Live write remains locked pending approval.")
