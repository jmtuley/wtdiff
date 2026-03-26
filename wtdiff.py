#!/usr/bin/env python3
"""wtdiff — browse diffs across all worktrunk worktrees from one place.

Usage:
    wtdiff                           # auto-detect diff tool
    WTDIFF_TOOL=difft wtdiff         # force difftastic
    WTDIFF_TOOL=delta wtdiff         # force delta
    WTDIFF_TOOL=diff-so-fancy wtdiff

Keys:
    ↑↓ / j k       navigate worktrees
    J / K           scroll diff up/down
    Ctrl-d / Ctrl-u half-page scroll
    d               branch diff (all commits vs base, default)
    u               uncommitted diff (dirty working tree vs HEAD)
    /               filter worktree list
    Esc             clear filter
    r               refresh
    q               quit
"""

import argparse
import configparser
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

CONFIG_PATH = Path.home() / ".config" / "wtdiff" / "config.ini"

DEFAULT_CONFIG = """\
# wtdiff configuration

[wtdiff]
# Default diff tool. Overridden by the WTDIFF_TOOL environment variable.
# Options: difft, delta, diff-so-fancy, plain
# default_tool = difft

# Per-tool environment variables.
# Keys in each section are passed as environment variables when running that tool.

[difft]
# Display mode: inline (recommended for TUI) or side-by-side
DFT_DISPLAY = inline
# Background brightness for color palette selection: light or dark
DFT_BACKGROUND = light
# Always use color (don't auto-detect TTY)
DFT_COLOR = always

[delta]
# Add delta-specific environment variables here, e.g.:
# DELTA_PAGER = cat

[diff-so-fancy]
# Add diff-so-fancy environment variables here
"""


def load_config() -> configparser.RawConfigParser:
    cfg = configparser.RawConfigParser()
    cfg.optionxform = str  # preserve key case (env var names are case-sensitive)
    if not CONFIG_PATH.exists():
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        CONFIG_PATH.write_text(DEFAULT_CONFIG)
    cfg.read(CONFIG_PATH)
    return cfg


def _tool_env(cfg: configparser.RawConfigParser, tool: str) -> dict:
    """Return env var overrides for a tool from config, or {} if none."""
    if cfg and cfg.has_section(tool):
        return dict(cfg[tool])
    return {}

from prompt_toolkit import Application
from prompt_toolkit.formatted_text import ANSI, to_formatted_text
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import Layout, HSplit, VSplit, Window
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.styles import Style


# ---------------------------------------------------------------------------
# Git / wt helpers
# ---------------------------------------------------------------------------

def find_git_root() -> Path:
    r = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        raise RuntimeError("Not inside a git repository")
    return Path(r.stdout.strip())


def detect_default_branch(git_root: Path) -> str:
    """Try origin/HEAD, then check for main/master."""
    r = subprocess.run(
        ["git", "-C", str(git_root), "symbolic-ref", "refs/remotes/origin/HEAD"],
        capture_output=True, text=True,
    )
    if r.returncode == 0:
        ref = r.stdout.strip()
        if "/" in ref:
            return ref.rsplit("/", 1)[-1]
    for candidate in ("main", "master"):
        r2 = subprocess.run(
            ["git", "-C", str(git_root), "rev-parse", "--verify", candidate],
            capture_output=True, text=True,
        )
        if r2.returncode == 0:
            return candidate
    return "main"


def load_worktrees(git_root: Path, base: str) -> list[dict]:
    """Return list of worktree dicts. Prefers `wt list --format=json`."""
    if shutil.which("wt"):
        r = subprocess.run(
            ["wt", "list", "--format=json"],
            capture_output=True, text=True,
            cwd=str(git_root),
        )
        if r.returncode == 0:
            try:
                raw = json.loads(r.stdout)
            except json.JSONDecodeError:
                raw = []
            result = []
            for item in raw:
                if item.get("kind") == "branch" and not item.get("path"):
                    continue
                branch = item.get("branch") or "(detached)"
                path = item.get("path") or str(git_root)
                is_main = bool(item.get("is_main"))
                wt_info = item.get("working_tree") or {}
                symbols = _status_symbols(wt_info)
                main_info = item.get("main") or {}
                ahead = int(main_info.get("ahead") or 0)
                behind = int(main_info.get("behind") or 0)
                result.append(_wt_entry(branch, path, is_main, symbols, ahead, behind, base))
            if result:
                return result

    # Fallback: parse git worktree list --porcelain
    r = subprocess.run(
        ["git", "-C", str(git_root), "worktree", "list", "--porcelain"],
        capture_output=True, text=True,
    )
    entries: list[dict] = []
    current: dict = {}
    for line in r.stdout.splitlines():
        if line.startswith("worktree "):
            if current:
                entries.append(current)
            current = _wt_entry("(unknown)", line[9:], False, "", 0, 0, base)
        elif line.startswith("branch "):
            b = line[7:]
            current["branch"] = b[len("refs/heads/"):] if b.startswith("refs/heads/") else b
        elif line == "bare":
            current["is_main"] = True
    if current:
        entries.append(current)
    if entries:
        entries[0]["is_main"] = True
    return entries


