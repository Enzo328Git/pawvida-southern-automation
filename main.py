
import csv, io, os, time, re
from typing import Any
from urllib.parse import urljoin
import httpx
from bs4 import BeautifulSoup
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse

APP_NAME = "PawVida Southern Automation"
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

app = FastAPI(title=APP_NAME, version="2.0.0")
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
    headers = {"User-Agent": "PawVidaAutomation/2.0"}
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

def parse_delimited(text: str, limit: int = 20):
    sample = text[:10000]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
    except Exception:
        dialect = csv.excel
    reader = csv.DictReader(io.StringIO(text), dialect=dialect)
    rows = []
    for i, row in enumerate(reader):
        rows.append({str(k): v for k, v in row.items()})
        if i + 1 >= limit:
            break
    return reader.fieldnames or [], rows

def money_values(text: str):
    vals = []
    for m in re.finditer(r"\$\s*([0-9][0-9,]*\.?[0-9]{0,2})", text):
        try:
            vals.append(float(m.group(1).replace(",", "")))
        except Exception:
            pass
    return vals

def first_match(patterns, text):
    for p in patterns:
        m = re.search(p, text, re.I)
        if m:
            return m.group(1).strip()
    return None

def parse_southern_products(html: str, limit: int = 50):
    soup = BeautifulSoup(html, "html.parser")
    products = []

    # Southern pages render product rows/blocks with SKU, description, price, weight and stock.
    # Use several candidate containers so minor theme changes do not immediately break parsing.
    candidates = []
    for selector in [
        "li.product", ".product", "tr", ".products > *",
        ".product-row", ".woocommerce-loop-product"
    ]:
        candidates.extend(soup.select(selector))

    seen = set()
    for node in candidates:
        text = " ".join(node.stripped_strings)
        if not text or len(text) < 15:
            continue

        sku = first_match([
            r"\bSKU\b\s*[:\-]?\s*([A-Z0-9][A-Z0-9.\-_/]+)",
            r"\b([A-Z]{1,6}[0-9][A-Z0-9.\-_/]{2,})\b"
        ], text)
        if not sku or sku in seen:
            continue

        # Only accept blocks that appear product-like.
        if "price" not in text.lower() and "$" not in text:
            continue

        prices = money_values(text)
        active_price = prices[-1] if prices else None  # sale price normally appears after crossed-out price
        old_price = prices[0] if len(prices) > 1 else None

        weight = first_match([
            r"\bWeight\b\s*[:\-]?\s*([0-9.]+\s*kg)",
            r"\b([0-9.]+\s*kg)\b"
        ], text)
        stock = first_match([
            r"\b(?:Stock|Quantity)\b\s*[:\-]?\s*([0-9]+)",
        ], text)

        # Product title: prefer a heading/link text, otherwise clean the block text.
        title = None
        heading = node.find(["h2","h3","h4"])
        if heading:
            title = " ".join(heading.stripped_strings)
        if not title:
            a = node.find("a", href=True)
            if a:
                at = " ".join(a.stripped_strings)
                if len(at) > 4 and "product info" not in at.lower():
                    title = at
        if not title:
            title = text[:180]

        image = None
        img = node.find("img")
        if img:
            image = img.get("data-src") or img.get("src")

        link = None
        for a in node.find_all("a", href=True):
            label = " ".join(a.stripped_strings).lower()
            href = a["href"]
            if "product" in label or "/product/" in href:
                link = urljoin(SOUTHERN_PRICING_SAMPLE_URL, href)
                break

        products.append({
            "sku": sku,
            "title": title,
            "active_cost_ex_gst": active_price,
            "former_cost_ex_gst": old_price,
            "weight": weight,
            "displayed_stock": int(stock) if stock and stock.isdigit() else None,
            "image": image,
            "product_url": link,
        })
        seen.add(sku)
        if len(products) >= limit:
            break

    return products

