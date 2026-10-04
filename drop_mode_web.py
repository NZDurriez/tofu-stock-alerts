"""Drop mode in your browser.

Runs a small web app on this PC only (http://127.0.0.1:8765) that does the
same job as drop_mode.py: log in behind the shop password, pick products and
quantities, then watch every few seconds and open ⚡ checkout the moment a pick
can be bought. You still press Pay yourself. The alert sound (picked in the
page) plays from this program, so it works even when the page is in the
background. Mr Tofu's shop is built in; other Shopify shops can be added as
extra tabs, each watched separately.

Start it with the "Tofu Drop Mode" shortcut on the desktop.
"""
import base64
import json
import os
import re
import subprocess
import threading
import time
import traceback
import webbrowser
from contextlib import closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import drop_mode as dm  # shop access, labels and matching come from drop mode
import sounds

HOST, PORT = "127.0.0.1", int(os.environ.get("DROP_WEB_PORT", "8765"))
PAGE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "drop_mode_web.html")
ICON = os.path.join(os.path.dirname(os.path.abspath(__file__)), "drop_mode_icon.svg")


def price_cap(value):
    """A max price from the page: a positive number of dollars, or None (no limit)."""
    try:
        value = round(float(value), 2)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


WATCHLIST_FILE = os.path.join(dm.SOUND_DIR, "watchlist.json")  # Mr Tofu's watchlist (kept here, not in the browser)
STORES_FILE = os.path.join(dm.SOUND_DIR, "stores.json")        # other shops you've added as tabs
DISCORD_FILE = os.path.join(dm.SOUND_DIR, "discord.json")      # your Discord ping (the webhook link stays on this PC)
WEBHOOK = re.compile(r"^https://(?:(?:ptb|canary)\.)?discord(?:app)?\.com/api/webhooks/\d+/[\w-]+(?:\?[\w=&-]*)?$")
if os.environ.get("DROP_DISCORD_TEST") == "1":  # tests only: a pretend Discord on this PC
    WEBHOOK = re.compile(r"^http://127\.0\.0\.1:\d+/api/webhooks/\d+/[\w-]+$")
# The page's background picture (yours, kept in the settings folder, not the code)
BG_TYPES = {".webp": "image/webp", ".jpg": "image/jpeg", ".png": "image/png", ".gif": "image/gif"}


def background_file():
    return next((os.path.join(dm.SOUND_DIR, "background" + ext) for ext in BG_TYPES
                 if os.path.exists(os.path.join(dm.SOUND_DIR, "background" + ext))), None)


def background_info():
    path = background_file()
    return {"has": bool(path), "v": int(os.path.getmtime(path)) if path else 0}


def picture_type(data):
    """The file type of an image from its first bytes (None if it isn't one we show)."""
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    if data[:3] == b"\xff\xd8\xff":
        return ".jpg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return ".png"
    if data[:4] == b"GIF8":
        return ".gif"
    return None

# Mr Tofu's menu, left to right: (id, name, the shop collections it covers)
CATEGORIES = [
    ("live", "Mr Tofu Live Stream", ["mr-tofu-live-stream"]),
    ("pokemon", "Pokémon", ["pokemon-tcg", "pokemon-tcg-japanese", "pokemon-tcg-single-cards"]),
    ("onepiece", "One Piece", ["one-piece-tcg", "one-piece-tcg-single-cards"]),
    ("riftbound", "Riftbound", ["riftbound-league-of-legends-tcg"]),
    ("magic", "Magic: The Gathering", ["magic-the-gathering"]),
    ("gundam", "Gundam Card Game", ["gundam-card-game"]),
    ("events", "Store Events & Tournaments", ["store-events-tournaments"]),
]
# The live stream split up by game. Those products aren't in Tofu's game
# categories, so this goes by words in their names.
GAMES = [
    ("pokemon", "Pokémon", ["pokemon"]),
    ("onepiece", "One Piece", ["one piece"]),
    ("magic", "Magic", ["magic", "mtg"]),
    ("finalfantasy", "Final Fantasy", ["final fantasy"]),
    ("riftbound", "Riftbound", ["riftbound", "league of legends"]),
    ("dragonball", "Dragon Ball", ["dragon ball"]),
    ("gundam", "Gundam", ["gundam"]),
    ("weiss", "Weiss Schwarz", ["weiss schwarz"]),
]


def game_of(title):
    t = norm(title)
    return next((gid for gid, _, words in GAMES if any(w in t for w in words)), "other")


