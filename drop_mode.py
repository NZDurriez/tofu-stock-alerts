"""Drop mode: watch Mr Tofu's shop every few seconds from this PC and, the
moment an item you picked becomes buyable, open its ⚡ checkout page in your
browser with a loud alert. You still press Pay yourself.

Start it with the "Tofu Drop Mode" shortcut on the desktop (or
`python drop_mode.py`). Close the window or press Ctrl+C to stop.
"""
import json
import os
import re
import subprocess
import tempfile
import time
import webbrowser

SHOP = os.environ.get("DROP_SHOP", "https://mrtofu.store")
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130 Safari/537.36")
# Same watch list as the Discord alerts (only used for labelling here)
KEYWORDS = [k.strip() for k in (
    "delta reign, booster bundle, elite trainer box, etb, booster box, booster display, display, booster case, "
    "booster pack, booster packs, sleeved booster, enhanced booster, premium booster, half booster box, blister, "
    "collection box, premium collection, ultra premium collection, special collection, poster collection, "
    "binder collection, surprise box, gift box, tin, mini tin, build & battle, build and battle, battle deck, "
    "starter deck, starter set, double pack, bundle, pre-order, preorder, pre order, pre-release, prerelease, "
    "collection, figure collection, commander deck, opus, op, eb, prb, vault").split(",") if k.strip()]
DRY_OPEN = os.environ.get("DROP_DRY_OPEN") == "1"  # tests: print instead of opening the browser

