# wtdiff

Browse diffs across all your [worktrunk](https://worktrunk.dev) git worktrees from a single terminal session — without needing to `cd` into each worktree.

```
 main  [main]          │ diff --git a/app/models/foo.rb b/app/models/foo.rb
 jlt/feature  ↑3  !    │ @@ -12,6 +12,10 @@
                        │ +  def new_method
```

Tracked changes and **untracked files** are both shown — untracked files appear in the file list with `?` status and display as a full-file diff.

## Requirements

- Python 3.9+
- [worktrunk](https://worktrunk.dev) (`wt`) — optional but recommended; falls back to `git worktree list`
- At least one diff renderer (optional — plain `git diff` works without any):
  - [difftastic](https://difftastic.wilfred.me.uk) (`brew install difftastic`)
  - [delta](https://dandavison.github.io/delta/) (`brew install git-delta`)
  - [diff-so-fancy](https://github.com/so-fancy/diff-so-fancy) (`brew install diff-so-fancy`)

## Installation

```bash
# 1. Clone
git clone https://github.com/yourname/wtdiff ~/wtdiff   # or wherever you like

# 2. Create a virtualenv and install dependencies
cd ~/wtdiff
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# 3. Create a wrapper script in your PATH
cat > ~/.local/bin/wtdiff << 'EOF'
#!/bin/zsh
exec /Users/yourname/wtdiff/.venv/bin/python3 /Users/yourname/wtdiff/wtdiff.py "$@"
EOF
chmod +x ~/.local/bin/wtdiff
```

> **Note:** Replace `/Users/yourname/wtdiff` with the actual path where you cloned the repo.

## Usage

Run from any directory inside a git repository:

```bash
wtdiff
```

### Keys

| Key | Action |
|-----|--------|
| `↑` `↓` / `j` `k` | Navigate worktree list |
| `J` `K` | Scroll diff up/down |
| `Ctrl-d` / `Ctrl-u` | Half-page scroll |
| `d` | Branch diff — all commits on this branch vs base |
| `u` | Uncommitted diff — `git diff HEAD` + untracked files (default) |
| `t` | Cycle diff tool |
| `/` | Filter worktree list |
| `Esc` | Clear filter |
| `r` | Refresh |
| `q` | Quit |

### Diff tools

`wtdiff` auto-detects installed tools and cycles through them with `t`. You can also force a specific tool at startup:

```bash
WTDIFF_TOOL=difft wtdiff
WTDIFF_TOOL=delta wtdiff
WTDIFF_TOOL=plain wtdiff   # raw git diff, no renderer
```

## Configuration

On first run, a config file is created at `~/.config/wtdiff/config.ini`. It controls per-tool environment variables:

```ini
[wtdiff]
# Default diff tool (overridden by WTDIFF_TOOL env var)
default_tool = difft

[difft]
DFT_DISPLAY = inline
DFT_BACKGROUND = light
DFT_COLOR = always

[delta]
# DELTA_PAGER = cat

[diff-so-fancy]
```

To find or edit the config:

```bash
# Print config path
wtdiff --config

# Edit
$EDITOR $(wtdiff --config)
```