class Watcher:
    """One shop's watching: everything its tab sees, shared between the web
    requests and the watch thread."""

    def __init__(self, sid="tofu", name="Mr Tofu", site=None, categories=CATEGORIES, watchlist_file=WATCHLIST_FILE):
        self.id, self.name = sid, name
        self.site = site or dm.DEFAULT
        self.cat_cfg = categories        # menu headers to filter by (Mr Tofu's; none for other shops)
        self.watchlist_file = watchlist_file
        self.last_check = None
        self.lock = threading.Lock()
        self.login_lock = threading.Lock()  # one login at a time (the start-up look and a Load shop click)
        self.ev_seq = 0         # numbers the events; the page asks for the ones after the last it saw
        self.password = ""
        self.logged_in = False
        self.shop_state = None   # open / locked / ok / bad (from the last login)
        self.products = {}      # id -> summary (last full fetch)
        self.etag = None
        self.picks = {}         # product id -> (qty, max price or None)
        self.words = []         # [(words, qty, text, max price or None)]
        self.interval = 3.0
        self.running = False
        self.opened = set()     # variant ids already sent to checkout
        self.skipped = set()    # variant ids not opened because they cost more than your max (said once)
        self.capped = set()     # products whose per-customer limit cut the quantity (said once)
        self.events = []        # [{id, t, kind, text, url?}]
        self.last_poll = 0.0    # when the page last asked for events
        self.thread = None
        self.checker = dm.Checker(self.site)  # keeps one connection to the shop open while watching
        self.gen = 0            # bumped on start/stop, so an old watch thread knows to quit
        self.restart = False    # start a fresh batch of checks (new speed or new login)
        self.slow_downs = 0     # "slow down" replies in a row
        self.trouble = None     # what's going wrong with the checks, if anything
        self.watchlist = self.load_watchlist()  # what you're watching for (the page's list)
        self.wl_version = 1     # goes up on every change, so other open pages reload it
        self.categories = {}    # category id -> product ids in it (Tofu's menu)
        self.cats_at = 0.0      # when they were last loaded (0 = loading / never)

    def log(self, kind, text, url=None, **extra):
        with self.lock:
            self.ev_seq += 1
            ev = {"id": self.ev_seq, "t": time.strftime("%H:%M:%S"), "kind": kind, "text": text, **extra}
            if url:
                ev["url"] = url
            self.events.append(ev)
            del self.events[:-500]  # keep the feed bounded
        print(f"[{ev['t']}] [{self.name}] {text}" + (f" {url}" if url else ""))

    # ---- shop ----
    def refresh(self):
        code, headers, body = dm.fetch(site=self.site)
        if code == 200:
            self.etag = headers.get("etag")
            self.products = {str(p["id"]): dm.summarise(p) for p in json.loads(body).get("products", [])}
            return True, len(self.products)
        return False, code

    def load_soon(self):
        """Look at the shop in the background (when drop mode starts, or the shop is added)."""
        threading.Thread(target=self.login, args=(self.password,), daemon=True).start()

    def login(self, password):
        with self.login_lock:
            return self._login(password)

    def _login(self, password):
        password = password.strip() or self.password  # an empty box keeps the saved password (Log out forgets it)
        unchanged = (self.shop_state, self.password, len(self.products))
        self.shop_state = dm.login_status(password, self.site)  # open / locked / ok / bad
        self.logged_in = self.shop_state == "ok"
        # Keep a password we couldn't check yet (shop open) so it's tried if the shop locks
        self.password = password if self.shop_state in ("ok", "open") else ""
        before = self.products
        ok, info = self.refresh()
        if ok and unchanged == (self.shop_state, self.password, info):  # loaded again: one line, not the lot
            self.log("info", f"Checked the shop again: still {'open' if self.shop_state == 'open' else 'behind the password'}, {info} products.")
        else:
            self.log(*{
                "ok": ("ok", "Password accepted: watching behind the lock."),
                "bad": ("warn", "That password didn't work. Check it and log in again."),
                "open": ("info", "The shop isn't locked right now, so no password is needed."
                         + (" I'll try yours automatically if it locks." if password else "")),
                "locked": ("warn", "The shop is locked. Enter the password to see behind the lock."),
            }[self.shop_state])
            if ok:
                self.log("info", f"Shop has {info} products.")
        if ok:
            if info >= 250:
                self.log("warn", "That's as many as drop mode can read at once (250), so anything past them isn't watched."
                         + ("" if self.id == "tofu" else " To watch just one section, remove this tab and add the section's link (…/collections/…)."))
            self.load_categories_soon()
            if self.running:  # logged in mid-watch (e.g. after a lock): catch anything that went live meanwhile
                self.checkout(self.went_live(before, self.products, quiet=True))
        elif self.shop_state not in ("locked", "bad"):
            self.log("warn", f"Couldn't list the shop (reply {info}).")
        if self.running:
            self.renew()
        return ok

    def logout(self):
        with self.login_lock:
            self._logout()

    def _logout(self):
        self.password, self.logged_in = "", False
        if os.path.exists(self.site.cookies):
            os.unlink(self.site.cookies)
        self.etag = None
        ok, info = self.refresh()
        self.shop_state = "open" if ok else "locked"
        if ok:
            self.load_categories_soon()
        self.log("info", "Logged out and forgot the password."
                 + ("" if ok else " The shop is locked, so watching can only catch new listings by name."))
        if self.running:
            self.renew()

    def renew(self):
        """Make the watch pick up a new speed or login straight away."""
        self.restart = True
        self.checker.stop()

    def load_categories_soon(self):
        threading.Thread(target=self.load_categories, daemon=True).start()

    def load_categories(self):
        """Which products are in each of the shop's menu headers (a few requests,
        done in the background after the shop loads). Only Mr Tofu's has them."""
        self.cats_at = 0.0
        found = {}
        for cid, _, handles in self.cat_cfg:
            ids = set()
            for handle in handles:
                for page in range(1, 5):
                    try:
                        code, _, body = dm.curl([f"{self.site.shop}/collections/{handle}/products.json?limit=250&page={page}",
                                                 "-H", "Accept: application/json", "--compressed"], self.site.cookies)
                        batch = json.loads(body).get("products", []) if code == 200 else []
                    except Exception:
                        batch = []
                    ids.update(str(p["id"]) for p in batch)
                    if len(batch) < 250:
                        break
            found[cid] = ids
        self.categories, self.cats_at = found, time.time()

    def category_list(self, everything=False):
        if not self.cat_cfg:
            return {"loading": False, "categories": [], "liveGames": []}
        if self.products and time.time() - self.cats_at > 600 and self.cats_at:  # refresh every 10 minutes
            self.load_categories_soon()
        in_stock = {pid for pid, it in self.products.items() if dm.buyable(it)}
        pool = set(self.products) if everything else in_stock  # what the counts count
        live = [game_of(self.products[pid]["title"]) for pid in self.categories.get("live", set()) & pool]
        games = [{"id": gid, "name": name, "count": live.count(gid)} for gid, name, _ in GAMES + [("other", "Other", [])]]
        return {"loading": not self.cats_at and bool(self.products),
                "categories": [{"id": cid, "name": name, "count": len(self.categories.get(cid, set()) & pool)}
                               for cid, name, _ in self.cat_cfg],
                "liveGames": [g for g in games if g["count"]]}

    def listing(self, flt, cat="", game="", limit=40, everything=False):
        """The exact-product search: what you can buy right now (or with
        everything, sold-out and coming-soon ones too, after those), optionally
        in one of Tofu's categories, and one game (for the live stream)."""
        keys = keywords(flt)
        in_cat = self.categories.get(cat) if cat else None
        out = []
        for pid, it in self.products.items():
            if not everything and not dm.buyable(it):
                continue
            if in_cat is not None and pid not in in_cat:
                continue
            if keys and not keyword_match(keys, it["title"]):
                continue
            if game and game_of(it["title"]) != game:
                continue
            out.append(card(pid, it, self.site.shop))
        out.sort(key=lambda x: ({"buyable": 0, "soon": 1}.get(x["state"], 2), x["title"]))  # in stock first
        return {"items": out[:limit], "total": len(out)}

    def watch_info(self, ids, texts):
        """How each watchlist entry stands right now (for the watchlist panel)."""
        return {
            "picks": {pid: card(pid, self.products[pid], self.site.shop) if pid in self.products else {"state": "gone"} for pid in ids},
            "words": {text: self.matches(text, limit=3) for text in texts},
        }

    # ---- watching ----
    def checkout(self, items):
        if not items:
            return
        for vid, _, _ in items:
            self.opened.add(vid)
        url = f"{self.site.shop}/cart/" + ",".join(f"{vid}:{q}" for vid, q, _ in items)
        names = "; ".join(f"x{q} {t[:60]}" for _, q, t in items)
        # The page normally opens it (in the browser with Shop Pay). If the page
        # hasn't checked in for a few seconds (closed, or a background tab the
        # browser has slowed down), open it here in the default browser straight
        # away, and tell the page so it doesn't open a second copy later.
        by_program = time.time() - self.last_poll > 3
        self.log("checkout", f"⚡ Opening checkout: {names}", url, openedByProgram=by_program)
        if by_program:
            webbrowser.open(url)
        play_alert()
        if DISCORD.webhook and DISCORD.on_checkout:
            lines, picture = [], None
            for vid, q, t in items:
                it = next((p for p in self.products.values() if vid in p["variants"]), None)
                price = it["variants"][vid][2] if it else None
                lines.append(f"x{q} {t[:120]}" + (f" · ${price}" if price else ""))
                picture = picture or (it or {}).get("image")
            DISCORD.send(DISCORD.checkout_message(self.name, url, lines, picture), self.log)

    def target(self, pid, it, wanted=None, words=None):
        """(how many, max price) if this product is on the watchlist, else None."""
        wanted = self.picks if wanted is None else wanted
        words = self.words if words is None else words
        if pid in wanted:
            return wanted[pid]
        for keys, q, _, cap in words:
            if keyword_match(keys, it["title"]):
                return q, cap
        return None

    def affordable(self, it, vid, cap):
        """Is it within the max price you set for it (if any)? Says so once if not."""
        try:
            price = float(it["variants"][vid][2])
        except (KeyError, TypeError, ValueError):
            return True
        if cap is None or price <= cap + 1e-9:
            return True
        if vid not in self.skipped:
            self.skipped.add(vid)
            self.log("warn", f"⛔ Skipped {it['title'][:70]}: ${price:.2f} is over your max of ${cap:.2f}.",
                     f"{self.site.shop}/products/{it['handle']}")
        return False

    def limited(self, qty, it):
        """How many to put in the cart: never more than the shop's per-customer limit."""
        n = dm.capped(qty, it)
        if n < qty and it["handle"] not in self.capped:
            self.capped.add(it["handle"])
            self.log("info", f"{it['title'][:70]} is limited to {n} per customer, so checkout gets {n} (not {qty}).")
        return n

    def matches(self, text, limit=6):
        """What some keywords match right now, as cards: ones you can buy, then
        ones you can't (coming soon first, then sold out)."""
        keys = keywords(text)
        if not keys:
            return {"items": [], "total": 0, "unavailable": [], "unavailableTotal": 0}
        hits = [(pid, it) for pid, it in self.products.items() if keyword_match(keys, it["title"])]
        live = sorted([(p, it) for p, it in hits if dm.buyable(it)], key=lambda x: x[1]["title"])
        gone = sorted([(p, it) for p, it in hits if not dm.buyable(it)], key=lambda x: (not dm.upcoming(x[1]), x[1]["title"]))
        out = {
            "items": [card(p, it, self.site.shop) for p, it in live[:limit]],
            "total": len(live),
            "unavailable": [card(p, it, self.site.shop) for p, it in gone[:limit]],
            "unavailableTotal": len(gone),
        }
        need = [k for k in keys if not k.startswith("-")]
        if not hits and need != keys:  # nothing yet, but minus words rule some out: one of those, for a picture
            near = [(p, it) for p, it in self.products.items() if keyword_match(need, it["title"])]
            near.sort(key=lambda x: (not x[1].get("image"), not dm.buyable(x[1]), x[1]["title"]))
            if near:
                out["similar"] = card(*near[0], self.site.shop)
        return out

    def parse_wanted(self, picks, watches):
        """Page data -> ({product id: (qty, max)}, [(keywords, qty, text, max)])."""
        # Picks stay watched even if Tofu takes the listing down for a while
        wanted = {str(p["id"]): (max(1, int(p["qty"])), price_cap(p.get("max"))) for p in picks}
        words = [(keywords(w["text"]), max(1, int(w["qty"])), w["text"].strip(), price_cap(w.get("max")))
                 for w in watches if keywords(w["text"])]
        return wanted, words

    def ready_items(self, wanted=None, words=None):
        """What would go to checkout right now: wanted items that are buyable and not opened yet."""
        wanted = self.picks if wanted is None else wanted
        words = self.words if words is None else words
        ready = []
        for pid, it in self.products.items():
            want = self.target(pid, it, wanted, words)
            live = [vid for vid, v in it["variants"].items() if v[1] and vid not in self.opened]
            if want and live and self.affordable(it, live[0], want[1]):
                ready.append((live[0], self.limited(want[0], it), it["title"]))
        return ready

    # ---- the watchlist (kept here rather than in the browser) ----
    def load_watchlist(self):
        try:
            with open(self.watchlist_file, encoding="utf-8") as f:
                items = json.load(f)
            return items if isinstance(items, list) else []
        except (OSError, ValueError):
            return []

    def set_watchlist(self, items):
        """Save the list from a page, and if watching, watch for the new list."""
        clean = []
        for w in items[:200] if isinstance(items, list) else []:
            qty = max(1, min(5, int(w.get("qty") or 1))) if isinstance(w, dict) else 1
            cap = price_cap(w.get("max")) if isinstance(w, dict) else None
            if isinstance(w, dict) and w.get("kind") == "product" and w.get("id"):
                clean.append({"kind": "product", "id": str(w["id"]), "title": str(w.get("title", ""))[:200],
                              "image": w["image"] if isinstance(w.get("image"), str) else None, "qty": qty})
            elif isinstance(w, dict) and w.get("kind") == "words" and str(w.get("text", "")).strip():
                clean.append({"kind": "words", "text": str(w["text"]).strip()[:200], "qty": qty})
            else:
                continue
            if cap:
                clean[-1]["max"] = cap
        self.watchlist = clean
        self.wl_version += 1
        os.makedirs(dm.SOUND_DIR, exist_ok=True)
        with open(self.watchlist_file, "w", encoding="utf-8") as f:
            json.dump(clean, f)
        if self.running:
            self.start([{"id": w["id"], "qty": w["qty"], "max": w.get("max")} for w in clean if w["kind"] == "product"],
                       [{"text": w["text"], "qty": w["qty"], "max": w.get("max")} for w in clean if w["kind"] == "words"],
                       self.interval)

    def start(self, picks, watches, interval, open_now=True):
        """Start (or update) watching. Anything on the watchlist that's in stock
        right now goes straight to checkout (once); the rest opens the moment
        it's added to the shop or comes back in stock."""
        self.picks, self.words = self.parse_wanted(picks, watches)
        interval = max(0.5, float(interval or 3))
        faster_or_slower, self.interval = interval != self.interval, interval
        if self.running:
            self.log("info", "Updated what to watch.")
            if faster_or_slower:
                self.renew()
        else:
            self.opened, self.skipped, self.capped = set(), set(), set()
            self.running = True
            self.gen += 1
            self.thread = threading.Thread(target=self.loop, args=(self.gen,), daemon=True)
            self.thread.start()
        upto = lambda cap: f" (max ${cap:.2f} each)" if cap else ""
        summary = [f"{self.products[p]['title'][:60] if p in self.products else 'product ' + p} x{q}{upto(cap)}"
                   for p, (q, cap) in self.picks.items()]
        summary += [f"keywords [{t}] x{q}{upto(cap)}" for _, q, t, cap in self.words]
        self.log("info", "Watching every %gs for: %s" % (self.interval, "; ".join(summary) if summary else "nothing yet (announcing changes only)"))
        if open_now:
            self.checkout(self.ready_items())

    def went_live(self, old, current, quiet=False):
        """Wanted items that came on sale (new, or back in stock) between two
        fetches, ready for checkout. Logs each change unless quiet."""
        ready = []
        for pid, it in current.items():
            before = old.get(pid, {"variants": {}})
            fresh = [(vid, v) for vid, v in it["variants"].items()
                     if v[1] and not before["variants"].get(vid, ("", False))[1]]
            if not fresh:
                continue
            want = self.target(pid, it)
            if not quiet:
                tag = "🎯 YOUR PICK" if want else ("🚨 hot item" if dm.watched(it["title"]) else "new/restock")
                self.log("pick" if want else "change", f"{tag}: {it['title']} ${fresh[0][1][2]}",
                         None if want else f"{self.site.shop}/products/{it['handle']}")
            if want and fresh[0][0] not in self.opened and self.affordable(it, fresh[0][0], want[1]):
                ready.append((fresh[0][0], self.limited(want[0], it), it["title"]))
        return ready

    def stop(self):
        self.running = False
        self.gen += 1
        self.checker.stop()
        self.log("info", "Stopped watching.")

    def loop(self, gen):
        """Watch until stopped. Checks run over one kept-open connection
        (dm.Checker), a steady interval apart, in batches of about a minute; a
        new batch starts after a change, a login, a new speed, or a pause."""
        self.slow_downs, self.trouble = 0, None
        while self.running and self.gen == gen:
            self.restart, pause, n = False, None, 0
            try:
                with closing(self.checker.checks(self.etag, self.interval)) as checks:
                    for n, (code, etag, body, retry) in enumerate(checks, 1):
                        if not self.running or self.gen != gen or self.restart:
                            break
                        pause = self.handle(code, etag, body, retry)
                        if pause is not None:
                            break
            except Exception as exc:
                self.log("warn", f"Check failed: {exc}")
                pause = 2.0
            if not n and not self.restart:  # nothing came back at all: don't spin
                pause = max(pause or 0.0, self.interval)
            if pause:
                time.sleep(pause)

    def handle(self, code, etag, body, retry):
        """Deal with one check. Returns None to carry on with this batch, or how
        long to wait before starting a fresh one."""
        if code in (200, 304, 401):
            self.slow_downs = 0
            if self.trouble:
                self.trouble = None
                self.log("ok", "Checks are getting through again.")
            with self.lock:
                self.last_check = time.strftime("%H:%M:%S")
        if code == 304:  # nothing changed
            return None
        if code == 200:
            if self.shop_state in ("locked", "bad"):  # reopened before a working password was given
                self.shop_state = "open"
                self.log("ok", "🔓 The shop is open again. Carrying on watching.", None, alert="open")
            current = {str(p["id"]): dm.summarise(p) for p in json.loads(body).get("products", [])}
            self.etag = etag or None
            ready = self.went_live(self.products, current)
            self.products = current
            self.checkout(ready)
            return 0.0  # carry on, comparing against this new version
        if code == 401:
            if self.password and dm.login(self.password, self.site):
                self.logged_in, self.shop_state = True, "ok"
                self.log("ok", "🔒 The shop locked. Logged in with your password: watching behind the lock.")
                self.etag = None
                return 0.0  # start again with the new login
            if self.password:
                self.log("warn", "🔒 The shop locked and the saved password didn't work. Log in again with the right one.")
                self.password = ""
            if self.shop_state not in ("locked", "bad"):  # once per lock, not after each wrong try
                # Ask the page to pop up a password box (and sound the alert here)
                self.log("lock", f"🔒 {self.name} just locked the shop. Enter the password to keep watching.", None, alert="lock")
                play_alert()
                if DISCORD.webhook and DISCORD.on_lock:
                    DISCORD.send(DISCORD.lock_message(self.name), self.log)
            self.logged_in = False
            if self.shop_state != "bad":  # keep showing "wrong password" until a new try
                self.shop_state = "locked"
            return None  # keep checking, so a password (or the shop reopening) is picked up fast
        if code in (429, 430, 503):
            # Back off briefly (or as long as the shop asks), then carry on at
            # your speed: a long pause could mean missing the drop.
            self.slow_downs += 1
            wait = float(retry) if retry.replace(".", "", 1).isdigit() else (2, 5, 10)[min(self.slow_downs, 3) - 1]
            wait = min(wait, 30.0)
            self.log("warn", f"The shop said slow down ({code}); waiting {wait:g}s."
                     + (" If this keeps happening, pick a slower speed." if self.interval < 2 else ""))
            return wait
        problem = {0: "can't reach the shop", 403: "the shop refused the check (403)"}.get(code, f"unexpected reply {code}")
        if self.trouble != problem:  # say it once, not every check
            self.trouble = problem
            self.log("warn", f"Checks failing: {problem}. Still trying.")
        return None if code == 0 else (10.0 if code == 403 else 2.0)


