"""Drop mode in your browser.

Runs a small web app on this PC only (http://127.0.0.1:8765) that does the
same job as drop_mode.py: log in behind the shop password, pick products and
quantities, then watch every few seconds and open ⚡ checkout the moment a pick
can be bought. You still press Pay yourself.

Start it with the "Tofu Drop Mode (Browser)" shortcut on the desktop.
"""
import json
import os
import re
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import drop_mode as dm  # shop access, labels and matching come from drop mode

HOST, PORT = "127.0.0.1", int(os.environ.get("DROP_WEB_PORT", "8765"))
PAGE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "drop_mode_web.html")


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
        elif self.shop_state != "locked":
            self.log("warn", f"Couldn't list the shop (reply {info}).")
        return ok

    def logout(self):
        self.password, self.logged_in = "", False
        if os.path.exists(dm.COOKIES):
            os.unlink(dm.COOKIES)
        self.etag = None
        ok, info = self.refresh()
        self.shop_state = "open" if ok else "locked"
        self.log("info", "Logged out and forgot the password."
                 + ("" if ok else " The shop is locked, so watching can only catch new listings by name."))

    def listing(self, flt):
        """The shop list: only things you can buy right now (watch rows cover the rest)."""
        words = dm_words(flt)  # search box: every word must appear
        out = []
        for pid, it in self.products.items():
            if not dm.buyable(it):
                continue
            if words and not all(w in norm(it["title"]) for w in words):
                continue
            out.append(card(pid, it))
        out.sort(key=lambda x: x["title"])
        return out

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
        threading.Thread(target=dm.alarm, daemon=True).start()

    def target_qty(self, pid, it):
        if pid in self.picks:
            return self.picks[pid]
        for keys, q, _ in self.words:
            if keyword_match(keys, it["title"]):
                return q
        return None

    def matches(self, text, limit=6):
        """For a watch row's preview: in-stock matches as cards, plus the names of
        matches you can't buy right now (so you can see a restock would be caught)."""
        keys = keywords(text)
        if not keys:
            return {"items": [], "total": 0, "unavailable": []}
        hits = [(pid, it) for pid, it in self.products.items() if keyword_match(keys, it["title"])]
        live = sorted([(p, it) for p, it in hits if dm.buyable(it)], key=lambda x: x[1]["title"])
        gone = sorted([it for _, it in hits if not dm.buyable(it)], key=lambda it: (not dm.upcoming(it), it["title"]))
        return {
            "items": [card(p, it) for p, it in live[:limit]],
            "total": len(live),
            "unavailable": [{"title": it["title"], "state": "soldout" if dm.sold_out(it) else "soon"} for it in gone[:3]],
            "unavailableTotal": len(gone),
        }

    def start(self, picks, watches, interval):
        self.picks = {str(p["id"]): max(1, int(p["qty"])) for p in picks if str(p["id"]) in self.products}
        self.words = [(keywords(w["text"]), max(1, int(w["qty"])), w["text"].strip()) for w in watches if keywords(w["text"])]
        self.interval = max(1.0, float(interval or 3))
        if self.running:
            self.log("info", "Updated what to watch.")
        else:
            self.opened = set()  # kept across updates so changing picks never re-opens a checkout
            self.running = True
            self.thread = threading.Thread(target=self.loop, daemon=True)
            self.thread.start()
        summary = [f"{self.products[p]['title'][:60]} x{q}" for p, q in self.picks.items()]
        summary += [f"keywords [{t}] x{q}" for _, q, t in self.words]
        self.log("info", "Watching every %gs for: %s" % (self.interval, "; ".join(summary) if summary else "nothing yet (announcing changes only)"))
        # Anything wanted that's already buyable goes straight to checkout
        ready = []
        for pid, it in self.products.items():
            q = self.target_qty(pid, it)
            live = [vid for vid, v in it["variants"].items() if v[1] and vid not in self.opened]
            if q and live:
                ready.append((live[0], q, it["title"]))
        self.checkout(ready)

    def stop(self):
        self.running = False
        self.log("info", "Stopped watching.")

    def loop(self):
        wait = self.interval
        while self.running:
            try:
                code, headers, body = dm.fetch(self.etag)
                if code == 304:
                    wait = self.interval
                    with self.lock:
                        self.last_check = time.strftime("%H:%M:%S")
                elif code == 200:
                    self.etag = headers.get("etag")
                    current = {str(p["id"]): dm.summarise(p) for p in json.loads(body).get("products", [])}
                    ready = []
                    for pid, it in current.items():
                        old = self.products.get(pid, {"variants": {}})
                        fresh = [(vid, v) for vid, v in it["variants"].items()
                                 if v[1] and not old["variants"].get(vid, ("", False))[1]]
                        if not fresh:
                            continue
                        q = self.target_qty(pid, it)
                        tag = "🎯 YOUR PICK" if q else ("🚨 watch list" if dm.watched(it["title"]) else "new/restock")
                        self.log("pick" if q else "change", f"{tag}: {it['title']} ${fresh[0][1][2]}",
                                 None if q else f"{dm.SHOP}/products/{it['handle']}")
                        if q and fresh[0][0] not in self.opened:
                            ready.append((fresh[0][0], q, it["title"]))
                    self.checkout(ready)
                    self.products = current
                    with self.lock:
                        self.last_check = time.strftime("%H:%M:%S")
                    wait = self.interval
                elif code == 401:
                    if self.password and dm.login(self.password):
                        self.logged_in, self.shop_state = True, "ok"
                        self.log("ok", "🔒 The shop locked. Logged in with your password: watching behind the lock.")
                        self.etag = None
                    else:
                        if self.password:
                            self.log("warn", "🔒 The shop locked and the saved password didn't work. Log in again with the right one.")
                            self.password = ""
                        elif self.shop_state != "locked":
                            self.log("warn", "🔒 The shop is password-locked and I'm not logged in. Enter the password above.")
                        self.logged_in, self.shop_state = False, "locked"
                        wait = 10
                elif code in (429, 430, 503):
                    wait = min(wait * 2, 30)
                    self.log("warn", f"Shop said slow down ({code}); waiting {wait:g}s.")
                else:
                    self.log("warn", f"Unexpected reply {code}; retrying.")
            except Exception as exc:
                self.log("warn", f"Check failed: {exc}")
            time.sleep(wait)


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


