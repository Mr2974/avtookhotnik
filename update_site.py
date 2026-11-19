#!/usr/bin/env python3
"""Merges a fresh scan (out/deals.json) into the site data and sends Telegram alerts.

Site data lives in docs/data/:
  deals.json  every deal found so far: first_found_at, last_seen_at, live (still listed and
              still below market in the latest scan), price_history
  meta.json   stats of the latest scan

Telegram (optional, set as GitHub secrets / env):
  TELEGRAM_BOT_TOKEN  bot token from @BotFather
  TELEGRAM_CHAT_ID    channel/group id, e.g. @my_channel or -1001234567890
Optional env: SITE_URL (link in messages), TELEGRAM_INVITE (public link shown on the site).
"""
import html
import json
import os
import sys
import urllib.parse
import urllib.request
import datetime as dt

DATA = "docs/data"
KEEP_DAYS_AFTER_GONE = 30
MAX_IN_MESSAGE = 8


def n(v):
    return f"{int(v):,}".replace(",", " ")


def load(path, default):
    try:
        return json.load(open(path, encoding="utf-8"))
    except (OSError, ValueError):
        return default


def telegram(text):
    token, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat:
        print("Telegram is not configured (TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID), skipping message")
        return
    body = urllib.parse.urlencode({"chat_id": chat, "text": text, "parse_mode": "HTML",
                                   "disable_web_page_preview": "true"}).encode()
    try:
        with urllib.request.urlopen(f"https://api.telegram.org/bot{token}/sendMessage", body, timeout=30) as r:
            print("Telegram:", json.load(r).get("ok"))
    except Exception as e:  # never fail the run because of a notification
        print("Telegram error:", e, file=sys.stderr)


def card(d, prefix=""):
    tags = []
    tags.append("частник" if d.get("private") else "дилер")
    tags += d.get("hot") or []
    warn = " ⚠️" if d.get("warnings") else ""
    return (f"{prefix}<b>{html.escape(d['model'])} {d['year']}</b>, {n(d['km'])} км{warn}\n"
            f"<b>{n(d['price'])} Kč</b> — рынок ≈ {n(d['market'])} (−{round(d['discount'] * 100)}%), "
            f"перепродажа ≈ +{n(d['resale_margin'])} Kč\n"
            f"{html.escape(' · '.join(tags))}\n"
            f"<a href=\"{html.escape(d['url'])}\">{html.escape(d['title'][:60])}</a>")


def main():
    scan = json.load(open("out/deals.json", encoding="utf-8"))
    stats, found = scan["stats"], scan["deals"]
    state = load(f"{DATA}/deals.json", {"deals": []})
    by_id = {d["id"]: d for d in state["deals"]}
    now = stats["run_at"]
    found_ids = set()
    new, cheaper = [], []

    for d in found:
        found_ids.add(d["id"])
        old = by_id.get(d["id"])
        if not old:
            d.update(first_found_at=now, last_seen_at=now, live=True, price_history=[[now, d["price"]]])
            d.pop("new", None)
            by_id[d["id"]] = d
            new.append(d)
            continue
        if d["price"] < old["price"] * 0.97:
            cheaper.append((d, old["price"]))
        hist = old.get("price_history") or [[old.get("first_found_at", now), old["price"]]]
        if hist[-1][1] != d["price"]:
            hist.append([now, d["price"]])
        # Known car: refresh what the quick scan re-measured, keep what only the full read knows.
        old.update(live=True, last_seen_at=now, price=d["price"], market=d["market"], comps=d["comps"],
                   comps_basis=d["comps_basis"], discount=d["discount"], resale_margin=d["resale_margin"],
                   offer=d["offer"], price_history=hist[-10:])

    cutoff = (dt.datetime.now() - dt.timedelta(days=KEEP_DAYS_AFTER_GONE)).strftime("%Y-%m-%d %H:%M")
    for k in list(by_id):
        d = by_id[k]
        if k not in found_ids:
            d["live"] = False
            if (d.get("last_seen_at") or "") < cutoff:
                del by_id[k]

    deals = sorted(by_id.values(), key=lambda d: (d.get("live", False), d["score"]), reverse=True)
    os.makedirs(DATA, exist_ok=True)
    json.dump({"deals": deals}, open(f"{DATA}/deals.json", "w", encoding="utf-8"), ensure_ascii=False, separators=(",", ":"))
    meta = dict(stats, new_added=len(new), cheaper=len(cheaper), live=sum(d.get("live", False) for d in deals),
                total=len(deals), telegram_url=os.environ.get("TELEGRAM_INVITE", ""))
    json.dump(meta, open(f"{DATA}/meta.json", "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"new={len(new)} cheaper={len(cheaper)} live={meta['live']} total={len(deals)}")

    if not new and not cheaper:
        return
    site = os.environ.get("SITE_URL", "")
    new.sort(key=lambda d: d["score"], reverse=True)
    parts = [f"🚗 <b>Новые машины ниже рынка: {len(new)}</b>" if new else "📉 <b>Подешевели машины из ленты</b>"]
    parts += [card(d) for d in new[:MAX_IN_MESSAGE]]
    room = MAX_IN_MESSAGE - min(len(new), MAX_IN_MESSAGE)
    parts += [card(d, prefix=f"📉 было {n(old_price)} Kč → ") for d, old_price in cheaper[:max(room, 2)]]
    if len(new) > MAX_IN_MESSAGE:
        parts.append(f"…и ещё {len(new) - MAX_IN_MESSAGE}.")
    if site:
        parts.append(f"Вся лента: {html.escape(site)}")
    text = "\n\n".join(parts)
    telegram(text[:4000])


if __name__ == "__main__":
    main()