def my_discord_id():
    """Your Discord user ID from the stock-alert bot's settings (so a ping @mentions you)."""
    try:
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "worker", "wrangler.toml"), encoding="utf-8") as f:
            m = re.search(r'DISCORD_OWNER_ID\s*=\s*"(\d+)"', f.read())
        return m.group(1) if m else ""
    except OSError:
        return ""


class Discord:
    """Pings you on Discord (through a channel's webhook) when checkout opens,
    and if you like when a shop locks. The webhook link is a secret (anyone
    with it can post there), so it's kept in the settings folder and the page
    only ever gets told whether one is set."""

    def __init__(self):
        try:
            with open(DISCORD_FILE, encoding="utf-8") as f:
                saved = json.load(f)
        except (OSError, ValueError):
            saved = {}
        hook = str(saved.get("webhook") or "")
        self.webhook = hook if WEBHOOK.match(hook) else ""
        self.user = re.sub(r"\D", "", str(saved.get("user", my_discord_id())))
        self.on_checkout = bool(saved.get("checkout", True))
        self.on_lock = bool(saved.get("lock", False))
        self.failing = False  # (say so once, not on every ping)

    def info(self):
        start, _, rest = self.webhook.partition("/api/webhooks/")
        return {"set": bool(self.webhook), "hint": f"{start}/api/webhooks/{rest.split('/')[0][:6]}…" if self.webhook else "",  # (never the secret part)
                "user": self.user, "botUser": my_discord_id(), "checkout": self.on_checkout, "lock": self.on_lock}

    def save(self, data):
        """Returns an error message, or None when saved."""
        hook = str(data.get("webhook") or "").strip()
        if data.get("remove"):
            self.webhook = ""
        elif hook:
            if not WEBHOOK.match(hook):
                return ("That isn't a Discord webhook link. In Discord: the channel's ⚙ settings → Integrations → "
                        "Webhooks → New Webhook → Copy Webhook URL.")
            self.webhook = hook
        self.user = re.sub(r"\D", "", str(data.get("user", self.user)))[:25]
        self.on_checkout = bool(data.get("checkout", self.on_checkout))
        self.on_lock = bool(data.get("lock", self.on_lock))
        self.failing = False
        os.makedirs(dm.SOUND_DIR, exist_ok=True)
        with open(DISCORD_FILE, "w", encoding="utf-8") as f:
            json.dump({"webhook": self.webhook, "user": self.user, "checkout": self.on_checkout, "lock": self.on_lock}, f)
        return None

    def _message(self, text, embed=None):
        ping = f"<@{self.user}> " if self.user else ""
        # parse: [] so a product called "@everyone" can't ping the whole server; only you get mentioned
        msg = {"content": ping + text, "allowed_mentions": {"parse": [], "users": [self.user] if self.user else []}}
        if embed:
            msg["embeds"] = [embed]
        return msg

    def checkout_message(self, shop, url, lines, picture=None):
        # The link in the ping goes straight to Shop Pay (signed in: pay; not: the Shop Pay sign-in, rather
        # than the plain checkout form). The checkout drop mode opens on the PC stays as it is.
        link = url + ("&" if "?" in url else "?") + "payment=shop_pay"
        embed = {"title": (lines[0] if len(lines) == 1 else f"{len(lines)} items")[:256], "url": link, "color": 0xD9A24B,
                 "description": ("\n".join(lines[:15]) + "\n\n" if len(lines) > 1 else "")
                 + "It's open in your browser on the PC. Or tap the title to pay with Shop Pay on this device."}
        if picture:
            embed["thumbnail"] = {"url": picture + ("&" if "?" in picture else "?") + "width=300"}
        return self._message(f"⚡ Drop mode opened checkout at {shop}", embed)

    def lock_message(self, shop):
        return self._message(f"🔒 {shop} just locked the shop. Enter the password in drop mode to keep watching.")

    def test_message(self):
        return self._message("🔔 Drop mode test: checkout pings will show up here.")

    def post(self, msg):
        """Send it now. Returns (worked, what went wrong). Never repeats the link (it's a secret)."""
        if not self.webhook:
            return False, "no webhook set"
        try:
            res = subprocess.run(["curl", "-sS", "--max-time", "10", "-X", "POST", "-H", "Content-Type: application/json",
                                  "--data-binary", "@-", "-o", os.devnull, "-w", "%{http_code}", self.webhook],
                                 input=json.dumps(msg).encode(), capture_output=True)
        except Exception as exc:
            return False, type(exc).__name__
        code = res.stdout.decode(errors="replace").strip()
        if res.returncode == 0 and code.startswith("2"):
            return True, ""
        return False, f"reply {code}" if res.returncode == 0 else "couldn't reach Discord"

    def send(self, msg, log):
        """Send it in the background (so the watching carries straight on)."""
        def go():
            ok, why = self.post(msg)
            if ok:
                self.failing = False
            elif not self.failing:
                self.failing = True
                log("warn", f"Couldn't ping Discord ({why}). Check the webhook in ⚙ Settings.")
        threading.Thread(target=go, daemon=True).start()


