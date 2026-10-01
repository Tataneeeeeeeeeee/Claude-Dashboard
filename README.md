# Agentboard

A local desktop dashboard for **every AI coding assistant you use**:
browse, search, compare and tidy the history that Claude Code, OpenAI Codex
CLI, Gemini CLI, and any tool you describe in a small spec file keep on your
machine.

It opens in a native OS window, works with no network connection, and
treats every tool's data as read-only apart from two operations you ask
for explicitly: moving a Claude Code transcript to a recoverable trash, and
editing a `CLAUDE.md` (which takes a timestamped backup first).

![the session list](assets/icon.png)

---

## What it does

**One history for every tool.** Every session from every provider, grouped
by project (the same repository worked on with Claude Code and Codex is one
project), with title, branch, dates, message count, duration, tokens,
estimated cost and file size. A **provider switcher** in the title bar
scopes everything to one tool, or shows them all. Sort by recency,
duration, cost, size or message count; filter by project, model, tool, tag
or favourite.

**Full-text search** across every provider's transcripts, including prose,
reasoning, tool arguments and tool output. Each hit names its provider and
links straight to the message.

**Conversation viewer** with virtualised scrolling, Markdown rendering,
syntax-highlighted code with a copy button per block, and collapsible
reasoning, tool calls and tool results, whichever tool wrote them. Export
to Markdown, HTML or JSON through a native save dialog.

**Usage dashboard** with token, cost and tool statistics by day, week or
month, per project and per model, an activity heatmap and disk usage. With
every provider in view it opens on a **side-by-side comparison**: cost,
sessions, tokens, tool calls and activity per provider. Costs come from
pricing tables you control, one per provider.

**Graceful gaps.** Each provider declares what its data records. A tool
that keeps no token counts, cannot reopen a session, or must not be
modified says so ("Not supported", "Read-only", "No resume") instead of
showing zeros or failing.

**Settings.** Switch providers on and off, point one at another data
folder, set a resume command, override prices, add a custom provider, and
set the refresh interval. Claude Code's own settings, `CLAUDE.md` files,
skills, commands, agents and todos have their own panels.

**Maintenance** (Claude Code). Delete single or many sessions into a
recoverable trash, with bulk rules for old, aborted or orphaned sessions.
Restore anything until it is purged.

**Resume** a session in a new terminal, in its original directory, for
tools that can reopen a session by id.

### Supported providers

| Provider | Reads | Tokens & cost | Resume | Delete |
| --- | --- | :-: | :-: | :-: |
| Claude Code | `~/.claude/projects/*/*.jsonl` (or `$CLAUDE_CONFIG_DIR`) | yes, with cache TTLs | `claude --resume` | yes |
| Codex CLI | `~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl` (or `$CODEX_HOME`) | yes, with cached input | `codex resume` | no |
| Gemini CLI | `~/.gemini/tmp/<project-hash>/chats/*.json` | yes, with cached input | configurable | no |
| Anything else | a spec file you write: JSON or JSONL, any layout | if it logs them | if it can | no |

