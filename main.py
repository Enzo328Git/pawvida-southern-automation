
import csv, io, os, time, re, math
from typing import Any
from urllib.parse import urljoin
import httpx
from bs4 import BeautifulSoup
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse

APP_NAME = "PawVida Southern Automation"
VERSION = "5.0.0"

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
        return {"name":"clothing", "min_margin_rate":0.28, "min_contribution":15.0, "returns_rate":0.03}
    if any(x in n for x in ["bed", "bedding", "mattress"]):
        return {"name":"bedding", "min_margin_rate":0.30, "min_contribution":18.0, "returns_rate":0.0075}
    if any(x in n for x in ["toy", "kong", "ball", "chew"]):
        return {"name":"toys", "min_margin_rate":0.28, "min_contribution":10.0, "returns_rate":0.005}
    if any(x in n for x in ["lead", "leash", "harness", "collar", "walking"]):
        return {"name":"walking", "min_margin_rate":0.28, "min_contribution":12.0, "returns_rate":0.0075}
    if any(x in n for x in ["food", "kibble", "adult dog", "puppy", "cat food", "diet"]):
        return {"name":"mainstream_food", "min_margin_rate":0.18, "min_contribution":10.0, "returns_rate":0.005}
    return {"name":"general", "min_margin_rate":0.24, "min_contribution":10.0, "returns_rate":0.005}

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

    # Find the lowest retail price that passes both $ contribution and % margin.
    test_price = max(cost_inc_gst + 1, 9.95)
    found = None
    for cents in range(int(test_price*100), int((test_price*2.5 + 150)*100), 5):
        p = cents / 100.0
        payment_fee = p * PAYMENT_FEE_RATE + PAYMENT_FEE_FIXED
        returns_allowance = p * rule["returns_rate"]
        contribution = p - cost_inc_gst - freight_subsidy - payment_fee - returns_allowance
        margin_rate = contribution / p if p else 0
        if contribution >= rule["min_contribution"] and margin_rate >= rule["min_margin_rate"]:
            found = p
            break
    if found is None:
        found = cost_inc_gst * 1.5

    proposed = round_95(found)
    payment_fee = proposed * PAYMENT_FEE_RATE + PAYMENT_FEE_FIXED
    returns_allowance = proposed * rule["returns_rate"]
    contribution = proposed - cost_inc_gst - freight_subsidy - payment_fee - returns_allowance
    margin_rate = contribution / proposed if proposed else 0

    return {
        "category_rule": rule["name"],
        "min_margin_rate": rule["min_margin_rate"],
        "min_contribution": rule["min_contribution"],
        "southern_cost_ex_gst": round(cost_ex_gst,2),
        "cost_inc_gst": round(cost_inc_gst,2),
        "freight_subsidy": round(freight_subsidy,2),
        "payment_fee_allowance": round(payment_fee,2),
        "returns_allowance": round(returns_allowance,2),
        "proposed_price": proposed,
        "contribution": round(contribution,2),
        "margin_rate": round(margin_rate,4),
        "passes_margin": contribution >= rule["min_contribution"] and margin_rate >= rule["min_margin_rate"],
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
        raise HTTPException(502, f"SerpApi error: {data['error']}")
    _serp_cache[cache_key] = {"ts": time.time(), "data": data}
    return data

def filter_serp_offers(product_row, shopping_results):
    product_name = product_row.get("product") or ""
    gtin = str(product_row.get("gtin") or "").strip()
    wanted_sizes = pack_tokens(product_name)
    offers = []

    for item in shopping_results or []:
        source = item.get("source") or ""
        if source_blocked(source):
            continue
        title = item.get("title") or ""
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

        result_sizes = pack_tokens(title)
        if wanted_sizes and result_sizes and not (wanted_sizes & result_sizes):
            continue

        similarity = token_similarity(product_name, title)

        # GTIN searches are usually highly precise; title fallback needs stronger similarity.
        match_type = "GTIN_QUERY" if gtin else "TITLE_QUERY"
        min_sim = 0.18 if gtin else 0.42
        if similarity < min_sim:
            continue

        offers.append({
            "retailer": source,
            "price": round(price,2),
            "title": title,
            "url": item.get("product_link"),
            "position": item.get("position"),
            "match_type": match_type,
            "similarity": round(similarity,3),
            "delivery": item.get("delivery"),
        })

    # de-dupe exact same retailer/price/title
    dedup={}
    for o in offers:
        dedup[(o["retailer"],o["price"],o["title"])]=o
    offers=list(dedup.values())
    offers.sort(key=lambda x:x["price"])
    return offers

async def live_market_for_product(product_row):
    gtin = str(product_row.get("gtin") or "").strip()
    name = product_row.get("product") or product_row.get("sku")
    query = gtin if gtin else name
    data = await serpapi_shopping_search(query)
    offers = filter_serp_offers(product_row, data.get("shopping_results") or [])

    # require distinct credible retailers
    distinct=[]
    seen=set()
    for o in offers:
        retailer=(o.get("retailer") or "").lower()
        if retailer in seen:
            continue
        seen.add(retailer)
        distinct.append(o)

    if len(distinct) < MIN_BENCHMARK_OFFERS:
        return {
            "verified": False,
            "query": query,
            "offer_count": len(distinct),
            "offers": distinct[:10],
            "benchmark_price": None,
            "market_ceiling": None,
        }

    lowest = distinct[:3]
    prices=sorted(o["price"] for o in lowest)
    benchmark=prices[1]
    ceiling=round(benchmark * MARKET_CEILING_MULTIPLIER,2)
    return {
        "verified": True,
        "query": query,
        "offer_count": len(distinct),
        "offers": distinct[:10],
        "benchmark_price": round(benchmark,2),
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
            "market_query": market["query"],
            "market_verified": market["verified"],
            "market_offer_count": market["offer_count"],
            "market_benchmark": market["benchmark_price"],
            "market_ceiling": market["market_ceiling"],
            "market_offers": market["offers"],
            "final_status": status,
            "hold_reason": list(dict.fromkeys(hold)),
        })
    return final


@app.get("/")
async def root():
    return {
        "service": APP_NAME, "status":"running", "version":VERSION, "dry_run":DRY_RUN,
        "next":["/health","/shopify/test","/southern/stock-preview","/southern/gtin-preview",
                "/southern/login-test","/southern/pricing-preview","/southern/commercial-preview",
                "/southern/commercial-table","/market/benchmark-preview","/southern/final-commercial-table","/market/serpapi-test","/southern/serp-commercial-table"]
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
        product = clean_title(product, sku)
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


@app.post("/sync/stock")
async def sync_stock():
    if DRY_RUN:
        return JSONResponse({"ok":True,"dry_run":True,
            "message":"Live writes remain disabled while DRY_RUN=true."})
    raise HTTPException(501,"Live write remains locked pending approval.")