DISCORD = Discord()


def play_alert():
    """Sound the alert on this PC (without holding up the watching)."""
    threading.Thread(target=dm.alarm, daemon=True).start()


def sound_info():
    return {**sounds.load(),
            "sounds": [{"id": k, "name": v} for k, v in sounds.NAMES.items()],
            "files": [{"id": k, "name": v} for k, v in sounds.library().items()]}


def card(pid, it, shop=None):
    """What the page needs to show one product."""
    vs = list(it["variants"].values())
    price = next((v[2] for v in vs if v[1]), vs[0][2] if vs else None)
    img = it.get("image")
    if img:  # Shopify's CDN resizes on request; small thumbs load fast
        img += ("&" if "?" in img else "?") + "width=160"
    return {
        "id": pid, "title": it["title"], "price": price, "image": img,
        "state": "buyable" if dm.buyable(it) else "soldout" if dm.sold_out(it) else "soon",
        "url": f"{shop or dm.SHOP}/products/{it['handle']}",
        "limit": it.get("limit"),  # per customer, if the shop has one
    }


def norm(text):
    """Lowercase and drop accents, so 'pokemon' matches 'Pokémon'."""
    import unicodedata
    text = unicodedata.normalize("NFD", text or "")
    return "".join(c for c in text if unicodedata.category(c) != "Mn").lower().strip()


