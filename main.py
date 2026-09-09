
import csv, io, os, time, re, math
from typing import Any
from urllib.parse import urljoin
import httpx
from bs4 import BeautifulSoup
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse

APP_NAME = "PawVida Southern Automation"
VERSION = "4.0.0"

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

# Optional market benchmark CSV:
# SKU,MarketPrice
MARKET_REFERENCE_URL = os.getenv("MARKET_REFERENCE_URL", "")
MARKET_PRICE_TOLERANCE = float(os.getenv("MARKET_PRICE_TOLERANCE", "0.03"))

DRY_RUN = os.getenv("DRY_RUN", "true").lower() in {"1","true","yes","on"}

GST = 0.10
PAYMENT_FEE_RATE = 0.024
PAYMENT_FEE_FIXED = 0.30
FREE_SHIPPING_THRESHOLD = float(os.getenv("FREE_SHIPPING_THRESHOLD", "99"))
MAX_FREIGHT_SUBSIDY = float(os.getenv("MAX_FREIGHT_SUBSIDY", "6"))
DEFAULT_CUSTOMER_SHIPPING = float(os.getenv("DEFAULT_CUSTOMER_SHIPPING", "9.95"))

app = FastAPI(title=APP_NAME, version=VERSION)
_token_cache: dict[str, Any] = {"token": None, "expires_at": 0}

def shop_domain():
    s = SHOPIFY_SHOP.strip().replace("https://","").replace("http://","").rstrip("/")
    if not s.endswith(".myshopify.com"):
        s += ".myshopify.com"
    return s

async def get_shopify_token():
    if not SHOPIFY_CLIENT_ID or not SHOPIFY_CLIENT_SECRET:
        raise HTTPException(500, "Shopify credentials are not configured")
    if _token_cache["token"] and time.time() < _token_cache["expires_at"] - 300:
        return _token_cache["token"]
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(
            f"https://{shop_domain()}/admin/oauth/access_token",
            data={
                "grant_type":"client_credentials",
                "client_id":SHOPIFY_CLIENT_ID,
                "client_secret":SHOPIFY_CLIENT_SECRET
            }
        )
    if r.status_code >= 400:
        raise HTTPException(r.status_code, f"Shopify token request failed: {r.text[:400]}")
    d = r.json()
    _token_cache["token"] = d["access_token"]
    _token_cache["expires_at"] = time.time() + int(d.get("expires_in", 86399))
    return d["access_token"]

async def shopify_graphql(query, variables=None):
    token = await get_shopify_token()
    async with httpx.AsyncClient(timeout=45) as client:
        r = await client.post(
            f"https://{shop_domain()}/admin/api/{SHOPIFY_API_VERSION}/graphql.json",
            headers={"X-Shopify-Access-Token":token,"Content-Type":"application/json"},
            json={"query":query,"variables":variables or {}}
        )
    if r.status_code >= 400:
        raise HTTPException(r.status_code, r.text[:800])
    d=r.json()
    if d.get("errors"):
        raise HTTPException(502, str(d["errors"]))
    return d.get("data")

async def fetch_csv_or_discover(url):
    headers={"User-Agent":"PawVidaAutomation/4.0"}
    async with httpx.AsyncClient(timeout=45,follow_redirects=True,headers=headers) as client:
        r=await client.get(url)
        r.raise_for_status()
        text=r.text
        ctype=r.headers.get("content-type","").lower()
        if "csv" in ctype or str(r.url).lower().endswith(".csv"):
            return str(r.url), text
        if "<html" not in text[:1000].lower() and ("," in text[:1000] or "\t" in text[:1000]):
            return str(r.url), text
        soup=BeautifulSoup(text,"html.parser")
        for a in soup.find_all("a",href=True):
            href=urljoin(str(r.url),a["href"])
            label=" ".join(a.stripped_strings).lower()
            if ".csv" in href.lower() or "csv" in label or "spreadsheet" in label:
                rr=await client.get(href)
                if rr.status_code<400:
                    return str(rr.url),rr.text
    raise HTTPException(502,f"Could not discover CSV at {url}")

def parse_csv(text):
    try:
        dialect=csv.Sniffer().sniff(text[:10000],delimiters=",;\t|")
    except Exception:
        dialect=csv.excel
    return list(csv.DictReader(io.StringIO(text),dialect=dialect))

