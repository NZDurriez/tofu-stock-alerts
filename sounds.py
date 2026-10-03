"""Alert sounds for drop mode.

The browser page picks a sound and a volume; this turns it into a WAV file
that drop mode plays on this PC the moment checkout opens or the shop locks
(see drop_mode.alarm), so it works even while the page is in the background.
The built-in sounds are made here from plain tones. "Your sounds" are files
you add in the page (it converts them to WAV and sends them over); they're
kept in a "sounds" folder in drop_mode.SOUND_DIR, not in the code.
"""
import io
import json
import math
import os
import re
import wave
from array import array

import drop_mode as dm

RATE = 22050  # plenty for alert sounds, and keeps the files small
SETTINGS = os.path.join(dm.SOUND_DIR, "sound.json")
LIBRARY = os.path.join(dm.SOUND_DIR, "sounds")         # your sound files, as WAV
OLD_CUSTOM_WAV = os.path.join(dm.SOUND_DIR, "custom.wav")  # the single file slot before the library
DEFAULT = {"sound": "chime", "volume": 80}
MAX_CUSTOM_SECONDS = 15

# Built-in sounds: id -> name shown in the page, in menu order
NAMES = {
    "chime": "Chime",
    "doorbell": "Doorbell (ding-dong)",
    "coin": "Arcade coin",
    "alarm": "Alarm clock",
    "siren": "Siren",
    "beeps": "Original beeps",
    "none": "No sound",
}


def library():
    """Your sound files: id ("file:<name>") -> name."""
    try:
        names = sorted(f[:-4] for f in os.listdir(LIBRARY) if f.lower().endswith(".wav"))
    except OSError:
        names = []
    return {f"file:{n}": n for n in names}


def file_path(sound):
    return os.path.join(LIBRARY, sound[len("file:"):] + ".wav")


def valid(sound):
    return sound in NAMES or (str(sound).startswith("file:") and os.path.exists(file_path(sound)))

# Partials as (frequency ratio, level, how long it rings compared to the note)
BELL = ((1, 1.0, 1.0), (2, 0.4, 0.55), (3, 0.18, 0.35), (4.2, 0.08, 0.2))
PURE = ((1, 1.0, 1.0),)
WARM = ((1, 1.0, 1.0), (2, 0.25, 1.0))


def add_note(buf, start, freq, dur, ring=0.0, partials=PURE, square=False):
    """Mix one note into buf. freq can be a function of time (for a siren);
    ring is how fast it fades (seconds), 0 = holds steady."""
    i0 = int(start * RATE)
    n = min(int(dur * RATE), len(buf) - i0)
    attack, release = int(0.004 * RATE), int(0.012 * RATE)  # no clicks
    levels = [lvl for _, lvl, _ in partials]
    fades = [math.exp(-1.0 / (ring * k * RATE)) if ring else 1.0 for _, _, k in partials]
    phase = 0.0
    for j in range(n):
        f = freq(j / RATE) if callable(freq) else freq
        phase += 2 * math.pi * f / RATE
        s = 0.0
        for k, (ratio, _, _) in enumerate(partials):
            v = math.sin(phase * ratio)
            if square:
                v = max(-1.0, min(1.0, v * 4))  # rounded square: buzzy but not harsh
            s += v * levels[k]
            levels[k] *= fades[k]
        env = j / attack if j < attack else 1.0
        if n - j < release:
            env *= (n - j) / release
        buf[i0 + j] += s * env


def built_in(name):
    """The samples for one of the built-in sounds."""
    if name == "chime":  # four rising bell notes
        buf = [0.0] * int(1.7 * RATE)
        for i, f in enumerate((1046.5, 1318.5, 1568.0, 2093.0)):
            add_note(buf, 0.13 * i, f, 1.2, 0.32, BELL)
    elif name == "doorbell":  # ding-dong, twice
        buf = [0.0] * int(3.6 * RATE)
        for t in (0.0, 1.7):
            add_note(buf, t, 659.3, 1.5, 0.55, BELL)
            add_note(buf, t + 0.5, 523.3, 1.4, 0.6, BELL)
    elif name == "coin":  # arcade coin, three times
        buf = [0.0] * int(1.45 * RATE)
        for k in range(3):
            add_note(buf, 0.42 * k, 987.8, 0.08, 0, PURE, square=True)
            add_note(buf, 0.42 * k + 0.08, 1318.5, 0.34, 0.12, PURE, square=True)
    elif name == "alarm":  # beep-beep-beep-beep, three times
        buf = [0.0] * int(2.5 * RATE)
        for g in range(3):
            for b in range(4):
                add_note(buf, 0.85 * g + 0.12 * b, 1200.0, 0.07, 0, PURE, square=True)
    elif name == "siren":  # rising and falling wail
        buf = [0.0] * int(2.7 * RATE)
        add_note(buf, 0, lambda t: 850 + 350 * math.sin(2 * math.pi * 1.1 * t - math.pi / 2), 2.6, 0, WARM)
    elif name == "beeps":  # what drop mode used to play
        buf = [0.0] * int(1.3 * RATE)
        for t, f in zip((0, .18, .36, .7, .88, 1.06), (1400, 1900, 2400) * 2):
            add_note(buf, t, f, 0.16, 0.03, PURE)
    else:  # "none": a moment of silence, so nothing plays
        buf = [0.0] * int(0.1 * RATE)
    return buf


