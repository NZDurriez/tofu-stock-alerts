"""Drop mode in your browser.

Runs a small web app on this PC only (http://127.0.0.1:8765) that does the
same job as drop_mode.py: log in behind the shop password, pick products and
quantities, then watch every few seconds and open ⚡ checkout the moment a pick
can be bought. You still press Pay yourself. The alert sound (picked in the
page) plays from this program, so it works even when the page is in the
background.

Start it with the "Tofu Drop Mode (Browser)" shortcut on the desktop.
"""
import base64
import json
import os
import re
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
WATCHLIST_FILE = os.path.join(dm.SOUND_DIR, "watchlist.json")  # the watchlist (kept here, not in the browser)
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
    """Everything the page sees, shared between the web requests and the watch thread."""

    def __init__(self):
        self.lock = threading.Lock()
        self.password = ""
        self.logged_in = False
        self.shop_state = None   # open / locked / ok / bad (from the last login)
        self.products = {}      # id -> summary (last full fetch)
        self.etag = None
        self.picks = {}         # product id -> qty
        self.words = []         # [(words, qty, text)]
        self.interval = 3.0
        self.running = False
        self.opened = set()     # variant ids already sent to checkout
        self.events = []        # [{id, t, kind, text, url?}]
        self.last_poll = 0.0    # when the page last asked for events
        self.thread = None
        self.checker = dm.Checker()  # keeps one connection to the shop open while watching
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
            ev = {"id": len(self.events) + 1, "t": time.strftime("%H:%M:%S"), "kind": kind, "text": text, **extra}
            if url:
                ev["url"] = url
            self.events.append(ev)
            del self.events[:-500]  # keep the feed bounded
        print(f"[{ev['t']}] {text}" + (f" {url}" if url else ""))

    # ---- shop ----
    def refresh(self):
        code, headers, body = dm.fetch()
        if code == 200:
            self.etag = headers.get("etag")
            self.products = {str(p["id"]): dm.summarise(p) for p in json.loads(body).get("products", [])}
            return True, len(self.products)
        return False, code

    def login(self, password):
        password = password.strip()
        self.shop_state = dm.login_status(password)  # open / locked / ok / bad
        self.logged_in = self.shop_state == "ok"
        # Keep a password we couldn't check yet (shop open) so it's tried if the shop locks
        self.password = password if self.shop_state in ("ok", "open") else ""
        before = self.products
        ok, info = self.refresh()
        self.log(*{
            "ok": ("ok", "Password accepted: watching behind the lock."),
            "bad": ("warn", "That password didn't work. Check it and log in again."),
            "open": ("info", "The shop isn't locked right now, so no password is needed."
                     + (" I'll try yours automatically if it locks." if password else "")),
            "locked": ("warn", "The shop is locked. Enter the password to see behind the lock."),
        }[self.shop_state])
        if ok:
            self.log("info", f"Shop has {info} products.")
            self.load_categories_soon()
            if self.running:  # logged in mid-watch (e.g. after a lock): catch anything that went live meanwhile
                self.checkout(self.went_live(before, self.products, quiet=True))
        elif self.shop_state not in ("locked", "bad"):
            self.log("warn", f"Couldn't list the shop (reply {info}).")
        if self.running:
            self.renew()
        return ok

    def logout(self):
        self.password, self.logged_in = "", False
        if os.path.exists(dm.COOKIES):
            os.unlink(dm.COOKIES)
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
        """Which products are in each of Tofu's menu headers (a few requests,
        done in the background after the shop loads)."""
        self.cats_at = 0.0
        found = {}
        for cid, _, handles in CATEGORIES:
            ids = set()
            for handle in handles:
                for page in range(1, 5):
                    try:
                        code, _, body = dm.curl([f"{dm.SHOP}/collections/{handle}/products.json?limit=250&page={page}",
                                                 "-H", "Accept: application/json", "--compressed"])
                        batch = json.loads(body).get("products", []) if code == 200 else []
                    except Exception:
                        batch = []
                    ids.update(str(p["id"]) for p in batch)
                    if len(batch) < 250:
                        break
            found[cid] = ids
        self.categories, self.cats_at = found, time.time()

    def category_list(self):
        if self.products and time.time() - self.cats_at > 600 and self.cats_at:  # refresh every 10 minutes
            self.load_categories_soon()
        in_stock = {pid for pid, it in self.products.items() if dm.buyable(it)}
        live = [game_of(self.products[pid]["title"]) for pid in self.categories.get("live", set()) & in_stock]
        games = [{"id": gid, "name": name, "count": live.count(gid)} for gid, name, _ in GAMES + [("other", "Other", [])]]
        return {"loading": not self.cats_at and bool(self.products),
                "categories": [{"id": cid, "name": name, "count": len(self.categories.get(cid, set()) & in_stock)}
                               for cid, name, _ in CATEGORIES],
                "liveGames": [g for g in games if g["count"]]}

    def listing(self, flt, cat="", game="", limit=40):
        """The exact-product search: only what you can buy right now (sold out
        or unlisted things are for keyword watches), optionally in one of
        Tofu's categories, and one game (for the live stream)."""
        keys = keywords(flt)
        in_cat = self.categories.get(cat) if cat else None
        out = []
        for pid, it in self.products.items():
            if not dm.buyable(it):
                continue
            if in_cat is not None and pid not in in_cat:
                continue
            if keys and not keyword_match(keys, it["title"]):
                continue
            if game and game_of(it["title"]) != game:
                continue
            out.append(card(pid, it))
        out.sort(key=lambda x: x["title"])
        return {"items": out[:limit], "total": len(out)}

    def watch_info(self, ids, texts):
        """How each watchlist entry stands right now (for the watchlist panel)."""
        return {
            "picks": {pid: card(pid, self.products[pid]) if pid in self.products else {"state": "gone"} for pid in ids},
            "words": {text: self.matches(text, limit=3) for text in texts},
        }

    # ---- watching ----
    def checkout(self, items):
        if not items:
            return
        for vid, _, _ in items:
            self.opened.add(vid)
        url = f"{dm.SHOP}/cart/" + ",".join(f"{vid}:{q}" for vid, q, _ in items)
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

    def target_qty(self, pid, it, wanted=None, words=None):
        wanted = self.picks if wanted is None else wanted
        words = self.words if words is None else words
        if pid in wanted:
            return wanted[pid]
        for keys, q, _ in words:
            if keyword_match(keys, it["title"]):
                return q
        return None

    def matches(self, text, limit=6):
        """What some keywords match right now, as cards: ones you can buy, then
        ones you can't (coming soon first, then sold out)."""
        keys = keywords(text)
        if not keys:
            return {"items": [], "total": 0, "unavailable": [], "unavailableTotal": 0}
        hits = [(pid, it) for pid, it in self.products.items() if keyword_match(keys, it["title"])]
        live = sorted([(p, it) for p, it in hits if dm.buyable(it)], key=lambda x: x[1]["title"])
        gone = sorted([(p, it) for p, it in hits if not dm.buyable(it)], key=lambda x: (not dm.upcoming(x[1]), x[1]["title"]))
        return {
            "items": [card(p, it) for p, it in live[:limit]],
            "total": len(live),
            "unavailable": [card(p, it) for p, it in gone[:limit]],
            "unavailableTotal": len(gone),
        }

    def parse_wanted(self, picks, watches):
        """Page data -> ({product id: qty}, [(keywords, qty, text)])."""
        # Picks stay watched even if Tofu takes the listing down for a while
        wanted = {str(p["id"]): max(1, int(p["qty"])) for p in picks}
        words = [(keywords(w["text"]), max(1, int(w["qty"])), w["text"].strip()) for w in watches if keywords(w["text"])]
        return wanted, words

    def ready_items(self, wanted=None, words=None):
        """What would go to checkout right now: wanted items that are buyable and not opened yet."""
        wanted = self.picks if wanted is None else wanted
        words = self.words if words is None else words
        ready = []
        for pid, it in self.products.items():
            q = self.target_qty(pid, it, wanted, words)
            live = [vid for vid, v in it["variants"].items() if v[1] and vid not in self.opened]
            if q and live:
                ready.append((live[0], q, it["title"]))
        return ready

    # ---- the watchlist (kept here rather than in the browser) ----
    @staticmethod
    def load_watchlist():
        try:
            with open(WATCHLIST_FILE, encoding="utf-8") as f:
                items = json.load(f)
            return items if isinstance(items, list) else []
        except (OSError, ValueError):
            return []

    def set_watchlist(self, items):
        """Save the list from a page, and if watching, watch for the new list."""
        clean = []
        for w in items[:200] if isinstance(items, list) else []:
            qty = max(1, min(5, int(w.get("qty") or 1))) if isinstance(w, dict) else 1
            if isinstance(w, dict) and w.get("kind") == "product" and w.get("id"):
                clean.append({"kind": "product", "id": str(w["id"]), "title": str(w.get("title", ""))[:200],
                              "image": w["image"] if isinstance(w.get("image"), str) else None, "qty": qty})
            elif isinstance(w, dict) and w.get("kind") == "words" and str(w.get("text", "")).strip():
                clean.append({"kind": "words", "text": str(w["text"]).strip()[:200], "qty": qty})
        self.watchlist = clean
        self.wl_version += 1
        os.makedirs(dm.SOUND_DIR, exist_ok=True)
        with open(WATCHLIST_FILE, "w", encoding="utf-8") as f:
            json.dump(clean, f)
        if self.running:
            self.start([{"id": w["id"], "qty": w["qty"]} for w in clean if w["kind"] == "product"],
                       [{"text": w["text"], "qty": w["qty"]} for w in clean if w["kind"] == "words"], self.interval)

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
            self.opened = set()
            self.running = True
            self.gen += 1
            self.thread = threading.Thread(target=self.loop, args=(self.gen,), daemon=True)
            self.thread.start()
        summary = [f"{self.products[p]['title'][:60] if p in self.products else 'product ' + p} x{q}" for p, q in self.picks.items()]
        summary += [f"keywords [{t}] x{q}" for _, q, t in self.words]
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
            q = self.target_qty(pid, it)
            if not quiet:
                tag = "🎯 YOUR PICK" if q else ("🚨 hot item" if dm.watched(it["title"]) else "new/restock")
                self.log("pick" if q else "change", f"{tag}: {it['title']} ${fresh[0][1][2]}",
                         None if q else f"{dm.SHOP}/products/{it['handle']}")
            if q and fresh[0][0] not in self.opened:
                ready.append((fresh[0][0], q, it["title"]))
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
            self.checkout(self.went_live(self.products, current))
            self.products = current
            return 0.0  # carry on, comparing against this new version
        if code == 401:
            if self.password and dm.login(self.password):
                self.logged_in, self.shop_state = True, "ok"
                self.log("ok", "🔒 The shop locked. Logged in with your password: watching behind the lock.")
                self.etag = None
                return 0.0  # start again with the new login
            if self.password:
                self.log("warn", "🔒 The shop locked and the saved password didn't work. Log in again with the right one.")
                self.password = ""
            if self.shop_state not in ("locked", "bad"):  # once per lock, not after each wrong try
                # Ask the page to pop up a password box (and sound the alert here)
                self.log("lock", "🔒 Mr Tofu just locked the shop. Enter the password to keep watching.", None, alert="lock")
                play_alert()
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