Aider, Continue, Cursor exports, OpenCode, Ollama front-ends and the like
are added with a spec: see [How to add a new AI provider](#how-to-add-a-new-ai-provider).
`examples/providers/` has a ready one for Continue.

---

## Install

One command. It installs the app, gives you an `agentboard` command you
can run from anywhere, and detects which AI tools you have.

**macOS and Linux**

```sh
curl -fsSL https://raw.githubusercontent.com/Tataneeeeeeeeeee/Claude-Dashboard/main/install.sh | bash
```

**Windows** (PowerShell)

```powershell
irm https://raw.githubusercontent.com/Tataneeeeeeeeeee/Claude-Dashboard/main/install.ps1 | iex
```

Then:

```sh
agentboard
```

That is the whole installation. You need **Python 3.11 or newer**; the
installer checks for it and stops with instructions if it is missing.

### Linux also needs the GTK WebKit backend

It cannot be installed with `pip`, so install it with your package manager
before or after running the command above:

| Distribution | Command |
| --- | --- |
| Arch | `sudo pacman -S --needed webkit2gtk-4.1 python-gobject` |
| Debian / Ubuntu | `sudo apt install python3-gi gir1.2-webkit2-4.1` |
| Fedora | `sudo dnf install python3-gobject webkit2gtk4.1` |

The installer tells you if it is missing and carries on regardless, so you
can install it afterwards. On Windows the webview is **WebView2**, which
ships with Windows 10 and 11. On macOS it is **WKWebView**, part of the
system. Neither needs anything installed.

### What the command does

| | |
| --- | --- |
| Installs to | `~/.local/share/agentboard` (Windows: `%LOCALAPPDATA%\Agentboard`) |
| Creates | `agentboard` in `~/.local/bin` (Windows: `WindowsApps`) |
| Adds | a desktop entry, so the app appears in your application menu |
| Detects | every supported AI tool, and configures its adapter (see below) |
| Touches | nothing else, and never any AI tool's data |

It builds its own virtual environment, so it cannot disturb your system
Python packages. On Windows the launcher runs through `pythonw.exe`, so no
console window appears.

The window opens and your terminal is handed straight back to you. The app
is detached, so closing the terminal does not close it.

The command forwards every option:

```sh
agentboard --foreground            # stay attached and watch the output
agentboard --check                 # report the webview backend
agentboard --headless --browser    # run without a native window
agentboard --debug                 # open the webview inspector
agentboard --detect-providers      # detect AI tools again and print a report
```

Anything that prints to the terminal keeps the foreground automatically,
so `--check` and `--version` still show their output. Everything a
detached run writes goes to `~/.agentboard/launch.log`, and if the
app dies during start-up the command prints the reason instead of
returning quietly.

### Provider detection

After installing, the installer runs `agentboard --detect-providers`, which
prints what it found:

```
  + Claude Code  41 session files           /home/you/.claude/projects
  + Codex CLI    12 session files           /home/you/.codex/sessions
  - Gemini CLI   not found                  /home/you/.gemini/tmp
  * remembered CODEX_HOME=/data/codex for codex
```

A data folder known only through an environment variable
(`CLAUDE_CONFIG_DIR`, `CODEX_HOME`, `GEMINI_CLI_HOME`) is saved to the
config, because an app started from the desktop menu does not inherit your
shell's variables. A bundled example spec is enabled when its tool's
history is found. Nothing is ever switched off: a tool installed later
simply appears. Run it again any time, or use **Settings > Providers >
Detect again**.

### Updating from Claude Code Dashboard

This project used to be called *Claude Code Dashboard*. Installing
Agentboard removes the old `claude-dashboard` command, desktop entry and
installation, and on first run moves `~/.claude-dashboard` to
`~/.agentboard`, so your favourites, tags, notes, pricing, cache, trash and
backups carry over. The old `CLAUDE_DASHBOARD_*` environment variables are
still read.

### Updating

Run the same command again. It replaces the application and its virtual
environment, and leaves your config, cache, trash and backups alone.

### Removing it

```sh
curl -fsSL https://raw.githubusercontent.com/Tataneeeeeeeeeee/Claude-Dashboard/main/install.sh | bash -s -- --uninstall
```

```powershell
irm https://raw.githubusercontent.com/Tataneeeeeeeeeee/Claude-Dashboard/main/install.ps1 | iex; install.ps1 -Uninstall
```

This removes the application, its virtual environment and the launcher.
Your data is left alone: each tool's own folder (`~/.claude`, `~/.codex`,
`~/.gemini`...) belongs to that tool, and `~/.agentboard` holds this app's
config, cache, trash and backups.
Delete that second directory by hand if you want it gone too.

### Installing somewhere else, or from a fork

```sh
curl -fsSL https://raw.githubusercontent.com/Tataneeeeeeeeeee/Claude-Dashboard/main/install.sh | AGENTBOARD_PREFIX=/opt/agentboard \
  AGENTBOARD_BIN=/usr/local/bin bash
```

`AGENTBOARD_SRC` overrides where the source comes from, and accepts a
git URL, a `.tar.gz`, a `.zip` or a local path.

> If the installer warns that the launcher directory is not on your `PATH`,
> it prints the exact line to add to your shell profile. Until you add it,
> run the full path it shows you.

