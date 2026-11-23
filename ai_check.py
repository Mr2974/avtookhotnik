#!/usr/bin/env python3
"""Optional photo + description check by Claude for new deals.

Runs only when ANTHROPIC_API_KEY is set (a GitHub Actions secret). For each deal not checked yet it
sends up to `ai_photos` photos and the ad text to Claude and gets back, as strict JSON: a visible
condition score 1-5, a verdict (ok / caution / reject), the issues it found with the evidence,
and a short summary in Russian, English, Ukrainian and Czech.

  python3 ai_check.py --deals out/deals.json --feed docs/data/deals.json --config config.json

Writes the results back into out/deals.json: "ai_check" on each checked deal, rejected deals moved
to "rejected" (update_site.py then removes them from the feed), and run totals in "ai".
Settings in config.json: ai_model, ai_effort, ai_photos, ai_max_per_run.
"""
import argparse
import base64
import datetime as dt
import json
import os
import sys
import urllib.request

import anthropic

UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
PRICES = {  # $ per 1M input / output tokens
    "claude-opus-5-5": (4.00, 20.00), "claude-sonnet-5-5": (2.00, 10.00), "claude-haiku-5-5": (0.10, 0.50),
}
# Models that take the server-side refusal fallback (Claude Haiku 5.5 has none).
FALLBACK_MODELS = {"claude-opus-5-5", "claude-sonnet-5-5", "claude-opus-5", "claude-fable-5-2", "claude-fable-5-1"}

CODES = [
    "not_running", "parts_or_repair", "engine_failure", "gearbox_failure", "head_gasket", "overheating", "flood",
    "crashed", "accident_damage", "structural_rust", "coolant_leak", "oil_leak", "oil_consumption", "smoke", "clutch",
    "flywheel", "turbo", "dpf", "injectors", "warning_lights", "electrical", "suspension_brakes", "rust", "no_stk",
    "repair_needed", "mileage_doubt", "paint_mismatch", "body_cosmetic", "glass", "tyres", "interior_wear", "ac",
    "photos_insufficient", "stock_or_other_car_photos", "other",
]
SCHEMA = {
    "type": "object",
    "properties": {
        "photos_usable": {"type": "boolean"},
        "condition_score": {"type": "integer"},
        "verdict": {"type": "string", "enum": ["ok", "caution", "reject"]},
        "issues": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "code": {"type": "string", "enum": CODES},
                    "severity": {"type": "string", "enum": ["critical", "serious", "minor"]},
                    "source": {"type": "string", "enum": ["photo", "description"]},
                    "evidence": {"type": "string"},
                },
                "required": ["code", "severity", "source", "evidence"],
                "additionalProperties": False,
            },
        },
        "summary": {
            "type": "object",
            "properties": {k: {"type": "string"} for k in ("ru", "en", "uk", "cs")},
            "required": ["ru", "en", "uk", "cs"],
            "additionalProperties": False,
        },
    },
    "required": ["photos_usable", "condition_score", "verdict", "issues", "summary"],
    "additionalProperties": False,
}

SYSTEM = """You check used-car ads from Czech marketplaces (Sauto.cz, Bazoš.cz) for a buyer who buys cheap cars to drive or resell. You get the ad's photos (numbered in order) and its text. Report only what the photos show or what the seller writes; do not guess hidden faults.

Photos: judge the visible condition for the car's age: dents, scratches, rust (sills, wheel arches, door bottoms, tailgate), accident damage, mismatched paint colour or uneven panel gaps (signs of a repair), cracked glass, tyre wear, interior wear or dirt, warning lights lit on the dashboard, and the odometer if it is visible and differs from the stated mileage. Say so when there are too few photos, they are blurry, or they look like stock photos or a different car.

Text: find defects the seller admits: not running, engine or gearbox faults, coolant or oil loss, overheating, smoke, clutch, dual-mass flywheel, turbo, DPF, injectors, warning lights, rust, no valid STK, sold for parts or for repair. A defect the seller says is already fixed does not count, and neither does a negation ("nic neteče", "bez koroze").

condition_score: 5 = looks very good for its age, 4 = normal wear, 3 = visible flaws worth a lower price, 2 = significant damage or rust, 1 = a wreck. If the photos are unusable, score from the text and set photos_usable to false.
verdict: reject = not a working, sellable car, or a sign of serious hidden risk (not running, engine or gearbox failure, heavy accident damage, structural rust); caution = flaws that lower the price or need checking on the spot; ok = nothing notable.
evidence: for a text issue, quote the seller's words in Czech; for a photo issue, give the photo number and what is visible.
summary: one or two plain sentences for the buyer about the car's condition, in Russian (ru), English (en), Ukrainian (uk) and Czech (cs)."""


def fetch_image(url):
    """Downloads a photo for the request; Sauto (sdn.cz) photos are requested at 1024 px."""
    tries = [url + "?fl=res,1024,768,3", url] if "sdn.cz/" in url and "?" not in url else [url]
    for u in tries:
        try:
            req = urllib.request.Request(u, headers={"User-Agent": UA, "Referer": "https://www.sauto.cz/" if "sdn.cz" in u else "https://auto.bazos.cz/"})
            with urllib.request.urlopen(req, timeout=20) as r:
                ctype = (r.headers.get("Content-Type") or "").split(";")[0].strip()
                data = r.read(5_000_000)
            if ctype in ("image/jpeg", "image/png", "image/webp", "image/gif") and len(data) > 1000:
                return ctype, base64.standard_b64encode(data).decode("ascii")
        except Exception as e:  # a missing photo is not a reason to fail the check
            print(f"  photo {u[:80]}: {e}", file=sys.stderr)
    return None