SKU_RE = re.compile(r"^(?=.*\d)[A-Z0-9][A-Z0-9.\-_/]{3,24}$",re.I)
BAD_SKUS={"SKU","INFO","DESCRIPTION","PRICE","WEIGHT","QUANTITY"}

def valid_sku(s):
    if not s: return False
    s=s.strip()
    return s.upper() not in BAD_SKUS and bool(SKU_RE.match(s))

def clean_title(t, sku=""):
    t=re.sub(r"\s+"," ",t or "").strip()
    if sku and t.upper().startswith(sku.upper()):
        t=t[len(sku):].strip(" -|:")
    t=re.sub(r"\b(?:Original price was|Current price is)\b.*$","",t,flags=re.I)
    t=re.sub(r"\bAdd to cart\b.*$","",t,flags=re.I)
    t=re.sub(r"\bProduct Info\b.*$","",t,flags=re.I)
    t=re.sub(r"\s+\d+(?:\.\d+)?\s*kg\s+\d+(?:\.\d+)?\s*kg\s+\d+\s*$","",t,flags=re.I)
    t=re.sub(r"\s+\d+(?:\.\d+)?\s*kg\s+\d+\s*$","",t,flags=re.I)
    t=re.sub(r"\$\s*\d[\d,.]*","",t)
    return t.strip(" -|:")[:180]

def money_values(text):
    vals=[]
    for m in re.finditer(r"\$\s*([0-9][0-9,]*\.?[0-9]{0,2})",text):
        try: vals.append(float(m.group(1).replace(",","")))
        except: pass
    return vals

def first_match(patterns,text):
    for p in patterns:
        m=re.search(p,text,re.I)
        if m: return m.group(1).strip()
    return None

async def southern_client():
    if not SOUTHERN_USERNAME or not SOUTHERN_PASSWORD:
        raise HTTPException(500,"Southern credentials are not configured")
    client=httpx.AsyncClient(
        timeout=45,follow_redirects=True,
        headers={"User-Agent":"Mozilla/5.0 (compatible; PawVidaAutomation/4.0)"}
    )
    lp=await client.get(SOUTHERN_LOGIN_URL)
    soup=BeautifulSoup(lp.text,"html.parser")
    form=None
    for f in soup.find_all("form"):
        if f.find("input",{"name":"username"}) and f.find("input",{"name":"password"}):
            form=f; break
    if not form:
        await client.aclose(); raise HTTPException(502,"Southern login form not found")
    payload={}
    for inp in form.find_all("input"):
        if inp.get("name"):
            payload[inp["name"]]=inp.get("value","")
    payload["username"]=SOUTHERN_USERNAME
    payload["password"]=SOUTHERN_PASSWORD
    payload["login"]=payload.get("login") or "Log in"
    payload["rememberme"]="forever"
    await client.post(urljoin(str(lp.url),form.get("action") or SOUTHERN_LOGIN_URL),data=payload)
    test=await client.get(SOUTHERN_PRICING_SAMPLE_URL)
    if "login to view prices" in test.text.lower():
        await client.aclose(); raise HTTPException(401,"Southern login was not accepted")
    return client

def parse_products(html,limit=100):
    soup=BeautifulSoup(html,"html.parser")
    by={}
    for node in soup.select("li.product, tr, .product, .products > *"):
        text=" ".join(node.stripped_strings)
        if "$" not in text: continue
        sku=first_match([
            r"\bSKU\b\s*[:\-]?\s*([A-Z0-9][A-Z0-9.\-_/]+)",
            r"\b([A-Z]{1,8}\d[A-Z0-9.\-_/]{3,})\b"
        ],text)
        if not valid_sku(sku): continue
        vals=money_values(text)
        if not vals: continue
        active=vals[-1]
        former=vals[0] if len(vals)>1 and vals[0]!=vals[-1] else None
        heading=node.find(["h2","h3","h4"])
        title=" ".join(heading.stripped_strings) if heading else ""
        if not title:
            labels=[]
            for a in node.find_all("a",href=True):
                label=" ".join(a.stripped_strings)
                if len(label)>8 and "product info" not in label.lower() and "add to cart" not in label.lower():
                    labels.append(label)
            title=labels[0] if labels else text[:220]
        title=clean_title(title,sku)
        weight=first_match([r"\b([0-9.]+\s*kg)\b"],text)
        cand={"sku":sku,"title":title,"active_cost_ex_gst":round(active,2),
              "former_cost_ex_gst":round(former,2) if former else None,"page_weight":weight}
        prev=by.get(sku)
        score=sum(bool(cand.get(k)) for k in ["title","page_weight"])
        prevscore=sum(bool(prev.get(k)) for k in ["title","page_weight"]) if prev else -1
        if score>prevscore: by[sku]=cand
        if len(by)>=limit: break
    return list(by.values())

