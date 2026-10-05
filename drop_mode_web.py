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
import secrets
import subprocess
import threading
import time
import traceback
import webbrowser
from contextlib import closing
from datetime import datetime, timezone
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
FRIENDS_FILE = os.path.join(dm.SOUND_DIR, "friends.json")      # friends with their own wishlists (pinged, never opened here)
PINGED_FILE = os.path.join(dm.SOUND_DIR, "pinged.json")        # what friends were pinged about (so a restart doesn't ping again)
# The stock-alert bot, where people keep wishlists of their own with /wishlist (drop mode reads them with your key)
BOT_URL = (os.environ.get("DROP_BOT_URL") or "https://tofu-stock-watch.alex-mangin35.workers.dev").rstrip("/")
LISTS_EVERY = float(os.environ.get("DROP_LISTS_EVERY") or 5)  # seconds between looks at them (so a new item pings within seconds)
PING_CARDS = 5  # pings with buttons for one person at once; any more go in one message
WEBHOOK = re.compile(r"^https://(?:(?:ptb|canary)\.)?discord(?:app)?\.com/api/webhooks/\d+/[\w-]+(?:\?[\w=&-]*)?$")
if os.environ.get("DROP_DISCORD_TEST") == "1":  # tests only: a pretend Discord on this PC
    WEBHOOK = re.compile(r"^http://127\.0\.0\.1:\d+/api/webhooks/\d+/[\w-]+$")
# The page's background picture (yours, kept in the settings folder, not the code)
BG_TYPES = {".webp": "image/webp", ".jpg": "image/jpeg", ".png": "image/png", ".gif": "image/gif"}
# Pictures you've given watches the shop has no picture for (kept in the settings folder, not the code)
PICTURES_DIR = os.path.join(dm.SOUND_DIR, "pictures")
PICTURE_NAME = re.compile(r"^[0-9a-f]{12}\.(?:webp|jpg|png|gif)$")


