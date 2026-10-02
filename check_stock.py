"""Watch mrtofu.store and post to a Discord webhook when stock changes.

Alerts on:
  - new products appearing in the shop (even if not on sale yet)
  - products coming back in stock (sold out -> available)
  - a new buyable option on an existing product (e.g. a "Pre-order" variant)
Products matching WATCH_KEYWORDS get a louder 🚨 alert.

State lives in state.json so each check only reports what changed since the last.
WATCH_MINUTES > 0 keeps checking every WATCH_INTERVAL seconds for that long
(for release nights). Without DISCORD_WEBHOOK_URL, alerts are printed (dry run).
"""
import json
import os
import re
import subprocess
import sys
import time
import urllib.request

SHOP = "https://mrtofu.store"
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state.json")
WEBHOOK = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
PING = os.environ.get("DISCORD_PING", "").strip()  # e.g. <@123456789012345678>
KEYWORDS = [k.strip().lower() for k in os.environ.get("WATCH_KEYWORDS", "").split(",") if k.strip()]
WATCH_MINUTES = float(os.environ.get("WATCH_MINUTES") or 0)
WATCH_INTERVAL = max(30.0, float(os.environ.get("WATCH_INTERVAL") or 60))
BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/130 Safari/537.36")


class ShopBusy(Exception):
    """The shop kept rate-limiting us; try again next check."""


def get_json(url, attempts=4):
    # Fetch with curl: the shop answers 429 to Python's own HTTP client from
    # cloud servers but serves curl normally. Still back off if it gets busy.
    for attempt in range(attempts):
        res = subprocess.run(
            ["curl", "-sS", "-L", "--max-time", "30", "-A", BROWSER_UA,
             "-H", "Accept: application/json", "-H", "Cache-Control: no-cache",
             "-w", "\n%{http_code}", url],
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
        # Cache-buster so we never get a stale copy of the product list
        batch = get_json(f"{SHOP}/products.json?limit=250&page={page}&_={int(time.time())}")["products"]
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
        "variants": {str(v["id"]): {"title": v.get("title") or "", "available": bool(v.get("available"))}
                     for v in variants},
    }


def watched(item):
    # Whole-word match so "tin" doesn't fire on "Martin"
    t = item["title"].lower()
    return any(re.search(r"(?<![a-z0-9])" + re.escape(k) + r"(?![a-z0-9])", t) for k in KEYWORDS)


def buy_links(item, kind):
    """Quantity links for each option, kept under Discord's 1024-char field limit.

    kind="cart":     'ATC n'  adds n to the cart and opens the cart page
    kind="checkout": '⚡ n'   cart permalink: jumps straight to checkout with n
                     (still needs the buyer to confirm payment)
    """
    vids = [(vid, v) for vid, v in item["variants"].items() if v["available"]]
    live = bool(vids)
    if not live:  # not on sale yet: still give links, they'll work once it goes live
        vids = list(item["variants"].items())
    note = "" if live else "\n*Not on sale yet: these work once it goes live.*"
    lines = []
    for vid, v in vids:
        if kind == "cart":
            links = " · ".join(f"[ATC {n}]({SHOP}/cart/add?id={vid}&quantity={n})" for n in range(1, 6))
        else:
            links = " · ".join(f"[⚡ {n}]({SHOP}/cart/{vid}:{n})" for n in range(1, 6))
        name = v["title"] if v["title"] and v["title"] != "Default Title" else ""
        line = (f"**{name}:** " if name and len(vids) > 1 else "") + links
        if len("\n".join(lines + [line])) + len(note) > 1024:
            break
        lines.append(line)
    return "\n".join(lines) + note if lines else None


LABELS = {
    "new": ("🆕 New in the shop", 0xF5A524),
    "new-soon": ("🆕 New listing (not on sale yet)", 0xA78BFA),
    "restock": ("🔁 Back in stock", 0x22C55E),
    "option": ("➕ New option available", 0x38BDF8),
}


