"""Watch mrtofu.store and post to a Discord webhook when stock changes.

Alerts on:
  - new products appearing in the shop
  - products coming back in stock (sold out -> available)

State lives in state.json so each run only reports what changed since the last.
Without DISCORD_WEBHOOK_URL set, alerts are printed instead of sent (dry run).
"""
import json
import os
import sys
import time
import subprocess
import urllib.request

SHOP = "https://mrtofu.store"
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state.json")
WEBHOOK = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
PING = os.environ.get("DISCORD_PING", "").strip()  # e.g. <@123456789012345678>
BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/130 Safari/537.36")


class ShopBusy(Exception):
    """The shop kept rate-limiting us; try again next run."""


def get_json(url, attempts=4):
    # Fetch with curl: the shop answers 429 to Python's own HTTP client from
    # cloud servers but serves curl normally. Still back off if it gets busy.
    for attempt in range(attempts):
        res = subprocess.run(
            ["curl", "-sS", "-L", "--max-time", "30", "-A", BROWSER_UA,
             "-H", "Accept: application/json", "-w", "\n%{http_code}", url],
            capture_output=True, text=True, encoding="utf-8",
        )
        if res.returncode != 0:
            raise RuntimeError(f"curl failed: {res.stderr.strip()}")
        body, _, code = res.stdout.rpartition("\n")
        if code == "200":
            return json.loads(body)
        if code not in ("429", "430", "503"):
            raise RuntimeError(f"HTTP {code}")
        if attempt == attempts - 1:
            raise ShopBusy(f"HTTP {code}")
        wait = 15 * 2 ** attempt
        print(f"Shop said {code}; waiting {wait}s before retrying", file=sys.stderr)
        time.sleep(wait)


def fetch_products():
    products, page = [], 1
    while True:
        batch = get_json(f"{SHOP}/products.json?limit=250&page={page}")["products"]
        products += batch
        if len(batch) < 250:  # last page; saves a request
            return products
        page += 1


def summarise(p):
    variants = p.get("variants") or []
    in_stock = [v for v in variants if v.get("available")]
    prices = sorted(float(v["price"]) for v in (in_stock or variants) if v.get("price"))
    images = p.get("images") or []
    return {
        "title": p["title"],
        "handle": p["handle"],
        "available": bool(in_stock),
        "price": prices[0] if prices else None,
        "image": images[0]["src"] if images else None,
    }


def embed(kind, item):
    colour = {"new": 0xF5A524, "restock": 0x22C55E}[kind]
    label = {"new": "🆕 New in the shop", "restock": "🔁 Back in stock"}[kind]
    fields = []
    if item["price"] is not None:
        fields.append({"name": "Price", "value": f"${item['price']:.2f}", "inline": True})
    fields.append({"name": "Stock", "value": "In stock ✅" if item["available"] else "Sold out ❌", "inline": True})
    e = {
        "author": {"name": label},
        "title": item["title"][:256],
        "url": f"{SHOP}/products/{item['handle']}",
        "color": colour,
        "fields": fields,
    }
    if item["image"]:
        e["thumbnail"] = {"url": item["image"]}
    return e


def send(embeds):
    # Discord allows 10 embeds per message
    for i in range(0, len(embeds), 10):
        chunk = embeds[i:i + 10]
        payload = {
            "username": "Mr Tofu Stock Watch",
            "content": (PING + " " if PING else "") + f"{len(chunk)} update{'s' if len(chunk) != 1 else ''} at Mr Tofu's shop",
            "embeds": chunk,
            "allowed_mentions": {"parse": ["users", "roles", "everyone"]},
        }
        if not WEBHOOK:
            print(json.dumps(payload, indent=2, ensure_ascii=False))
            continue
        req = urllib.request.Request(
            WEBHOOK, data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json", "User-Agent": "DiscordBot (tofu-stock-alerts, 1.0)"},
        )
        urllib.request.urlopen(req, timeout=30).read()
        time.sleep(1)  # stay well inside Discord's rate limit


def main():
    if os.environ.get("GITHUB_ACTIONS") and not WEBHOOK:
        # Don't record changes nobody was told about
        print("DISCORD_WEBHOOK_URL secret isn't set yet; skipping this check.", file=sys.stderr)
        return 1
    try:
        current = {str(p["id"]): summarise(p) for p in fetch_products()}
    except ShopBusy as exc:
        # Not a real failure; exit cleanly so GitHub doesn't email about it.
        # State is untouched, so the next run still catches any changes.
        print(f"Shop is rate-limiting right now ({exc}); will try again next run.", file=sys.stderr)
        return 0
    except Exception as exc:  # leave state untouched so nothing is missed next run
        print(f"Could not read the shop: {exc}", file=sys.stderr)
        return 1
    if not current:
        print("Shop returned no products; skipping so a blip doesn't wipe the state.", file=sys.stderr)
        return 1

    first_run = not os.path.exists(STATE_FILE)
    previous = {} if first_run else json.load(open(STATE_FILE, encoding="utf-8"))

    alerts = []
    if not first_run:
        for pid, item in current.items():
            old = previous.get(pid)
            if old is None:
                alerts.append(embed("new", item))
            elif item["available"] and not old.get("available"):
                alerts.append(embed("restock", item))

    if alerts:
        send(alerts)
    print(f"{len(current)} products, {sum(i['available'] for i in current.values())} in stock, "
          f"{len(alerts)} alert(s){' (first run: baseline saved, no alerts)' if first_run else ''}")

    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(current, f, indent=1, ensure_ascii=False, sort_keys=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