COOKIES = os.path.join(tempfile.gettempdir(), "tofu_drop_cookies.txt")
# The alert sound picked in the browser version (made by sounds.py)
SOUND_DIR = os.environ.get("DROP_SOUND_DIR") or os.path.join(
    os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"), "TofuDropMode")
ALERT_WAV = os.path.join(SOUND_DIR, "alert.wav")


def watched(title):
    t = title.lower()
    return any(re.search(r"(?<![a-z0-9])" + re.escape(k) + r"(?![a-z0-9])", t) for k in KEYWORDS)


def curl(args):
    """Run curl (built into Windows) and return (status_code, headers, body)."""
    hdr = tempfile.NamedTemporaryFile(delete=False)
    hdr.close()
    try:
        res = subprocess.run(
            ["curl", "-sS", "--max-time", "10", "-A", UA, "-b", COOKIES, "-c", COOKIES,
             "-D", hdr.name, "-w", "\n%{http_code}"] + args,
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        if res.returncode != 0:
            raise RuntimeError(res.stderr.strip() or f"curl exit {res.returncode}")
        body, _, code = res.stdout.rpartition("\n")
        with open(hdr.name, encoding="latin-1") as f:
            headers = {}
            for line in f:
                if ":" in line:
                    k, v = line.split(":", 1)
                    headers[k.strip().lower()] = v.strip()
        return int(code), headers, body
    finally:
        os.unlink(hdr.name)


def login(password):
    curl(["-X", "POST", "--data-urlencode", "form_type=storefront_password", "--data-urlencode", "utf8=✓",
          "--data-urlencode", f"password={password}", "-o", os.devnull, f"{SHOP}/password"])
    code, _, _ = curl([f"{SHOP}/products.json?limit=1&_={time.time_ns()}"])
    return code == 200


def login_status(password):
    """'open' (shop isn't locked, so the password can't be checked), 'locked'
    (no password given), 'ok' (password got us in) or 'bad' (it didn't).
    Checking the lock first matters: an open shop lets anyone in, so a
    login 'working' there proves nothing."""
    if os.path.exists(COOKIES):
        os.unlink(COOKIES)
    code, _, _ = curl([f"{SHOP}/products.json?limit=1&_={time.time_ns()}"])
    if code == 200:
        return "open"
    if not password:
        return "locked"
    return "ok" if login(password) else "bad"


def fetch(etag=None):
    args = [f"{SHOP}/products.json?limit=250&_={time.time_ns()}", "-H", "Accept: application/json"]
    if etag:
        args += ["-H", f"If-None-Match: {etag}"]
    return curl(args)


def alarm(times=3):
    """Play the alert sound picked in the browser version, or beeps if none was."""
    if os.environ.get("DROP_MUTE") == "1":  # tests: say it instead of playing it
        print("[alert sound]", flush=True)
        return
    try:
        import winsound
        if os.path.exists(ALERT_WAV):
            winsound.PlaySound(ALERT_WAV, winsound.SND_FILENAME | winsound.SND_NODEFAULT)
            return
        for _ in range(times):
            for f in (1400, 1900, 2400):
                winsound.Beep(f, 120)
    except Exception:
        print("\a" * times, end="", flush=True)


def summarise(p):
    return {
        "title": p["title"],
        "handle": p["handle"],
        "published": p.get("published_at") or "",
        "image": ((p.get("images") or [{}])[0] or {}).get("src"),
        "variants": {str(v["id"]): (v.get("title") or "", bool(v.get("available")), v.get("price")) for v in p.get("variants") or []},
    }


UPCOMING_DAYS = 7


def buyable(it):
    return any(v[1] for v in it["variants"].values())


def upcoming(it):
    """Not buyable yet but a new release rather than old sold-out stock.
    Shopify reports both as plain "unavailable", and Tofu leaves "Pre-Order"
    in the names of old sold-out listings, so go by when it was listed."""
    if buyable(it):
        return False
    try:
        from datetime import datetime, timezone
        published = datetime.fromisoformat(it["published"])
        return (datetime.now(timezone.utc) - published).days <= UPCOMING_DAYS
    except ValueError:
        return False


def sold_out(it):
    """Unavailable and public for more than 10 minutes = sold out (Shopify has no
    separate flag; something unavailable right as it's published isn't on sale yet)."""
    if buyable(it):
        return False
    try:
        from datetime import datetime, timezone
        return (datetime.now(timezone.utc) - datetime.fromisoformat(it["published"])).total_seconds() > 600
    except ValueError:
        return False


def status(it):
    vs = list(it["variants"].values())
    price = next((v[2] for v in vs if v[1]), vs[0][2] if vs else None)
    label = "✅ buyable" if buyable(it) else "❌ sold out" if sold_out(it) else "🔜 not on sale yet"
    return label + (f" · ${price}" if price else "")


def ask(env_name, prompt):
    """input(), unless a DROP_* environment variable supplies the answer (tests)."""
    val = os.environ.get(env_name)
    return val if val is not None else input(prompt)


def as_qty(text, default):
    text = text.strip()
    return int(text) if text.isdigit() and int(text) > 0 else default


def choose_targets(current, default_qty):
    """Let the user pick which products should auto-open checkout.

    Returns ({product_id: qty}, [(words, qty)]). Word picks catch products
    that aren't listed yet, matched by title when they appear."""
    by_id, by_words = {}, []
    if current:
        flt = ask("DROP_FILTER", "\nNarrow the list? Type words like 'delta reign' (Enter = show new releases from the last week): ").strip().lower()
        if flt:
            words = flt.split()
            items = [(pid, it) for pid, it in current.items()
                     if all(w in it["title"].lower() for w in words) and (buyable(it) or upcoming(it))]
        else:  # default: upcoming releases, which is what drops are made of
            items = [(pid, it) for pid, it in current.items() if upcoming(it)]
        # Old sold-out stock is never listed; upcoming first, then buyable
        items.sort(key=lambda x: (buyable(x[1]), x[1]["title"]))
        items = items[:40]
        print()
        if items:
            for n, (pid, it) in enumerate(items, 1):
                print(f"  {n:>2}. {it['title']}  [{status(it)}]")
        else:
            print("  (nothing matched)")
        picks = ask("DROP_PICK", "\nPick items to buy by number, e.g. 1,3 (Enter = none): ")
        for part in re.split(r"[,\s]+", picks.strip()):
            if part.isdigit() and 1 <= int(part) <= len(items):
                pid, it = items[int(part) - 1]
                by_id[pid] = as_qty(ask("DROP_PICK_QTY", f"   Quantity for '{it['title'][:60]}' [{default_qty}]: "), default_qty)
    words = ask("DROP_WORDS", "\nAlso catch items that aren't listed yet? Type words, e.g. 'elite trainer box'"
                              " (comma between several, Enter = skip): ").strip().lower()
    for chunk in [c.strip() for c in words.split(",") if c.strip()]:
        q = as_qty(ask("DROP_WORDS_QTY", f"   Quantity for new items matching '{chunk}' [{default_qty}]: "), default_qty)
        by_words.append((chunk.split(), q))
    return by_id, by_words


def target_qty(pid, it, by_id, by_words):
    if pid in by_id:
        return by_id[pid]
    title = it["title"].lower()
    for words, q in by_words:
        if all(w in title for w in words):
            return q
    return None


def main():
    print("=" * 60)
    print("  MR TOFU DROP MODE")
    print("  Opens ⚡ checkout the second an item you picked can be bought.")
    print("  You still press Pay. Ctrl+C or close this window to stop.")
    print("=" * 60)
    password = os.environ.get("DROP_PASSWORD")
    if password is None:
        password = input("Shop password (press Enter if the shop isn't locked): ")
    password = password.strip()
    default_qty = as_qty(ask("DROP_QTY", "Default quantity [1]: "), 1)
    interval = ask("DROP_INTERVAL", "Seconds between checks [3]: ").strip()
    interval = max(1.5, float(interval)) if re.fullmatch(r"\d+(\.\d+)?", interval) else 3.0

    state = login_status(password)
    print({
        "ok": "✅ Password accepted, watching behind the lock.",
        "bad": "❌ That password didn't work. Carrying on without it.",
        "open": "🔓 The shop isn't locked, so no password is needed." + (" I'll try yours if it locks." if password else ""),
        "locked": "🔒 The shop is locked and no password was given; only new listings by name can be caught.",
    }[state])

    # Baseline, then pick what to buy
    etag, seen = None, {}
    code, headers, body = fetch()
    if code == 200:
        etag = headers.get("etag")
        seen = {str(p["id"]): summarise(p) for p in json.loads(body).get("products", [])}
        print(f"Shop has {len(seen)} products.")
    else:
        print(f"Couldn't list the shop right now (reply {code}); you can still pick items by words.")
    by_id, by_words = choose_targets(seen, default_qty)

    print("\n" + "-" * 60)
    if by_id or by_words:
        print("Will open checkout for:")
        for pid, q in by_id.items():
            print(f"  • {seen[pid]['title']}  x{q}  [{status(seen[pid])}]")
        for words, q in by_words:
            print(f"  • anything new matching '{' '.join(words)}'  x{q}")
    else:
        print("Nothing picked: I'll only announce changes in this window, without opening checkout.")
    print("Tip: your default browser should be signed in to Shop Pay and have the shop password entered.")
    print("-" * 60)

    opened = set()

    def open_checkout(items):
        """items: [(variant_id, qty, title)]. Several picks go into ONE checkout."""
        if not items:
            return
        for vid, q, title in items:
            opened.add(vid)
            print(f"   ⚡ x{q} {title[:70]}")
        url = f"{SHOP}/cart/" + ",".join(f"{vid}:{q}" for vid, q, _ in items)
        print(f"   Opening {'one checkout for all of these' if len(items) > 1 else 'checkout'}: {url}")
        if DRY_OPEN:
            print("   (dry run: not opening browser)")
        else:
            webbrowser.open(url)
        alarm()

    # Picks that are already buyable get opened straight away
    ready = []
    for pid, q in by_id.items():
        live = [vid for vid, v in seen[pid]["variants"].items() if v[1]]
        if live:
            print(f"\n'{seen[pid]['title']}' is already buyable!")
            ready.append((live[0], q, seen[pid]["title"]))
    open_checkout(ready)

    wait = interval
    print(f"\nWatching every {interval:g}s...\n")
    while True:
        try:
            code, headers, body = fetch(etag)
            stamp = time.strftime("%H:%M:%S")
            if code == 304:
                print(f"\r[{stamp}] no change", end="", flush=True)
                wait = interval
            elif code == 200:
                etag = headers.get("etag")
                current = {str(p["id"]): summarise(p) for p in json.loads(body).get("products", [])}
                ready = []
                for pid, it in current.items():
                    old = seen.get(pid, {"variants": {}})
                    fresh = [(vid, v) for vid, v in it["variants"].items()
                             if v[1] and not old["variants"].get(vid, ("", False))[1]]
                    if not fresh:
                        continue
                    q = target_qty(pid, it, by_id, by_words)
                    tag = "🎯 YOUR PICK" if q else ("🚨 watch list" if watched(it["title"]) else "new/restock")
                    names = ", ".join(v[0] for _, v in fresh if v[0] != "Default Title")
                    print(f"\n[{stamp}] {tag}: {it['title']}" + (f" ({names})" if names else "") + f" ${fresh[0][1][2]}")
                    if q and fresh[0][0] not in opened:
                        ready.append((fresh[0][0], q, it["title"]))
                    else:
                        print(f"   View: {SHOP}/products/{it['handle']}")
                open_checkout(ready)
                seen = current
                wait = interval
            elif code == 401:
                print(f"\n[{stamp}] Shop is password-locked and I'm not logged in.", end="")
                if password and login(password):
                    print(" Logged back in.")
                    etag = None
                else:
                    print(" Restart drop mode with the shop password.")
                    wait = 10
            elif code in (429, 430, 503):
                wait = min(wait * 2, 30)
                print(f"\n[{stamp}] Shop said slow down ({code}); waiting {wait:g}s")
            else:
                print(f"\n[{stamp}] Unexpected reply {code}; retrying")
        except KeyboardInterrupt:
            raise
        except Exception as exc:
            print(f"\n[{time.strftime('%H:%M:%S')}] Check failed: {exc}")
        time.sleep(wait)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nDrop mode stopped.")