def prune_pictures():
    """Delete pictures no watchlist uses any more (not brand-new ones: one may be about to go on a list)."""
    used = {w.get("picture") for W in list(WATCHERS.values()) for w in W.watchlist}
    try:
        names = os.listdir(PICTURES_DIR)
    except OSError:
        return
    for name in names:
        path = os.path.join(PICTURES_DIR, name)
        try:
            if PICTURE_NAME.match(name) and name not in used and time.time() - os.path.getmtime(path) > 600:
                os.remove(path)
        except OSError:
            pass


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
]
# Mr Tofu's Store Events & Tournaments: tickets for things at the shop in Auckland, so they're left out
# everywhere (not shown, not watched, not pinged), like in-store-only listings.
EVENTS = "store-events-tournaments"
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

    def __init__(self, sid="tofu", name="Mr Tofu", site=None, categories=CATEGORIES, watchlist_file=WATCHLIST_FILE, events=None):
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
        self.friend_pinged = load_pinged(sid)  # (friend, variant) pinged about while in stock (kept, so a restart doesn't ping again)
        self.friend_over = set()    # (friend, variant) over their max (said once)
        self.ping_lock = threading.Lock()
        self.no_hook_said = False   # (friends can't be pinged without a webhook: said once)
        self.events = []        # [{id, t, kind, text, url?}]
        self.last_poll = 0.0    # when the page last asked for events
        self.thread = None
        self.checker = dm.Checker(self.site)  # keeps one connection to the shop open while watching
        self.gen = 0            # bumped on start/stop, so an old watch thread knows to quit
        self.restart = False    # start a fresh batch of checks (new speed or new login)
        self.slow_downs = 0     # "slow down" replies in a row
        self.trouble = None     # what's going wrong with the checks, if anything
        self.left_out = {}      # listings left out, by why: in-store only (can't be bought online), store events
        self.events_collection = events  # the shop's events section, if it has one (Mr Tofu's)
        self.event_ids = set()  # products in it (loaded with the menu headers)
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
            self.products, self.left_out = dm.summarise_all(body)
            self.products = self.without_events(self.products)
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
                self.log("info", f"Shop has {info} products{self.left_out_text()}.")
        if ok:
            if info + sum(self.left_out.values()) >= 250:
                self.log("warn", "That's as many as drop mode can read at once (250), so anything past them isn't watched."
                         + ("" if self.id == "tofu" else " To watch just one section, remove this tab and add the section's link (…/collections/…)."))
            self.load_categories_soon()
            if self.running:  # logged in mid-watch (e.g. after a lock): catch anything that went live meanwhile
                self.checkout(self.went_live(before, self.products, quiet=True))
                self.ping_friends(before, self.products)
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

    def without_events(self, products):
        """Leave out what's in the shop's events section (the description check in drop mode catches
        new ones before the section's list is next loaded)."""
        if not self.event_ids:
            return products
        keep = {pid: it for pid, it in products.items() if pid not in self.event_ids}
        if len(keep) < len(products):
            self.left_out["store events"] = self.left_out.get("store events", 0) + len(products) - len(keep)
        return keep

    def left_out_text(self):
        bits = [f"{n} in-store only" if why == "in-store only" else f"{n} store event{'' if n == 1 else 's'}"
                for why, n in self.left_out.items() if n]
        return f" (and {' and '.join(bits)}, which drop mode leaves out)" if bits else ""

    def load_categories_soon(self):
        threading.Thread(target=self.load_categories, daemon=True).start()

    def load_categories(self):
        """Which products are in each of the shop's menu headers (a few requests,
        done in the background after the shop loads). Only Mr Tofu's has them."""
        self.cats_at = 0.0
        found = {cid: set().union(*(self.collection_ids(h) for h in handles)) for cid, _, handles in self.cat_cfg}
        if self.events_collection:  # (and what's in the events section, to leave out)
            self.event_ids = self.collection_ids(self.events_collection)
            self.products = self.without_events(self.products)
        self.categories, self.cats_at = found, time.time()

    def collection_ids(self, handle):
        """The ids of the products in one of the shop's collections."""
        ids = set()
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
        return ids

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

    def listing(self, flt, cat="", game="", limit=40, everything=False, sort="new"):
        """The exact-product search: what you can buy right now (or with
        everything, sold-out and coming-soon ones too), optionally in one of
        Tofu's categories, and one game (for the live stream). Newest listings
        first, or (sort "az") in stock first, then A to Z."""
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
        if sort == "az":
            out.sort(key=lambda x: ({"buyable": 0, "soon": 1}.get(x["state"], 2), x["title"]))  # in stock first
        else:
            out.sort(key=lambda x: (-listed_at(x["listed"]), x["title"]))  # newest listings first
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
            parts, picture = [], None
            for vid, q, t in items:
                it = next((p for p in self.products.values() if vid in p["variants"]), None)
                parts.append((q, t, it["variants"][vid][2] if it else None, (it or {}).get("limit")))
                picture = picture or (it or {}).get("image")
            DISCORD.send(DISCORD.checkout_message(self.name, url, parts, picture), self.log)

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

    # ---- friends' wishlists: they get a Discord ping with checkout links; nothing opens here ----
    def friend_items(self):
        """[(friend, kind, product id or keywords, qty, max, item)]: friends' wishlists for this shop,
        then the ones people keep in Discord (keywords, watched on every shop; item is the entry from
        the bot: its key, for the button on the ping that takes it off their list, and when it was added)."""
        friends, out = {f["id"]: f for f in FRIENDS}, []
        for w in self.watchlist:
            f = friends.get(w.get("who"))
            key = (w["id"] if w["kind"] == "product" else keywords(w["text"])) if f else None
            if key:
                out.append((f, w["kind"], key, w["qty"], w.get("max"), None))
        for person in LISTS.people:
            out += [(person, "words", keywords(it["text"]), it["qty"], it.get("max"), it) for it in person["items"]]
        return out

    def ping_friends(self, old, current, first=False):
        """Ping friends when something on their wishlist comes on sale (and when watching starts,
        or their list changes, about what's on sale already: once, even after a restart)."""
        items = self.friend_items()
        if not items:
            return
        with self.ping_lock:
            done, todo = set(), {}
            for pid, it in current.items():
                live = [vid for vid, v in it["variants"].items() if v[1]]
                if not first:
                    before = old.get(pid, {"variants": {}})["variants"]
                    live = [vid for vid in live if not before.get(vid, ("", False))[1]]
                if not live:
                    continue
                vid = live[0]
                for friend, kind, key, qty, cap, item in items:
                    seen = (friend["id"], vid)  # (one ping per person per product, each time)
                    # Told about it already, for this entry (one taken off the list and added again is new)
                    told = (f"{friend['id']}@{item.get('added') or 0}" if item else friend["id"], vid)
                    if seen in done or (first and told in self.friend_pinged):
                        continue
                    if not (key == pid if kind == "product" else keyword_match(key, it["title"])):
                        continue
                    price = it["variants"][vid][2]
                    try:
                        over = cap is not None and float(price) > cap + 1e-9
                    except (TypeError, ValueError):
                        over = False
                    if over:
                        if seen not in self.friend_over:
                            self.friend_over.add(seen)
                            self.log("info", f"Didn't ping {friend['name']} about {it['title'][:60]}: ${price} is over their max of ${cap:.2f}.")
                        continue
                    done.add(seen)
                    if not friend.get("hook") and not DISCORD.friends_hook():
                        if not self.no_hook_said:
                            self.no_hook_said = True
                            self.log("warn", f"{it['title'][:60]} is on {friend['name']}'s wishlist, but drop mode can't ping them "
                                             "until a Discord webhook is set up in ⚙ Settings.")
                        continue
                    self.friend_pinged.add(told)
                    todo.setdefault(friend["id"], (friend, []))[1].append((it, vid, dm.capped(qty, it), price, item and item.get("key")))
            for friend, pings in todo.values():
                where = " in their channel" if friend.get("hook") else ""
                for it, vid, q, price, _ in pings:
                    self.log("info", f"📣 Pinged {friend['name']}{where} on Discord: x{q} {it['title'][:70]} ${price}",
                             f"{self.site.shop}/products/{it['handle']}")
                # A ping each (with buttons), but not dozens at once: past a few, the rest go in one message
                msgs = [DISCORD.friend_message(friend, self.name, self.site.shop, it, vid, q, price, item)
                        for it, vid, q, price, item in pings[:PING_CARDS]]
                if len(pings) > PING_CARDS:
                    msgs.append(DISCORD.more_message(friend, self.name, self.site.shop, pings[PING_CARDS:]))
                DISCORD.send(msgs, self.log, friend.get("hook") or DISCORD.friends_hook(), friend["name"] if friend.get("hook") else None)
            # Remember who's been told about what's in stock (forgetting what's sold out, so a restock pings again)
            live_now = {vid for it in current.values() for vid, v in it["variants"].items() if v[1]}
            kept = {x for x in self.friend_pinged if x[1] in live_now}
            if todo or kept != self.friend_pinged:
                self.friend_pinged = kept
                save_pinged(self.id, kept)

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
        clean, friend_ids = [], {f["id"] for f in FRIENDS}
        for w in items[:200] if isinstance(items, list) else []:
            if isinstance(w, dict) and w.get("who") and w["who"] not in friend_ids:
                continue  # for a friend who's gone: never let it become yours (it'd open checkout here)
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
            if isinstance(w, dict) and w.get("who") in friend_ids:
                clean[-1]["who"] = w["who"]  # on a friend's wishlist: pings them, never opens checkout here
            pic = str(w.get("picture") or "")
            if PICTURE_NAME.match(pic) and os.path.exists(os.path.join(PICTURES_DIR, pic)):
                clean[-1]["picture"] = pic  # your own picture (shown while the shop has none)
        self.watchlist = clean
        self.wl_version += 1
        os.makedirs(dm.SOUND_DIR, exist_ok=True)
        with open(self.watchlist_file, "w", encoding="utf-8") as f:
            json.dump(clean, f)
        prune_pictures()
        if self.running:
            mine = [w for w in clean if not w.get("who")]
            self.start([{"id": w["id"], "qty": w["qty"], "max": w.get("max")} for w in mine if w["kind"] == "product"],
                       [{"text": w["text"], "qty": w["qty"], "max": w.get("max")} for w in mine if w["kind"] == "words"],
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
            self.opened, self.skipped, self.capped, self.friend_over = set(), set(), set(), set()
            self.running = True
            self.gen += 1
            self.thread = threading.Thread(target=self.loop, args=(self.gen,), daemon=True)
            self.thread.start()
        upto = lambda cap: f" (max ${cap:.2f} each)" if cap else ""
        summary = [f"{self.products[p]['title'][:60] if p in self.products else 'product ' + p} x{q}{upto(cap)}"
                   for p, (q, cap) in self.picks.items()]
        summary += [f"keywords [{t}] x{q}{upto(cap)}" for _, q, t, cap in self.words]
        theirs = [x for x in self.friend_items() if not x[0].get("hook")]
        if theirs:
            summary.append(f"friends' wishlists ({len(theirs)} item{'' if len(theirs) == 1 else 's'})")
        kept = sum(len(p["items"]) for p in LISTS.people)
        if kept:
            summary.append(f"wishlists from Discord ({len(LISTS.people)} {'person' if len(LISTS.people) == 1 else 'people'}, "
                           f"{kept} thing{'' if kept == 1 else 's'})")
        self.log("info", "Watching every %gs for: %s" % (self.interval, "; ".join(summary) if summary else "nothing yet (announcing changes only)"))
        if open_now:
            self.checkout(self.ready_items())
        self.ping_friends({}, self.products, first=True)

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
            current, self.left_out = dm.summarise_all(body)
            current = self.without_events(current)
            self.etag = etag or None
            ready = self.went_live(self.products, current)
            self.ping_friends(self.products, current)
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
        self.on_live = bool(saved.get("live", True))  # Tofu goes live on Twitch
        hook = str(saved.get("friends_webhook") or "")
        self.friends_webhook = hook if WEBHOOK.match(hook) else ""  # (optional: friends' pings to another channel)
        self.bot_key = str(saved.get("bot_key") or "")  # for reading the wishlists people keep in Discord (a secret too)
        self.failing = set()    # webhooks that aren't working (said once, not on every ping)
        self.hook_locks = {}    # one message at a time per channel, in order

    @staticmethod
    def hint(hook):
        start, _, rest = hook.partition("/api/webhooks/")
        return f"{start}/api/webhooks/{rest.split('/')[0][:6]}…" if hook else ""  # (never the secret part)

    def friends_hook(self):
        return self.friends_webhook or self.webhook

    def info(self):
        return {"set": bool(self.webhook), "hint": self.hint(self.webhook), "friendsSet": bool(self.friends_webhook),
                "friendsHint": self.hint(self.friends_webhook), "user": self.user, "botUser": my_discord_id(),
                "checkout": self.on_checkout, "lock": self.on_lock, "live": self.on_live,
                "botKeySet": bool(self.bot_key), "lists": LISTS.info()}

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
        theirs = str(data.get("friendsWebhook") or "").strip()
        if data.get("removeFriends"):
            self.friends_webhook = ""
        elif theirs:
            if not WEBHOOK.match(theirs):
                return "That isn't a Discord webhook link (for the friends' channel)."
            self.friends_webhook = theirs
        key = str(data.get("botKey") or "").strip()
        if data.get("removeBotKey"):
            self.bot_key = ""
        elif key:
            if not re.fullmatch(r"[\x21-\x7e]{16,256}", key):  # (one line, no spaces: it goes in a header)
                return "That doesn't look like the key: it's one long line of letters and numbers (at least 16)."
            self.bot_key = key
        self.user = re.sub(r"\D", "", str(data.get("user", self.user)))[:25]
        self.on_checkout = bool(data.get("checkout", self.on_checkout))
        self.on_lock = bool(data.get("lock", self.on_lock))
        self.on_live = bool(data.get("live", self.on_live))
        self.failing = set()
        os.makedirs(dm.SOUND_DIR, exist_ok=True)
        with open(DISCORD_FILE, "w", encoding="utf-8") as f:
            json.dump({"webhook": self.webhook, "user": self.user, "checkout": self.on_checkout, "lock": self.on_lock,
                       "live": self.on_live, "friends_webhook": self.friends_webhook, "bot_key": self.bot_key}, f)
        return None

    def _message(self, text, embed=None):
        ping = f"<@{self.user}> " if self.user else ""
        # parse: [] so a product called "@everyone" can't ping the whole server; only you get mentioned
        msg = {"content": ping + text, "allowed_mentions": {"parse": [], "users": [self.user] if self.user else []}}
        if embed:
            msg["embeds"] = [embed]
        return msg

    @staticmethod
    def card(header, colour, title, url, fields=(), picture=None, footer="", description=None):
        """A Discord embed laid out the same way for every ping."""
        embed = {"author": {"name": header[:256]}, "title": title[:256], "url": url, "color": colour,
                 "fields": [{"name": n, "value": v[:1024], "inline": True} for n, v in fields if v],
                 "footer": {"text": ("Drop mode" + (f" · {footer}" if footer else ""))[:2048]},
                 "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds")}
        if description:
            embed["description"] = description[:4096]
        if picture:  # (a small picture on the right keeps the ping compact)
            embed["thumbnail"] = {"url": picture + ("&" if "?" in picture else "?") + "width=300"}
        return embed

    @staticmethod
    def buttons(*links):
        """Link buttons under the message ([(emoji, label, url)])."""
        return [{"type": 1, "components": [{"type": 2, "style": 5, "label": label, "emoji": {"name": emoji}, "url": url}
                                           for emoji, label, url in links[:5]]}]

    def checkout_message(self, shop, url, parts, picture=None):
        # The button in the ping goes straight to Shop Pay (signed in: pay; not: the Shop Pay sign-in, rather
        # than the plain checkout form). The checkout drop mode opens on the PC stays as it is.
        link = url + ("&" if "?" in url else "?") + "payment=shop_pay"
        if len(parts) == 1:
            q, title, price, limit = parts[0]
            embed = self.card(f"⚡ Checkout opened at {shop}", 0xD9A24B, title, link,
                              [("Price", f"${price}" if price else ""), ("Quantity", str(q)), ("Limit", f"{limit} per customer" if limit else "")],
                              picture, "it's open in your browser on the PC")
            text = f"⚡ **{title[:90]}** — checkout's open on your PC!"
        else:
            embed = self.card(f"⚡ Checkout opened at {shop}", 0xD9A24B, f"{len(parts)} items in one checkout", link, (), picture,
                              "it's open in your browser on the PC",
                              "\n".join(f"• x{q} {title[:100]}" + (f" — ${price}" if price else "") for q, title, price, _ in parts[:15]))
            text = f"⚡ {len(parts)} things you're watching — checkout's open on your PC!"
        msg = self._message(text, embed)
        msg["components"] = self.buttons(("⚡", "Pay on this device", link))
        return msg

    @classmethod
    def friend_message(cls, friend, shop_name, shop, it, vid, qty, price, item=None):
        """For a friend: their @mention, the product, and buttons to check out (Shop Pay), add it to
        their cart, or look at it, all on their own device. Someone's Discord wishlist item also gets
        a button to take it off their list (the bot answers that one)."""
        checkout = f"{shop}/cart/{vid}:{qty}?payment=shop_pay"
        add = f"{shop}/cart/add?id={vid}&quantity={qty}"
        view = f"{shop}/products/{it['handle']}"
        limit = it.get("limit")
        embed = cls.card(f"🛒 In stock at {shop_name}", 0x6CC08D, it["title"], checkout,
                         [("Price", f"${price}" if price else ""), ("Quantity", str(qty)), ("Limit", f"{limit} per customer" if limit else "")],
                         it.get("image"), "Checkout goes straight to Shop Pay")
        return {"content": f"<@{friend['discord']}> 🛒 **{it['title'][:90]}** just came in stock at **{shop_name}**!",
                "allowed_mentions": {"parse": [], "users": [friend["discord"]]}, "embeds": [embed],
                "components": cls.buttons(("⚡", "Checkout", checkout), ("🛒", "Add to cart", add), ("🔎", "View", view))
                + ([cls.bot_buttons(("🗑️", "Remove from my wishlist", f"wl:drop:{item}"))] if item and friend.get("hook") else [])}

    @staticmethod
    def bot_buttons(*buttons):
        """A row of buttons the bot answers ([(emoji, label, id)]): they only work in messages sent
        through the bot's own webhooks (each Discord wishlist channel's)."""
        return {"type": 1, "components": [{"type": 2, "style": 2, "label": label, "emoji": {"name": emoji}, "custom_id": cid}
                                          for emoji, label, cid in buttons[:5]]}

    @classmethod
    def more_message(cls, friend, shop_name, shop, rest):
        """Lots on someone's wishlist came in stock at once: the rest in one message (each with its
        checkout link) rather than a ping each."""
        tidy = lambda title: title.replace("[", "(").replace("]", ")")[:90]
        lines = [f"• [{tidy(it['title'])}]({shop}/cart/{vid}:{q}?payment=shop_pay)" + (f" — ${price}" if price else "")
                 for it, vid, q, price, _ in rest[:15]]
        if len(rest) > 15:
            lines.append(f"…and {len(rest) - 15} more")
        embed = cls.card(f"🛒 Also in stock at {shop_name}", 0x6CC08D, f"{len(rest)} more from your wishlist", shop, (), None,
                         "Each link goes straight to checkout (Shop Pay)", "\n".join(lines))
        tip = " Narrow your keywords with `/wishlist` to get fewer pings." if friend.get("hook") else ""
        msg = {"content": f"…and **{len(rest)} more** things on your wishlist are in stock at **{shop_name}**.{tip}",
               "allowed_mentions": {"parse": []}, "embeds": [embed]}
        if friend.get("hook"):
            msg["components"] = [cls.bot_buttons(("📋", "My wishlist", "wl:list"))]
        return msg

    def lock_message(self, shop):
        return self._message(f"🔒 {shop} just locked the shop. Enter the password in drop mode to keep watching.")

    def live_message(self):
        return self._message("📺 Mr Tofu just went live on Twitch. Open the Shop app now, so it's quick if something drops."
                             f" <https://www.twitch.tv/{TWITCH_CHANNEL}>")

    def test_message(self):
        """A sample ping, using something on sale in Mr Tofu's shop (if it's loaded), to see what they look like."""
        tofu = WATCHERS.get("tofu")
        sample = next(((pid, it) for pid, it in (tofu.products.items() if tofu else []) if dm.buyable(it) and it.get("image")), None)
        if not sample:
            return self._message("🔔 Drop mode test: pings will show up here.")
        it = sample[1]
        vid = next(v for v, x in it["variants"].items() if x[1])
        msg = self.friend_message({"discord": ""}, tofu.name, tofu.site.shop, it, vid, 1, it["variants"][vid][2])
        msg["content"] = (f"<@{self.user}> " if self.user else "") + "🔔 Drop mode test: this is what a ping looks like. (Nothing's been added to a cart.)"
        msg["allowed_mentions"] = {"parse": [], "users": [self.user] if self.user else []}
        msg["embeds"][0]["author"]["name"] = "🔔 Test · " + msg["embeds"][0]["author"]["name"]
        return msg

    def post(self, msg, hook=None, again=True):
        """Send it now. Returns (worked, what went wrong). Never repeats the link (it's a secret).
        Buttons need with_components on a channel's webhook; if Discord won't take them, the same
        message goes again with the buttons as plain links. Too many at once: waits as long as
        Discord asks, then tries once more."""
        hook = hook or self.webhook
        if not hook:
            return False, "no webhook set"
        url = hook + ("&" if "?" in hook else "?") + "with_components=true" if msg.get("components") else hook
        try:
            res = subprocess.run(["curl", "-sS", "--max-time", "10", "-X", "POST", "-H", "Content-Type: application/json",
                                  "--data-binary", "@-", "-w", "\n%{http_code}", url],
                                 input=json.dumps(msg).encode(), capture_output=True)
        except Exception as exc:
            return False, type(exc).__name__
        body, _, code = res.stdout.decode(errors="replace").rpartition("\n")
        code = code.strip()
        if res.returncode == 0 and code.startswith("2"):
            return True, ""
        if res.returncode == 0 and code == "429" and again:
            try:
                wait = float(json.loads(body).get("retry_after", 1))
            except (ValueError, TypeError, AttributeError):
                wait = 1.0
            time.sleep(min(max(wait, 0.2), 10) + 0.1)
            return self.post(msg, hook, again=False)
        if res.returncode == 0 and code == "400" and msg.get("components"):
            plain = {k: v for k, v in msg.items() if k != "components"}
            links = "  ·  ".join(f"[{b['emoji']['name']} {b['label']}]({b['url']})" for row in msg["components"] for b in row["components"] if b.get("url"))
            if plain.get("embeds") and links:
                plain["embeds"] = [dict(plain["embeds"][0], description=((plain["embeds"][0].get("description") or "") + "\n\n" + links).strip())]
            return self.post(plain, hook, again)
        return False, f"reply {code}" if res.returncode == 0 else "couldn't reach Discord"

    def send(self, msgs, log, hook=None, who=None):
        """Send in the background (so the watching carries straight on): one message, or a few in
        order. who: whose own channel it is (someone's Discord wishlist), for the warning."""
        msgs = msgs if isinstance(msgs, list) else [msgs]
        hook = hook or self.webhook

        def go():
            with self.hook_locks.setdefault(hook, threading.Lock()):
                for msg in msgs:
                    ok, why = self.post(msg, hook)
                    if ok:
                        self.failing.discard(hook)
                    elif hook not in self.failing:
                        self.failing.add(hook)
                        log("warn", f"Couldn't ping {who} in their wishlist channel ({why}). If the channel was deleted, "
                                    "they get a new one with /wishlist add." if who else
                                    f"Couldn't ping Discord ({why}). Check the webhook in ⚙ Settings.")
        threading.Thread(target=go, daemon=True).start()