def dm_words(text):
    return [w for w in re.split(r"\s+", norm(text)) if w]


def norm(text):
    """Lowercase and drop accents, so 'pokemon' matches 'Pokémon'."""
    import unicodedata
    text = unicodedata.normalize("NFD", text or "")
    return "".join(c for c in text if unicodedata.category(c) != "Mn").lower().strip()


def keywords(text):
    """'Delta Reign, Elite Trainer Box' -> ['delta reign', 'elite trainer box']."""
    return [re.sub(r"\s+", " ", norm(k)) for k in (text or "").split(",") if k.strip()]


def keyword_match(keys, title):
    """Every keyword (phrase) must appear in the product name."""
    t = re.sub(r"\s+", " ", norm(title))
    return all(k in t for k in keys)


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

    def do_GET(self):
        path, _, query = self.path.partition("?")
        params = dict(p.split("=", 1) for p in query.split("&") if "=" in p)
        if path == "/":
            with open(PAGE, "rb") as f:
                body = f.read()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(body)
        elif path == "/api/products":
            from urllib.parse import unquote_plus
            self.send_json({"items": W.listing(unquote_plus(params.get("filter", "")))})
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
            self.send_json(W.matches(unquote_plus(params.get("k", ""))) or {"items": [], "total": 0})
        elif path == "/api/events":
            W.last_poll = time.time()
            since = int(params.get("since", "0") or 0)
            with W.lock:
                evs = [e for e in W.events if e["id"] > since]
            self.send_json({"events": evs, "running": W.running, "loggedIn": W.logged_in,
                            "shopState": W.shop_state, "passwordSaved": bool(W.password),
                            "products": len(W.products), "lastCheck": W.last_check, "interval": W.interval})
        else:
            self.send_json({"error": "not found"}, 404)

    def do_POST(self):
        if not self.local_only():
            return
        n = int(self.headers.get("Content-Length", "0") or 0)
        data = json.loads(self.rfile.read(n) or b"{}")
        if self.path == "/api/login":
            ok = W.login(data.get("password", ""))
            self.send_json({"ok": ok, "loggedIn": W.logged_in, "shopState": W.shop_state, "products": len(W.products)})
        elif self.path == "/api/start":
            W.start(data.get("picks", []), data.get("watches", []), data.get("interval", 3))
            self.send_json({"ok": True})
        elif self.path == "/api/logout":
            W.logout()
            self.send_json({"ok": True})
        elif self.path == "/api/stop":
            W.stop()
            self.send_json({"ok": True})
        else:
            self.send_json({"error": "not found"}, 404)


def main():
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    url = f"http://{HOST}:{PORT}/"
    print(f"Mr Tofu Drop Mode is running at {url}  (close this window to stop)")
    if os.environ.get("DROP_WEB_NO_BROWSER") != "1":
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