def keywords(text):
    """'Delta Reign, Elite Trainer Box' -> ['delta', 'reign', 'elite', 'trainer', 'box'].
    Punctuation around words doesn't count ('TCG:' -> 'tcg', '(Pre-order)' -> 'pre-order'),
    except a minus in front, which means "leave out products with this word" ('-tin')."""
    out = []
    for w in re.split(r"[\s,:;|/·•—–]+", norm(text)):
        minus = re.match(r"-+\w", w)  # (a lone "-" between words is just punctuation)
        w = re.sub(r"^\W+|\W+$", "", w)
        if w:
            out.append("-" + w if minus else w)
    return out


def keyword_match(keys, title):
    """Every word must start a word in the product name, in any order (the
    search box works the same, so what you see is what's watched). 'box'
    finds 'Boxes' but 'ex' doesn't find 'Next'; 'preorder' finds 'Pre-order'.
    A '-word' must NOT be in the name: '-tin' leaves out '... + 2 Mini Tin'
    (but not 'Destined', since words count from their start)."""
    t = norm(title)
    squashed = re.sub(r"[^\w\s]", "", t)
    has = lambda k: re.search(r"(?<!\w)" + re.escape(k), t) or re.search(r"(?<!\w)" + re.escape(k), squashed)
    need = [k for k in keys if not k.startswith("-")]
    return bool(need) and all(has(k) for k in need) and not any(has(k[1:]) for k in keys if k.startswith("-"))