---

## Building a standalone executable

Optional, and not an installation route: the command above is all you need
to use the app. This is for handing someone a single file that runs with no
Python at all. From a checkout:

```sh
./build.sh          # macOS and Linux
build.bat           # Windows
```

The result is in `dist/`:

| Platform | Output |
| --- | --- |
| Windows | `dist\Agentboard.exe` |
| macOS | `dist/Agentboard.app` |
| Linux | `dist/Agentboard` |

It is a single windowed file that opens with a double-click, needs no
Python installation, and shows no terminal window. Add `--clean` to rebuild
from scratch.

On Linux the GTK and WebKit libraries come from the system rather than the
bundle, so the binary runs on machines that have `webkit2gtk-4.1`
installed. Windows and macOS bundles are self-contained.

---

## Configuration

Settings live in `~/.agentboard/config.json`, which the application
creates on first run. A copy of the defaults is in `config.json` in this
repository. Most of it is editable from the **Settings** page.

| Key | Meaning |
| --- | --- |
| `window` | size, position and maximised state, restored on launch |
| `theme` | `system`, `light` or `dark` |
| `minimize_to_tray` | closing the window hides it instead of quitting |
| `auto_refresh` | watch the filesystem and update the list live |
| `trash_retention_days` | how long deleted sessions stay recoverable; `0` disables purging |
| `providers` | per provider: `enabled`, `path` (data folder; empty = default), `resume_command`, `pricing` overrides |
| `custom_providers` | provider specs, see [How to add a new AI provider](#how-to-add-a-new-ai-provider) |
| `refresh_interval_seconds` | how often the window checks for new activity |
| `pricing` | the global price table below |
| `terminal` | how to open a terminal; `resume_command` is the legacy Claude Code setting |
| `editor_command` | used by the "Editor" button; usually `code` |
| `favorites`, `tags`, `notes` | your annotations, kept out of every tool's own files |

### The pricing table

Costs shown anywhere in this application are **local estimates**, not
billing data. They are computed from a table you own:

```json
"claude-opus-5": {
  "input": 5.00,
  "output": 25.00,
  "cache_write_5m": 6.25,
  "cache_write_1h": 10.00,
  "cache_read": 0.50
}
```

Values are US dollars per **million** tokens. Cache writes are charged by
time-to-live: the one-hour tier costs about twice the input rate, the
five-minute tier about 1.25 times. A model with no entry falls back to the
`default` row.

The starting values were derived by fitting the four token counters against
the `costUSD` figures Claude Code recorded in its own `cost-state` entries;
for `claude-opus-5` the fit reproduces those figures exactly. Even so, the
estimate is a **lower bound**: Claude Code bills for background work, such
as title generation, that never appears in a transcript. Where a session
recorded its own cost, the dashboard shows that figure alongside.

Each provider prices its own models. Its effective table is, per model:
its override in `providers.<id>.pricing`, else this global table, else the
row built into its adapter (Codex and Gemini ship OpenAI and Google
prices). A model with no row anywhere uses the provider's **own**
`default` row, so an unknown OpenAI model is never priced at an Anthropic
rate. Claude Code's rows *are* the global table.

Edit any of them in **Settings > Pricing**, or in `config.json` directly.
Every estimate updates immediately.

### The terminal command

"Resume" opens a terminal in the session's original directory and runs
the provider's resume command: `claude --resume <id>`, `codex resume <id>`,
or whatever you set in **Settings > Providers** (`{session_id}` is
substituted). A provider without one shows "No resume". The terminal
detection tries, in order:

- **Windows** — Windows Terminal (`wt.exe`), then `cmd.exe`
- **macOS** — Terminal.app, then iTerm, via AppleScript
- **Linux** — `x-terminal-emulator`, `gnome-terminal`, `konsole`,
  `xfce4-terminal`, `alacritty`, `kitty`, `wezterm`, `foot`, `xterm`

To use something else, set an argv template in `config.json`. Nothing is
passed through a shell, so paths with spaces or quotes are safe:

```json
"terminal": {
  "linux": [["kitty", "--directory", "{cwd}", "sh", "-c", "{command}"]],
  "resume_command": "claude --resume {session_id}"
}
```

`{cwd}`, `{session_id}` and `{command}` are substituted.

---

## How to add a new AI provider

There are two ways. Most tools need only the first.

### 1. Describe it in a spec file (no code)

If the tool logs each session as a JSONL file (one record per line) or as a
JSON document holding a list of messages, describe where the files are and
which fields hold what. Save it as `~/.agentboard/providers/<id>.json`
(or `.yaml` with PyYAML installed), or paste it into **Settings >
Providers > Add a provider**:

```json
{
  "id": "mytool",
  "name": "My Tool",
  "color": "#e11d48",
  "home": "~/.mytool",
  "glob": "sessions/**/*.jsonl",
  "format": "jsonl",
  "session": { "id": "session_id", "cwd": "cwd" },
  "fields": {
    "role": "role",
    "text": "content",
    "timestamp": "ts",
    "model": "model",
    "input_tokens": "usage.prompt_tokens",
    "output_tokens": "usage.completion_tokens"
  },
  "pricing": { "default": { "input": 1.0, "output": 4.0 } },
  "resume_command": "mytool --resume {session_id}"
}
```

| Key | Meaning |
| --- | --- |
| `id`, `name` | identifier (lowercase, `-`, `_`) and display name |
| `color`, `monogram` | badge colour (`#rrggbb`) and its one or two letters |
| `home`, `glob` | the data folder, and a glob relative to it matching one file per session |
| `format` | `jsonl`, or `json` with `messages_path` naming the list (`"history"`) |
| `session` | session-wide values: `id`, `cwd`, `title`, `branch` |
| `fields` | per message: `role`, `text`, `timestamp`, `model`, `message_id`, `tool_name`, `tool_input`, `tool_output`, `input_tokens`, `output_tokens`, `cache_read_tokens`, `cache_write_tokens`, `reasoning_tokens`, `cost` |
| `roles` | extra role spellings, e.g. `{"assistant": ["copilot"]}` |
| `input_includes_cached` | `true` when the tool counts cached tokens inside input (OpenAI-style) |
| `pricing`, `resume_command`, `binary` | prices per model, how to reopen a session, executables that mean "installed" |

Every value is a dotted path into the record (`message.usage.0.tokens`
indexes lists). Text may be a string or a list of parts. Lines that share a
`message_id` count as one response. Capabilities follow from what you map:
no token fields means the dashboard says "token usage not supported"
rather than showing zeros. A bad spec is reported in Settings with the
reason and never stops the app. `examples/providers/` has a JSON example
for Continue and a commented YAML template.

### 2. Write an adapter (for anything else)

When the format needs real logic, subclass `ProviderAdapter`. Three methods
are required: list the session files, summarise one in a single pass, and
parse one for the viewer. This one reads `~/.plainlog/*.jsonl` files of
`{"role", "text", "time", "model", "in", "out"}` records:

```python
import json
from pathlib import Path

from agentboard.model import EMPTY_DAY_TOTALS, EMPTY_MODEL_TOTALS, ParsedMessage, SessionMeta
from agentboard.providers.base import Capabilities, ProviderAdapter
from agentboard.providers.registry import register


@register
class PlainLogAdapter(ProviderAdapter):
    id = "plainlog"
    name = "PlainLog"
    monogram = "PL"
    color = "#0891b2"
    binary_names = ("plainlog",)
    default_pricing = {"default": {"input": 1.0, "output": 2.0}}
    capabilities = Capabilities(tool_calls=False)  # read-only, no resume

    def default_home(self) -> Path:
        return Path.home() / ".plainlog"

    def session_files(self):
        return sorted(self.root.glob("*.jsonl")) if self.root.is_dir() else []

    def _records(self, path):
        for line in Path(path).read_text(encoding="utf-8").splitlines():
            if line.strip():
                yield json.loads(line)

    def scan(self, path) -> SessionMeta:
        meta = SessionMeta(session_id=Path(path).stem, path=str(path),
                           project_dir="plainlog", provider=self.id)
        meta.tokens = {"input": 0, "output": 0, "cache_write": 0, "cache_read": 0}
        for record in self._records(path):
            meta.message_count += 1
            meta.first_timestamp = meta.first_timestamp or record["time"]
            meta.last_timestamp = record["time"]
            if record["role"] == "user":
                meta.user_messages += 1
                meta.title = meta.title or record["text"][:120]
                continue
            meta.assistant_messages += 1
            model = meta.models.setdefault(record["model"], dict(EMPTY_MODEL_TOTALS))
            day = meta.daily.setdefault(record["time"][:10], dict(EMPTY_DAY_TOTALS))
            for row in (meta.tokens, model, day):
                row["input"] += record.get("in", 0)
                row["output"] += record.get("out", 0)
            model["messages"] += 1
            day["messages"] += 1
        return meta

    def parse(self, path, tool_output_limit=20_000, include_attachments=True):
        messages = [
            ParsedMessage(index=i, line=i + 1, uuid=None, parent_uuid=None,
                          kind=r["role"], role=r["role"], timestamp=r["time"],
                          model=r.get("model"), is_sidechain=False, is_meta=False,
                          is_error=False, blocks=[{"type": "text", "text": r["text"]}]).to_dict()
            for i, r in enumerate(self._records(path))
        ]
        return {"info": {"session_id": Path(path).stem, "title": ""},
                "messages": messages, "errors": []}
```

Put it in `agentboard/providers/plainlog.py` and add `"plainlog"` to
`BUILTIN_MODULES` in `agentboard/providers/registry.py`. Everything else
follows: detection, the switcher, search (from `parse`, or override
`search_entries` for speed), usage and comparison, live updates (override
`watch_roots` or `file_suffixes` if needed), settings and pricing. Override
`resume_command`, `guess_project_path` or `owns` when the tool needs it,
and add `"agentboard.providers.plainlog"` to the hidden imports in
`agentboard.spec` for the standalone build. `tests/test_providers.py`
shows how to test one against fixture files.

---

## How it works

### Reading the data

Each provider adapter (`agentboard/providers/`) turns one tool's files into
the normalized model in `agentboard/model.py`: a `Session` summary, the
`Message` list the viewer renders, `Usage` counters and cross-provider
`Project`s. Indexing, search, usage, the API and the UI only ever see that
model, plus each provider's declared `Capabilities`.

Each format has its traps. Codex writes running token totals that can
repeat, so usage is charged as the increase between them, and it counts
cached tokens inside input. Gemini stores the working directory only as a
hash, recovered by hashing the paths other sessions recorded.

Claude Code's `~/.claude/projects/<encoded-path>/<session-id>.jsonl` holds
one JSON object per line. `SCHEMA.md` documents the format as it was found
on a real install, including several things that are easy to get wrong.
The two that matter most:

- **One API response is written as several lines**, each repeating the same
  `message.usage`. Summing line by line overstates tokens and cost by about
  57%. The indexer charges each response once, keyed on `message.id`.
- **Cache writes are priced by TTL.** The 1-hour tier costs twice the input
  rate. Pricing every cache write at the 5-minute rate understates cost by
  roughly a third.

Transcripts are streamed, never loaded whole: the largest on the reference
install is 30 MB. A cold index of 190 MB takes about half a second, and
results are cached in `~/.agentboard/cache.json` keyed by path, mtime,
size and provider, so later launches are instant.

Search needs no term index. It reads each file as raw bytes and rejects
non-matching files in a single pass, then lets that file's adapter parse
only what it needs.

### The application

A FastAPI server binds `127.0.0.1` on a **random free port** and runs in a
background thread; it refuses to bind any other interface. A pywebview
window loads that port. Closing the window stops the server, joins its
thread and exits, leaving nothing behind.

| Module | Responsibility |
| --- | --- |
| `model.py` | the normalized Session, Message, Usage and Project model |
| `providers/base.py` | the `ProviderAdapter` interface and `Capabilities` |
| `providers/registry.py` | built-in and custom providers, per-provider settings |
| `providers/claude.py`, `codex.py`, `gemini.py` | the built-in adapters |
| `providers/generic.py` | the spec-driven adapter for any other tool |
| `detect.py` | tool detection, run by the installers and "Detect again" |
| `paths.py` | filesystem locations, the project-name encoding, the containment check |
| `config.py` | user configuration and the global pricing table |
| `parser.py` | compatibility shim for the Claude Code reader |
| `indexer.py` | session index across providers, disk cache, full-text search |
| `usage.py` | token, cost and tool aggregation, and the provider comparison |
| `exporters.py` | Markdown, HTML and JSON rendering |
| `actions.py` | trash, restore, terminal launch — the only writes |
| `content.py` | `CLAUDE.md`, skills and commands, todos, backup |
| `watcher.py` | coalescing filesystem watcher |
| `api.py` | the HTTP surface |
| `server.py` | uvicorn on loopback, in a thread |
| `app.py` | pywebview window, native menu, tray |

The front end is plain HTML, CSS and JavaScript with no build step and no
CDN. The Markdown renderer, the syntax highlighter and the charts are all
in `agentboard/web/`, so the application works with no network.

### Safety

Every provider is read-only by default; deleting is a capability only the
Claude Code adapter declares, and the server refuses it for any other.

Deletion is a **move**, never an unlink. Files go to
`~/.agentboard/trash/<timestamp>/` with a manifest, and can be
restored until the retention period expires.

Every destructive call is validated first. A path is rejected unless it is
a plain `.jsonl` file, with no `..` component, that is not a symlink and
does not pass through one, resolving strictly inside `~/.claude/projects`
at exactly `<projects>/<project>/<session>.jsonl`. Configuration files are
refused by name as well. Validation covers the whole batch before anything
moves, and a failure part-way rolls back.

The `CLAUDE.md` editor is equally narrow: the target must be named
`CLAUDE.md` or `CLAUDE.local.md`, must not be a symlink, and must sit in
`~/.claude` or in a project the index already knows. A timestamped copy
goes to `~/.agentboard/backups/` before every save.

Favourites, tags and notes are kept in the dashboard's own config, so
annotating a session never modifies any tool's data.

---

## Keyboard shortcuts

| Key | Action |
| --- | --- |
| `/` | focus search |
| `j` / `k` | next / previous session |
| `Enter` | open the highlighted session |
| `r` | resume it in a terminal |
| `f` | star or unstar it |
| `t` | edit its tags and note |
| `x` | add it to the selection |
| `Del` | move the selection to the trash |
| `e` / `c` | expand / collapse every block |
| `1` `2` `3` | Sessions / Usage / Settings |
| `p` | switch to the next provider (arrow keys inside the switcher) |
| `b` | back up every transcript |
| `F5` | rebuild the index |
| `?` | show this list |

---

## Development

Clone the repository, then:

```sh
git clone https://github.com/Tataneeeeeeeeeee/Claude-Dashboard.git
cd Claude-Dashboard

./start.sh                 # run from source, creating .venv on first use
./run_tests.sh             # Python and JavaScript tests, plus lint
.venv/bin/python check_backend.py   # exercise every HTTP endpoint
```

`./install.sh` run from inside a checkout installs *that* copy rather than
downloading, which is the quickest way to try a change as an installed
command.

Tests are `pytest` for the Python side and `node --test` for the browser
code, which runs against a small DOM stub in `tests/js/dom-stub.mjs`. Node
is optional; without it the JavaScript tests are skipped.

Two environment variables help when testing against something other than
your real data:

| Variable | Effect |
| --- | --- |
| `AGENTBOARD_CLAUDE_HOME` | read a different `~/.claude` (Claude Code's own `CLAUDE_CONFIG_DIR` works too) |
| `CODEX_HOME`, `GEMINI_CLI_HOME` | read a different Codex or Gemini home, as those tools do |
| `AGENTBOARD_HOME` | put config, cache and trash elsewhere |

The pre-rename `CLAUDE_DASHBOARD_*` names are still accepted.

---

## Privacy

The application makes no network requests. Everything it renders comes from
your own disk, and every asset it loads is served from the local process.
The page enforces this with a Content Security Policy restricted to its own
origin. Identity fields in `~/.claude.json` are hidden in the Claude Code
config viewer. Nothing is ever uploaded, and there is no telemetry.
