"""Terminal dashboard opened in a Herdr popup: account limits plus local token history.

The screen is rebuilt as a list of lines (each a list of (text, style) segments) for the current
width, then drawn from the scroll offset. Color is an enhancement; every value is also spelled out.
"""

import curses
import os
import time
from collections import defaultdict

from . import cache, collect, config, fmt, history

RANGES = (("today", 1), ("7 days", 7), ("30 days", 30), ("all history", None))
SHADES = " ░▒▓█"
TRACK = 238  # dark grey for the unfilled part of limit bars on 256-color terminals
WINDOW_NAMES = {"session": "Session limit", "weekly": "Weekly limit", "monthly": "Monthly limit"}
HELP = [
    "Keys",
    "  q / Esc        close the dashboard",
    "  r              refresh limits and history now",
    "  ↑ ↓ / j k      scroll one line;  PgUp PgDn / b space: one page;  g G: top / bottom",
    "  t w m a        date range: today, 7 days, 30 days, all history (or 1 2 3 4)",
    "  f              filter by account (cycles through profiles)",
    "  p              filter by provider (Claude, Codex, ...)",
    "  ?              toggle this help",
    "",
    "What the numbers mean",
    "  Limits are the subscription allowance each provider reports for the whole account,",
    "  including use outside Herdr. Windows are never summed or averaged together.",
    "  Token activity comes from local session records on this machine only, so it can be",
    "  incomplete. It counts tokens; it is not a bill.",
]


def run():
    os.environ.setdefault("ESCDELAY", "25")
    curses.wrapper(lambda screen: Dashboard(screen).loop())