DISCORD = Discord()
PINGED_LOCK = threading.Lock()


def load_pinged(sid):
    """{(friend, variant)} this shop's friends were pinged about (while it's still in stock)."""
    try:
        with open(PINGED_FILE, encoding="utf-8") as f:
            return {tuple(x) for x in json.load(f).get(sid, []) if isinstance(x, list) and len(x) == 2}
    except (OSError, ValueError, AttributeError):
        return set()


def save_pinged(sid, pinged):
    with PINGED_LOCK:
        try:
            with open(PINGED_FILE, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            data = {}
        data = data if isinstance(data, dict) else {}
        data[sid] = sorted(list(x) for x in pinged)
        os.makedirs(dm.SOUND_DIR, exist_ok=True)
        with open(PINGED_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f)


def load_friends():
    try:
        with open(FRIENDS_FILE, encoding="utf-8") as f:
            friends = json.load(f)
        return [x for x in friends if isinstance(x, dict) and x.get("id") and x.get("name") and x.get("discord")]
    except (OSError, ValueError):
        return []


FRIENDS = load_friends()  # [{id, name, discord}]


def save_friends(items):
    """Replace the friends list (from the page). Returns an error message, or None when saved.
    A friend who's removed takes their wishlists with them."""
    global FRIENDS
    clean, ids = [], set()
    for x in items if isinstance(items, list) else []:
        if not isinstance(x, dict):
            continue
        name = re.sub(r"[^\w &'.-]", "", str(x.get("name") or "")).strip()[:30]
        discord = re.sub(r"\D", "", str(x.get("discord") or ""))
        if not name or not 15 <= len(discord) <= 25:
            return "Give each friend a name and their Discord ID (the long number from Copy User ID)."
        fid = re.sub(r"[^a-z0-9-]", "", str(x.get("id") or "")) or re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "friend"
        while fid in ids:
            fid += "-2"
        ids.add(fid)
        clean.append({"id": fid, "name": name, "discord": discord})
    gone = {f["id"] for f in FRIENDS} - ids
    FRIENDS = clean
    os.makedirs(dm.SOUND_DIR, exist_ok=True)
    with open(FRIENDS_FILE, "w", encoding="utf-8") as f:
        json.dump(clean, f, indent=1)
    for w in list(WATCHERS.values()):
        if any(i.get("who") in gone for i in w.watchlist):
            w.set_watchlist([i for i in w.watchlist if i.get("who") not in gone])
    return None


class DiscordLists:
    """Wishlists people keep themselves in Discord (/wishlist, with the stock-alert bot). Drop mode
    reads them from the bot every few seconds, with the key in ⚙ Settings, and watches them on every
    shop: each person is pinged in their own private channel (the bot made it, and tells drop mode
    its webhook). Nothing opens on this PC, and they can't be changed here."""

    def __init__(self):
        self.people = []      # [{id: "discord:<their id>", name, discord, hook, items: [{text, qty, max}]}]
        self.state = "off"    # off (no key) / ok / badkey / nokey (the bot hasn't got one) / down
        self.said = "off"     # (the state the activity log last mentioned)
        self.fails = 0        # looks in a row that couldn't reach the bot
        self.checked = None   # when the lists last came in
        self.seq = 0          # goes up when they change, so open pages show the new ones
        self.lock = threading.Lock()

    @staticmethod
    def fetch():
        """(state, the bot's answer)."""
        if not DISCORD.bot_key:
            return "off", None
        try:  # (the key goes to curl on stdin, so it isn't on a command line)
            res = subprocess.run(["curl", "-sS", "--max-time", "15", "-H", "@-", "-w", "\n%{http_code}", BOT_URL + "/wishlists"],
                                 input=f"Authorization: Bearer {DISCORD.bot_key}\n".encode(), capture_output=True)
        except Exception:
            return "down", None
        body, _, code = res.stdout.decode("utf-8", errors="replace").rpartition("\n")
        code = code.strip()
        if res.returncode != 0 or code not in ("200", "403", "503"):
            return "down", None
        if code != "200":
            return {"403": "badkey", "503": "nokey"}[code], None
        try:
            data = json.loads(body)
        except ValueError:
            return "down", None
        return ("ok", data) if isinstance(data, dict) else ("down", None)

    @staticmethod
    def clean(data):
        """The bot's answer -> people with a channel to ping and something to look for."""
        out = []
        for p in data.get("people") or []:
            if not isinstance(p, dict):
                continue
            discord, hook = re.sub(r"\D", "", str(p.get("id") or "")), str(p.get("hook") or "")
            if not 15 <= len(discord) <= 25 or not WEBHOOK.match(hook):
                continue
            items = []
            for it in p.get("items") or []:
                text = re.sub(r"\s+", " ", str(it.get("text") or "")).strip()[:100] if isinstance(it, dict) else ""
                if not any(not k.startswith("-") for k in keywords(text)):
                    continue
                try:
                    qty = max(1, min(5, int(it.get("qty") or 1)))
                except (TypeError, ValueError):
                    qty = 1
                items.append({"text": text, "qty": qty, "max": price_cap(it.get("max")),
                              "key": it["key"] if re.fullmatch(r"[0-9a-f]{8}", str(it.get("key") or "")) else None,
                              "added": it["added"] if isinstance(it.get("added"), int) else 0})
            if items:
                name = re.sub(r"[^\w &'.-]", "", str(p.get("name") or "")).strip()[:30] or "Someone"
                out.append({"id": "discord:" + discord, "name": name, "discord": discord, "hook": hook, "items": items[:15]})
        return out

    def watch(self):
        while True:
            try:
                self.update()
            except Exception as exc:
                print(f"(Couldn't look at the Discord wishlists: {exc})")
            time.sleep(LISTS_EVERY)

    def update(self):
        """Look at the lists now (every few seconds, and straight away when the key changes)."""
        with self.lock:
            state, data = self.fetch()
            old, said = self.people, self.said
            if state == "ok":
                self.people, self.checked, self.fails = self.clean(data), time.strftime("%H:%M:%S"), 0
            elif state == "down":
                self.fails += 1  # (keep watching the last lists while the bot can't be reached)
            else:
                self.people = []
            self.state = state
            self.said = state if state != "down" or self.fails * LISTS_EVERY >= 60 else said  # (under a minute without the bot isn't news)
            changed = self.people != old
            if changed:
                self.seq += 1
        self.report(said, old)
        if changed:  # someone added something that's in stock already: tell them now
            for w in list(WATCHERS.values()):
                if w.running:
                    w.ping_friends({}, w.products, first=True)

    def report(self, said, old):
        """Say so in Mr Tofu's activity log: connecting (or not), and who changed their list."""
        log = WATCHERS["tofu"].log
        if self.said != said:
            n = sum(len(p["items"]) for p in self.people)
            log(*{
                "ok": ("ok", f"🤖 Reading the wishlists people keep in Discord: {len(self.people)} "
                             f"{'person' if len(self.people) == 1 else 'people'}, {n} thing{'' if n == 1 else 's'}"
                             + (f" ({', '.join(p['name'] for p in self.people[:8])})" if self.people else "")
                             + ". Watched on every shop; each person is pinged in their own channel."),
                "down": ("warn", "🤖 Can't reach the bot for the Discord wishlists. Still watching the last ones, and still trying."),
                "badkey": ("warn", "🤖 The bot didn't accept drop mode's key, so the Discord wishlists aren't being watched (⚙ Settings → Discord)."),
                "nokey": ("warn", "🤖 The bot hasn't been given drop mode's key yet, so it can't share the Discord wishlists."),
                "off": ("info", "🤖 Stopped watching the Discord wishlists."),
            }[self.said])
            return
        if self.state != "ok":
            return
        before = {p["id"]: p for p in old}
        for p in self.people:
            had = {i["text"] for i in before.pop(p["id"], {"items": []})["items"]}
            has = {i["text"] for i in p["items"]}
            bits = [f"+ {t}" for t in sorted(has - had)] + [f"− {t}" for t in sorted(had - has)]
            if bits:
                log("info", f"📋 {p['name']}'s Discord wishlist: " + ", ".join(bits))
        for p in before.values():
            log("info", f"📋 {p['name']} emptied their Discord wishlist.")

    def info(self):
        return {"state": self.state, "people": len(self.people), "items": sum(len(p["items"]) for p in self.people),
                "checked": self.checked}

    def public(self):
        """For the page: who keeps a list in Discord, and what's on it (never their channel's webhook)."""
        return [{"id": p["id"], "name": p["name"], "discord": p["discord"], "items": p["items"]} for p in self.people]


LISTS = DiscordLists()
TWITCH_CHANNEL = "mrtofulive"
# Twitch's public thumbnail of the stream: there while he's live, otherwise it redirects to a placeholder
TWITCH_PREVIEW = os.environ.get("DROP_TWITCH_PREVIEW") or f"https://static-cdn.jtvnw.net/previews-ttv/live_user_{TWITCH_CHANNEL}-320x180.jpg"
TWITCH_EVERY = float(os.environ.get("DROP_TWITCH_EVERY") or 60)  # seconds between looks


class Live:
    """Whether Tofu is live on Twitch (looked at once a minute while drop mode
    runs). Going live gets you a heads-up ping, so you can open the Shop app on
    your phone before anything drops: once per stream (a stream that drops and
    reconnects doesn't ping again)."""

    def __init__(self):
        self.lock = threading.Lock()
        self.on = False
        self.pinged = 0.0

    @staticmethod
    def check():
        """True / False, or None if Twitch didn't answer."""
        try:
            res = subprocess.run(["curl", "-sS", "-I", "--max-time", "10", "-o", os.devnull, "-w", "%{http_code}", TWITCH_PREVIEW],
                                 capture_output=True, text=True)
        except Exception:
            return None
        code = res.stdout.strip()
        return True if code == "200" else False if code in ("301", "302", "404") else None

    def watch(self):
        first = True  # (if he's already live when drop mode starts, that's not news: no ping)
        while True:
            on = self.check()
            if on is not None:
                self.update(on, quiet=first)
                first = False
            time.sleep(TWITCH_EVERY)

    def update(self, on, quiet=False):
        with self.lock:
            if on == self.on:
                return
            self.on = on
            ping = on and not quiet and time.time() - self.pinged > 30 * 60
            if ping:
                self.pinged = time.time()
        if quiet:
            return
        tofu = WATCHERS["tofu"]
        tofu.log("info", "📺 Mr Tofu just went live on Twitch." if on else "📺 Mr Tofu's stream ended.")
        if ping and DISCORD.webhook and DISCORD.on_live:
            DISCORD.send(DISCORD.live_message(), tofu.log)


LIVE = Live()


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
        "listed": it.get("published") or "",  # when the shop put it up
    }