def stock_map(rows):
    out={}
    for r in rows:
        sku=str(r.get("SKU") or "").strip()
        if valid_sku(sku):
            try: out[sku]=int(float(str(r.get("Available") or 0)))
            except: out[sku]=0
    return out

def gtin_map(rows):
    out={}
    for r in rows:
        sku=str(r.get("SKU") or "").strip()
        if valid_sku(sku):
            out[sku]={
                "product_name":clean_title(str(r.get("Product Name") or ""),sku),
                "gtin":str(r.get("GTIN") or "").strip() or None,
                "weight":str(r.get("Weight") or "").strip() or None
            }
    return out

def market_map(rows):
    out={}
    for r in rows:
        sku=str(r.get("SKU") or "").strip()
        if not valid_sku(sku): continue
        raw=r.get("MarketPrice") or r.get("Market Price") or r.get("Price")
        try: out[sku]=float(str(raw).replace("$","").replace(",","").strip())
        except: pass
    return out

def weight_kg(v):
    if not v: return None
    m=re.search(r"([0-9.]+)",str(v))
    return float(m.group(1)) if m else None

def category_rule(name):
    n=name.lower()
    if any(x in n for x in ["coat","jacket","clothing","jumper"]):
        return ("clothing",0.28,15,0.03)
    if any(x in n for x in ["bed","bedding","mattress"]):
        return ("bedding",0.30,18,0.0075)
    if any(x in n for x in ["toy","kong","ball","chew"]):
        return ("toys",0.28,10,0.005)
    if any(x in n for x in ["lead","leash","harness","collar","walking"]):
        return ("walking",0.28,12,0.0075)
    if any(x in n for x in ["food","kibble","adult dog","puppy","cat food","diet"]):
        return ("mainstream_food",0.18,10,0.005)
    return ("general",0.24,10,0.005)

def freight_subsidy(w):
    if w is None:return 3
    if w<=1:return 2
    if w<=5:return 3.5
    if w<=10:return 4.5
    return MAX_FREIGHT_SUBSIDY

def round95(x):
    whole=math.floor(x)
    c=whole+0.95
    if c<x:c=whole+1.95
    return round(c,2)

def calc(cost_ex,w,name):
    rule,minmargin,mincontrib,returns_rate=category_rule(name)
    costinc=cost_ex*(1+GST)
    fs=freight_subsidy(w)
    p=max(costinc+1,9.95)
    chosen=None
    while p<costinc*2.5+150:
        pay=p*PAYMENT_FEE_RATE+PAYMENT_FEE_FIXED
        ret=p*returns_rate
        contrib=p-costinc-fs-pay-ret
        mr=contrib/p
        if contrib>=mincontrib and mr>=minmargin:
            chosen=p;break
        p+=0.05
    if chosen is None:chosen=costinc*1.5
    proposed=round95(chosen)
    pay=proposed*PAYMENT_FEE_RATE+PAYMENT_FEE_FIXED
    ret=proposed*returns_rate
    contrib=proposed-costinc-fs-pay-ret
    mr=contrib/proposed
    return {
        "category_rule":rule,"min_margin_rate":minmargin,"min_contribution":mincontrib,
        "cost_inc_gst":round(costinc,2),"freight_subsidy":round(fs,2),
        "payment_fee_allowance":round(pay,2),"returns_allowance":round(ret,2),
        "proposed_price":proposed,"contribution":round(contrib,2),"margin_rate":round(mr,4),
        "passes_margin":contrib>=mincontrib and mr>=minmargin
    }