async def southern_authenticated_client():
    if not SOUTHERN_USERNAME or not SOUTHERN_PASSWORD:
        raise HTTPException(500, "SOUTHERN_USERNAME / SOUTHERN_PASSWORD are not configured")

    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; PawVidaAutomation/2.0; +https://pawvida-6.myshopify.com)",
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
        txt = " ".join(f.stripped_strings).lower()
        if "log in" in txt or "login" in txt:
            if f.find("input", {"name": "username"}) or f.find("input", {"name": "password"}):
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

    # Standard WooCommerce account login fields.
    payload["username"] = SOUTHERN_USERNAME
    payload["password"] = SOUTHERN_PASSWORD
    payload["login"] = payload.get("login") or "Log in"
    payload["rememberme"] = "forever"

    action = form.get("action") or SOUTHERN_LOGIN_URL
    action = urljoin(str(login_page.url), action)
    post = await client.post(action, data=payload)

    # Verify login by checking My Account and a price-gated catalogue page.
    account = await client.get(SOUTHERN_LOGIN_URL)
    account_text = account.text.lower()
    if "logout" not in account_text and "log out" not in account_text:
        # Some sites omit logout from the rendered page; the pricing page gives a second verification.
        test = await client.get(SOUTHERN_PRICING_SAMPLE_URL)
        if "login to view prices" in test.text.lower():
            await client.aclose()
            raise HTTPException(401, "Southern login was not accepted. Recheck username/password.")
    return client

@app.get("/")
async def root():
    return {
        "service": APP_NAME,
        "status": "running",
        "version": "2.0.0",
        "dry_run": DRY_RUN,
        "shop": shop_domain(),
        "api_version": SHOPIFY_API_VERSION,
        "next": [
            "/health",
            "/shopify/test",
            "/southern/stock-preview",
            "/southern/gtin-preview",
            "/southern/login-test",
            "/southern/pricing-preview"
        ]
    }

@app.get("/health")
async def health():
    return {"ok": True, "dry_run": DRY_RUN, "version": "2.0.0"}

@app.get("/shopify/test")
async def shopify_test():
    q = """
    query PawVidaConnectionTest {
      shop { name myshopifyDomain }
      locations(first: 10) { nodes { id name isActive } }
      products(first: 3) { nodes { id title handle status } }
    }
    """
    data = await shopify_graphql(q)
    return {"ok": True, "shopify": data}

@app.get("/southern/stock-preview")
async def stock_preview(limit: int = 10):
    url, text = await fetch_csv_or_discover(SOUTHERN_STOCK_URL)
    fields, rows = parse_delimited(text, min(max(limit,1),50))
    return {"ok": True, "resolved_url": url, "fields": fields, "rows": rows, "note": "Preview only; no Shopify changes are made."}

@app.get("/southern/gtin-preview")
async def gtin_preview(limit: int = 10):
    url, text = await fetch_csv_or_discover(SOUTHERN_GTIN_URL)
    fields, rows = parse_delimited(text, min(max(limit,1),50))
    return {"ok": True, "resolved_url": url, "fields": fields, "rows": rows, "note": "Preview only; no Shopify changes are made."}

@app.get("/southern/login-test")
async def southern_login_test():
    client = await southern_authenticated_client()
    try:
        r = await client.get(SOUTHERN_PRICING_SAMPLE_URL)
        logged_in = "login to view prices" not in r.text.lower()
        return {
            "ok": logged_in,
            "logged_in": logged_in,
            "url": str(r.url),
            "credentials_present": bool(SOUTHERN_USERNAME and SOUTHERN_PASSWORD),
            "note": "No credentials are returned. Read-only test."
        }
    finally:
        await client.aclose()

@app.get("/southern/pricing-preview")
async def southern_pricing_preview(limit: int = 20, url: str | None = None):
    target = url or SOUTHERN_PRICING_SAMPLE_URL
    if not target.startswith("https://www.southernpetsupplies.com.au/"):
        raise HTTPException(400, "Pricing preview is restricted to southernpetsupplies.com.au")
    client = await southern_authenticated_client()
    try:
        r = await client.get(target)
        if r.status_code >= 400:
            raise HTTPException(r.status_code, f"Southern pricing page failed: {r.status_code}")
        if "login to view prices" in r.text.lower():
            raise HTTPException(401, "Southern session is not authenticated")
        rows = parse_southern_products(r.text, min(max(limit,1),100))
        return {
            "ok": True,
            "dry_run": DRY_RUN,
            "url": str(r.url),
            "count": len(rows),
            "rows": rows,
            "note": "Read-only pricing preview. No Shopify changes and no Southern orders are made."
        }
    finally:
        await client.aclose()

@app.post("/sync/stock")
async def sync_stock():
    if DRY_RUN:
        url, text = await fetch_csv_or_discover(SOUTHERN_STOCK_URL)
        fields, rows = parse_delimited(text, 5)
        return JSONResponse({
            "ok": True,
            "dry_run": True,
            "message": "Stock sync is intentionally disabled while DRY_RUN=true.",
            "resolved_url": url,
            "detected_fields": fields,
            "sample_rows": rows,
        })
    raise HTTPException(501, "Live stock write remains locked until pricing, margin rules and SKU mappings are approved.")
