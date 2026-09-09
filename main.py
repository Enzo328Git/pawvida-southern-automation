
import csv, io, os, time, re, math
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

# Commercial assumptions. All can be overridden in Render.
PAYMENT_FEE_RATE = float(os.getenv("PAYMENT_FEE_RATE", "0.022"))
PAYMENT_FEE_FIXED = float(os.getenv("PAYMENT_FEE_FIXED", "0.30"))
RETURNS_ALLOWANCE_RATE = float(os.getenv("RETURNS_ALLOWANCE_RATE", "0.01"))
DEFAULT_MIN_MARGIN_RATE = float(os.getenv("DEFAULT_MIN_MARGIN_RATE", "0.22"))
DEFAULT_MIN_CONTRIBUTION = float(os.getenv("DEFAULT_MIN_CONTRIBUTION", "12.00"))
PRICE_ROUND_SUFFIX = os.getenv("PRICE_ROUND_SUFFIX", "95")

app = FastAPI(title=APP_NAME, version="3.0.0")
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
        for a in soup.find_all("a", href=True):
            href = a["href"]
            label = " ".join(a.stripped_strings).lower()
            if ".csv" in href.lower() or "csv" in label or "spreadsheet" in label:
                rr = await client.get(urljoin(str(r.url), href))
                if rr.status_code < 400:
                    c2 = rr.headers.get("content-type", "").lower()
                    if "csv" in c2 or "<html" not in rr.text[:500].lower():
                        return str(rr.url), rr.text
        raise HTTPException(502, f"Could not discover a CSV at {source_url}")

def parse_all_csv(text: str):
    sample = text[:10000]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
    except Exception:
        dialect = csv.excel
    reader = csv.DictReader(io.StringIO(text), dialect=dialect)
    return reader.fieldnames or [], [{str(k): v for k, v in row.items()} for row in reader]

def money_values(text: str):
    vals = []
    for m in re.finditer(r"\$\s*([0-9][0-9,]*\.?[0-9]{0,2})", text):
        try:
            vals.append(float(m.group(1).replace(",", "")))
        except Exception:
            pass
    return vals

def valid_sku(s: str | None) -> bool:
    if not s:
        return False
    s = s.strip()
    if s.lower() in {"description","price","weight","quantity","sku","product"}:
        return False
    # Southern examples include AD374963, KA79055, 10001, 07.21B.
    return bool(re.fullmatch(r"(?:[A-Z]{1,6}[A-Z0-9.\-_/]{2,}|[0-9]{4,}[A-Z0-9.\-_/]*)", s, re.I))

def normalize_sku(s: str) -> str:
    return s.strip().upper()

def parse_southern_products(html: str, limit: int = 100):
    soup = BeautifulSoup(html, "html.parser")
    candidates = []
    for selector in ["li.product", ".product", "tr", ".products > *", ".product-row", ".woocommerce-loop-product"]:
        candidates.extend(soup.select(selector))

    results = {}
    for node in candidates:
        text = " ".join(node.stripped_strings)
        if len(text) < 15 or "$" not in text:
            continue

        sku = None
        m = re.search(r"\bSKU\b\s*[:\-]?\s*([A-Z0-9.\-_/]+)", text, re.I)
        if m and valid_sku(m.group(1)):
            sku = normalize_sku(m.group(1))
        if not sku:
            for token in re.findall(r"\b[A-Z0-9][A-Z0-9.\-_/]{3,}\b", text, re.I):
                if valid_sku(token):
                    sku = normalize_sku(token)
                    break
        if not sku:
            continue

        prices = money_values(text)
        if not prices:
            continue
        active_price = prices[-1]
        former_price = prices[0] if len(prices) > 1 and prices[0] != active_price else None

        title = None
        heading = node.find(["h2","h3","h4"])
        if heading:
            title = " ".join(heading.stripped_strings)
        if not title:
            for a in node.find_all("a", href=True):
                at = " ".join(a.stripped_strings)
                if len(at) > 5 and "product info" not in at.lower() and "$" not in at:
                    title = at
                    break
        if not title:
            title = text[:160]

        weight = None
        wm = re.search(r"\b([0-9]+(?:\.[0-9]+)?)\s*kg\b", text, re.I)
        if wm:
            weight = f"{wm.group(1)}kg"

        image = None
        img = node.find("img")
        if img:
            image = img.get("data-src") or img.get("src")

        product_url = None
        for a in node.find_all("a", href=True):
            href = a["href"]
            if "/product/" in href or "product info" in " ".join(a.stripped_strings).lower():
                product_url = urljoin(SOUTHERN_PRICING_SAMPLE_URL, href)
                break

        record = {
            "sku": sku,
            "title": title,
            "active_cost_ex_gst": active_price,
            "former_cost_ex_gst": former_price,
            "page_weight": weight,
            "image": image,
            "product_url": product_url,
        }

        # Prefer records with a cleaner title or product URL when the same SKU appears multiple times.
        current = results.get(sku)
        score = (1 if product_url else 0) + (1 if title and len(title) < 120 else 0)
        cur_score = 0 if not current else (1 if current.get("product_url") else 0) + (1 if current.get("title") and len(current["title"]) < 120 else 0)
        if current is None or score >= cur_score:
            results[sku] = record
        if len(results) >= limit:
            break

    return list(results.values())[:limit]