async def joined_rows(limit=50):
    client=await southern_client()
    try:
        pr=await client.get(SOUTHERN_PRICING_SAMPLE_URL)
        prices=parse_products(pr.text,min(max(limit,1),100))
    finally:
        await client.aclose()

    _,st=await fetch_csv_or_discover(SOUTHERN_STOCK_URL)
    sm=stock_map(parse_csv(st))
    _,gt=await fetch_csv_or_discover(SOUTHERN_GTIN_URL)
    gm=gtin_map(parse_csv(gt))

    mm={}
    if MARKET_REFERENCE_URL:
        try:
            _,mt=await fetch_csv_or_discover(MARKET_REFERENCE_URL)
            mm=market_map(parse_csv(mt))
        except Exception:
            mm={}

    out=[]
    for p in prices:
        sku=p["sku"]; g=gm.get(sku,{})
        product=g.get("product_name") or p.get("title") or sku
        product=clean_title(product,sku)
        wt=g.get("weight") or p.get("page_weight")
        w=weight_kg(wt)
        stock=sm.get(sku,0)
        c=calc(float(p["active_cost_ex_gst"]),w,product)
        market=mm.get(sku)
        ceiling=round(market*(1+MARKET_PRICE_TOLERANCE),2) if market else None

        holds=[]
        if stock<=0: holds.append("supplier_stock")
        if not c["passes_margin"]: holds.append("margin")
        if market is None: holds.append("market_benchmark_missing")
        elif c["proposed_price"]>ceiling: holds.append("price_above_market")

        if "supplier_stock" in holds or "margin" in holds or "price_above_market" in holds:
            status="OUT_OF_STOCK"
        elif "market_benchmark_missing" in holds:
            status="REVIEW_MARKET"
        else:
            status="APPROVED_DRAFT"

        out.append({
            "sku":sku,"product":product,"southern_cost_ex_gst":p["active_cost_ex_gst"],
            "stock":stock,"gtin":g.get("gtin"),"weight":wt,
            **c,
            "market_price":market,"market_ceiling":ceiling,
            "status":status,"hold_reason":holds
        })
    return out

@app.get("/health")
async def health():
    return {"ok":True,"version":VERSION,"dry_run":DRY_RUN}

@app.get("/shopify/test")
async def shopify_test():
    q='query { shop { name myshopifyDomain } products(first:3){nodes{id title status}} }'
    return {"ok":True,"shopify":await shopify_graphql(q)}

@app.get("/southern/commercial-preview")
async def preview(limit:int=50):
    rows=await joined_rows(limit)
    return {"ok":True,"dry_run":DRY_RUN,"count":len(rows),"rows":rows,
            "market_reference_configured":bool(MARKET_REFERENCE_URL),
            "note":"No Shopify changes are made."}

@app.get("/southern/commercial-table",response_class=HTMLResponse)
async def table(limit:int=50):
    rows=await joined_rows(limit)
    trs=[]
    for r in rows:
        mp=f"${r['market_price']:.2f}" if r["market_price"] is not None else "—"
        mc=f"${r['market_ceiling']:.2f}" if r["market_ceiling"] is not None else "—"
        trs.append(
            f"<tr><td>{r['sku']}</td><td>{r['product']}</td><td>${r['southern_cost_ex_gst']:.2f}</td>"
            f"<td>{r['stock']}</td><td>{r['weight'] or ''}</td><td>${r['proposed_price']:.2f}</td>"
            f"<td>{mp}</td><td>{mc}</td><td>${r['contribution']:.2f}</td>"
            f"<td>{r['margin_rate']*100:.1f}%</td><td>{r['status']}</td></tr>"
        )
    html=f"""
    <html><head><title>PawVida Final Commercial Preview</title>
    <style>
    body{{font-family:Arial,sans-serif;margin:28px;color:#1e2d23}}
    table{{border-collapse:collapse;width:100%;font-size:13px}}
    th,td{{padding:9px;border-bottom:1px solid #ddd;text-align:left}}
    th{{background:#eef4ed;position:sticky;top:0}}
    .note{{background:#f5f6ef;padding:12px;border-radius:8px;margin:14px 0}}
    </style></head><body>
    <h1>PawVida — Final Commercial Preview</h1>
    <p>Dry run. No Shopify changes.</p>
    <div class="note">
    APPROVED_DRAFT requires stock + margin + verified market benchmark. REVIEW_MARKET means the economics pass,
    but no market benchmark has been supplied yet.
    </div>
    <table><thead><tr>
    <th>SKU</th><th>Product</th><th>Southern Cost ex GST</th><th>Stock</th><th>Weight</th>
    <th>Required PawVida Price</th><th>Market Price</th><th>Market Ceiling</th>
    <th>Contribution</th><th>Margin</th><th>Status</th>
    </tr></thead><tbody>{''.join(trs)}</tbody></table>
    </body></html>
    """
    return HTMLResponse(html)