def link_to_site(link):
    """'animalkingdoms.co.nz/collections/pokemon-tcg' -> ('https://animalkingdoms.co.nz', 'pokemon-tcg')."""
    link = link.strip()
    if not re.match(r"^https?://", link, re.I):
        link = "https://" + link
    m = re.match(r"^(https?://[^/?#\s]+)(?:/collections/([^/?#\s]+))?", link, re.I)
    if not m:
        return None, None
    return m.group(1).lower(), (m.group(2) or "").lower()


def load_stores():
    try:
        with open(STORES_FILE, encoding="utf-8") as f:
            stores = json.load(f)
        return [s for s in stores if isinstance(s, dict) and s.get("id") and s.get("shop")]
    except (OSError, ValueError):
        return []


def save_stores():
    os.makedirs(dm.SOUND_DIR, exist_ok=True)
    with open(STORES_FILE, "w", encoding="utf-8") as f:
        json.dump([{"id": w.id, "name": w.name, "shop": w.site.shop, "collection": w.site.collection}
                   for w in list(WATCHERS.values()) if w.id != "tofu"], f, indent=1)


def make_watcher(s):
    return Watcher(s["id"], s.get("name") or s["id"], dm.Site(s["shop"], s.get("collection", "")), [],
                   os.path.join(dm.SOUND_DIR, f"watchlist-{s['id']}.json"))