def ask_claude(client, deal, cfg):
    photos = []
    for url in (deal.get("photos") or ([deal["image"]] if deal.get("image") else []))[: cfg.get("ai_photos", 5)]:
        img = fetch_image(url)
        if img:
            photos.append(img)
    text = (deal.get("description") or "").strip()
    if not photos and not text:
        return None, None
    content = []
    for i, (ctype, b64) in enumerate(photos, 1):
        content.append({"type": "text", "text": f"Photo {i}:"})
        content.append({"type": "image", "source": {"type": "base64", "media_type": ctype, "data": b64}})
    content.append({"type": "text", "text": (
        f"Ad: {deal['title']}\nModel: {deal['model']}, year {deal['year']}, mileage {deal['km']} km, "
        f"price {deal['price']} CZK ({'private seller' if deal.get('private') else 'dealer'})\n\n"
        f"Seller's text:\n{text or '(no text)'}")})

    model = cfg.get("ai_model", "claude-opus-5-5")
    params = dict(
        model=model,
        max_tokens=4000,
        system=SYSTEM,
        output_config={"effort": cfg.get("ai_effort", "low"), "format": {"type": "json_schema", "schema": SCHEMA}},
        messages=[{"role": "user", "content": content}],
    )
    if model in FALLBACK_MODELS:
        resp = client.beta.messages.create(betas=["server-side-fallback-2026-07-01"], fallbacks="default", **params)
    else:
        resp = client.messages.create(**params)
    if resp.stop_reason == "refusal":
        return None, resp.usage
    data = json.loads(next(b.text for b in resp.content if b.type == "text"))
    data["condition_score"] = max(1, min(5, int(data.get("condition_score") or 3)))
    data["photos_checked"] = len(photos)
    return data, resp.usage


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--deals", default="out/deals.json")
    ap.add_argument("--feed", default="docs/data/deals.json")
    ap.add_argument("--config", default="config.json")
    a = ap.parse_args()
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("ANTHROPIC_API_KEY не задан — проверка фото пропущена")
        return
    cfg = json.load(open(a.config, encoding="utf-8"))
    scan = json.load(open(a.deals, encoding="utf-8"))
    feed = {}
    if os.path.exists(a.feed):
        feed = {d["id"]: d for d in json.load(open(a.feed, encoding="utf-8"))["deals"]}

    todo = []
    for d in scan["deals"]:
        known = feed.get(d["id"], {})
        if d.get("ai_check") or (known.get("ai_check") and not d.get("checks_version")):
            continue
        # Known deals keep their photos and text on the site; a quick re-scan row may not carry them.
        for k in ("photos", "description", "image"):
            if not d.get(k) and known.get(k):
                d[k] = known[k]
        todo.append(d)
    todo.sort(key=lambda d: d["score"], reverse=True)
    waiting = len(todo)
    todo = todo[: cfg.get("ai_max_per_run", 40)]

    client = anthropic.Anthropic()
    model = cfg.get("ai_model", "claude-opus-5-5")
    tin = tout = checked = 0
    reject_ids = set()
    now = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    for d in todo:
        try:
            result, usage = ask_claude(client, d, cfg)
        except anthropic.RateLimitError:
            print("  лимит запросов API — остальные машины проверю в следующий раз", file=sys.stderr)
            break
        except anthropic.APIStatusError as e:
            print(f"  {d['id']}: ошибка API {e.status_code}: {e.message}", file=sys.stderr)
            continue
        except anthropic.APIConnectionError as e:
            print(f"  {d['id']}: нет связи с API: {e}", file=sys.stderr)
            continue
        if usage:
            tin += usage.input_tokens or 0
            tout += usage.output_tokens or 0
        if not result:
            continue
        checked += 1
        d["ai_check"] = dict(result, model=model, checked_at=now)
        critical = [i for i in result["issues"] if i["severity"] == "critical"]
        if result["verdict"] == "reject" or critical:
            reject_ids.add(d["id"])
            reason = critical[0] if critical else (result["issues"][0] if result["issues"] else {"code": "other", "evidence": ""})
            scan.setdefault("rejected", []).append({"id": d["id"], "codes": ["ai:" + reason["code"]], "quote": reason["evidence"]})
        else:
            d["score"] = round(d["score"] + (result["condition_score"] - 3) * 4 - (5 if result["verdict"] == "caution" else 0), 1)
        print(f"  {d['id']}: {result['verdict']} {result['condition_score']}/5 {result['summary']['ru'][:90]}", file=sys.stderr)

    scan["deals"] = [d for d in scan["deals"] if d["id"] not in reject_ids]
    pin, pout = PRICES.get(model, (0, 0))
    scan["ai"] = {"model": model, "checked": checked, "rejected": len(reject_ids), "input_tokens": tin,
                  "output_tokens": tout, "usd": round(tin / 1e6 * pin + tout / 1e6 * pout, 3), "backlog": max(0, waiting - checked)}
    json.dump(scan, open(a.deals, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print("ИИ-проверка:", json.dumps(scan["ai"], ensure_ascii=False))


if __name__ == "__main__":
    main()
