"""The helper for /dropmode in Discord.

Starts when you log in to Windows (a shortcut in your Startup folder runs it with pythonw, so there's no
window) and every few seconds tells the bot whether drop mode is running and watching, and asks whether
there's anything to do:

  /dropmode start   opens drop mode (Start Drop Mode.bat, as the desktop shortcut does) if it's closed, then
                    watches what it was watching last time (your saved watchlist, at your speed)
  /dropmode stop    closes drop mode
  /speed            each shop's check speed, and watching or not (it reports your shops for that)

It uses drop mode's own key for the bot (⚙ Settings > Bot), read from drop mode's settings folder, and it
only ever does those two things. What it did goes in helper.log in that folder.
"""
import json
import os
import socket
import subprocess
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
SETTINGS = os.environ.get("DROP_SOUND_DIR") or os.path.join(
    os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"), "TofuDropMode")
BOT_URL = (os.environ.get("DROP_BOT_URL") or "https://tofu-stock-watch.alex-mangin35.workers.dev").rstrip("/")
PORT = int(os.environ.get("DROP_WEB_PORT", "8765"))
DROP = f"http://127.0.0.1:{PORT}"
LAUNCH = os.environ.get("DROP_HELPER_LAUNCH") or os.path.join(HERE, "Start Drop Mode.bat")
EVERY = float(os.environ.get("DROP_HELPER_EVERY") or 5)
LOCK_PORT = int(os.environ.get("DROP_HELPER_LOCK_PORT") or 8763)  # (held while running: only one helper at a time)
LOG = os.path.join(SETTINGS, "helper.log")
QUIET = getattr(subprocess, "CREATE_NO_WINDOW", 0)  # (no console windows flashing up)


def log(text):
    try:
        os.makedirs(SETTINGS, exist_ok=True)
        if os.path.exists(LOG) and os.path.getsize(LOG) > 200_000:  # (keep it small)
            os.replace(LOG, LOG + ".old")
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + text + "\n")
    except OSError:
        pass


def bot_key():
    try:
        with open(os.path.join(SETTINGS, "discord.json"), encoding="utf-8") as f:
            return str(json.load(f).get("bot_key") or "")
    except (OSError, ValueError, AttributeError):
        return ""


