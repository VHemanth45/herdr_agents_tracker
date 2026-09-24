"""Text formatting shared by the status line and the dashboard, and the one-line status itself."""

import math
import time
import unicodedata

STATE_TEXT = {"auth": "sign-in needed", "unavailable": "n/a", "error": "error"}
HERDR_MAX_WIDTH = 80  # Herdr 0.8.2 shows at most 80 columns of a tab-bar entry and cuts the rest
SEPARATOR = "   "  # between accounts; setup sets Herdr's separator between tab-bar entries to match
# Herdr 0.8.2 shows tab-bar command output as plain text (ANSI color codes appear literally and
# entries have no color setting), so "blocks" is drawn in the tab bar's own text color and the
# "color" bar uses emoji squares, which terminals draw in their own colors.
BAR_SQUARES = ("🟩", "🟨", "🟥")  # by level: under 70%, under 90%, 90% and up
BAR_EMPTY = "⬛"
EIGHTHS = " ▏▎▍▌▋▊▉"  # partly filled cells, for the dashboard, which draws the track behind them
SPLIT_CELLS = "░▄▀█"  # indexed by 2 * top + bottom: which halves of the cell are filled


def level(used):
    """0 = fine, 1 = getting close, 2 = nearly exhausted (shared with the dashboard's colors)."""
    return 0 if used < 70 else 1 if used < 90 else 2


def eighths(used, width):
    """Whole cells, and eighths of the next cell, filled by `used` percent of `width` cells."""
    return divmod(round(width * 8 * used / 100), 8)


def cells(used, width):
    """Whole cells to fill. In the tab bar a partly filled cell would show the bar's background
    rather than the track beside it, and a bar only looks full at 100%."""
    filled = round(width * used / 100)
    return filled if used >= 100 else min(filled, width - 1)


def bar(used, width, style):
    """Progress bar `width` columns wide: "blocks" (███░░), "color" (emoji squares), else none."""
    if used is None or width < 1 or style not in ("color", "blocks"):
        return ""
    if style == "color":
        squares = width // 2  # emoji are two columns wide
        filled = cells(used, squares)
        return BAR_SQUARES[level(used)] * filled + BAR_EMPTY * (squares - filled)
    filled = cells(used, width)
    return "█" * filled + "░" * (width - filled)


def split_bar(top, bottom, width):
    """Two bars in one row, split horizontally: the top half shows `top` %, the bottom half `bottom` %."""
    t, b = cells(top, width), cells(bottom, width)
    return "".join(SPLIT_CELLS[2 * (i < t) + (i < b)] for i in range(width))


def width(text):
    """Terminal columns: emoji and other wide characters take two."""
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 0 if unicodedata.combining(c) else 1 for c in text)


def clip(text, limit):
    if width(text) <= limit:
        return text
    out, used = "", 0
    for c in text:
        if used + width(c) > limit - 1:
            break
        out, used = out + c, used + width(c)
    return out + "…"


def number(value, default):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def pct(value):
    return "--" if value is None else f"{value:.0f}%"


def duration(seconds):
    s = max(0, int(seconds))
    days, s = divmod(s, 86400)
    hours, s = divmod(s, 3600)
    minutes = s // 60
    if days:
        return f"{days}d{hours}h"
    if hours:
        return f"{hours}h{minutes:02d}m"
    return f"{minutes}m" if minutes else "<1m"


def tokens(n):
    n = n or 0
    for unit, size in (("B", 1e9), ("M", 1e6), ("k", 1e3)):
        if abs(n) >= size:
            return f"{n / size:.1f}{unit}"
    return str(int(n))


def clock(ts, now):
    """Absolute local time; the date is included when it is not today."""
    if not ts:
        return "--"
    same_day = time.localtime(ts)[:3] == time.localtime(now)[:3]
    return time.strftime("%H:%M" if same_day else "%a %d %b %H:%M", time.localtime(ts))


def current(windows, now):
    """Windows with a known value that has not passed its reset time."""
    return [w for w in windows if w.get("used") is not None and not (w.get("resets_at") and w["resets_at"] <= now)]


def pick(windows, mode, now):
    """The window to show: the configured one when present, otherwise the highest % used."""
    live = current(windows, now)
    if mode and mode != "max":
        for w in live:
            if mode in (w["id"], w["label"]):
                return w
    return max(live, key=lambda w: w["used"], default=None)


def forecast(w, now):
    """Where the window is heading at its recent rate (since the saved reading in "base", a fifth of
    the window ago), else at its average rate since it started: (% used at the reset, seconds until
    100% when that comes before the reset, else None). None while less than a tenth of the window
    has passed, which is too early to tell."""
    used, reset, minutes = w.get("used"), w.get("resets_at"), w.get("minutes")
    if used is None or not (reset and minutes) or reset <= now:
        return None
    elapsed = now - (reset - minutes * 60)
    if elapsed < minutes * 6:  # a tenth of the window, in seconds
        return None
    base = w.get("base")
    rate = max(used - base[1], 0) / (now - base[0]) if base and base[0] < now else used / elapsed
    eta = (100 - used) / rate if rate else None
    return used + rate * (reset - now), (eta if eta is not None and now + eta < reset else None)


SPARK = "▁▂▃▄▅▆▇█"


def trend(readings, start, end, now, width):
    """A sparkline of % used across a window, one column per equal slice of [start, end): each shows
    the latest reading by the slice's end, height by eighths of 100%. Slices before the first
    reading and after now are blank. readings: [(first seen, last seen, % used)], oldest first."""
    out = []
    for i in range(width):
        t0, t1 = start + (end - start) * i / width, start + (end - start) * (i + 1) / width
        value = None if t0 > now else next((used for first, _, used in reversed(readings) if first < t1), None)
        out.append(" " if value is None else SPARK[min(7, max(0, math.ceil(value / 12.5) - 1))])
    return "".join(out)