async def southern_authenticated_client():
    if not SOUTHERN_USERNAME or not SOUTHERN_PASSWORD:
        raise HTTPException(500, "SOUTHERN_USERNAME / SOUTHERN_PASSWORD are not configured")
    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; PawVidaAutomation/3.0)",
        "Accept-Language": "en-AU,en;q=0.9",
    }
    client = httpx.AsyncClient(timeout=45, follow_redirects=True, headers=headers)
    login_page = await client.get(SOUTHERN_LOGIN_URL)
    soup = BeautifulSoup(login_page.text, "html.parser")
    form = None
    for f in soup.find_all("form"):
        if f.find("input", {"name":"username"}) and f.find("input", {"name":"password"}):
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

def find_col(row, names):
    lowered = {str(k).strip().lower(): v for k,v in row.items()}
    for n in names:
        if n.lower() in lowered:
            return lowered[n.lower()]
    return None

def stock_index(rows):
    out = {}
    for r in rows:
        sku = find_col(r, ["SKU","Product SKU"])
        qty = find_col(r, ["Available","Quantity","Stock"])
        if sku and qty is not None and valid_sku(str(sku)):
            try:
                out[normalize_sku(str(sku))] = int(float(str(qty).strip()))
            except Exception:
                out[normalize_sku(str(sku))] = 0
    return out

def gtin_index(rows):
    out = {}
    for r in rows:
        sku = find_col(r, ["SKU"])
        if sku and valid_sku(str(sku)):
            out[normalize_sku(str(sku))] = {
                "product_name": find_col(r, ["Product Name","Description"]),
                "gtin": find_col(r, ["GTIN","Barcode"]),
                "weight": find_col(r, ["Weight","Cubic Weight"]),
            }
    return out

def category_rules(title: str):
    t = title.lower()
    # Conservative defaults based on Southern's dropship guidance.
    if any(k in t for k in ["coat","jacket","apparel","clothing"]):
        return {"min_margin_rate":0.30, "min_contribution":15.0, "returns_allowance":0.05, "category":"clothing"}
    if any(k in t for k in ["bed","mattress","snooza"]):
        return {"min_margin_rate":0.28, "min_contribution":18.0, "returns_allowance":0.015, "category":"bedding"}
    if any(k in t for k in ["toy","kong","outward hound","gigwi","starmark"]):
        return {"min_margin_rate":0.30, "min_contribution":10.0, "returns_allowance":0.01, "category":"toys"}
    if any(k in t for k in ["lead","leash","harness","collar","walking"]):
        return {"min_margin_rate":0.28, "min_contribution":12.0, "returns_allowance":0.015, "category":"walking"}
    if any(k in t for k in ["advance","black hawk","hill","royal canin","dog food","cat food","kibble"]):
        return {"min_margin_rate":0.18, "min_contribution":10.0, "returns_allowance":0.005, "category":"mainstream_food"}
    return {"min_margin_rate":DEFAULT_MIN_MARGIN_RATE, "min_contribution":DEFAULT_MIN_CONTRIBUTION, "returns_allowance":RETURNS_ALLOWANCE_RATE, "category":"default"}

def estimated_freight(weight_value):
    # Placeholder commercial allowance only; not a customer shipping quote.
    try:
        w = float(str(weight_value).lower().replace("kg","").strip())
    except Exception:
        w = 0
    if w <= 1: return 7.0
    if w <= 3: return 9.0
    if w <= 10: return 13.0
    if w <= 20: return 18.0
    return 25.0

def round_price(v: float):
    whole = math.floor(v)
    suffix = int(PRICE_ROUND_SUFFIX)
    candidate = whole + suffix/100
    if candidate < v:
        candidate += 1
    return round(candidate, 2)