def ask(url, data=None, headers=None, timeout=15):
    """JSON from a URL (posting data if given), or None."""
    req = urllib.request.Request(url, data=None if data is None else json.dumps(data).encode(),
                                 headers={"Content-Type": "application/json", "User-Agent": "TofuDropModeHelper/1.0",
                                          **(headers or {})},
                                 method="GET" if data is None else "POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as res:
            return json.loads(res.read() or b"null")
    except (urllib.error.URLError, OSError, ValueError):
        return None


def drop_state():
    """(is drop mode running, is it watching Mr Tofu's shop, [its shops: id, name, watching, speed]).
    (From the list of shops, not the page's news feed: asking that would tell drop mode its page is open,
    and then it wouldn't open checkout itself when the page is closed.)"""
    r = ask(f"{DROP}/api/stores", timeout=4)
    if r is None:
        return False, False, []
    shops = [{"id": s.get("id"), "name": s.get("name"), "running": bool(s.get("running")), "interval": s.get("interval")}
             for s in r.get("stores", []) if s.get("id")]
    return True, any(s["running"] for s in shops if s["id"] == "tofu"), shops


def start():
    up = drop_state()[0]
    opened = False
    if not up:
        log("Opening drop mode.")
        subprocess.Popen(["cmd", "/c", "start", "", LAUNCH], cwd=HERE, creationflags=QUIET)
        for _ in range(60):  # (it takes a few seconds to start)
            time.sleep(1)
            if drop_state()[0]:
                break
        else:
            return False, "Drop mode didn't open within a minute. Have a look at your PC."
        opened = True
    r = ask(f"{DROP}/api/resume", {}) or {}
    shops = r.get("watching") or []
    if not shops:
        return False, ("Drop mode's open" if opened else "Drop mode was already open") + ", but it couldn't start watching."
    return True, ("Drop mode's open" if opened else "Drop mode was already open") + " and watching " + ", ".join(shops) + "."


def remote(job):
    """/speed: one shop's speed, or start or stop watching it."""
    up, _, shops = drop_state()
    if not up:
        return False, "Drop mode is closed. Use `/dropmode start` first."
    body = {"interval": job.get("interval")} if job["action"] == "speed" else {"watch": job["action"] == "watch"}
    r = ask(f"{DROP}/api/remote?store={job.get('shop') or 'tofu'}", body)
    if not r:
        return False, "Drop mode didn't take that. Restart it, then try again."
    every = "½ second" if r["interval"] == 0.5 else f"{r['interval']:g} second" + ("" if r["interval"] == 1 else "s")
    if job["action"] == "speed":
        return True, f"{r['name']} now checks every {every}" + ("." if r["running"] else " (when it's watching).")
    return True, f"{r['name']}: " + (f"watching every {every}." if r["running"] else "stopped watching.")


def listening_pid():
    """The process answering on drop mode's port, if any."""
    try:
        out = subprocess.run(["netstat", "-ano", "-p", "TCP"], capture_output=True, text=True, creationflags=QUIET).stdout
    except OSError:
        return None
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 5 and parts[1] == f"127.0.0.1:{PORT}" and parts[3] == "LISTENING":
            return parts[4]
    return None


def parent_window(pid):
    """The command window running Start Drop Mode.bat that started this process (closing it closes both)."""
    ps = (f"$p = Get-CimInstance Win32_Process -Filter 'ProcessId={int(pid)}'; "
          "$c = Get-CimInstance Win32_Process -Filter \"ProcessId=$($p.ParentProcessId)\"; "
          "if ($c -and $c.CommandLine -like '*Start Drop Mode*') { $c.ProcessId }")
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, text=True,
                             creationflags=QUIET).stdout.strip()
    except OSError:
        return None
    return out if out.isdigit() else None


def stop():
    pid = listening_pid()
    if not pid:
        return True, "Drop mode was already closed."
    log(f"Closing drop mode (process {pid}).")
    target = parent_window(pid) or pid
    subprocess.run(["taskkill", "/PID", str(target), "/T", "/F"], capture_output=True, creationflags=QUIET)
    for _ in range(10):
        time.sleep(1)
        if not drop_state()[0]:
            return True, "Drop mode's closed."
    return False, "Drop mode didn't close. Have a look at your PC."


def main():
    lock = socket.socket()
    try:
        lock.bind(("127.0.0.1", LOCK_PORT))
    except OSError:
        return  # (another helper is already running)
    log("Helper started.")
    while True:
        try:
            key = bot_key()
            if key:
                up, watching, shops = drop_state()
                auth = {"Authorization": f"Bearer {key}"}
                r = ask(f"{BOT_URL}/dropmode/poll", {"running": up, "watching": watching, "shops": shops}, headers=auth)
                job = (r or {}).get("job")
                if job and job.get("action") in ("start", "stop", "speed", "watch", "unwatch"):
                    log(f"/dropmode {job['action']} {job.get('shop') or ''} {job.get('interval') or ''}".rstrip())
                    ok, message = (start() if job["action"] == "start" else stop() if job["action"] == "stop" else remote(job))
                    log(("Done: " if ok else "Problem: ") + message)
                    up, watching, shops = drop_state()
                    ask(f"{BOT_URL}/dropmode/done", {"id": job.get("id"), "ok": ok, "message": message,
                                                      "running": up, "watching": watching, "shops": shops}, headers=auth)
        except Exception as exc:  # (keep going whatever happens)
            log(f"Something went wrong: {exc!r}")
        time.sleep(EVERY)


if __name__ == "__main__":
    main()
