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
        self.password = password.strip()
        if os.path.exists(dm.COOKIES):
            os.unlink(dm.COOKIES)
        self.logged_in = bool(self.password) and dm.login(self.password)
        ok, info = self.refresh()
        if self.password and not self.logged_in:
            self.log("warn", "That password didn't work (or the shop isn't locked).")
        elif self.logged_in:
            self.log("ok", "Password accepted, watching behind the lock.")
        if ok:
            self.log("info", f"Shop has {info} products.")
        else:
            self.log("warn", f"Couldn't list the shop (reply {info}).")
        return ok

    def listing(self, flt):
        words = dm_words(flt)  # search box: every word must appear
        out = []
        for pid, it in self.products.items():
            if not (dm.buyable(it) or dm.upcoming(it)):
                continue  # old sold-out stock is never listed
            if words and not all(w in norm(it["title"]) for w in words):
                continue
            vs = list(it["variants"].values())
            price = next((v[2] for v in vs if v[1]), vs[0][2] if vs else None)
            out.append({
                "id": pid, "title": it["title"], "price": price,
                "state": "buyable" if dm.buyable(it) else "soldout" if dm.sold_out(it) else "soon",
                "url": f"{dm.SHOP}/products/{it['handle']}",
                "picked": self.picks.get(pid),
            })
        out.sort(key=lambda x: ({"soon": 0, "soldout": 1, "buyable": 2}[x["state"]], x["title"]))
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
        """Products a keyword watch would hit right now (any state), for the page's preview."""
        keys = keywords(text)
        if not keys:
            return []
        hits = [it for it in self.products.values() if keyword_match(keys, it["title"])]
        rank = lambda it: (0 if dm.buyable(it) else 1 if dm.upcoming(it) else 2, it["title"])
        out = []
        for it in sorted(hits, key=rank)[:limit]:
            state = "buyable" if dm.buyable(it) else "soldout" if dm.sold_out(it) else "soon"
            out.append({"title": it["title"], "state": state})
        return {"items": out, "total": len(hits)}

    def start(self, picks, watches, interval):
        self.picks = {str(p["id"]): max(1, int(p["qty"])) for p in picks if str(p["id"]) in self.products}
        self.words = [(keywords(w["text"]), max(1, int(w["qty"])), w["text"].strip()) for w in watches if keywords(w["text"])]
        self.interval = max(1.5, float(interval or 3))
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
                        self.log("info", "Logged back in behind the password.")
                        self.etag = None
                    else:
                        self.log("warn", "The shop is password-locked and I'm not logged in. Log in again with the password.")
                        wait = 10
                elif code in (429, 430, 503):
                    wait = min(wait * 2, 30)
                    self.log("warn", f"Shop said slow down ({code}); waiting {wait:g}s.")
                else:
                    self.log("warn", f"Unexpected reply {code}; retrying.")
            except Exception as exc:
                self.log("warn", f"Check failed: {exc}")
            time.sleep(wait)


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
        elif path == "/api/match":
            from urllib.parse import unquote_plus
            self.send_json(W.matches(unquote_plus(params.get("k", ""))) or {"items": [], "total": 0})
        elif path == "/api/events":
            W.last_poll = time.time()
            since = int(params.get("since", "0") or 0)
            with W.lock:
                evs = [e for e in W.events if e["id"] > since]
            self.send_json({"events": evs, "running": W.running, "loggedIn": W.logged_in,
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
            self.send_json({"ok": ok, "loggedIn": W.logged_in, "products": len(W.products)})
        elif self.path == "/api/start":
            W.start(data.get("picks", []), data.get("watches", []), data.get("interval", 3))
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