def embed(kind, item, note=None):
    label, colour = LABELS[kind]
    if watched(item):
        label, colour = "🚨 " + label, 0xEF4444
    fields = []
    if item["price"] is not None:
        fields.append({"name": "Price", "value": f"${item['price']:.2f}", "inline": True})
    fields.append({"name": "Stock", "value": "In stock ✅" if item["available"] else "Not available yet ❌", "inline": True})
    opts = [v["title"] for v in item["variants"].values() if v["available"] and v["title"] != "Default Title"]
    if opts:
        fields.append({"name": "Available options", "value": ", ".join(opts)[:1024], "inline": False})
    atc = buy_links(item, "cart")
    if atc:
        fields.append({"name": "🛒 Add to cart", "value": atc, "inline": False})
    fast = buy_links(item, "checkout")
    if fast:
        fields.append({"name": "⚡ Lightning checkout (straight to payment)", "value": fast, "inline": False})
    e = {
        "author": {"name": label},
        "title": item["title"][:256],
        "url": f"{SHOP}/products/{item['handle']}",
        "color": colour,
        "fields": fields,
    }
    if note:
        e["description"] = note
    if item["image"]:
        e["thumbnail"] = {"url": item["image"]}
    return e


def send(embeds, loud=False):
    # Discord allows 10 embeds per message
    for i in range(0, len(embeds), 10):
        chunk = embeds[i:i + 10]
        head = "🚨 **Watched item update!** " if loud else ""
        payload = {
            "username": "Mr Tofu Stock Watch",
            "content": (PING + " " if PING else "") + head
                       + f"{len(chunk)} update{'s' if len(chunk) != 1 else ''} at Mr Tofu's shop",
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


def diff(previous, current):
    """Return (kind, item, note) for everything worth telling the user about."""
    out = []
    for pid, item in current.items():
        old = previous.get(pid)
        if old is None:
            out.append(("new" if item["available"] else "new-soon", item, None))
            continue
        if item["available"] and not old.get("available"):
            out.append(("restock", item, None))
            continue
        old_vars = old.get("variants")
        if old_vars is None:  # snapshot from before option tracking; nothing to compare
            continue
        fresh = [v["title"] for vid, v in item["variants"].items()
                 if v["available"] and not old_vars.get(vid, {}).get("available")]
        if fresh:
            named = [t for t in fresh if t and t != "Default Title"]
            out.append(("option", item, "Now buyable" + (": **" + ", ".join(named)[:300] + "**" if named else "")))
    # Watched items first so they're at the top of the message
    out.sort(key=lambda a: not watched(a[1]))
    return out


def check(previous, first_run):
    current = {str(p["id"]): summarise(p) for p in fetch_products()}
    if not current:
        raise RuntimeError("Shop returned no products")
    found = [] if first_run else diff(previous, current)
    if found:
        send([embed(k, i, n) for k, i, n in found], loud=any(watched(i) for _, i, _ in found))
    return current, found


def save(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=1, ensure_ascii=False, sort_keys=True)


def main():
    if os.environ.get("GITHUB_ACTIONS") and not WEBHOOK:
        # Don't record changes nobody was told about
        print("DISCORD_WEBHOOK_URL secret isn't set yet; skipping this check.", file=sys.stderr)
        return 1

    first_run = not os.path.exists(STATE_FILE)
    state = {} if first_run else json.load(open(STATE_FILE, encoding="utf-8"))

    if os.environ.get("SEND_TEST") == "true":
        current = {str(p["id"]): summarise(p) for p in fetch_products()}
        sample = next((i for i in current.values() if i["available"]), next(iter(current.values())))
        e = embed("new", sample, "This is how alerts will look. Example product below.")
        e["author"]["name"] = "✅ Stock watch is connected (test message)"
        send([e])

    deadline = time.time() + WATCH_MINUTES * 60
    if WATCH_MINUTES:
        print(f"Watch mode: checking every {WATCH_INTERVAL:.0f}s for {WATCH_MINUTES:.0f} minutes"
              + (f", keywords: {', '.join(KEYWORDS)}" if KEYWORDS else ""))
    checks = 0
    while True:
        try:
            current, found = check(state, first_run)
            state, first_run = current, False
            save(state)  # after every check, so a crash never re-sends old alerts
            checks += 1
            stamp = time.strftime("%H:%M:%S")
            print(f"[{stamp}] {len(current)} products, {sum(i['available'] for i in current.values())} in stock, "
                  f"{len(found)} alert(s)")
        except ShopBusy as exc:
            # Not a real failure; state is untouched so the next check still catches changes
            print(f"Shop is rate-limiting right now ({exc}); will try again.", file=sys.stderr)
        except Exception as exc:
            print(f"Check failed: {exc}", file=sys.stderr)
            if not WATCH_MINUTES:
                return 1
        if time.time() + WATCH_INTERVAL > deadline:
            break
        time.sleep(WATCH_INTERVAL)
    if WATCH_MINUTES:
        print(f"Watch mode finished after {checks} checks.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