def listed_at(stamp):
    """'2026-10-03T12:00:00+13:00' -> seconds, for sorting newest first (0 if there's none)."""
    try:
        return datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return 0.0


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


WATCHERS = {"tofu": Watcher(events=EVENTS)}   # Mr Tofu's shop first, then the ones you've added
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
                                     everything=params.get("all") == "1", sort=params.get("sort", "new")))
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
                            "wlVersion": W.wl_version, "live": LIVE.on, "lists": LISTS.seq})
        elif path == "/api/watchlist":
            self.send_json({"items": W.watchlist, "version": W.wl_version})
        elif path.startswith("/picture/"):  # a picture you gave a watch
            name = path[len("/picture/"):]
            pic = os.path.join(PICTURES_DIR, name)
            if not PICTURE_NAME.match(name) or not os.path.exists(pic):
                self.send_json({"error": "no such picture"}, 404)
                return
            with open(pic, "rb") as f:
                body = f.read()
            self.send_response(200)
            self.send_header("Content-Type", BG_TYPES[os.path.splitext(name)[1]])
            self.send_header("Cache-Control", "max-age=31536000, immutable")  # (a new picture gets a new name)
            self.end_headers()
            self.wfile.write(body)
        elif path == "/api/stores":
            self.send_json({"stores": store_list()})
        elif path == "/api/discord":
            self.send_json(DISCORD.info())
        elif path == "/api/friends":
            self.send_json({"friends": FRIENDS, "discord": LISTS.public(), "lists": LISTS.seq})
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
        elif path == "/api/picture":  # a picture for a watch the shop has none for (the page makes it small first)
            data = base64.b64decode(data.get("data", ""))
            ext = picture_type(data)
            if not ext or len(data) > 5 * 1024 * 1024:
                self.send_json({"error": "Pick a JPG, PNG, WebP or GIF picture (up to 5 MB)."}, 400)
                return
            name = secrets.token_hex(6) + ext
            os.makedirs(PICTURES_DIR, exist_ok=True)
            with open(os.path.join(PICTURES_DIR, name), "wb") as f:
                f.write(data)
            self.send_json({"picture": name})
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
        elif path == "/api/friends":  # friends with their own wishlists
            error = save_friends(data.get("friends"))
            if error:
                self.send_json({"error": error}, 400)
                return
            self.send_json({"friends": FRIENDS, "discord": LISTS.public(), "lists": LISTS.seq})
        elif path == "/api/discord":  # your Discord ping settings
            error = DISCORD.save(data)
            if error:
                self.send_json({"error": error}, 400)
                return
            if data.get("botKey") or data.get("removeBotKey"):
                LISTS.update()  # (straight away, to say whether the key works)
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
    threading.Thread(target=LIVE.watch, daemon=True).start()  # is Tofu live? (for the heads-up ping)
    threading.Thread(target=LISTS.watch, daemon=True).start()  # the wishlists people keep in Discord
    if os.environ.get("DROP_WEB_NO_BROWSER") != "1":
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