def play_alert():
    """Sound the alert on this PC (without holding up the watching)."""
    threading.Thread(target=dm.alarm, daemon=True).start()


def sound_info():
    return {**sounds.load(),
            "sounds": [{"id": k, "name": v} for k, v in sounds.NAMES.items()],
            "files": [{"id": k, "name": v} for k, v in sounds.library().items()]}


def card(pid, it):
    """What the page needs to show one product."""
    vs = list(it["variants"].values())
    price = next((v[2] for v in vs if v[1]), vs[0][2] if vs else None)
    img = it.get("image")
    if img:  # Shopify's CDN resizes on request; small thumbs load fast
        img += ("&" if "?" in img else "?") + "width=160"
    return {
        "id": pid, "title": it["title"], "price": price, "image": img,
        "state": "buyable" if dm.buyable(it) else "soldout" if dm.sold_out(it) else "soon",
        "url": f"{dm.SHOP}/products/{it['handle']}",
    }


def norm(text):
    """Lowercase and drop accents, so 'pokemon' matches 'Pokémon'."""
    import unicodedata
    text = unicodedata.normalize("NFD", text or "")
    return "".join(c for c in text if unicodedata.category(c) != "Mn").lower().strip()


def keywords(text):
    """'Delta Reign, Elite Trainer Box' -> ['delta', 'reign', 'elite', 'trainer', 'box'].
    Punctuation around words doesn't count ('TCG:' -> 'tcg', '(Pre-order)' -> 'pre-order')."""
    words = re.split(r"[\s,:;|/·•—–]+", norm(text))
    return [w for w in (re.sub(r"^\W+|\W+$", "", w) for w in words) if w]