def commercial_price(cost_ex_gst: float, weight, rules):
    # Treat Southern web account price as ex-GST wholesale cost by default.
    cost_inc_gst = cost_ex_gst * 1.10
    freight = estimated_freight(weight)
    min_margin = rules["min_margin_rate"]
    min_contrib = rules["min_contribution"]
    return_allow = rules["returns_allowance"]

    # Solve approximately for a retail price satisfying both contribution and margin.
    # contribution = price - cost - freight - payment fee - returns allowance
    # payment fee = rate*price + fixed
    # returns allowance = rate*price
    denom = 1 - PAYMENT_FEE_RATE - return_allow
    p1 = (cost_inc_gst + freight + PAYMENT_FEE_FIXED + min_contrib) / denom
    p2 = (cost_inc_gst + freight + PAYMENT_FEE_FIXED) / max(0.01, (1 - PAYMENT_FEE_RATE - return_allow - min_margin))
    proposed = round_price(max(p1,p2))

    payment_fee = proposed*PAYMENT_FEE_RATE + PAYMENT_FEE_FIXED
    returns_allow = proposed*return_allow
    contribution = proposed - cost_inc_gst - freight - payment_fee - returns_allow
    margin_rate = contribution / proposed if proposed else 0
    passes = contribution >= min_contrib and margin_rate >= min_margin

    return {
        "cost_inc_gst": round(cost_inc_gst,2),
        "freight_allowance": round(freight,2),
        "payment_fee_allowance": round(payment_fee,2),
        "returns_allowance": round(returns_allow,2),
        "proposed_price": round(proposed,2),
        "contribution": round(contribution,2),
        "margin_rate": round(margin_rate,4),
        "passes_margin": passes,
    }

@app.get("/")
async def root():
    return {"service":APP_NAME,"version":"3.0.0","dry_run":DRY_RUN,
            "next":["/health","/shopify/test","/southern/login-test","/southern/commercial-preview?limit=50"]}

@app.get("/health")
async def health():
    return {"ok":True,"version":"3.0.0","dry_run":DRY_RUN}

@app.get("/shopify/test")
async def shopify_test():
    q = """query { shop { name myshopifyDomain } locations(first:10){nodes{id name isActive}} products(first:3){nodes{id title handle status}} }"""
    return {"ok":True,"shopify":await shopify_graphql(q)}

@app.get("/southern/login-test")
async def southern_login_test():
    c = await southern_authenticated_client()
    try:
        r = await c.get(SOUTHERN_PRICING_SAMPLE_URL)
        return {"ok":True,"logged_in":"login to view prices" not in r.text.lower(),"credentials_present":True,"url":str(r.url)}
    finally:
        await c.aclose()

@app.get("/southern/commercial-preview")
async def commercial_preview(limit:int=50, url:str|None=None):
    if not DRY_RUN:
        raise HTTPException(400,"Commercial preview is only available while DRY_RUN=true")
    target = url or SOUTHERN_PRICING_SAMPLE_URL
    if not target.startswith("https://www.southernpetsupplies.com.au/"):
        raise HTTPException(400,"Southern URL only")

    # Parallel fetch public feeds.
    stock_pair, gtin_pair = await __import__("asyncio").gather(
        fetch_csv_or_discover(SOUTHERN_STOCK_URL),
        fetch_csv_or_discover(SOUTHERN_GTIN_URL)
    )
    _, stock_text = stock_pair
    _, gtin_text = gtin_pair
    _, stock_rows = parse_all_csv(stock_text)
    _, gtin_rows = parse_all_csv(gtin_text)
    stocks = stock_index(stock_rows)
    gtins = gtin_index(gtin_rows)

    client = await southern_authenticated_client()
    try:
        r = await client.get(target)
        if r.status_code >= 400:
            raise HTTPException(r.status_code, "Southern pricing page failed")
        priced = parse_southern_products(r.text, min(max(limit,1),100))
    finally:
        await client.aclose()

    out=[]
    for p in priced:
        sku = p["sku"]
        meta = gtins.get(sku,{})
        stock = stocks.get(sku,0)
        title = meta.get("product_name") or p["title"]
        weight = meta.get("weight") or p.get("page_weight")
        rules = category_rules(title or "")
        econ = commercial_price(float(p["active_cost_ex_gst"]), weight, rules)
        status = "LIVE" if stock > 0 and econ["passes_margin"] else "OUT_OF_STOCK"
        reason = []
        if stock <= 0: reason.append("supplier_stock")
        if not econ["passes_margin"]: reason.append("margin_hold")
        out.append({
            "sku":sku,
            "product":title,
            "southern_cost_ex_gst":p["active_cost_ex_gst"],
            "former_cost_ex_gst":p.get("former_cost_ex_gst"),
            "stock":stock,
            "gtin":meta.get("gtin"),
            "weight":weight,
            "category_rule":rules["category"],
            "min_margin_rate":rules["min_margin_rate"],
            "min_contribution":rules["min_contribution"],
            **econ,
            "status":status,
            "hold_reason":reason,
        })

    return {
        "ok":True,
        "dry_run":True,
        "count":len(out),
        "rows":out,
        "assumptions":{
            "payment_fee_rate":PAYMENT_FEE_RATE,
            "payment_fee_fixed":PAYMENT_FEE_FIXED,
            "default_min_margin_rate":DEFAULT_MIN_MARGIN_RATE,
            "default_min_contribution":DEFAULT_MIN_CONTRIBUTION,
            "price_round_suffix":PRICE_ROUND_SUFFIX
        },
        "note":"Commercial preview only. No Shopify changes and no Southern orders are made."
    }
