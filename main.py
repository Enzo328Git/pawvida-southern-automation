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
DRY_RUN = os.getenv("DRY_RUN", "true").lower() in {"1","true","yes","on"}

app = FastAPI(title=APP_NAME, version="1.0.0")
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
    headers = {"User-Agent": "PawVidaAutomation/1.0"}
    async with httpx.AsyncClient(timeout=45, follow_redirects=True, headers=headers) as client:
        r = await client.get(source_url)
        r.raise_for_status()
        ctype = r.headers.get("content-type", "").lower()
        text = r.text
        # Direct CSV / TSV response
        if "csv" in ctype or source_url.lower().endswith(".csv"):
            return str(r.url), text
        # Heuristic: CSV-looking content
        first = text[:1000]
        if "<html" not in first.lower() and ("," in first or "\t" in first):
            return str(r.url), text
        # Otherwise discover a linked CSV from the page
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

@app.get("/")
async def root():
    return {
        "service": APP_NAME,
        "status": "running",
        "dry_run": DRY_RUN,
        "shop": shop_domain(),
        "api_version": SHOPIFY_API_VERSION,
        "next": ["/health", "/shopify/test", "/southern/stock-preview", "/southern/gtin-preview"]
    }

@app.get("/health")
async def health():
    return {"ok": True, "dry_run": DRY_RUN}

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
    raise HTTPException(501, "Live stock write is locked in v1 until Southern column mapping and Shopify location are verified.")
