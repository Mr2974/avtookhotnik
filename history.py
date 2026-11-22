#!/usr/bin/env python3
"""Tracks every Sauto listing between runs and records the ones that disappear: a sold car.

  python3 history.py --listings out/sauto_listings.json --dir history

State (kept on the `history` branch by the workflow):
  history/active.json  listings seen in the latest runs
  history/sold.json    listings that disappeared: last asking price and days on the market

A listing counts as sold when it is missing from two complete scans in a row (one missing scan
can be a page that shifted while new ads arrived). Models whose scan did not finish are skipped.
Removed ads also include ones the seller withdrew; the quick ones (1–60 days) are mostly sales,
and only those are used for price estimates (car_hunter.py --history).
"""
import argparse
import datetime as dt
import json
import os

KEEP_SOLD_DAYS = 120
FIELDS = ["model", "year", "km", "fuel", "gearbox", "engine", "perf", "private", "created"]
ACTIVE_COLS = ["id"] + FIELDS + ["first_seen", "last_seen", "price", "first_price", "missing"]
SOLD_COLS = ["id"] + FIELDS + ["first_seen", "removed_at", "price", "first_price", "days"]
FMT = "%Y-%m-%d %H:%M"


def load(path, cols):
    if not os.path.exists(path):
        return {}
    data = json.load(open(path, encoding="utf-8"))
    return {row[0]: dict(zip(data["columns"], row)) for row in data["items"]}


def save(path, cols, rows):
    tmp = path + ".tmp"
    json.dump({"columns": cols, "items": [[r.get(c) for c in cols] for r in rows]},
              open(tmp, "w", encoding="utf-8"), ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, path)


def parse_time(s):
    s = (s or "").replace("T", " ")[:16]
    try:
        return dt.datetime.strptime(s, FMT)
    except ValueError:
        return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--listings", default="out/sauto_listings.json")
    ap.add_argument("--dir", default="history")
    a = ap.parse_args()
    os.makedirs(a.dir, exist_ok=True)

    scan = json.load(open(a.listings, encoding="utf-8"))
    complete = set(scan["complete_models"])
    now = dt.datetime.now().strftime(FMT)
    current = {}
    for row in scan["items"]:
        d = dict(zip(scan["columns"], row))
        current[d["id"]] = d

    active = load(os.path.join(a.dir, "active.json"), ACTIVE_COLS)
    sold = load(os.path.join(a.dir, "sold.json"), SOLD_COLS)
    added = back = gone = 0

    for i, d in current.items():
        if i in sold:  # it was only hidden for a while: not sold after all
            old = sold.pop(i)
            active[i] = dict({k: d.get(k) for k in FIELDS}, id=i, first_seen=old["first_seen"], last_seen=now,
                             price=d["price"], first_price=old.get("first_price") or d["price"], missing=0)
            back += 1
            continue
        a_row = active.get(i)
        if a_row:
            a_row.update({k: d.get(k) for k in FIELDS}, last_seen=now, price=d["price"], missing=0)
        else:
            active[i] = dict({k: d.get(k) for k in FIELDS}, id=i, first_seen=now, last_seen=now,
                             price=d["price"], first_price=d["price"], missing=0)
            added += 1

    for i in list(active):
        r = active[i]
        if i in current or r["model"] not in complete:
            continue
        r["missing"] = (r.get("missing") or 0) + 1
        if r["missing"] >= 2:
            start = parse_time(r.get("created")) or parse_time(r["first_seen"])
            end = parse_time(r["last_seen"])
            days = max(0, (end - start).days) if start and end else None
            sold[i] = dict({k: r.get(k) for k in FIELDS}, id=i, first_seen=r["first_seen"], removed_at=r["last_seen"],
                           price=r["price"], first_price=r.get("first_price"), days=days)
            del active[i]
            gone += 1

    cutoff = (dt.datetime.now() - dt.timedelta(days=KEEP_SOLD_DAYS)).strftime(FMT)
    sold = {i: r for i, r in sold.items() if (r.get("removed_at") or "") >= cutoff and r.get("days") is not None}

    save(os.path.join(a.dir, "active.json"), ACTIVE_COLS, active.values())
    save(os.path.join(a.dir, "sold.json"), SOLD_COLS, sorted(sold.values(), key=lambda r: r["removed_at"]))
    quick = sum(1 for r in sold.values() if 1 <= r["days"] <= 60)
    print(f"история: в продаже {len(active)}, новых {added}, исчезло {gone}, вернулось {back}, "
          f"продано всего {len(sold)} (за 1–60 дней: {quick}), полных моделей {len(complete)}")


if __name__ == "__main__":
    main()