class Dashboard:
    def __init__(self, screen):
        self.screen, self.range, self.scroll, self.help = screen, 1, 0, False
        self.account = self.provider = None
        self.refreshing, self.loaded_at = None, 0
        self.styles = {}
        self.setup_colors()
        self.reload()

    # ---- data -------------------------------------------------------------------------------

    def reload(self):
        self.cfg = config.load()
        self.state_dir = config.state_dir()
        self.now = time.time()
        self.entries = collect.entries(self.cfg, self.state_dir, self.now)
        self.history = history.rows(self.state_dir, self.cfg["profiles"])
        self.coverage = history.coverage(self.state_dir, self.cfg["profiles"])
        self.readings = {(pid, w["id"]): history.limit_readings(self.state_dir, pid, w)
                         for pid, entry in self.entries.items() for w in fmt.current(entry.get("windows") or [], self.now)}
        self.loaded_at = time.time()

    def profiles(self):
        return [p for p in self.cfg["profiles"] if p["enabled"]
                and self.account in (None, p["id"]) and self.provider in (None, p["provider"])]

    def since(self):
        days = RANGES[self.range][1]
        if days is None:
            return 0
        midnight = time.mktime(time.localtime(self.now)[:3] + (0, 0, 0, 0, 0, -1))
        return midnight - (days - 1) * 86400

    # ---- drawing ----------------------------------------------------------------------------

    def setup_colors(self):
        self.styles = {"bold": curses.A_BOLD, "dim": curses.A_DIM, "": curses.A_NORMAL, "ok": curses.A_NORMAL,
                       "warn": curses.A_BOLD, "bad": curses.A_BOLD | curses.A_UNDERLINE, "head": curses.A_BOLD,
                       "track": curses.A_DIM, "value": curses.A_BOLD, "selected": curses.A_REVERSE | curses.A_BOLD}
        self.track = "░"  # character for the unfilled part of a bar
        try:
            curses.curs_set(0)
            if curses.has_colors():
                curses.use_default_colors()
                levels = (curses.COLOR_GREEN, curses.COLOR_YELLOW, curses.COLOR_RED)
                for i, color in enumerate(levels + (curses.COLOR_CYAN,), 1):
                    curses.init_pair(i, color, -1)
                self.styles.update(ok=curses.color_pair(1), warn=curses.color_pair(2),
                                   bad=curses.color_pair(3) | curses.A_BOLD, head=curses.color_pair(4) | curses.A_BOLD)
                if curses.COLORS >= 256:
                    # Solid dark track, and the partly filled cell drawn on it, as in Claude Code's bars.
                    curses.init_pair(5, TRACK, -1)
                    for i, color in enumerate(levels, 6):
                        curses.init_pair(i, color, TRACK)
                    self.styles.update(track=curses.color_pair(5), ok_edge=curses.color_pair(6),
                                       warn_edge=curses.color_pair(7), bad_edge=curses.color_pair(8) | curses.A_BOLD)
                    self.track = "█"
        except curses.error:
            pass  # monochrome or limited terminal: the text labels carry the meaning
        for level in ("ok", "warn", "bad"):
            self.styles.setdefault(level + "_edge", self.styles[level])
            self.styles[level + "_value"] = self.styles[level] | curses.A_BOLD

    def draw(self):
        self.screen.erase()
        height, width = self.screen.getmaxyx()
        if width < 40 or height < 8:
            self.put(0, [("Terminal too small for the dashboard. Press q to close.", "bold")], width)
            self.screen.refresh()
            return
        header = self.header(width)
        body = HELP if self.help else self.body(width)
        body = [[(line, "head" if line and not line.startswith(" ") else "")] if isinstance(line, str) else line
                for line in body]
        view = height - len(header)
        self.scroll = max(0, min(self.scroll, len(body) - view))
        for y, line in enumerate(header):
            self.put(y, line, width)
        for y, line in enumerate(body[self.scroll:self.scroll + view]):
            self.put(len(header) + y, line, width)
        self.screen.refresh()

    def put(self, y, segments, width):
        x = 0
        for text, style in segments:
            if x >= width - 1:
                break
            text = text[:width - 1 - x]
            try:
                self.screen.addstr(y, x, text, self.styles.get(style, curses.A_NORMAL))
            except curses.error:
                pass
            x += len(text)

    # ---- content ----------------------------------------------------------------------------

    def header(self, width):
        ranges = [(f" {name} ", "selected" if i == self.range else "") for i, (name, _) in enumerate(RANGES)]
        account = next((p["label"] for p in self.cfg["profiles"] if p["id"] == self.account), "all")
        state = "refreshing…" if self.refreshing else f"loaded {time.strftime('%H:%M:%S', time.localtime(self.loaded_at))}"
        return [[("Usage", "head"), (" ", "")] + ranges +
                [(f"   account: {account}   provider: {self.provider or 'all'}", "")],
                [(f"{state}   r refresh  f account  p provider  ? help  q close", "dim")],
                [("─" * (width - 1), "dim")]]

    def body(self, width):
        lines = self.limits(width) + [""] + self.activity(width)
        if self.cfg.get("error"):
            lines = [[("Config error: ", "bad"), (self.cfg["error"], "")], ""] + lines
        return lines

    def limits(self, width):
        lines = [section("ACCOUNT LIMITS", "subscription allowance reported by each provider (account-wide)")]
        profiles = self.profiles()
        if not profiles:
            return lines + ["  No accounts match the filter. Configure accounts via Usage setup."]
        bar = max(10, min(50, width - 28))
        for p in profiles:
            entry = self.entries.get(p["id"]) or {}
            title = p["label"] + (f" · {entry['plan']}" if entry.get("plan") else "") + \
                (" (estimate)" if entry.get("estimated") else "")
            lines += ["", [(f"{title}", "bold"), (f"   {self.freshness(entry)}", "dim")]]
            for w in entry.get("windows") or []:
                lines += self.window_lines(w, entry, bar, width, self.readings.get((p["id"], w["id"])))
            if entry.get("state") in ("auth", "unavailable", "error") and entry.get("error"):
                style = "bad" if entry["state"] == "auth" else "warn"
                label = {"auth": "SIGN-IN NEEDED", "unavailable": "UNAVAILABLE", "error": "LAST REFRESH FAILED"}
                lines.append([(f"  {label[entry['state']]}: ", style), (entry["error"], "")])
            if entry.get("estimated"):
                lines.append([("  Estimate from local records only; open the provider's own usage page for exact values.", "dim")])
        return lines

    def freshness(self, entry):
        if not entry:
            return "waiting for the first refresh…"
        if not entry.get("updated_at"):
            return f"no data yet · checked {fmt.duration(self.now - entry.get('checked_at', self.now))} ago"
        age = self.now - entry["updated_at"]
        stale = "STALE · " if age > self.cfg["refresh"]["stale_seconds"] else ""
        return f"{stale}updated {fmt.duration(age)} ago ({fmt.clock(entry['updated_at'], self.now)}) · {entry.get('source', '')}"

    def window_lines(self, w, entry, bar, width, readings=None):
        """As in Claude Code's usage view: the limit's name and reset, then its bar and numbers, and
        a trend line across the whole window once two readings of it are saved."""
        used, name = w["used"], f"  {window_name(w)}"
        if used is None:
            return [[(name, "bold"), (" · no percentage reported", "dim")]]
        if w.get("resets_at") and w["resets_at"] <= self.now:
            return [[(name, "bold"), (f" · reset at {fmt.clock(w['resets_at'], self.now)}; not re-read since "
                                      f"(was {fmt.pct(used)})", "dim")]]
        style = ("ok", "warn", "bad")[fmt.level(used)]
        full, part = fmt.eighths(used, bar)
        approx = "~" if entry.get("estimated") else ""
        meter = [("  ", ""), ("█" * full, style), (fmt.EIGHTHS[part] if part else "", style + "_edge"),
                 (self.track * (bar - full - bool(part)), "track"), (f"  {approx}{fmt.pct(used)} used", style + "_value"),
                 (f" · {approx}{fmt.pct(100 - used)} left", "dim")]
        ahead = fmt.forecast(w, self.now) if self.now - (entry.get("updated_at") or 0) <= self.cfg["refresh"]["stale_seconds"] else None
        if ahead and ahead[1] is not None:
            meter.append((f" · at this rate 100% in {fmt.duration(ahead[1])} ({fmt.clock(self.now + ahead[1], self.now)})", "bad"))
        elif ahead and used:
            meter.append((f" · on pace for {fmt.pct(min(ahead[0], 100))} at the reset", "dim"))
        reset = (f"resets in {fmt.duration(w['resets_at'] - self.now)} ({fmt.clock(w['resets_at'], self.now)})"
                 if w.get("resets_at") else "no fixed reset")
        lines = [[(name, "bold"), (f" · {reset}", "dim")], meter] if len(name) + 3 + len(reset) < width else \
            [[(name, "bold")], meter, [(f"  {reset}", "dim")]]
        if len(readings or []) > 1 and w.get("minutes") and w.get("resets_at"):
            start = w["resets_at"] - w["minutes"] * 60
            lines.append([("  ", ""), (fmt.trend(readings, start, w["resets_at"], self.now, bar), style),
                          (f"  trend since {fmt.clock(start, self.now)}", "dim")])
        return lines

    def activity(self, width):
        since, name = self.since(), RANGES[self.range][0]
        ids = {p["id"] for p in self.profiles()}
        scoped = [r for r in self.history if r["profile"] in ids]
        rows = [r for r in scoped if (r["last"] or 0) >= since]
        lines = [section("TOKEN ACTIVITY", f"local session records · {name}"),
                 [("  From this machine's logs only (may be incomplete); not the subscription allowance above.", "dim")]]
        if not rows:
            lines.append("  No recorded activity in this range yet (history is built in the background).")
            return lines + self.coverage_lines()
        total = self.totals(rows)
        days = len({r["day"] for r in rows})
        span = RANGES[self.range][1] or max(1, round((self.now - min(r["first"] for r in rows)) / 86400))
        sessions = len({(r["profile"], r["session"]) for r in rows})
        lines += [""] + tiles([("Input", fmt.tokens(total["input"])), ("Output", fmt.tokens(total["output"])),
                               ("incl. reasoning", fmt.tokens(total["reasoning"])),
                               ("Cache read", fmt.tokens(total["cache_read"])),
                               ("Cache write", fmt.tokens(total["cache_write"]))], width)
        lines += [""] + tiles([("Requests", f"{total['requests']:,}"), ("Sessions", f"{sessions:,}"),
                               ("Active days", f"{days} of {span}")], width)
        lines += [""] + self.heatmap(scoped, width)
        for title, key in (("BY ACCOUNT", lambda r: r["label"]), ("BY BACKEND", lambda r: r["backend"] or "?"),
                           ("BY MODEL", lambda r: r["model"]), ("BY PROJECT", lambda r: short_path(r["project"]))):
            lines += [""] + self.table(title, rows, key, width)
        return lines + [""] + self.sessions(rows, width) + [""] + self.coverage_lines()

    def totals(self, rows):
        total = defaultdict(int)
        for r in rows:
            for field in ("input", "output", "reasoning", "cache_read", "cache_write", "requests"):
                total[field] += r[field] or 0
        return total

    def table(self, title, rows, key, width, limit=8):
        groups = defaultdict(lambda: {"tok": 0, "cache": 0, "requests": 0})
        for r in rows:
            g = groups[key(r)]
            g["tok"] += in_out(r)
            g["cache"] += (r["cache_read"] or 0) + (r["cache_write"] or 0)
            g["requests"] += r["requests"]
        name_width = max(12, min(40, width - 36))
        lines = [[(f"{title:<{name_width + 2}}", "head"), (f"{'in+out':>9} {'cache':>9} {'requests':>9}", "dim")]]
        ranked = sorted(groups.items(), key=lambda kv: -kv[1]["tok"])
        for name, g in ranked[:limit]:
            used = g["tok"] or g["cache"]
            lines.append([(f"  {clip(str(name), name_width)}{fmt.tokens(g['tok']):>9} {fmt.tokens(g['cache']):>9}"
                           f" {g['requests']:>9,}", "" if used else "dim")])
        if len(ranked) > limit:
            lines.append([(f"  … {len(ranked) - limit} more", "dim")])
        return lines

    def sessions(self, rows, width, limit=8):
        groups = defaultdict(lambda: {"tok": 0, "last": 0, "models": set(), "project": None, "label": ""})
        for r in rows:
            g = groups[(r["profile"], r["session"])]
            g["tok"] += in_out(r)
            g["last"] = max(g["last"], r["last"] or 0)
            g["models"].add(r["model"])
            g["project"], g["label"] = g["project"] or r["project"], r["label"]
        lines = [section("RECENT SESSIONS", f"{len(groups)} in range")]
        name_width = max(10, min(36, width - 60))
        for g in sorted(groups.values(), key=lambda g: -g["last"])[:limit]:
            lines.append(f"  {fmt.clock(g['last'], self.now):<16} {clip(g['label'], 12)} "
                         f"{clip(short_path(g['project']), name_width)} {fmt.tokens(g['tok']):>8}  "
                         f"{clip(', '.join(sorted(g['models'])), 28)}")
        return lines

    def heatmap(self, rows, width):
        """Weekday rows by week columns of input+output tokens per day."""
        per_day = defaultdict(int)
        for r in rows:
            per_day[r["day"]] += in_out(r)
        weeks = max(1, min((width - 10) // 2, 26))
        today = time.localtime(self.now)
        start = time.mktime((today.tm_year, today.tm_mon, today.tm_mday - today.tm_wday - 7 * (weeks - 1),
                             12, 0, 0, 0, 0, -1))
        peak = max(per_day.values(), default=0) or 1
        lines = [section("DAILY ACTIVITY", f"input+output tokens per day, last {weeks} week{'s' * (weeks > 1)} "
                                           f"(peak {fmt.tokens(peak)})")]
        for weekday in range(7):
            cells = []
            for week in range(weeks):
                day = time.localtime(start + (week * 7 + weekday) * 86400)
                if time.mktime(day) > self.now:
                    cells.append(("  ", ""))
                    continue
                value = per_day.get(time.strftime("%Y-%m-%d", day), 0)
                level = 0 if not value else 1 + min(3, int(4 * value / peak))
                cells.append((SHADES[level] * 2 if level else "· ", "ok" if level else "dim"))
            lines.append([(f"  {'MTWTFSS'[weekday]}  ", "dim")] + cells)
        lines.append([("      less · ", "dim"), ("░ ▒ ▓ █", "ok"), (" more", "dim")])
        return lines

    def coverage_lines(self):
        lines = [section("HISTORY COVERAGE", "what the local records contain")]
        for p in self.profiles():
            first, last, count = self.coverage.get(p["id"], (None, None, 0))
            span = (f"{time.strftime('%Y-%m-%d', time.localtime(first))} → "
                    f"{time.strftime('%Y-%m-%d', time.localtime(last))}") if count else "nothing recorded yet"
            lines.append(f"  {clip(p['label'], 14)} {count:>9,} requests  {span}  from {short_path(p['dir'])}")
        return lines

    # ---- input ------------------------------------------------------------------------------

    def loop(self):
        self.screen.timeout(1000)
        while True:
            self.draw()
            key = self.screen.getch()
            if self.refreshing and self.refreshing.poll() is not None:
                self.refreshing = None
                self.reload()
            elif time.time() - self.loaded_at > 30:
                self.reload()
            if key == -1:
                continue
            if self.help and key not in (ord("q"), 27):
                self.help = False
                continue
            if not self.handle(key):
                return

    def handle(self, key):
        height = self.screen.getmaxyx()[0]
        ids = [None] + [p["id"] for p in self.cfg["profiles"] if p["enabled"]]
        kinds = [None] + sorted({p["provider"] for p in self.cfg["profiles"] if p["enabled"]})
        if key in (ord("q"), 27):
            return False
        if key == ord("r") and not self.refreshing:
            self.refreshing = cache.spawn("refresh", "--force")
        elif key in (curses.KEY_DOWN, ord("j")):
            self.scroll += 1
        elif key in (curses.KEY_UP, ord("k")):
            self.scroll -= 1
        elif key in (curses.KEY_NPAGE, ord(" ")):
            self.scroll += height - 4
        elif key in (curses.KEY_PPAGE, ord("b")):
            self.scroll -= height - 4
        elif key in (ord("g"), curses.KEY_HOME):
            self.scroll = 0
        elif key in (ord("G"), curses.KEY_END):
            self.scroll = 10 ** 6
        elif key in map(ord, "twma1234"):
            self.range = "twma1234".index(chr(key)) % 4
        elif key == ord("f"):
            self.account = ids[(ids.index(self.account) + 1) % len(ids)] if self.account in ids else None
        elif key == ord("p"):
            self.provider = kinds[(kinds.index(self.provider) + 1) % len(kinds)] if self.provider in kinds else None
        elif key == ord("?"):
            self.help = True
        self.scroll = max(0, self.scroll)
        return True


def section(title, note):
    return [(title, "head"), (f"  {note}", "dim")]


def tiles(items, width, size=17):
    """(label, value) pairs as columns of dim labels over bold numbers, wrapped to the width."""
    per_row, lines = max(1, (width - 3) // size), []
    for i in range(0, len(items), per_row):
        row = items[i:i + per_row]
        lines += [[("  ", "")] + [(label.ljust(size), "dim") for label, _ in row],
                  [("  ", "")] + [(value.ljust(size), "value") for _, value in row]]
    return lines


def window_name(w):
    """"Session limit (5h)", "Weekly limit (7d) · Opus"; windows of other lengths keep their label."""
    base, _, scope = w["label"].partition(" ")
    kind = WINDOW_NAMES.get(w["id"].split("_")[0])
    return f"{kind} ({base})" + (f" · {scope}" if scope else "") if kind else w["label"]


def in_out(row):
    return (row["input"] or 0) + (row["output"] or 0)


def clip(text, width):
    text = str(text or "-")
    return (text[:width - 1] + "…" if len(text) > width else text).ljust(width)


def short_path(path):
    if not path:
        return "-"
    home = os.path.expanduser("~")
    return "~" + path[len(home):] if path.startswith(home) else path
