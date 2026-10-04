"""Drop mode: watch Mr Tofu's shop every few seconds from this PC and, the
moment an item you picked becomes buyable, open its ⚡ checkout page in your
browser with a loud alert. You still press Pay yourself.

Start it with the "Tofu Drop Mode" shortcut on the desktop (or
`python drop_mode.py`). Close the window or press Ctrl+C to stop.
"""
import html
import json
import os
import re
import subprocess
import tempfile
import threading
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


class Site:
    """One shop to watch: its address, optionally just one collection of it
    (e.g. a store's Pokémon section), and its own cookie jar (for its password)."""

    def __init__(self, shop, collection="", cookies=None):
        self.shop = shop.rstrip("/")
        self.collection = collection.strip("/")
        host = re.sub(r"^https?://", "", self.shop).split("/")[0]
        self.host = host
        self.cookies = cookies or os.path.join(tempfile.gettempdir(), "drop_cookies_" + re.sub(r"[^\w.-]", "_", host) + ".txt")

    @property
    def products(self):
        """The products.json to read: the whole shop, or just the collection."""
        return (f"{self.shop}/collections/{self.collection}" if self.collection else self.shop) + "/products.json"


DEFAULT = Site(SHOP, os.environ.get("DROP_COLLECTION", ""), COOKIES)  # Mr Tofu's shop, unless told otherwise
# The alert sound picked in the browser version (made by sounds.py)
SOUND_DIR = os.environ.get("DROP_SOUND_DIR") or os.path.join(
    os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"), "TofuDropMode")
ALERT_WAV = os.path.join(SOUND_DIR, "alert.wav")


def watched(title):
    t = title.lower()
    return any(re.search(r"(?<![a-z0-9])" + re.escape(k) + r"(?![a-z0-9])", t) for k in KEYWORDS)


def curl(args, cookies=None):
    """Run curl (built into Windows) and return (status_code, headers, body)."""
    jar = cookies or COOKIES
    hdr = tempfile.NamedTemporaryFile(delete=False)
    hdr.close()
    try:
        res = subprocess.run(
            ["curl", "-sS", "--max-time", "10", "-A", UA, "-b", jar, "-c", jar,
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


def login(password, site=None):
    site = site or DEFAULT
    curl(["-X", "POST", "--data-urlencode", "form_type=storefront_password", "--data-urlencode", "utf8=✓",
          "--data-urlencode", f"password={password}", "-o", os.devnull, f"{site.shop}/password"], site.cookies)
    code, _, _ = curl([f"{site.products}?limit=1&_={time.time_ns()}"], site.cookies)
    return code == 200


def login_status(password, site=None):
    """'open' (shop isn't locked, so the password can't be checked), 'locked'
    (no password given), 'ok' (password got us in) or 'bad' (it didn't).
    Checking the lock first matters: an open shop lets anyone in, so a
    login 'working' there proves nothing."""
    site = site or DEFAULT
    if os.path.exists(site.cookies):
        os.unlink(site.cookies)
    code, _, _ = curl([f"{site.products}?limit=1&_={time.time_ns()}"], site.cookies)
    if code == 200:
        return "open"
    if not password:
        return "locked"
    return "ok" if login(password, site) else "bad"


def fetch(etag=None, site=None):
    site = site or DEFAULT
    args = [f"{site.products}?limit=250&_={time.time_ns()}", "-H", "Accept: application/json", "--compressed"]
    if etag:
        args += ["-H", f"If-None-Match: {etag}"]
    return curl(args, site.cookies)


MARK = "@@drop@@"  # ends each check's output in a Checker batch


class Checker:
    """Checks the shop again and again over ONE kept-open connection: a single
    curl is given a minute's worth of checks and paced with --rate, so each
    check takes ~0.3 s instead of ~0.6 s (no new connection and handshake every
    time) and they start a steady `interval` apart. Falls back to a new curl
    per check if this curl is too old for --rate."""

    def __init__(self, site=None):
        self.site = site or DEFAULT
        self.proc = None
        self.keep_open = True
        self.failed = False
        self.lock = threading.Lock()

    def checks(self, etag, interval, minutes=1.0):
        """Yields (status, etag, body, retry_after) for each check, for about
        `minutes`. Stop early by closing the generator, or with stop()."""
        count = max(2, round(minutes * 60 / interval))
        if self.keep_open:
            got, batch = 0, self._kept_open(etag, interval, count)
            try:
                for got, item in enumerate(batch, 1):
                    yield item
            finally:
                batch.close()
            if got or not self.failed:
                return
            self.keep_open = False  # curl can't do it: carry on the old way
        yield from self._one_by_one(etag, interval, count)

    def _kept_open(self, etag, interval, count):
        rate = f"{round(1 / interval)}/s" if interval < 1 else f"{round(60 / interval)}/m"
        # -N writes each reply straight through; the line saying how the check went
        # goes via stderr, which curl doesn't hold back (stdout would sit on it
        # until the next check), into the same pipe, so it always follows its reply.
        args = ["curl", "-s", "-N", "--max-time", "10", "-A", UA, "-b", self.site.cookies, "--compressed",
                "-H", "Accept: application/json", "--rate", rate,
                "-w", "%{stderr}\n" + MARK + " %{http_code}\t%header{etag}\t%header{retry-after}\n"]
        if etag:
            args += ["-H", f"If-None-Match: {etag}"]
        stamp = time.time_ns()
        args += [f"{self.site.products}?limit=250&_={stamp}{i}" for i in range(count)]
        proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, encoding="utf-8", errors="replace")
        with self.lock:
            self.proc = proc
        self.failed, got, body, ended = False, 0, [], False
        try:
            for line in proc.stdout:
                if line.startswith(MARK):
                    code, tag, retry = (line[len(MARK):].strip().split("\t") + ["", ""])[:3]
                    got += 1
                    yield (int(code) if code.isdigit() else 0), tag.strip(), "".join(body).strip(), retry.strip()
                    body = []
                else:
                    body.append(line)
            ended = True
        finally:
            if ended:  # curl finished (or was stopped): let it exit so its exit code is real
                try:
                    proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    pass
            self.stop(proc)
            proc.stdout.close()
        self.failed = not got and proc.returncode != 0 and not getattr(proc, "stopped", False)

    def _one_by_one(self, etag, interval, count):
        due = time.monotonic()
        for _ in range(count):
            due += interval
            code, headers, body = fetch(etag, self.site)
            yield code, headers.get("etag", ""), body, headers.get("retry-after", "")
            time.sleep(max(0.0, due - time.monotonic()))

    def stop(self, proc=None):
        """End the current batch (e.g. to start again with new settings)."""
        with self.lock:
            proc = proc or self.proc
            if proc is self.proc:
                self.proc = None
        if proc and proc.poll() is None:
            proc.stopped = True
            try:
                proc.kill()
                proc.wait(timeout=5)
            except Exception:
                pass


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


# Per-customer limits that shops write into a product's name, tags or description,
# e.g. "Limit 1 Per Person" (Mr Tofu), "This product is limited to two per customer"
# (Animal Kingdoms), "Max 2 per order". "Limited format", "Limit Break" etc. don't count.
_NUMBERS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10}
_N = r"(\d{1,3}|" + "|".join(_NUMBERS) + r")"
_PER = r"(?:\s+[a-z]+){0,2}?\s*(?:per|each|/)\s*"
_WHO = r"(?:person|customer|order|household|account|transaction)"
_LIMITS = [
    re.compile(r"\blimit(?:ed)?(?:\s+(?:of|to|is))?[\s:_-]*" + _N + r"\b" + _PER + _WHO, re.I),
    re.compile(r"\b(?:max(?:imum)?|only|strictly)(?:\s+of)?\s+" + _N + r"\b" + _PER + _WHO, re.I),
    re.compile(r"\b" + _N + r"\s*(?:per|each|/)\s*(?:person|customer|household)\b", re.I),
    re.compile(r"\blimit[\s:_-]*(?:of\s+)?" + _N + r"\b(?!\s*(?:%|percent|cop(?:y|ies)|units?|pieces?|made|worldwide))", re.I),
]


def purchase_limit(p):
    """The most one customer may buy, if the shop says so in the product's text (else None)."""
    tags = p.get("tags") or []
    text = " ".join([p.get("title") or "", " ".join(tags) if isinstance(tags, list) else str(tags),
                     html.unescape(re.sub(r"<[^>]+>", " ", p.get("body_html") or ""))])
    found = [int(m.group(1)) if m.group(1).isdigit() else _NUMBERS[m.group(1).lower()]
             for rx in _LIMITS for m in rx.finditer(text)]
    found = [n for n in found if n > 0]
    return min(found) if found else None


def capped(qty, it):
    """How many to put in the cart: what you asked for, or the shop's limit if that's lower."""
    return min(qty, it["limit"]) if it.get("limit") else qty


# Listings a shop only sells at the counter ("INSTORE ONLY …", "IN STORE …", "… IN-STORE PLAYERS
# ONLY"): drop mode leaves them out entirely, as they can't be bought online. ("in stores" doesn't count.)
IN_STORE = re.compile(r"\bin[\s-]?store\b", re.I)


def in_store_only(p):
    return bool(IN_STORE.search(p.get("title") or ""))


def summarise_all(body):
    """A products.json reply -> ({product id: summary}, how many in-store-only ones were left out)."""
    products = json.loads(body).get("products", [])
    keep = {str(p["id"]): summarise(p) for p in products if not in_store_only(p)}
    return keep, len(products) - len(keep)


def summarise(p):
    return {
        "title": p["title"],
        "handle": p["handle"],
        "published": p.get("published_at") or "",
        "image": ((p.get("images") or [{}])[0] or {}).get("src"),
        "variants": {str(v["id"]): (v.get("title") or "", bool(v.get("available")), v.get("price")) for v in p.get("variants") or []},
        "limit": purchase_limit(p),  # per customer, if the shop has one
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
        seen, _ = summarise_all(body)
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
            ready.append((live[0], capped(q, seen[pid]), seen[pid]["title"]))
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
                current, _ = summarise_all(body)
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
                        ready.append((fresh[0][0], capped(q, it), it["title"]))
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