def add_store(link, name):
    """Add another Shopify shop as a tab. Returns (watcher, error)."""
    shop, collection = link_to_site(link or "")
    if not shop:
        return None, "Paste the shop's link, e.g. https://animalkingdoms.co.nz/collections/pokemon-tcg"
    same = next((w for w in list(WATCHERS.values()) if (w.site.shop, w.site.collection) == (shop, collection)), None)
    if same:  # two tabs watching the same thing would open two checkouts
        return None, f"That's already the {same.name} tab."
    site = dm.Site(shop, collection)
    try:
        code, _, body = dm.fetch(site=site)
        ok = code == 401 or (code == 200 and "products" in json.loads(body))
    except Exception:
        code, ok = "no reply", False
    if not ok:
        return None, f"That doesn't look like a Shopify shop drop mode can read (reply {code})."
    labels = site.host.split(":")[0].split(".")  # shop.example.co.nz -> "example"
    while len(labels) > 2 and labels[0] in ("www", "shop", "store", "m"):
        labels = labels[1:]
    base = labels[0]
    sid = re.sub(r"[^a-z0-9]+", "-", f"{base}-{collection}" if collection else base).strip("-") or "shop"
    while sid in WATCHERS:
        sid += "-2"
    name = re.sub(r"[^\w &'.-]", "", (name or "").strip())[:40] or base.replace("-", " ").title()
    w = make_watcher({"id": sid, "name": name, "shop": shop, "collection": collection})
    WATCHERS[sid] = w
    save_stores()
    w.log("info", f"Added {name} ({site.host}{'/collections/' + collection if collection else ''}).")
    w.load_soon()
    return w, None


def store_list():
    return [{"id": w.id, "name": w.name, "host": w.site.host, "shop": w.site.shop, "collection": w.site.collection,
             "builtin": w.id == "tofu", "categories": bool(w.cat_cfg), "running": w.running}
            for w in list(WATCHERS.values())]


WATCHERS = {"tofu": Watcher()}   # Mr Tofu's shop first, then the ones you've added
for _s in load_stores():
    WATCHERS[_s["id"]] = make_watcher(_s)