def read_wav(path):
    """(samples, rate) of one of your sound files."""
    with wave.open(path, "rb") as w:
        rate = w.getframerate()
        pcm = array("h", w.readframes(w.getnframes()))
    return [s / 32768 for s in pcm], rate


def write_wav(path, samples, rate, volume):
    """Make the loudest point the chosen volume and save as 16-bit mono WAV."""
    peak = max((abs(s) for s in samples), default=0) or 1.0
    scale = 0.9 / peak * volume / 100 * 32767
    pcm = array("h", (int(max(-32767, min(32767, s * scale))) for s in samples))

    def write(target):
        with wave.open(target, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(rate)
            w.writeframes(pcm.tobytes())
    if isinstance(path, str):
        write(path + ".tmp")
        os.replace(path + ".tmp", path)  # never play a half-written file
    else:  # a file object
        write(path)


def load():
    settings = dict(DEFAULT)
    try:
        with open(SETTINGS, encoding="utf-8") as f:
            settings.update(json.load(f))
    except (OSError, ValueError):
        pass
    if os.path.exists(OLD_CUSTOM_WAV):  # move the old single sound file into the library
        name = safe_name(settings.get("customName") or "My sound")
        os.makedirs(LIBRARY, exist_ok=True)
        os.replace(OLD_CUSTOM_WAV, os.path.join(LIBRARY, name + ".wav"))
        if settings["sound"] == "custom":
            settings["sound"] = f"file:{name}"
        settings.pop("customName", None)
        with open(SETTINGS, "w", encoding="utf-8") as f:  # remember where it went
            json.dump(settings, f)
    settings.pop("customName", None)
    if not valid(settings["sound"]):
        settings["sound"] = DEFAULT["sound"]
    settings["volume"] = max(5, min(100, int(settings.get("volume") or DEFAULT["volume"])))
    return settings


def save(settings):
    os.makedirs(dm.SOUND_DIR, exist_ok=True)
    with open(SETTINGS, "w", encoding="utf-8") as f:
        json.dump(settings, f)
    render(settings)


def render(settings=None):
    """(Re)make the WAV drop mode plays, from the current settings."""
    settings = settings or load()
    os.makedirs(dm.SOUND_DIR, exist_ok=True)
    if settings["sound"].startswith("file:"):
        samples, rate = read_wav(file_path(settings["sound"]))
    else:
        samples, rate = built_in(settings["sound"]), RATE
    write_wav(dm.ALERT_WAV, samples, rate, settings["volume"] if settings["sound"] != "none" else 0)


def ensure():
    """Make sure there's a sound to play (first run, or the file was deleted)."""
    if not os.path.exists(dm.ALERT_WAV):
        render()


def safe_name(name):
    """A file name for the library from what you called the file: 'McDonalds Beep.mp3' -> 'McDonalds Beep'."""
    name = os.path.splitext(os.path.basename(name or ""))[0]
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", name).strip(" .")[:60]
    return name or "My sound"


def save_custom(wav_bytes, name):
    """Add a sound file (already converted to WAV by the page) to the library, and switch to it."""
    with wave.open(io.BytesIO(wav_bytes), "rb") as w:
        if w.getnchannels() != 1 or w.getsampwidth() != 2:
            raise ValueError("expected 16-bit mono WAV")
        if w.getnframes() > w.getframerate() * MAX_CUSTOM_SECONDS + w.getframerate():
            raise ValueError("too long")
    name = safe_name(name)
    os.makedirs(LIBRARY, exist_ok=True)
    with open(os.path.join(LIBRARY, name + ".wav"), "wb") as f:
        f.write(wav_bytes)
    settings = load()
    settings["sound"] = f"file:{name}"
    save(settings)
    return settings


def delete_file(sound):
    """Take one of your sound files out of the library (back to Chime if it was in use)."""
    if str(sound).startswith("file:") and os.path.exists(file_path(sound)):
        os.remove(file_path(sound))
    settings = load()  # falls back to the default if the one in use is gone
    save(settings)
    return settings