def keyword_match(keys, title):
    """Every word must start a word in the product name, in any order (the
    search box works the same, so what you see is what's watched). 'box'
    finds 'Boxes' but 'ex' doesn't find 'Next'; 'preorder' finds 'Pre-order'."""
    t = norm(title)
    squashed = re.sub(r"[^\w\s]", "", t)
    return all(re.search(r"(?<!\w)" + re.escape(k), t) or re.search(r"(?<!\w)" + re.escape(k), squashed) for k in keys)


W = Watcher()
W.last_check = None


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
        W.log("warn", f"Drop mode couldn't answer {self.path.split('?')[0]}: {exc}")
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

    def get(self):
        path, _, query = self.path.partition("?")
        params = dict(p.split("=", 1) for p in query.split("&") if "=" in p)
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
            self.send_json(W.listing(unquote_plus(params.get("filter", "")), params.get("cat", ""), params.get("game", "")))
        elif path == "/api/categories":
            self.send_json(W.category_list())
        elif path == "/api/password":
            # Lets the page unlock mrtofu.store in your browser with the saved password.
            # The custom header can't be sent by other websites without a CORS
            # pre-flight (which this server never approves), so only this page can read it.
            if self.headers.get("X-Drop-Mode") != "1":
                self.send_json({"error": "forbidden"}, 403)
            else:
                self.send_json({"password": W.password, "shop": dm.SHOP})
        elif path == "/api/match":
            from urllib.parse import unquote_plus
            limit = max(1, min(50, int(params.get("limit", "6") or 6)))
            self.send_json(W.matches(unquote_plus(params.get("k", "")), limit))
        elif path == "/api/sound":
            self.send_json(sound_info())
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
        else:
            self.send_json({"error": "not found"}, 404)

    def post(self):
        if not self.local_only():
            return
        n = int(self.headers.get("Content-Length", "0") or 0)
        data = json.loads(self.rfile.read(n) or b"{}")
        if self.path == "/api/login":
            ok = W.login(data.get("password", ""))
            self.send_json({"ok": ok, "loggedIn": W.logged_in, "shopState": W.shop_state, "products": len(W.products)})
        elif self.path == "/api/watchlist":
            W.set_watchlist(data.get("items", []))
            self.send_json({"items": W.watchlist, "version": W.wl_version})
        elif self.path == "/api/watchinfo":
            self.send_json(W.watch_info([str(i) for i in data.get("ids", [])], data.get("texts", [])))
        elif self.path == "/api/start":
            W.start(data.get("picks", []), data.get("watches", []), data.get("interval", 3), bool(data.get("openNow", True)))
            self.send_json({"ok": True})
        elif self.path == "/api/logout":
            W.logout()
            self.send_json({"ok": True})
        elif self.path == "/api/stop":
            W.stop()
            self.send_json({"ok": True})
        elif self.path == "/api/sound":  # pick a built-in sound and/or the volume
            s = sounds.load()
            if sounds.valid(data.get("sound")):
                s["sound"] = data["sound"]
            if "volume" in data:
                s["volume"] = max(5, min(100, int(data["volume"])))
            sounds.save(s)
            if data.get("play"):
                play_alert()
            self.send_json(sound_info())
        elif self.path == "/api/sound/custom":  # a sound file of your own (the page sends it as WAV)
            try:
                sounds.save_custom(base64.b64decode(data.get("wav", "")), data.get("name", ""))
            except Exception as exc:
                self.send_json({"error": str(exc)}, 400)
                return
            play_alert()
            self.send_json(sound_info())
        elif self.path == "/api/background":  # a new background picture (the page sends the file)
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
        elif self.path == "/api/background/remove":
            old = background_file()
            if old:
                os.remove(old)
            self.send_json(background_info())
        elif self.path == "/api/sound/delete":  # remove one of your sound files
            sounds.delete_file(data.get("id", ""))
            self.send_json(sound_info())
        elif self.path == "/api/sound/test":
            play_alert()
            self.send_json({"ok": True})
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
    print(f"Mr Tofu Drop Mode is running at {url}  (close this window to stop)")
    if os.environ.get("DROP_WEB_NO_BROWSER") != "1":
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