def _wt_entry(branch, path, is_main, symbols, ahead, behind, base) -> dict:
    return {
        "branch": branch,
        "path": path,
        "is_main": is_main,
        "symbols": symbols,
        "ahead": ahead,
        "behind": behind,
        "base": base,
    }


def _status_symbols(wt_info: dict) -> str:
    s = ""
    if wt_info.get("staged"):    s += "+"
    if wt_info.get("modified"):  s += "!"
    if wt_info.get("untracked"): s += "?"
    return s


def format_label(wt: dict) -> str:
    parts = [wt["branch"]]
    if wt["ahead"]:   parts.append(f"↑{wt['ahead']}")
    if wt["behind"]:  parts.append(f"↓{wt['behind']}")
    if wt["symbols"]: parts.append(wt["symbols"])
    if wt["is_main"]: parts.append("[main]")
    return "  ".join(parts)


# ---------------------------------------------------------------------------
# Diff rendering
# ---------------------------------------------------------------------------

def available_tools() -> list[str]:
    """All diff tools available on this system, plus 'plain'."""
    tools = [t for t in ("difft", "delta", "diff-so-fancy") if shutil.which(t)]
    tools.append("plain")
    return tools


def default_tool(cfg=None) -> str:
    """Initial tool: WTDIFF_TOOL env var, then config, then first available, then plain."""
    t = os.environ.get("WTDIFF_TOOL", "").strip()
    if t:
        return t
    if cfg and cfg.has_option("wtdiff", "default_tool"):
        return cfg.get("wtdiff", "default_tool").strip()
    return available_tools()[0]


def build_diff(wt_path: str, base: str, mode: str, tool: str, cfg=None) -> str:
    """
    mode='branch' — git diff <base>...HEAD  (all commits on the branch)
    mode='dirty'  — git diff HEAD           (uncommitted changes)
    """
    is_difft = tool in ("difft",) or tool.endswith("difft")
    range_arg = f"{base}...HEAD" if mode == "branch" else "HEAD"

    if is_difft:
        extra = _tool_env(cfg, "difft")
        extra.setdefault("DFT_COLOR", "always")
        extra.setdefault("DFT_DISPLAY", "inline")
        env = {**os.environ, "GIT_EXTERNAL_DIFF": "difft", **extra}
        r = subprocess.run(
            ["git", "-C", wt_path, "diff", range_arg],
            capture_output=True, text=True, env=env, errors="replace",
        )
        out = r.stdout
        if not out.strip():
            return f"(no changes vs {base})" if mode == "branch" else "(no uncommitted changes)"
        return out

    r = subprocess.run(
        ["git", "-C", wt_path, "diff", "--color=always", range_arg],
        capture_output=True, text=True, errors="replace",
    )
    diff_out = r.stdout

    if not diff_out.strip():
        return f"(no changes vs {base})" if mode == "branch" else "(no uncommitted changes)"

    if tool == "delta":
        extra = _tool_env(cfg, "delta")
        env = {**os.environ, **extra} if extra else None
        p = subprocess.run(["delta"], input=diff_out, capture_output=True, text=True, errors="replace", env=env)
        return p.stdout or diff_out

    if tool == "diff-so-fancy":
        extra = _tool_env(cfg, "diff-so-fancy")
        env = {**os.environ, **extra} if extra else None
        p = subprocess.run(["diff-so-fancy"], input=diff_out, capture_output=True, text=True, errors="replace", env=env)
        return p.stdout or diff_out

    return diff_out


# ---------------------------------------------------------------------------
# TUI
# ---------------------------------------------------------------------------