def window_text(w, entry, now, meter="", when=True, pace=False):
    """"18% 2h12m": used, then time until reset; a model's own limit is named instead: "0% Fable".
    With pace, "(100% in 40m)" follows when the limit runs out before its reset at this rate."""
    base, _, scope = w["label"].partition(" ")
    when = when and (scope or (duration(w["resets_at"] - now) if w.get("resets_at") else f"({base})"))
    eta = (forecast(w, now) or (0, None))[1] if pace else None
    return f"{meter + ' ' if meter else ''}{'~' if entry.get('estimated') else ''}{pct(w['used'])}" + \
        (f" {when}" if when else "") + (f" (100% in {duration(eta)})" if eta is not None else "")


def entry_text(profile, name, entry, cfg, now, detail=0):
    """One account's text. More detail leaves out: 1 extra limits (e.g. a model's own), 2 also reset
    times, 3 also the bars."""
    status = cfg["status"]
    if not entry:
        return f"{name} …"  # first collection still running
    windows = entry.get("windows") or []
    if not windows:
        return f"{name} {STATE_TEXT.get(entry.get('state'), '--')}"
    size, live = number(status.get("bar_width"), 10), current(windows, now)
    style = status.get("bar") if detail < 3 else None
    mode, when = profile.get("format") or status["format"], detail < 2
    age = now - (entry.get("updated_at") or 0)
    pace = when and age <= cfg["refresh"]["stale_seconds"]  # no forecast from old data
    pair = [next((w for w in live if w["id"] == wid), None) for wid in ("session", "weekly")]
    if mode == "split" and all(pair):
        # One bar for both windows: the top half is the 5-hour window, the bottom half the weekly
        # one. Other current windows (e.g. a model's own weekly limit) follow without a bar.
        meter = split_bar(pair[0]["used"], pair[1]["used"], size) if style in ("blocks", "color") else ""
        shown = pair + ([w for w in live if w not in pair] if detail < 1 else [])
        text = f"{name} {meter + ' ' if meter else ''}" + " | ".join(window_text(w, entry, now, when=when, pace=pace)
                                                                     for w in shown)
    else:  # "split" without both windows falls back to compact
        shown = live if mode == "detailed" else [w for w in [pick(windows, profile.get("window") or status["window"], now)] if w]
        if detail >= 1 and len(shown) > 1:
            shown = [w for w in shown if " " not in w["label"]] or shown[:1]  # drop limits scoped to a model
        # One bar, for the first window (the 5-hour one when there is one); the others are text.
        texts = [window_text(w, entry, now, bar(w["used"], size, style) if i == 0 else "", when, pace)
                 for i, w in enumerate(shown)]
        text = f"{name} " + (" | ".join(texts) if shown else "-- (reset)")
    if age > cfg["refresh"]["stale_seconds"]:
        text += f" ({duration(age)} old)"
    return text


def icon_text(icon):
    """A Nerd Font icon (a private-use character) gets an extra space: terminals draw the larger,
    non-Mono icons up to two cells wide, over the blank cell that follows them."""
    return icon + " " if icon and "\ue000" <= icon[-1] <= "\uf8ff" else icon or ""


def names(profiles):
    """Each account's icon, or its label without one; accounts sharing an icon get both."""
    icons = [p.get("icon") for p in profiles]

    def name(p):
        if not p.get("icon"):
            return p["label"]
        return f"{icon_text(p['icon'])} {p['label']}" if icons.count(p["icon"]) > 1 else icon_text(p["icon"])

    return {p["id"]: name(p) for p in profiles}


def status_parts(profiles, entries, cfg, now, detail=0):
    """One text per shown account, in order, plus a config-error note."""
    enabled = {p["id"]: p for p in profiles if p["enabled"]}
    order = [i for i in (cfg["status"]["order"] or enabled) if i in enabled]
    shown = names([enabled[i] for i in order])
    parts = [entry_text(enabled[i], shown[i], entries.get(i), cfg, now, detail) for i in order]
    if cfg.get("error"):
        parts.append("config error" if parts else "Usage: config error (see Usage diagnostics)")
    return parts or ["Usage: no accounts found (open Usage setup)"]


def fit(profiles, entries, cfg, now):
    """The account texts with as much detail as fits status.max_width together.

    Herdr hides the whole top-right summary when it is wider than the room beside the tabs, so
    extra limits (e.g. Fable) go first, then reset times, then bars, and last each text is cut to
    its share.
    """
    budget = max(20, number(cfg["status"]["max_width"], 120))
    for detail in (0, 1, 2, 3):
        parts = status_parts(profiles, entries, cfg, now, detail)
        if sum(map(width, parts)) + len(SEPARATOR) * (len(parts) - 1) <= budget:
            return parts
    share = max(1, (budget - len(SEPARATOR) * (len(parts) - 1)) // len(parts))
    return [clip(p, share) for p in parts]


def status_line(profiles, entries, cfg, now, part=None):
    """The summary, or with part "2" only the second account and with "3-" the third on.

    Herdr shows each tab-bar entry separately, so one entry per part gives each account its own
    80 columns. Returns None when the part has no account (the entry is then hidden).
    """
    parts = fit(profiles, entries, cfg, now)
    if part:
        first = int(part.rstrip("-")) - 1
        parts = parts[first:] if part.endswith("-") else parts[first:first + 1]
        if not parts:
            return None
    return clip(SEPARATOR.join(parts), HERDR_MAX_WIDTH)