# What each tab asks about (?store=...). The rest (sound, background, the list of shops) is shared.
STORE_PATHS = {"/api/products", "/api/categories", "/api/password", "/api/match", "/api/events", "/api/watchlist",
               "/api/watchinfo", "/api/start", "/api/logout", "/api/stop", "/api/login"}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send_json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def local_only(self):
        # Refuse requests that come from other websites (only this page may control it)
        origin = self.headers.get("Origin")
        if origin and origin not in (f"http://{HOST}:{PORT}", f"http://localhost:{PORT}"):
            self.send_json({"error": "forbidden"}, 403)
            return False
        return True

    def oops(self, exc):
        """Something went wrong answering a page: show it (instead of an empty
        reply) and log it, so it can be fixed."""
        traceback.print_exc()
        sid = dict(p.split("=", 1) for p in self.path.partition("?")[2].split("&") if "=" in p).get("store", "tofu")
        (WATCHERS.get(sid) or WATCHERS["tofu"]).log("warn", f"Drop mode couldn't answer {self.path.split('?')[0]}: {exc}")
        try:
            self.send_response(500)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(f"Drop mode hit an error: {exc}".encode())
        except Exception:
            pass

    def do_GET(self):
        try:
            self.get()
        except Exception as exc:
            self.oops(exc)

    def do_POST(self):
        try:
            self.post()
        except Exception as exc:
            self.oops(exc)

    def shop_for(self, path, query):
        """The shop a request is about (its tab), from ?store=...; Mr Tofu's if none
        is named. None (and a 404 sent) if that shop isn't here (removed meanwhile)."""
        params = dict(p.split("=", 1) for p in query.split("&") if "=" in p)
        W = WATCHERS.get(params.get("store", "tofu"))
        if W is None and path in STORE_PATHS:
            self.send_json({"error": "no such shop"}, 404)
        return params, W

    def get(self):
        path, _, query = self.path.partition("?")
        params, W = self.shop_for(path, query)
        if W is None and path in STORE_PATHS:
            return
        if path == "/":
            with open(PAGE, "rb") as f:
                body = f.read()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
        elif path == "/api/products":
            from urllib.parse import unquote_plus
            self.send_json(W.listing(unquote_plus(params.get("filter", "")), params.get("cat", ""), params.get("game", ""),
                                     everything=params.get("all") == "1"))
        elif path == "/api/categories":
            self.send_json(W.category_list(everything=params.get("all") == "1"))
        elif path == "/api/password":
            # Lets the page unlock mrtofu.store in your browser with the saved password.
            # The custom header can't be sent by other websites without a CORS
            # pre-flight (which this server never approves), so only this page can read it.
            if self.headers.get("X-Drop-Mode") != "1":
                self.send_json({"error": "forbidden"}, 403)
            else:
                self.send_json({"password": W.password, "shop": W.site.shop})
        elif path == "/api/match":
            from urllib.parse import unquote_plus
            limit = max(1, min(50, int(params.get("limit", "6") or 6)))
            self.send_json(W.matches(unquote_plus(params.get("k", "")), limit))
        elif path == "/api/sound":
            self.send_json(sound_info())
        elif path in ("/favicon.svg", "/favicon.ico"):
            with open(ICON, "rb") as f:
                body = f.read()
            self.send_response(200)
            self.send_header("Content-Type", "image/svg+xml")
            self.send_header("Cache-Control", "max-age=86400")
            self.end_headers()
            self.wfile.write(body)
        elif path == "/api/background":
            self.send_json(background_info())
        elif path == "/background":
            pic = background_file()
            if not pic:
                self.send_json({"error": "no background"}, 404)
                return
            with open(pic, "rb") as f:
                body = f.read()
            self.send_response(200)
            self.send_header("Content-Type", BG_TYPES[os.path.splitext(pic)[1]])
            self.send_header("Cache-Control", "max-age=31536000")  # the page asks with ?v=<when it changed>
            self.end_headers()
            self.wfile.write(body)
        elif path == "/api/events":
            W.last_poll = time.time()
            since = int(params.get("since", "0") or 0)
            with W.lock:
                evs = [e for e in W.events if e["id"] > since]
            self.send_json({"events": evs, "running": W.running, "loggedIn": W.logged_in,
                            "shopState": W.shop_state, "passwordSaved": bool(W.password),
                            "products": len(W.products), "lastCheck": W.last_check, "interval": W.interval,
                            "wlVersion": W.wl_version})
        elif path == "/api/watchlist":
            self.send_json({"items": W.watchlist, "version": W.wl_version})
        elif path == "/api/stores":
            self.send_json({"stores": store_list()})
        elif path == "/api/discord":
            self.send_json(DISCORD.info())
        else:
            self.send_json({"error": "not found"}, 404)

    def post(self):
        if not self.local_only():
            return
        n = int(self.headers.get("Content-Length", "0") or 0)
        data = json.loads(self.rfile.read(n) or b"{}")
        path, _, query = self.path.partition("?")
        _, W = self.shop_for(path, query)
        if W is None and path in STORE_PATHS:
            return
        if path == "/api/login":
            ok = W.login(data.get("password", ""))
            self.send_json({"ok": ok, "loggedIn": W.logged_in, "shopState": W.shop_state, "products": len(W.products)})
        elif path == "/api/watchlist":
            W.set_watchlist(data.get("items", []))
            self.send_json({"items": W.watchlist, "version": W.wl_version})
        elif path == "/api/watchinfo":
            self.send_json(W.watch_info([str(i) for i in data.get("ids", [])], data.get("texts", [])))
        elif path == "/api/start":
            W.start(data.get("picks", []), data.get("watches", []), data.get("interval", 3), bool(data.get("openNow", True)))
            self.send_json({"ok": True})
        elif path == "/api/logout":
            W.logout()
            self.send_json({"ok": True})
        elif path == "/api/stop":
            W.stop()
            self.send_json({"ok": True})
        elif path == "/api/sound":  # pick a built-in sound and/or the volume
            s = sounds.load()
            if sounds.valid(data.get("sound")):
                s["sound"] = data["sound"]
            if "volume" in data:
                s["volume"] = max(5, min(100, int(data["volume"])))
            sounds.save(s)
            if data.get("play"):
                play_alert()
            self.send_json(sound_info())
        elif path == "/api/sound/custom":  # a sound file of your own (the page sends it as WAV)
            try:
                sounds.save_custom(base64.b64decode(data.get("wav", "")), data.get("name", ""))
            except Exception as exc:
                self.send_json({"error": str(exc)}, 400)
                return
            play_alert()
            self.send_json(sound_info())
        elif path == "/api/background":  # a new background picture (the page sends the file)
            data = base64.b64decode(data.get("data", ""))
            ext = picture_type(data)
            if not ext or len(data) > 25 * 1024 * 1024:
                self.send_json({"error": "Pick a JPG, PNG, WebP or GIF picture (up to 25 MB)."}, 400)
                return
            old = background_file()
            if old:
                os.remove(old)
            os.makedirs(dm.SOUND_DIR, exist_ok=True)
            with open(os.path.join(dm.SOUND_DIR, "background" + ext), "wb") as f:
                f.write(data)
            self.send_json(background_info())
        elif path == "/api/background/remove":
            old = background_file()
            if old:
                os.remove(old)
            self.send_json(background_info())
        elif path == "/api/sound/delete":  # remove one of your sound files
            sounds.delete_file(data.get("id", ""))
            self.send_json(sound_info())
        elif path == "/api/sound/test":
            play_alert()
            self.send_json({"ok": True})
        elif path == "/api/discord":  # your Discord ping settings
            error = DISCORD.save(data)
            if error:
                self.send_json({"error": error}, 400)
                return
            self.send_json(DISCORD.info())
        elif path == "/api/discord/test":
            ok, why = DISCORD.post(DISCORD.test_message())
            self.send_json({"ok": ok, "message": why})
        elif path == "/api/stores/add":  # another Shopify shop as its own tab
            w, error = add_store(data.get("link", ""), data.get("name", ""))
            if error:
                self.send_json({"error": error}, 400)
                return
            self.send_json({"stores": store_list(), "id": w.id})
        elif path == "/api/stores/remove":
            gone = WATCHERS.pop(data.get("id", ""), None) if data.get("id") != "tofu" else None
            if gone:
                if gone.running:
                    gone.stop()
                save_stores()  # (its watchlist file stays, in case you add the shop again)
            self.send_json({"stores": store_list()})
        else:
            self.send_json({"error": "not found"}, 404)


class Server(ThreadingHTTPServer):
    # On Windows, "reuse address" lets a second copy of drop mode quietly share
    # the port (two copies watching at once). Without it, a second copy can't
    # start, and just opens the one that's already running.
    allow_reuse_address = os.name != "nt"


def main():
    try:
        sounds.ensure()  # first run: make the default alert sound
    except Exception as exc:
        print(f"(Couldn't make the alert sound, so it'll beep instead: {exc})")
    url = f"http://{HOST}:{PORT}/"
    try:
        server = Server((HOST, PORT), Handler)
    except OSError:
        print(f"Drop mode is already running in another window, so this just opens it: {url}")
        print("(To restart drop mode, close the other window first.)")
        if os.environ.get("DROP_WEB_NO_BROWSER") != "1":
            webbrowser.open(url)
        time.sleep(8)
        return
    print(f"Drop mode is running at {url}  (close this window to stop)")
    others = [w.name for w in WATCHERS.values() if w.id != "tofu"]
    if others:
        print("Shops: Mr Tofu, " + ", ".join(others))
    for w in list(WATCHERS.values()):
        w.load_soon()
    if os.environ.get("DROP_WEB_NO_BROWSER") != "1":
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