class WtdiffApp:
    # prompt_toolkit uses terminal-native colors by default.
    # 'selected' uses reverse-video so it works with any terminal theme.
    STYLE = Style.from_dict({
        "selected":     "reverse bold",
        "status":       "bold",
        "filter-label": "ansiyellow bold",
        "filter-text":  "ansiyellow",
        "key":          "ansicyan bold",
        "key-sep":      "ansibrightblack",
        "separator":    "ansibrightblack",
        "":             "",
    })

    def __init__(self, git_root: Path, base: str, cfg=None) -> None:
        self.git_root = git_root
        self.base = base
        self._cfg = cfg
        self.worktrees: list[dict] = []
        self._mode = "dirty"
        self._tool = default_tool(cfg)
        self._idx = 0          # index into filtered list
        self._filter = ""
        self._filter_mode = False
        self._diff_lines: list[str] = []
        self._diff_scroll = 0

        self._app = Application(
            layout=self._build_layout(),
            key_bindings=self._build_keys(),
            style=self.STYLE,
            full_screen=True,
            mouse_support=True,
        )

    # ------------------------------------------------------------------
    # Filtered worktree list
    # ------------------------------------------------------------------

    def _filtered(self) -> list[dict]:
        if not self._filter:
            return self.worktrees
        q = self._filter.lower()
        return [w for w in self.worktrees if q in w["branch"].lower()]

    # ------------------------------------------------------------------
    # Layout
    # ------------------------------------------------------------------

    def _build_layout(self) -> Layout:
        list_win = Window(
            FormattedTextControl(self._render_list, focusable=True),
            width=36,
        )
        self._diff_win = Window(
            FormattedTextControl(self._render_diff),
            wrap_lines=False,
            dont_extend_width=False,
        )
        status_win  = Window(FormattedTextControl(self._render_status), height=1)
        footer_win  = Window(FormattedTextControl(self._render_footer), height=1)
        filter_win  = Window(FormattedTextControl(self._render_filter), height=1)

        return Layout(
            HSplit([
                VSplit([
                    list_win,
                    Window(width=1, char="│", style="class:separator"),
                    self._diff_win,
                ]),
                status_win,
                filter_win,
                footer_win,
            ])
        )

    # ------------------------------------------------------------------
    # Render callbacks
    # ------------------------------------------------------------------

    def _render_list(self):
        items = []
        filtered = self._filtered()
        for i, wt in enumerate(filtered):
            label = f" {format_label(wt)}"
            style = "class:selected" if i == self._idx else ""
            items.append((style, label + "\n"))
        if not items:
            items.append(("", " (no matches)\n"))
        return items

    def _render_diff(self):
        visible = self._diff_lines[self._diff_scroll:]
        text = "\n".join(visible) if visible else "(select a worktree)"
        return to_formatted_text(ANSI(text))

    def _render_status(self):
        filtered = self._filtered()
        if not filtered:
            return [("class:status", "")]
        wt = filtered[self._idx]
        mode_str = "branch diff [d]" if self._mode == "branch" else "uncommitted [u]"
        return [("class:status", f" {wt['branch']}  ·  {mode_str}  ·  base: {wt['base']}  ·  tool: {self._tool}")]

    def _render_filter(self):
        if self._filter_mode or self._filter:
            cursor = "█" if self._filter_mode else ""
            return [
                ("class:filter-label", " filter: "),
                ("class:filter-text", self._filter + cursor),
            ]
        return [("", "")]

    def _render_footer(self):
        keys = [
            ("↑↓/jk", "list"),
            ("J/K", "scroll"),
            ("d", "branch diff"),
            ("u", "uncommitted"),
            ("t", "cycle tool"),
            ("/", "filter"),
            ("r", "refresh"),
            ("q", "quit"),
        ]
        result = []
        for key, desc in keys:
            result += [("class:key", f" {key} "), ("class:key-sep", f" {desc} ")]
        return result

    # ------------------------------------------------------------------
    # Diff loading
    # ------------------------------------------------------------------

    def _load_diff(self) -> None:
        self._diff_scroll = 0
        filtered = self._filtered()
        if not filtered:
            self._diff_lines = ["(no worktrees match filter)"]
            return
        wt = filtered[self._idx]
        mode = "dirty" if wt["is_main"] else self._mode
        text = build_diff(wt["path"], wt["base"], mode, self._tool, self._cfg)
        self._diff_lines = text.splitlines()

    def _reload_worktrees(self) -> None:
        self.worktrees = load_worktrees(self.git_root, self.base)
        self._idx = next((i for i, w in enumerate(self._filtered()) if not w["is_main"]), 0)
        self._load_diff()

    # ------------------------------------------------------------------
    # Key bindings
    # ------------------------------------------------------------------

    def _build_keys(self) -> KeyBindings:
        kb = KeyBindings()

        # -- Quit --
        @kb.add("q", filter=~self._is_filtering())
        def _quit(event): event.app.exit()

        # -- List navigation --
        @kb.add("up",   filter=~self._is_filtering())
        @kb.add("k",    filter=~self._is_filtering())
        def _up(event):
            if self._idx > 0:
                self._idx -= 1
                self._load_diff()

        @kb.add("down", filter=~self._is_filtering())
        @kb.add("j",    filter=~self._is_filtering())
        def _down(event):
            filtered = self._filtered()
            if self._idx < len(filtered) - 1:
                self._idx += 1
                self._load_diff()

        # -- Diff scroll --
        @kb.add("J",    filter=~self._is_filtering())
        @kb.add("pagedown", filter=~self._is_filtering())
        def _scroll_down(event):
            self._diff_scroll = min(
                self._diff_scroll + 10,
                max(0, len(self._diff_lines) - 1),
            )

        @kb.add("K",    filter=~self._is_filtering())
        @kb.add("pageup", filter=~self._is_filtering())
        def _scroll_up(event):
            self._diff_scroll = max(0, self._diff_scroll - 10)

        @kb.add("c-d",  filter=~self._is_filtering())
        def _half_down(event):
            self._diff_scroll = min(
                self._diff_scroll + 20,
                max(0, len(self._diff_lines) - 1),
            )

        @kb.add("c-u",  filter=~self._is_filtering())
        def _half_up(event):
            self._diff_scroll = max(0, self._diff_scroll - 20)

        # -- Mode --
        @kb.add("d",    filter=~self._is_filtering())
        def _branch(event):
            self._mode = "branch"
            self._load_diff()

        @kb.add("u",    filter=~self._is_filtering())
        def _dirty(event):
            self._mode = "dirty"
            self._load_diff()

        # -- Refresh --
        @kb.add("t",    filter=~self._is_filtering())
        def _cycle_tool(event):
            tools = available_tools()
            try:
                idx = tools.index(self._tool)
            except ValueError:
                idx = -1
            self._tool = tools[(idx + 1) % len(tools)]
            self._load_diff()

        @kb.add("r",    filter=~self._is_filtering())
        def _refresh(event):
            self._reload_worktrees()

        # -- Filter mode --
        @kb.add("/",    filter=~self._is_filtering())
        def _start_filter(event):
            self._filter_mode = True

        @kb.add("escape")
        def _clear_filter(event):
            self._filter = ""
            self._filter_mode = False
            self._idx = 0
            self._load_diff()

        @kb.add("enter", filter=self._is_filtering())
        def _end_filter(event):
            self._filter_mode = False
            self._idx = 0
            self._load_diff()

        @kb.add("backspace", filter=self._is_filtering())
        def _backspace(event):
            self._filter = self._filter[:-1]
            self._idx = 0
            self._load_diff()

        @kb.add("<any>", filter=self._is_filtering())
        def _type_filter(event):
            self._filter += event.data
            self._idx = 0
            self._load_diff()

        return kb

    def _is_filtering(self):
        """prompt_toolkit filter condition for filter mode."""
        from prompt_toolkit.filters import Condition
        return Condition(lambda: self._filter_mode)

    # ------------------------------------------------------------------
    # Run
    # ------------------------------------------------------------------

    def run(self) -> None:
        self._reload_worktrees()
        self._app.run()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def parse_args():
    tools = available_tools()
    parser = argparse.ArgumentParser(
        prog="wtdiff",
        description="Browse diffs across git worktrees.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=f"""\
Keys:
  ↑↓ / j k       navigate worktrees
  J / K           scroll diff up/down
  Ctrl-d/u        half-page scroll
  d               branch diff (all commits vs base branch)
  u               uncommitted changes (default)
  t               cycle diff tool
  /               filter worktree list
  Esc             clear filter
  r               refresh
  q               quit

Diff tools detected on this system: {", ".join(tools)}
  difft           difftastic — syntax-aware diffs
  delta           delta — syntax-highlighted pager
  diff-so-fancy   prettified traditional diff
  plain           raw git diff

Environment:
  WTDIFF_TOOL     force a specific diff tool at startup

Config:
  {CONFIG_PATH}
""",
    )
    parser.add_argument("--config", action="store_true",
                        help=f"print config file path and exit")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    cfg = load_config()

    if args.config:
        print(CONFIG_PATH)
        return

    try:
        root = find_git_root()
    except RuntimeError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    base = detect_default_branch(root)
    WtdiffApp(git_root=root, base=base, cfg=cfg).run()


if __name__ == "__main__":
    main()
