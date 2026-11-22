#!/usr/bin/env python3
"""Car Hunter CZ: finds underpriced, low-mileage, liquid used cars on Sauto.cz and Bazos.cz.

Market price = median asking price of comparable Sauto listings (same model, similar
year and mileage, same fuel/gearbox when there are enough of them). A listing is a deal
when it is well below that median. Hot seller phrases (rychlé jednání, dědictví, ...)
raise the score; red flags (na díly, havarované, cena při financování, ...) drop it.

Usage: python3 car_hunter.py [--config config.json] [--out out] [--seen seen.json]
Only the Python standard library is used.
"""
import argparse
import datetime as dt
import gzip
import http.client
import html
import json
import re
import statistics
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request

UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
SAUTO_SEARCH = "https://www.sauto.cz/api/v1/items/search"
SAUTO_ITEM = "https://www.sauto.cz/api/v1/items/{id}"
BAZOS = "https://auto.bazos.cz"
NOW = dt.datetime.now()
YEAR_NOW = NOW.year

# ---------------------------------------------------------------- text helpers

def norm(text):
    """Lowercase, strip Czech diacritics, collapse whitespace."""
    text = unicodedata.normalize("NFKD", text or "")
    text = "".join(c for c in text if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", text.replace("\xa0", " ")).lower().strip()


VIN_RE = re.compile(r"(?<![A-Z0-9])([A-HJ-NPR-Z0-9]{17})(?![A-Z0-9])")


def find_vin(text):
    for m in VIN_RE.finditer((text or "").upper()):
        v = m.group(1)
        if re.search(r"[A-Z]", v) and re.search(r"\d", v):
            return v
    return None


def strip_tags(s):
    s = re.sub(r"<br\s*/?>", "\n", s or "", flags=re.I)
    return html.unescape(re.sub(r"<[^>]+>", " ", s)).strip()


# Hot phrases: label -> regex on normalized text.
HOT = [
    ("Při rychlém jednání sleva", r"(pri )?rychl\w* jednan\w*.{0,40}slev|slev\w*.{0,40}rychl\w* jednan"),
    ("Rychlé jednání", r"rychl\w* jednan\w*|rychle jednajic"),
    ("Potřebuji prodat", r"potrebuj\w* (rychle )?prodat|musim (rychle )?prodat|nutn\w* (rychle )?prodat|rychle prodam|spech\w*|nutny prodej|rychly prodej"),
    ("Dohoda možná", r"dohoda (je )?(mozna|jista)|mozna dohoda|cena dohodou|cena k (jednani|diskuzi|domluve)|dohodneme se|domluva (je )?mozna|sleva mozna|mozna sleva|o cene (lze|se da) (jednat|diskutovat)|dohoda na miste"),
    ("Rodinné auto", r"rodinn\w* (auto|vuz|vozidlo|automobil)"),
    ("Dědictví", r"dedictv|zdedil|zdeden|pozustalost|po (zemrel|zesnul)|po (dedeckovi|dedovi|tatinkovi|babicce|otci)\b|z pozustalosti"),
    ("Переезд / новое авто", r"stehovan|odjezd\w* do zahranici|odjizdim|odjizdime|koupil\w* (jsme |jsem )?nov\w* (auto|vuz)|z duvodu koupe nove"),
]
HOT_RE = [(label, re.compile(rx)) for label, rx in HOT]

# Red flags that exclude a listing. A negation right before ("ne", "nikdy", "bez") cancels it.
RED = [
    ("на запчасти", r"na dily|rozprodam na dily"),
    ("не на ходу", r"nepojizdn|nejede\b|nestartuje|nenastartuje"),
    ("битая", r"havarovan|bouran|po havarii|po nehode|totalk|poskozen\w* (karoserie|predek|zadek|bok)"),
    ("проблема с мотором", r"vadn\w* motor|motor (je )?(ko|na opravu|vadny|odesel)|bez motoru|zadren|klepe motor|poskozen\w* motor|vadn\w* prevodovk|prevodovka (je )?(ko|vadna)"),
    ("цена только при кредите", r"(cena|plati) (plati )?(pri|s|v pripade) (financovan|uver|leasing)|pri financovani|s financovanim|cena pri uveru|cena na uver|akontac|na splatky bez"),
    ("лизинг/аренда", r"prevzeti leasingu|odkup leasingu|pronajem|operativni leasing"),
]
RED_RE = [(label, re.compile(rx)) for label, rx in RED]
TITLE_RED = re.compile(r"\b(koupim|vykup|vykoupim|pronajem|pronajmu|vymenim|na dily|dily|nahradni)\b")
WARN = [
    ("без STK (техосмотра)", r"bez stk|stk propadla|propadl\w* stk"),
    ("требует ремонта", r"nutn\w* oprav|potrebuje oprav|potreba oprav|sviti kontrolka|kontrolka motoru|chyba dpf|vymenit spojk|nutna vymena"),
    ("импорт без истории?", r"dovoz bez|bez servisni (knizky|historie)"),
]
WARN_RE = [(label, re.compile(rx)) for label, rx in WARN]
NEGATION_BEFORE = re.compile(r"(\bne|nikdy|bez|nebyl[oa]?|nebyly|zadn[ey]|zadna|neni|nemel[oa]?)\s*$")


def find_flags(text, rules):
    t = norm(text)
    out = []
    for label, rx in rules:
        for m in rx.finditer(t):
            before = t[max(0, m.start() - 22):m.start()]
            if t[max(0, m.start() - 2):m.start()] == "ne" or NEGATION_BEFORE.search(before):
                continue
            out.append(label)
            break
    return out


def hot_keywords(text):
    t = norm(text)
    found = [label for label, rx in HOT_RE if rx.search(t)]
    if "Při rychlém jednání sleva" in found and "Rychlé jednání" in found:
        found.remove("Rychlé jednání")
    return found


def fuel_group(seo_or_text):
    t = norm(seo_or_text)
    if re.search(r"hybrid|plug-in|\bphev\b|\bhev\b", t):
        return "hybrid"
    if re.search(r"^elektro$|elektromobil|\belektro\b|e-golf|\bbev\b|elektricky pohon|cisty elektr", t):
        return "elektro"
    if re.search(r"nafta|diesel|(?<![a-z])(tdi|crdi|dci|hdi|tdci|d-4d|d4d|cdti|crd|multijet|bluehdi|skyactiv-d)\b", t):
        return "nafta"
    if re.search(r"benzin|(?<![a-z])(tsi|tfsi|mpi|fsi|gdi|t-gdi|htp|vvt|vvt-i|lpg|cng|tce|cvvt|skyactiv-g)\b|ecoboost|\d[.,]\di\b", t):
        return "benzin"
    return None


ENGINE_RE = re.compile(r"(?<![\d.,])([0-4])[.,]([0-9])(?![\d.,])\s*(?:l\b|tsi|tfsi|tdi|mpi|fsi|crdi|cdti|tdci|t-gdi|gdi|dci|hdi|vvt|ecoboost|d-4d|i\b|t\b|benz|nafta|diesel|hybrid|skyactiv|\d{2,3}\s*kw|\s|,|$)")


def engine_of(text):
    m = ENGINE_RE.search(norm(text))
    if not m or m.group(1) == "0":
        return None
    return f"{m.group(1)}.{m.group(2)}"


def generation(year, bounds):
    """Index of the model generation for a year; boundary years are ambiguous."""
    return sum(1 for b in bounds if year >= b), year in bounds


# ---------------------------------------------------------------- http

class Http:
    def __init__(self, delay):
        self.delay = delay
        self.count = 0
        self.errors = 0

    def get(self, url, as_json=False):
        for attempt in range(3):
            try:
                req = urllib.request.Request(url, headers={
                    "User-Agent": UA,
                    "Accept": "application/json" if as_json else "text/html,application/xhtml+xml",
                    "Accept-Language": "cs-CZ,cs;q=0.9",
                    "Accept-Encoding": "gzip",
                })
                with urllib.request.urlopen(req, timeout=30) as r:
                    raw = r.read()
                    if r.headers.get("Content-Encoding") == "gzip":
                        raw = gzip.decompress(raw)
                self.count += 1
                time.sleep(self.delay)
                text = raw.decode("utf-8", errors="replace")
                return json.loads(text) if as_json else text
            except (urllib.error.URLError, http.client.HTTPException, OSError, json.JSONDecodeError, ValueError) as e:
                if isinstance(e, urllib.error.HTTPError) and e.code in (404, 410):
                    return None  # page past the last one, or a removed ad: not an error
                if attempt == 2:
                    self.errors += 1
                    print(f"  ! {url[:90]}: {e}", file=sys.stderr)
                    return None
                time.sleep(2 * (attempt + 1))


# ---------------------------------------------------------------- model matching

PERF_RE = re.compile(r"\b(rs|vrs|st|gti|gtd|gte|r32|cupra|n performance|type r|amg)\b(?![- ]?line)|\bgolf r\b|\bi30 n\b|\bceed gt\b(?![- ]?line)")


def alnum(text):
    return re.sub(r"[^a-z0-9]", "", norm(text))


def model_token(model):
    return alnum(model["bazos"][1])


def match_model(title, models):
    """The configured model whose name appears in the title (longest token wins)."""
    t = alnum(title)
    hits = [m for m in models if model_token(m) in t]
    return max(hits, key=lambda m: len(model_token(m))) if hits else None


# ---------------------------------------------------------------- sauto

def sauto_search(http, params, max_items=12000, status=None):
    """All listings for the query. status (dict) gets "complete": every page arrived."""
    offset, total, items = 0, None, []
    if status is not None:
        status["complete"] = False
    while total is None or offset < total:
        q = urllib.parse.urlencode(dict(params, limit=500, offset=offset, category_id=838,
                                        operating_lease="false", condition_seo="ojete"))
        data = http.get(f"{SAUTO_SEARCH}?{q}", as_json=True)
        if not data:
            break
        total = min(data.get("pagination", {}).get("total", 0), max_items)
        page = data.get("results") or []
        if not page:
            break
        items += page
        offset += len(page)
    if status is not None:
        status["complete"] = total is not None and offset >= total
    return items


def sauto_row(it, model):
    date = it.get("manufacturing_date") or it.get("in_operation_date") or ""
    if not date[:4].isdigit() or not it.get("price") or it.get("price_by_agreement"):
        return None
    name = it.get("name", "")
    if model_token(model) not in alnum(name):
        return None  # listed under the wrong model (e.g. a Kia Rio filed as Ceed)
    man = (it.get("manufacturer_cb") or {}).get("seo_name", "")
    mod = (it.get("model_cb") or {}).get("seo_name", "")
    img = (it.get("images") or [{}])[0].get("url")
    loc = it.get("locality") or {}
    premise = it.get("premise")
    full = name + " " + (it.get("additional_model_name") or "")
    return {
        "source": "sauto",
        "id": f"sauto:{it['id']}",
        "raw_id": it["id"],
        "url": f"https://www.sauto.cz/osobni/detail/{man}/{mod}/{it['id']}",
        "title": name,
        "model": model["name"],
        "tier": model.get("tier", 2),
        "year": int(date[:4]),
        "km": int(it.get("tachometer") or 0),
        "fuel": fuel_group((it.get("fuel_cb") or {}).get("seo_name", "")) or "benzin",
        "gearbox": "automat" if "autom" in norm((it.get("gearbox_cb") or {}).get("seo_name", "")) else "manual",
        "engine": engine_of(full),
        "perf": bool(PERF_RE.search(norm(full))),
        "price": int(it["price"]),
        "private": not premise,
        "seller": (premise or {}).get("name") if premise else "частник",
        "location": loc.get("municipality") or loc.get("district") or loc.get("region") or "",
        "region": loc.get("region") or "",
        "created": (it.get("create_date") or "")[:16],
        "image": ("https:" + img) if img and img.startswith("//") else img,
        "text": full,
        "kw_query": [],
        "cebia_report": bool(it.get("is_cebia_smart_code_url_verified")),
    }


def sauto_fetch_model(http, model, price_cap):
    """Rows of every Sauto listing of the model, and whether all pages arrived."""
    out, seen, complete = [], set(), True
    for seo in model["sauto"]:
        st = {}
        for it in sauto_search(http, {"manufacturer_model_seo": seo, "price_to": price_cap}, status=st):
            row = sauto_row(it, model)
            if row and row["id"] not in seen:  # pages can overlap when new ads arrive mid-fetch
                seen.add(row["id"])
                out.append(row)
        complete = complete and st.get("complete", False)
    return out, complete


def sauto_phrase_sweep(http, phrase, cfg, seo_map):
    """Full-text search across the whole market; keeps listings of the configured models."""
    out = []
    for it in sauto_search(http, {"phrase": phrase, "price_from": cfg["price_min"], "price_to": cfg["price_max"]}, 2000):
        key = f"{(it.get('manufacturer_cb') or {}).get('seo_name', '')}:{(it.get('model_cb') or {}).get('seo_name', '')}"
        model = seo_map.get(key)
        row = sauto_row(it, model) if model else None
        if row:
            row["kw_query"] = [phrase]
            out.append(row)
    return out


def sauto_enrich(http, row):
    data = http.get(SAUTO_ITEM.format(id=row["raw_id"]), as_json=True)
    r = (data or {}).get("result") or {}
    if not r:
        return False
    row["description"] = (r.get("description") or "")[:4000]
    row["text"] += " " + row["description"]
    row["crashed_in_past"] = bool(r.get("crashed_in_past"))
    row["service_book"] = r.get("service_book")
    row["vin"] = r.get("vin") or find_vin(row["description"])
    row["power_kw"] = r.get("engine_power")
    if r.get("engine_volume") and not row.get("engine"):
        row["engine"] = f"{round(r['engine_volume'] / 1000, 1):.1f}"
    row["stk"] = (r.get("stk_date") or "")[:7]
    row["origin"] = (r.get("country_of_origin_cb") or {}).get("name")
    return True


# ---------------------------------------------------------------- bazos

DEALER_RE = re.compile(r"autobazar|s\.r\.o|odpocet dph|\bdph\b|financovan|leasing|autosalon|zaruka \d+ mes|protiucet|\bico\b")


def parse_price(s):
    digits = re.sub(r"[^\d]", "", s or "")
    return int(digits) if digits and "kč" in (s or "").lower() else None


def bazos_page(http, path, query, cfg, models):
    """Parses one Bazos results page into rows for the configured models."""
    q = urllib.parse.urlencode({"hledat": query, "cenaod": cfg["price_min"], "cenado": cfg["price_max"], "order": ""})
    page_html = http.get(f"{BAZOS}{path}?{q}")
    rows = []
    if not page_html:
        return rows, False
    blocks = page_html.split('<div class="inzeraty inzeratyflex">')[1:]
    for b in blocks:
        m = re.search(r'<h2 class=nadpis><a href="(/inzerat/(\d+)/[^"]+)">(.*?)</a>', b, re.S)
        if not m:
            continue
        title = strip_tags(m.group(3))
        model = match_model(title, models)
        if not model or TITLE_RED.search(norm(title)):
            continue
        price = parse_price(strip_tags((re.search(r'<div class="inzeratycena">(.*?)</div>', b, re.S) or [None, ""])[1]))
        if not price:
            continue
        snippet = strip_tags((re.search(r"<div class=popis>(.*?)</div>", b, re.S) or [None, ""])[1])
        date = re.search(r"\[(\d{1,2})\.(\d{1,2})\.\s*(\d{4})\]", b)
        img = re.search(r'<img src="([^"]+)"', b)
        loc = re.search(r'<div class="inzeratylok">(.*?)<br>', b, re.S)
        text = f"{title}\n{snippet}"
        rows.append({
            "source": "bazos",
            "id": f"bazos:{m.group(2)}",
            "raw_id": m.group(2),
            "url": BAZOS + m.group(1),
            "title": title,
            "model": model["name"],
            "tier": model.get("tier", 2),
            "year": parse_year(title, snippet),
            "km": parse_km(title, snippet),
            "fuel": fuel_group(title) or fuel_group(snippet),
            "gearbox": "automat" if re.search(r"automat|\bdsg\b", norm(text)) else None,
            "engine": engine_of(title) or engine_of(snippet),
            "perf": bool(PERF_RE.search(norm(title))),
            "price": price,
            "private": not DEALER_RE.search(norm(text)),
            "seller": None,
            "location": strip_tags(loc.group(1)) if loc else "",
            "region": "",
            "created": f"{date.group(3)}-{int(date.group(2)):02d}-{int(date.group(1)):02d}" if date else "",
            "image": img.group(1) if img else None,
            "text": text,
            "snippet_only": True,
            "kw_query": [],
        })
    return rows, len(blocks) >= 20


def bazos_fetch_model(http, model, cfg, pages=None, band=None):
    cat, query = model["bazos"]
    rows = []
    page_cfg = dict(cfg, price_min=band[0], price_max=band[1]) if band else cfg
    for page in range(pages or cfg["bazos_pages"]):
        got, more = bazos_page(http, f"/{cat}/" + (f"{page * 40}/" if page else ""), query, page_cfg, [model])
        rows += got
        if not more:
            break
    return rows


def bazos_phrase_sweep(http, phrase, cfg, models):
    rows = []
    for page in range(cfg.get("bazos_phrase_pages", 2)):
        got, more = bazos_page(http, "/" + (f"{page * 40}/" if page else ""), phrase, cfg, models)
        for r in got:
            r["kw_query"] = [phrase]
        rows += got
        if not more:
            break
    return rows


def bazos_enrich(http, row):
    page = http.get(row["url"])
    if not page:
        return False
    m = re.search(r"<div class=popisdetail>(.*?)</div>", page, re.S)
    desc = strip_tags(m.group(1)) if m else ""
    row["description"] = desc[:4000]
    row["text"] = f"{row['title']}\n{desc}"
    row["snippet_only"] = False
    row["year"] = parse_year(row["title"], desc) or row["year"]
    row["km"] = parse_km(row["title"], desc) or row["km"]
    row["fuel"] = row["fuel"] or fuel_group(row["title"]) or fuel_group(desc)
    row["engine"] = row.get("engine") or engine_of(desc)
    row["perf"] = row.get("perf") or bool(PERF_RE.search(norm(desc[:200])))
    if not row.get("gearbox") and re.search(r"automat|\bdsg\b", norm(desc)):
        row["gearbox"] = "automat"
    row["private"] = not DEALER_RE.search(norm(row["text"]))
    row["vin"] = find_vin(desc)
    name = re.search(r"Jméno:.*?<b>(.*?)</b>", page, re.S) or re.search(r"Jméno:</td><td[^>]*>(.*?)</td>", page, re.S)
    if name:
        row["seller"] = strip_tags(name.group(1))[:60]
    return True


YEAR_EXPLICIT = re.compile(r"(?:r\.?\s?v\.?|rok(?: vyroby)?|vyroba|vyrobeno|registrace|1\.\s?reg\w*|uvedeni do provozu|model)\s*:?\s*(?:\d{1,2}\s*[/.]\s*)?((?:19|20)\d{2})")
YEAR_ANY = re.compile(r"(?<![\d/.,])((?:199\d|20[0-3]\d))(?![\d.,]*\s*(?:km|kc|,-))")


def parse_year(title, desc):
    t, d = norm(title), norm(desc)
    for txt in (t, d):
        m = YEAR_EXPLICIT.search(txt)
        if m and 1995 <= int(m.group(1)) <= YEAR_NOW:
            return int(m.group(1))
    ys = [int(y) for y in YEAR_ANY.findall(t) if 1995 <= int(y) <= YEAR_NOW]
    if ys:
        return min(ys)
    ys = [int(y) for y in YEAR_ANY.findall(d[:600]) if 1995 <= int(y) <= YEAR_NOW]
    return min(ys) if ys else None


KM_PATTERNS = [
    re.compile(r"(\d{1,3}(?:[ .]\d{3})+|\d{4,6})\s*km\b"),
    re.compile(r"(\d{2,3})\s*(?:tkm|tis\.?\s*km|tisic km|xxx\s*km|k\s*km)\b"),
    re.compile(r"(\d{2,3})\s*(?:tis\b|tis\.)"),
]
KM_CONTEXT = re.compile(r"(?:najeto|najezd|stav (?:tachometru|km)|tachometr|km)\s*:?\s*(?:pouze |jen |cca |realne |realnych )?(\d{1,3}(?:[ .]\d{3})+|\d{4,6}|\d{2,3}\s*(?:tkm|tis|xxx))")


def _km_value(s):
    s = s.strip()
    if re.search(r"tkm|tis", s) or (len(re.sub(r"\D", "", s)) <= 3):
        n = int(re.sub(r"\D", "", s)) * 1000
    else:
        n = int(re.sub(r"\D", "", s))
    return n if 1000 <= n <= 600000 else None


def parse_km(title, desc):
    t, d = norm(title), norm(desc)
    for txt in (t, d):
        m = KM_CONTEXT.search(txt)
        if m:
            v = _km_value(m.group(1))
            if v:
                return v
        for i, rx in enumerate(KM_PATTERNS):
            for m in rx.finditer(txt):
                v = _km_value(m.group(1) + (" tkm" if i else ""))
                if v:
                    return v
    return None


# ---------------------------------------------------------------- market estimate

TIERS = [
    # label, years ±, km window, same fuel, same gearbox, same engine
    ("двигатель", 1, lambda k: max(25000, 0.20 * k), True, True, True),
    ("топливо+КПП", 1, lambda k: max(25000, 0.20 * k), True, True, False),
    ("топливо", 1, lambda k: max(35000, 0.30 * k), True, False, False),
    ("модель", 2, lambda k: max(50000, 0.40 * k), True, False, False),
]
MARKET_NEED = {"двигатель": 6, "топливо+КПП": 8, "топливо": 10, "модель": 12}


def select_comps(row, pool, bounds, need):
    """Comparable listings from the tightest tier that has enough of them.
    need: {tier label: minimum count}. Returns (comps, basis text) or ([], "")."""
    y, k = row["year"], row["km"]
    gen, ambiguous = generation(y, bounds)
    # First year of a new generation: both generations were sold that year, so compare
    # with the older one only (cheaper, i.e. a conservative estimate).
    years = {y - 2, y - 1} if ambiguous else None

    def same_gen(c):
        if years is not None:
            return c["year"] in years
        cg, camb = generation(c["year"], bounds)
        return cg == gen and not camb

    for label, dy, kmw, same_fuel, same_gear, same_engine in TIERS:
        if same_engine and not row.get("engine"):
            continue
        dk = kmw(k)
        comps = [c for c in pool
                 if c["id"] != row["id"] and same_gen(c)
                 and (years is not None or abs(c["year"] - y) <= dy) and abs(c["km"] - k) <= dk
                 and (not same_fuel or not row["fuel"] or c.get("fuel") == row["fuel"])
                 and (not same_gear or not row["gearbox"] or c.get("gearbox") == row["gearbox"])
                 and (not same_engine or c.get("engine") == row["engine"])
                 and c.get("perf", False) == row.get("perf", False)]
        if len(comps) >= need.get(label, 99):
            yr = f"{min(years)}–{max(years)}" if years else f"{y - dy}–{y + dy}"
            return comps, f"{label}, {yr}, ±{int(dk / 1000)} тыс. км"
    return [], ""


def with_fuel_fallback(row, fn):
    """With the fuel unknown (some Bazos ads) use the cheapest fuel group's result:
    a petrol Yaris must not be priced against hybrids."""
    if row.get("fuel"):
        return fn(row)
    found = [fn(dict(row, fuel=f)) for f in ("benzin", "nafta", "hybrid")]
    found = [f for f in found if f]
    if not found:
        return None
    best = min(found, key=lambda f: f[0])
    return best[:-1] + (best[-1] + ", топливо не указано",)


def estimate_market(row, comps_by_model, gens_by_model):
    """Median asking price of comparable listings (Sauto + Bazos, dealers and private)."""
    pool, bounds = comps_by_model.get(row["model"], []), gens_by_model.get(row["model"], [])

    def fn(r):
        comps, basis = select_comps(r, pool, bounds, MARKET_NEED)
        return (int(statistics.median(c["price"] for c in comps)), len(comps), basis) if comps else None

    return with_fuel_fallback(row, fn) or (None, 0, "")


def quantile(values, q):
    v = sorted(values)
    if not v:
        return None
    i = (len(v) - 1) * q
    lo, hi = int(i), min(int(i) + 1, len(v) - 1)
    return v[lo] + (v[hi] - v[lo]) * (i - lo)


def estimate_resale(row, comps_by_model, sold_by_model, gens_by_model, cfg):
    """What the car realistically sells for as a private sale.
    1) "sold": last asking price of comparable Sauto ads that disappeared after 1–60 days
       (tracked by history.py) minus 3 % haggling — needs 5+ such sales;
    2) "private": asking prices of comparable private-seller ads (Sauto + Bazos):
       quick sale = lower quartile − 3 %, typical = median − 5 %;
    3) "estimate": market median × 0.82 / 0.88 when there is too little data."""
    bounds = gens_by_model.get(row["model"], [])
    need5 = {label: 5 for label, *_ in TIERS}

    def from_sold(r):
        comps, basis = select_comps(r, sold_by_model.get(r["model"], []), bounds, need5)
        if not comps:
            return None
        prices = [c["price"] for c in comps]
        days = statistics.median(c["days"] for c in comps)
        return (quantile(prices, 0.5) * 0.97, quantile(prices, 0.25) * 0.97, len(comps), int(days), basis)

    def from_private(r):
        private = [c for c in comps_by_model.get(r["model"], []) if c.get("private")]
        comps, basis = select_comps(r, private, bounds, need5)
        if not comps:
            return None
        prices = [c["price"] for c in comps]
        return (quantile(prices, 0.5) * 0.95, quantile(prices, 0.25) * 0.97, len(comps), None, basis)

    for kind, fn in (("sold", from_sold), ("private", from_private)):
        got = with_fuel_fallback(row, fn)
        if got:
            typical, quick, n, days, basis = got
            break
    else:
        kind, typical, quick, n, days, basis = "estimate", row["market"] * 0.88, row["market"] * 0.82, 0, None, ""
    quick = min(quick, typical)
    costs = cfg.get("resale_costs", 6000)
    r100 = lambda v: int(round(v / 100.0) * 100)
    return {
        "resale_basis": kind, "resale_n": n, "resale_days": days, "resale_comps_basis": basis,
        "resale_quick": r100(quick), "resale_typical": r100(typical),
        "profit_quick": r100(quick - row["price"] - costs), "profit_typical": r100(typical - row["price"] - costs),
    }


def load_sold(history_dir, max_days):
    """Comparable "sold" listings from history.py: ads that disappeared after 1..max_days days."""
    import os
    path = os.path.join(history_dir or "", "sold.json")
    if not history_dir or not os.path.exists(path):
        return {}, 0
    data = json.load(open(path, encoding="utf-8"))
    cols = data["columns"]
    by_model, n = {}, 0
    for item in data["items"]:
        d = dict(zip(cols, item))
        if not (1 <= d["days"] <= max_days) or d["price"] < 10000:
            continue
        d["id"] = "sold:" + str(d["id"])
        by_model.setdefault(d["model"], []).append(d)
        n += 1
    return by_model, n


# ---------------------------------------------------------------- scoring

def score(row, cfg):
    age = max(0.5, YEAR_NOW - row["year"] + (NOW.month - 6) / 12)
    kpy = row["km"] / age
    row["km_per_year"] = int(kpy)
    pts = row["discount"] * 100
    pts += max(-10, min(12, (cfg["typical_km_per_year"] - kpy) / 1000))
    pts += 6 * min(3, len(row["hot"]))
    pts += 8 if row["private"] else 0
    pts += 5 if row["tier"] == 1 else 2
    if row.get("crashed_in_past"):
        pts -= 10
    if row.get("chain_dealer"):
        pts -= 8
    if row["comps"] < 10 or row["comps_basis"].startswith("модель") or "топливо не указано" in row["comps_basis"]:
        pts -= 5  # rough market estimate
    pts -= 6 * len(row["warnings"])
    if "profit_typical" in row:
        pts += max(-6, min(8, row["profit_typical"] / 5000))
    if row["discount"] > 0.5:
        pts -= 12  # too good to be true: verify first
    row["score"] = round(pts, 1)


def offer_price(row):
    cut = 0.06 if row["discount"] >= 0.3 else 0.09
    if row["hot"]:
        cut += 0.03
    return int(round(row["price"] * (1 - cut) / 1000.0) * 1000)


def message_cz(row):
    return (f"Dobrý den, mám vážný zájem o Váš vůz {row['title']} ({row['year']}, {row['km']:,} km) "
            f"za {row['price']:,} Kč. Je ještě k dispozici? Mohu se přijet podívat a rozhodnout se hned na místě, "
            f"peníze mám připravené. Při rychlém jednání bych nabídl {row['offer']:,} Kč. "
            f"Můžete mi prosím poslat VIN a sdělit, zda má servisní knížku? Děkuji.").replace(",", " ")


# ---------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config.json")
    ap.add_argument("--out", default="out")
    ap.add_argument("--seen", help="JSON list of ids already in the feed (they skip the slow detail step)")
    ap.add_argument("--models", help="comma-separated subset of model names (debug)")
    ap.add_argument("--no-bazos", action="store_true")
    ap.add_argument("--no-phrases", action="store_true")
    ap.add_argument("--history", help="folder with history.py state (sold.json) for real sale prices")
    args = ap.parse_args()

    cfg = json.load(open(args.config, encoding="utf-8"))
    seen = {x.replace("-", ":", 1) for x in json.load(open(args.seen))} if args.seen else set()
    models = cfg["models"]
    if args.models:
        wanted = [w.strip().lower() for w in args.models.split(",")]
        models = [m for m in models if any(w in m["name"].lower() for w in wanted)]
    http = Http(cfg.get("request_delay_sec", 0.3))
    t0 = time.time()
    gens = {m["name"]: m.get("generations", []) for m in models}
    seo_map = {seo: m for m in models for seo in m["sauto"]}
    chains = [norm(c) for c in cfg.get("chain_dealers", [])]

    # 1. Market data (Sauto + Bazos) and newest Bazos ads, model by model.
    comps_by_model, pool, sauto_all, complete_models = {}, {}, [], []
    sold_by_model, sold_total = load_sold(args.history, cfg.get("sold_max_days", 60))

    def add(rows):
        for r in rows:
            if r["id"] in pool:
                pool[r["id"]]["kw_query"] = sorted(set(pool[r["id"]]["kw_query"] + r["kw_query"]))
            else:
                pool[r["id"]] = r

    for model in models:
        rows, complete = sauto_fetch_model(http, model, int(cfg["price_max"] * 2.5))
        sauto_all += rows
        if complete:
            complete_models.append(model["name"])
        ok = lambda r: r["price"] >= 10000 and (r["km"] >= 1000 or r["year"] >= YEAR_NOW - 1)
        comps_by_model[model["name"]] = [r for r in rows if ok(r)]
        bz = [] if args.no_bazos else bazos_fetch_model(
            http, model, cfg, pages=max(cfg["bazos_pages"], cfg.get("bazos_comp_pages", 0)),
            band=(int(cfg["price_min"] * 0.5), int(cfg["price_max"] * 2)))
        bz_comps = [r for r in bz if r["year"] and r["km"] and ok(r)]
        comps_by_model[model["name"]] += bz_comps
        print(f"{model['name']:<18} sauto {len(rows):>5}{'' if complete else ' (не полностью)'}  bazos {len(bz):>3} "
              f"(для сравнения {len(bz_comps)})  продажи {len(sold_by_model.get(model['name'], []))}", file=sys.stderr)
        add([r for r in comps_by_model[model["name"]] if r["source"] == "sauto" and cfg["price_min"] <= r["price"] <= cfg["price_max"]])
        add([r for r in bz if cfg["price_min"] <= r["price"] <= cfg["price_max"]])

    # 2. Hot phrases across the whole market.
    if not args.no_phrases:
        for ph in cfg.get("sauto_phrases", []):
            got = sauto_phrase_sweep(http, ph, cfg, seo_map)
            add(got)
            print(f"sauto  «{ph}»: {len(got)}", file=sys.stderr)
        if not args.no_bazos:
            for ph in cfg.get("bazos_phrases", []):
                got = bazos_phrase_sweep(http, ph, cfg, models)
                add(got)
                print(f"bazos  «{ph}»: {len(got)}", file=sys.stderr)
    candidates = list(pool.values())

    # 3. Bazos ads without year/km in the snippet: read the ad (newest first, capped, not already known).
    budget = cfg["max_detail_requests"]
    missing = [r for r in candidates if r["source"] == "bazos" and r["id"] not in seen and (r["year"] is None or r["km"] is None)]
    missing.sort(key=lambda r: (bool(r["kw_query"]), r["created"]), reverse=True)
    for r in missing[: budget // 3]:
        bazos_enrich(http, r)
        budget -= 1

    def eligible(r):
        return (r["year"] and r["km"] is not None and r["year"] >= cfg["year_min"]
                and r["km"] <= cfg["km_max"] and (r["km"] >= 1000 or r["year"] >= YEAR_NOW - 1)
                and cfg["price_min"] <= r["price"] <= cfg["price_max"])

    def price_it(r):
        market, n, basis = estimate_market(r, comps_by_model, gens)
        if market:
            r.update(market=market, comps=n, comps_basis=basis, discount=round(1 - r["price"] / market, 3))
        return bool(market)

    # 4. Price everything against the market; keep the plausible ones.
    pre = []
    for r in candidates:
        if not eligible(r) or not price_it(r):
            continue
        floor = cfg["min_discount_with_keywords"] if r["kw_query"] else cfg["min_discount"]
        if r["discount"] >= floor - 0.03:
            pre.append(r)

    # 5. Full descriptions (phrases and red flags live there) for the new ones, phrase hits first.
    pre.sort(key=lambda r: (bool(r["kw_query"]), r["discount"] + (0.1 if r["private"] else 0)), reverse=True)
    for r in pre:
        if budget <= 0:
            break
        if r["id"] in seen:
            continue
        if r["source"] == "sauto" and "description" not in r:
            sauto_enrich(http, r)
            budget -= 1
        elif r["source"] == "bazos" and r.get("snippet_only"):
            bazos_enrich(http, r)
            budget -= 1
            if eligible(r):
                price_it(r)

    # 6. Final verdict.
    deals, dropped = [], {}
    for r in pre:
        r["red_flags"] = find_flags(r["text"], RED_RE)
        if r["red_flags"]:
            for f in r["red_flags"]:
                dropped[f] = dropped.get(f, 0) + 1
            continue
        r["hot"] = hot_keywords(r["text"])
        if r["kw_query"] and not r["hot"] and r["source"] == "sauto" and "description" not in r:
            # Sauto's full-text search already matched the phrase in the (unread) description.
            r["hot"] = hot_keywords(" ".join(r["kw_query"]))
        r["warnings"] = find_flags(r["text"], WARN_RE)
        age = max(0.5, YEAR_NOW - r["year"] + (NOW.month - 6) / 12)
        if age >= 5 and r["km"] / age < 6000:
            r["warnings"].append("подозрительно малый пробег — проверить по VIN")
        r["chain_dealer"] = bool(r.get("seller") and any(c in norm(r["seller"]) for c in chains))
        if r["chain_dealer"]:
            r["warnings"].append("сетевой салон: уточните, не зависит ли цена от кредита/доп. услуг")
        threshold = cfg["min_discount_with_keywords"] if r["hot"] else cfg["min_discount"]
        if r["discount"] < threshold or r["discount"] > cfg.get("max_discount", 0.6):
            continue  # not cheap enough, or so cheap it is almost surely fake, a lease takeover or a wreck
        if r["discount"] > 0.5:
            r["warnings"].append("слишком дёшево — проверить на мошенничество и скрытые дефекты")
        r.update(estimate_resale(r, comps_by_model, sold_by_model, gens, cfg))
        score(r, cfg)
        r["offer"] = offer_price(r)
        r["resale_margin"] = r["profit_typical"]
        r["message_cz"] = message_cz(r)
        r["new"] = r["id"] not in seen
        r["found_at"] = NOW.strftime("%Y-%m-%d %H:%M")
        r["checked_full_text"] = "description" in r
        for k in ("text", "raw_id", "snippet_only", "kw_query", "red_flags"):
            r.pop(k, None)
        if "description" in r:
            r["description"] = r["description"][:700]
        deals.append(r)

    # Same car posted on both sites or twice: keep the best-scored copy.
    uniq = {}
    for d in sorted(deals, key=lambda d: d["score"], reverse=True):
        key = (d["model"], d["year"], round(d["km"] / 1000), round(d["price"] / 1000))
        uniq.setdefault(key, d)
    deals = sorted(uniq.values(), key=lambda d: d["score"], reverse=True)

    import os
    os.makedirs(args.out, exist_ok=True)
    stats = {
        "run_at": NOW.strftime("%Y-%m-%d %H:%M"), "seconds": int(time.time() - t0),
        "requests": http.count, "errors": http.errors,
        "listings_checked": len(candidates), "comparables": sum(len(v) for v in comps_by_model.values()),
        "deals": len(deals), "new_deals": sum(d["new"] for d in deals),
        "with_phrases": sum(bool(d["hot"]) for d in deals), "dropped_red_flags": dropped,
        "sold_tracked": sold_total, "resale_from_sales": sum(d["resale_basis"] == "sold" for d in deals),
        "resale_costs": cfg.get("resale_costs", 6000),
    }
    # Every Sauto listing, for history.py (detects sold cars: ads that disappear).
    cols = ["id", "model", "year", "km", "fuel", "gearbox", "engine", "perf", "private", "created", "price"]
    json.dump({"complete_models": complete_models, "columns": cols,
               "items": [[int(r["id"].split(":", 1)[1]) if k == "id" else r.get(k) for k in cols] for r in sauto_all]},
              open(f"{args.out}/sauto_listings.json", "w", encoding="utf-8"), ensure_ascii=False, separators=(",", ":"))
    json.dump({"stats": stats, "deals": deals}, open(f"{args.out}/deals.json", "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(json.dumps(stats, ensure_ascii=False), file=sys.stderr)
    for d in deals[:30]:
        print(f"{d['score']:>5} {'NEW' if d['new'] else '   '} {d['source']:<5} {d['model']:<15} {d['year']} "
              f"{d['km']:>7,} km {d['price']:>8,} Kč  рынок {d['market']:>8,} (-{d['discount']:.0%}, n={d['comps']})  "
              f"продать {d['resale_quick']:,}–{d['resale_typical']:,} ({d['resale_basis']}, n={d['resale_n']}) чистыми {d['profit_quick']:,}…{d['profit_typical']:,}  "
              f"{'+'.join(d['hot'])}  {'⚠ ' + ','.join(d['warnings']) if d['warnings'] else ''}\n      {d['url']}")


if __name__ == "__main__":
    main()
