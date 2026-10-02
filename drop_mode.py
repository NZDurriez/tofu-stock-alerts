"""Drop mode: watch Mr Tofu's shop every few seconds from this PC and, the
moment a watched item becomes buyable, open its ⚡ checkout page in your
browser with a loud alert. You still press Pay yourself.

Start it with the "Tofu Drop Mode" shortcut on the desktop (or
`python drop_mode.py`). Close the window or press Ctrl+C to stop.
"""
import getpass
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import webbrowser

SHOP = os.environ.get("DROP_SHOP", "https://mrtofu.store")
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130 Safari/537.36")
# Same watch list as the Discord alerts
KEYWORDS = [k.strip() for k in (
    "delta reign, booster bundle, elite trainer box, etb, booster box, booster display, display, booster case, "
    "booster pack, booster packs, sleeved booster, enhanced booster, premium booster, half booster box, blister, "
    "collection box, premium collection, ultra premium collection, special collection, poster collection, "
    "binder collection, surprise box, gift box, tin, mini tin, build & battle, build and battle, battle deck, "
    "starter deck, starter set, double pack, bundle, pre-order, preorder, pre order, pre-release, prerelease, "
    "collection, figure collection, commander deck, opus, op, eb, prb, vault").split(",") if k.strip()]
DRY_OPEN = os.environ.get("DROP_DRY_OPEN") == "1"  # tests: print instead of opening the browser

COOKIES = os.path.join(tempfile.gettempdir(), "tofu_drop_cookies.txt")


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


def alarm(times=3):
    try:
        import winsound
        for _ in range(times):
            for f in (1400, 1900, 2400):
                winsound.Beep(f, 120)
    except Exception:
        print("\a" * times, end="", flush=True)


def summarise(p):
    return {
        "title": p["title"],
        "handle": p["handle"],
        "variants": {str(v["id"]): (v.get("title") or "", bool(v.get("available")), v.get("price")) for v in p.get("variants") or []},
    }


def main():
    print("=" * 60)
    print("  MR TOFU DROP MODE")
    print("  Opens ⚡ checkout the second a watched item can be bought.")
    print("  You still press Pay. Ctrl+C or close this window to stop.")
    print("=" * 60)
    # DROP_* environment variables skip the prompts (used for testing)
    password = os.environ.get("DROP_PASSWORD")
    if password is None:
        password = getpass.getpass("Shop password (press Enter if the shop isn't locked; nothing shows as you type): ")
    password = password.strip()
    qty = os.environ.get("DROP_QTY") or input("Quantity to open checkout with [1]: ").strip()
    qty = int(qty) if qty.isdigit() and int(qty) > 0 else 1
    interval = os.environ.get("DROP_INTERVAL") or input("Seconds between checks [3]: ").strip()
    interval = max(1.5, float(interval)) if re.fullmatch(r"\d+(\.\d+)?", interval) else 3.0
    print("\nTip: make sure this PC's default browser is signed in to Shop Pay and has the shop password entered.\n")

    if os.path.exists(COOKIES):
        os.unlink(COOKIES)
    if password:
        if login(password):
            print("✅ Password accepted, watching behind the lock.")
        else:
            print("❌ That password didn't work (or the shop isn't locked). Carrying on without it.")

    etag, seen, opened = None, None, set()
    wait = interval
    print(f"Watching every {interval:g}s, opening checkout x{qty} for watched items...\n")
    while True:
        try:
            args = [f"{SHOP}/products.json?limit=250&_={time.time_ns()}", "-H", "Accept: application/json"]
            if etag:
                args += ["-H", f"If-None-Match: {etag}"]
            code, headers, body = curl(args)
            stamp = time.strftime("%H:%M:%S")
            if code == 304:
                print(f"\r[{stamp}] no change", end="", flush=True)
                wait = interval
            elif code == 200:
                etag = headers.get("etag")
                current = {str(p["id"]): summarise(p) for p in json.loads(body).get("products", [])}
                if seen is None:
                    buyable = sum(any(v[1] for v in it["variants"].values()) for it in current.values())
                    print(f"[{stamp}] Baseline: {len(current)} products, {buyable} buyable. Watching for changes...")
                else:
                    for pid, it in current.items():
                        old = seen.get(pid, {"variants": {}})
                        fresh = [(vid, v) for vid, v in it["variants"].items()
                                 if v[1] and not old["variants"].get(vid, ("", False))[1]]
                        if not fresh:
                            continue
                        is_watched = watched(it["title"])
                        tag = "🚨 WATCHED" if is_watched else "new/restock"
                        names = ", ".join(v[0] for _, v in fresh if v[0] != "Default Title")
                        print(f"\n[{stamp}] {tag}: {it['title']}" + (f" ({names})" if names else "") + f" ${fresh[0][1][2]}")
                        vid = fresh[0][0]
                        if is_watched and vid not in opened:
                            opened.add(vid)
                            url = f"{SHOP}/cart/{vid}:{qty}"
                            print(f"   ⚡ Opening checkout x{qty}: {url}")
                            if DRY_OPEN:
                                print("   (dry run: not opening browser)")
                            else:
                                webbrowser.open(url)
                            alarm()
                        else:
                            print(f"   View: {SHOP}/products/{it['handle']}")
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
