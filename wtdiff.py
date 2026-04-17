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
    /               filter current nav pane (worktrees or files)
    Esc             clear filter / go back
    r               refresh
    q               quit
"""

import argparse
import configparser
import os
import shutil
import subprocess
import sys
import threading
from pathlib import Path
from typing import Optional

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
    """Return list of worktree dicts from git worktree list."""
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


def filter_items(items: list[dict], query: str, key: str) -> list[dict]:
    if not query:
        return items
    q = query.lower()
    return [x for x in items if q in x[key].lower()]


def _untracked_diff(wt_path: str, file_path: str) -> str:
    """Return a git diff --no-index patch for a single untracked file."""
    r = subprocess.run(
        ["git", "-C", wt_path, "diff", "--no-index", "--", "/dev/null", file_path],
        capture_output=True, text=True, errors="replace",
    )
    return r.stdout  # exit code 1 is normal for diff --no-index with differences


def _untracked_diff_color(wt_path: str, file_path: str) -> str:
    r = subprocess.run(
        ["git", "-C", wt_path, "diff", "--no-index", "--color=always", "--", "/dev/null", file_path],
        capture_output=True, text=True, errors="replace",
    )
    return r.stdout


def _bat_render_new_file(wt_path: str, file_path: str) -> str:
    abs_path = str(Path(wt_path) / file_path)
    r = subprocess.run(
        ["bat", "--color=always", "--paging=never", "--style=plain", abs_path],
        capture_output=True, text=True, errors="replace",
    )
    notice = f"\x1b[1;32m(new file)\x1b[0m {file_path}\n"
    return notice + r.stdout


def _difft_untracked_diff(wt_path: str, file_path: str, cfg) -> str:
    extra = _tool_env(cfg, "difft")
    extra.setdefault("DFT_COLOR", "always")
    extra.setdefault("DFT_DISPLAY", "inline")
    env = {**os.environ, "GIT_EXTERNAL_DIFF": "difft", **extra}
    r = subprocess.run(
        ["git", "-C", wt_path, "diff", "--no-index", "--", "/dev/null", file_path],
        capture_output=True, text=True, env=env, errors="replace",
    )
    return r.stdout or _untracked_diff(wt_path, file_path)


def build_diff(wt_path: str, base: str, mode: str, tool: str, cfg=None, file_path: str = None) -> str:
    """
    mode='branch' — git diff <base>...HEAD  (all commits on the branch)
    mode='dirty'  — git diff HEAD           (uncommitted changes)
    """
    is_difft = tool in ("difft",) or tool.endswith("difft")
    range_arg = f"{base}...HEAD" if mode == "branch" else "HEAD"
    bat = shutil.which("bat")

    # Single untracked file selected — use --no-index diff
    if file_path:
        untracked = {p for p in subprocess.run(
            ["git", "-C", wt_path, "ls-files", "--others", "--exclude-standard"],
            capture_output=True, text=True,
        ).stdout.splitlines() if p}
        if file_path in untracked:
            raw = _untracked_diff(wt_path, file_path)
            if not raw.strip():
                return "(empty file)"
            if bat:
                return _bat_render_new_file(wt_path, file_path)
            if is_difft:
                return _difft_untracked_diff(wt_path, file_path, cfg)
            colored = _untracked_diff_color(wt_path, file_path)
            if tool == "delta":
                extra = _tool_env(cfg, "delta")
                env = {**os.environ, **extra} if extra else None
                p = subprocess.run(["delta"], input=colored, capture_output=True, text=True, errors="replace", env=env)
                return p.stdout or colored
            if tool == "diff-so-fancy":
                extra = _tool_env(cfg, "diff-so-fancy")
                env = {**os.environ, **extra} if extra else None
                p = subprocess.run(["diff-so-fancy"], input=colored, capture_output=True, text=True, errors="replace", env=env)
                return p.stdout or colored
            return colored

    file_args = ["--", file_path] if file_path else []

    if is_difft:
        extra = _tool_env(cfg, "difft")
        extra.setdefault("DFT_COLOR", "always")
        extra.setdefault("DFT_DISPLAY", "inline")
        env = {**os.environ, "GIT_EXTERNAL_DIFF": "difft", **extra}
        r = subprocess.run(
            ["git", "-C", wt_path, "diff", range_arg] + file_args,
            capture_output=True, text=True, env=env, errors="replace",
        )
        out = r.stdout
        if not file_path:
            for uf in load_untracked(wt_path):
                out += _bat_render_new_file(wt_path, uf["path"]) if bat else _difft_untracked_diff(wt_path, uf["path"], cfg)
        if not out.strip():
            return f"(no changes vs {base})" if mode == "branch" else "(no uncommitted changes)"
        return out

    r = subprocess.run(
        ["git", "-C", wt_path, "diff", "--color=always", range_arg] + file_args,
        capture_output=True, text=True, errors="replace",
    )
    diff_out = r.stdout

    if not file_path and not bat:
        for uf in load_untracked(wt_path):
            diff_out += _untracked_diff_color(wt_path, uf["path"])

    if not diff_out.strip() and not bat:
        return f"(no changes vs {base})" if mode == "branch" else "(no uncommitted changes)"

    if tool == "delta":
        extra = _tool_env(cfg, "delta")
        env = {**os.environ, **extra} if extra else None
        p = subprocess.run(["delta"], input=diff_out, capture_output=True, text=True, errors="replace", env=env)
        diff_out = p.stdout or diff_out

    elif tool == "diff-so-fancy":
        extra = _tool_env(cfg, "diff-so-fancy")
        env = {**os.environ, **extra} if extra else None
        p = subprocess.run(["diff-so-fancy"], input=diff_out, capture_output=True, text=True, errors="replace", env=env)
        diff_out = p.stdout or diff_out

    if not file_path and bat:
        for uf in load_untracked(wt_path):
            diff_out += _bat_render_new_file(wt_path, uf["path"])

    if not diff_out.strip():
        return f"(no changes vs {base})" if mode == "branch" else "(no uncommitted changes)"

    return diff_out


def load_untracked(wt_path: str) -> list[dict]:
    """Return untracked (not ignored) files as status '?' entries."""
    r = subprocess.run(
        ["git", "-C", wt_path, "ls-files", "--others", "--exclude-standard"],
        capture_output=True, text=True,
    )
    return [{"status": "?", "path": p} for p in r.stdout.splitlines() if p]


def load_files(wt_path: str, base: str, mode: str) -> list[dict]:
    range_arg = f"{base}...HEAD" if mode == "branch" else "HEAD"
    r = subprocess.run(
        ["git", "-C", wt_path, "diff", "--name-status", range_arg],
        capture_output=True, text=True,
    )
    files = []
    for line in r.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        raw_status = parts[0]
        status = raw_status[0]
        path = parts[2] if status == "R" and len(parts) >= 3 else parts[1]
        files.append({"status": status, "path": path})
    files.extend(load_untracked(wt_path))
    return files


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
        "back-header":  "fg:ansidarkgray",
        "file-added":   "fg:ansigreen",
        "file-modified":"fg:ansiyellow",
        "file-deleted": "fg:ansired",
        "file-renamed": "fg:ansicyan",
        "file-untracked": "fg:ansimagenta",
        "":             "",
    })

    def __init__(self, git_root: Path, base: str, cfg=None) -> None:
        self.git_root = git_root
        self.base = base
        self._cfg = cfg
        self.worktrees: list[dict] = []
        self._mode = "dirty"
        self._tool = default_tool(cfg)
        self._idx = 0          # index into filtered worktree list
        self._filter = ""
        self._filter_mode = False
        self._diff_lines: list[str] = []
        self._diff_scroll = 0
        self._view = "worktrees"   # "worktrees" | "files"
        self._files: list[dict] = []
        self._file_idx = 0
        self._loading = True
        self._load_seq = 0

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
        query = self._filter if self._view == "worktrees" else ""
        return filter_items(self.worktrees, query, "branch")

    def _filtered_files(self) -> list[dict]:
        query = self._filter if self._view == "files" else ""
        return filter_items(self._files, query, "path")

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

    _FILE_STATUS_STYLE = {
        "A": "class:file-added",
        "M": "class:file-modified",
        "D": "class:file-deleted",
        "R": "class:file-renamed",
        "?": "class:file-untracked",
    }

    def _render_list(self):
        if self._loading:
            return [("", " Loading…\n")]
        if self._view == "files":
            return self._render_file_list()
        items = []
        filtered = self._filtered()
        for i, wt in enumerate(filtered):
            label = f" {format_label(wt)}"
            style = "class:selected" if i == self._idx else ""
            items.append((style, label + "\n"))
        if not items:
            items.append(("", " (no matches)\n"))
        return items

    def _render_file_list(self):
        filtered_wts = self._filtered()
        branch = filtered_wts[self._idx]["branch"] if filtered_wts else ""
        header_style = "class:selected" if self._file_idx == -1 else "class:back-header"
        items = [(header_style, f" ← {branch}\n")]
        if not self._files:
            items.append(("", " (no changed files)\n"))
            return items
        ff = self._filtered_files()
        if not ff:
            items.append(("", " (no matches)\n"))
            return items
        pane_width = 34  # 36 - 2 chars for status prefix
        for i, f in enumerate(ff):
            status = f["status"]
            path = f["path"]
            if len(path) > pane_width:
                path = "…" + path[-(pane_width - 1):]
            style = "class:selected" if i == self._file_idx else self._FILE_STATUS_STYLE.get(status, "")
            items.append((style, f" {status}  {path}\n"))
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
        base_status = f" {wt['branch']}  ·  {mode_str}  ·  base: {wt['base']}  ·  tool: {self._tool}"
        if self._view == "files" and self._files:
            ff = self._filtered_files()
            shown = f"{self._file_idx + 1}/{len(ff)}"
            if len(ff) != len(self._files):
                shown += f" of {len(self._files)}"
            return [("class:status", base_status + f"  ·  file {shown}")]
        return [("class:status", base_status)]

    def _render_filter(self):
        if self._filter_mode or self._filter:
            cursor = "█" if self._filter_mode else ""
            return [
                ("class:filter-label", " filter: "),
                ("class:filter-text", self._filter + cursor),
            ]
        return [("", "")]

    def _render_footer(self):
        if self._view == "files":
            keys = [
                ("↑↓/jk", "files"),
                ("J/K", "scroll"),
                ("/", "filter"),
                ("h/Esc", "back"),
                ("d", "branch diff"),
                ("u", "uncommitted"),
                ("t", "cycle tool"),
                ("q", "quit"),
            ]
        else:
            keys = [
                ("↑↓/jk", "list"),
                ("↵", "browse files"),
                ("J/K", "scroll"),
                ("/", "filter"),
                ("d", "branch diff"),
                ("u", "uncommitted"),
                ("t", "cycle tool"),
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

    def _current_file(self) -> Optional[dict]:
        if self._view == "files" and self._file_idx >= 0:
            ff = self._filtered_files()
            if self._file_idx < len(ff):
                return ff[self._file_idx]
        return None

    def _load_diff(self) -> None:
        self._diff_scroll = 0
        self._load_seq += 1
        seq = self._load_seq
        self._diff_lines = ["\x1b[2mloading…\x1b[0m"]

        filtered = self._filtered()
        if not filtered:
            self._diff_lines = ["(no worktrees match filter)"]
            return
        wt = filtered[self._idx]
        mode = "dirty" if wt["is_main"] else self._mode
        cf = self._current_file()
        wt_path, wt_base = wt["path"], wt["base"]
        file_path = cf["path"] if cf else None
        tool, cfg, view = self._tool, self._cfg, self._view

        def _run() -> None:
            text = build_diff(wt_path, wt_base, mode, tool, cfg, file_path=file_path)
            lines = text.splitlines()
            if view == "worktrees":
                lines = ["\x1b[2m  ↵  Enter to browse files\x1b[0m", ""] + lines
            if seq == self._load_seq:
                self._diff_lines = lines
                self._app.invalidate()

        threading.Thread(target=_run, daemon=True).start()

    def _load_files(self) -> None:
        filtered = self._filtered()
        if not filtered:
            return
        wt = filtered[self._idx]
        mode = "dirty" if wt["is_main"] else self._mode
        self._files = load_files(wt["path"], wt["base"], mode)
        self._file_idx = 0
        self._filter = ""
        self._filter_mode = False
        self._view = "files"
        self._load_diff()

    def _back_to_worktrees(self) -> None:
        self._view = "worktrees"
        self._files = []
        self._file_idx = 0
        self._filter = ""
        self._filter_mode = False
        self._diff_lines = []
        self._diff_scroll = 0

    def _reload_worktrees(self) -> None:
        self.worktrees = load_worktrees(self.git_root, self.base)
        self._idx = next((i for i, w in enumerate(self._filtered()) if not w["is_main"]), 0)
        self._back_to_worktrees()
        self._load_diff()

    def _bg_reload(self) -> None:
        self._reload_worktrees()
        self._loading = False
        self._app.invalidate()

    # ------------------------------------------------------------------
    # Key bindings
    # ------------------------------------------------------------------

    def _build_keys(self) -> KeyBindings:
        kb = KeyBindings()
        nf = ~self._is_filtering()   # not in filter mode
        nl = ~self._is_loading()     # not loading
        bf = self._is_browsing_files()

        # -- Quit --
        @kb.add("q", filter=nf)
        def _quit(event): event.app.exit()

        # -- List navigation --
        @kb.add("up",   filter=nl)
        @kb.add("k",    filter=nf & nl)
        def _up(event):
            if self._view == "files":
                if self._file_idx > -1:
                    self._file_idx -= 1
                    self._load_diff()
            else:
                if self._idx > 0:
                    self._idx -= 1
                    self._load_diff()

        @kb.add("down", filter=nl)
        @kb.add("j",    filter=nf & nl)
        def _down(event):
            if self._view == "files":
                if self._file_idx < len(self._filtered_files()) - 1:
                    self._file_idx += 1
                    self._load_diff()
            elif self._view == "worktrees":
                filtered = self._filtered()
                if self._idx < len(filtered) - 1:
                    self._idx += 1
                    self._load_diff()

        @kb.add("enter", filter=nf & nl & ~bf)
        def _drill_in(event):
            self._load_files()

        @kb.add("enter", filter=nf & bf)
        def _files_enter(event):
            if self._file_idx == -1:
                self._back_to_worktrees()

        # -- Diff scroll --
        @kb.add("J",    filter=nf & nl)
        @kb.add("pagedown", filter=nf & nl)
        def _scroll_down(event):
            self._diff_scroll = min(
                self._diff_scroll + 10,
                max(0, len(self._diff_lines) - 1),
            )

        @kb.add("K",    filter=nf & nl)
        @kb.add("pageup", filter=nf & nl)
        def _scroll_up(event):
            self._diff_scroll = max(0, self._diff_scroll - 10)

        @kb.add("c-d",  filter=nf & nl)
        def _half_down(event):
            self._diff_scroll = min(
                self._diff_scroll + 20,
                max(0, len(self._diff_lines) - 1),
            )

        @kb.add("c-u",  filter=nf & nl)
        def _half_up(event):
            self._diff_scroll = max(0, self._diff_scroll - 20)

        # -- Mode --
        @kb.add("d",    filter=nf & nl)
        def _branch(event):
            self._mode = "branch"
            if self._view == "files":
                self._load_files()
            else:
                self._load_diff()

        @kb.add("u",    filter=nf & nl)
        def _dirty(event):
            self._mode = "dirty"
            if self._view == "files":
                self._load_files()
            else:
                self._load_diff()

        # -- Refresh --
        @kb.add("t",    filter=nf & nl)
        def _cycle_tool(event):
            tools = available_tools()
            try:
                idx = tools.index(self._tool)
            except ValueError:
                idx = -1
            self._tool = tools[(idx + 1) % len(tools)]
            self._load_diff()

        @kb.add("r",    filter=nf & nl)
        def _refresh(event):
            self._loading = True
            threading.Thread(target=self._bg_reload, daemon=True).start()

        # -- Filter mode (filters current nav pane) --
        @kb.add("/",    filter=nf & nl)
        def _start_filter(event):
            self._filter_mode = True

        @kb.add("escape")
        def _clear_filter(event):
            if self._view == "files" and not self._filter_mode and not self._filter:
                self._back_to_worktrees()
                return
            self._filter = ""
            self._filter_mode = False
            if self._view == "files":
                self._file_idx = 0
            else:
                self._idx = 0
            if not self._loading:
                self._load_diff()

        @kb.add("h",    filter=nf & nl)
        @kb.add("left", filter=nf & nl)
        def _back(event):
            if self._view == "files":
                self._back_to_worktrees()

        @kb.add("enter", filter=self._is_filtering())
        def _end_filter(event):
            self._filter_mode = False
            if self._view == "files":
                self._file_idx = 0
            else:
                self._idx = 0
            self._load_diff()

        @kb.add("backspace", filter=self._is_filtering())
        def _backspace(event):
            self._filter = self._filter[:-1]
            if self._view == "files":
                self._file_idx = 0
            else:
                self._idx = 0
            self._load_diff()

        @kb.add("<any>", filter=self._is_filtering())
        def _type_filter(event):
            if not event.data or not event.data.isprintable():
                return
            self._filter += event.data
            if self._view == "files":
                self._file_idx = 0
            else:
                self._idx = 0
            self._load_diff()

        return kb

    def _is_filtering(self):
        from prompt_toolkit.filters import Condition
        return Condition(lambda: self._filter_mode)

    def _is_browsing_files(self):
        from prompt_toolkit.filters import Condition
        return Condition(lambda: self._view == "files")

    def _is_loading(self):
        from prompt_toolkit.filters import Condition
        return Condition(lambda: self._loading)

    # ------------------------------------------------------------------
    # Run
    # ------------------------------------------------------------------

    def run(self) -> None:
        threading.Thread(target=self._bg_reload, daemon=True).start()
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
