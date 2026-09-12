# `~/.claude` on-disk schema

Observations made on **2026-09-11** against a real installation:

| Property | Value |
| --- | --- |
| Host | Linux (Arch), `$HOME = /home/ethan` |
| Claude Code versions seen in data | 2.1.241 → 2.1.267 |
| Total size of `~/.claude` | 246 MB |
| `projects/` | 183 MB, 12 directories, 53 `.jsonl` files |
| Lines parsed | 23 563, **0** malformed |
| Date range | 2026-08-24T17:21:59Z → 2026-09-11T20:17:31Z |

Everything below is what was **actually found**, not what the documentation
suggests. Fields marked *not present here* are handled defensively by the
parser because they exist in other Claude Code versions.

---

## 1. Directory layout

```
~/.claude/
├── .credentials.json          # secret — never read by the dashboard
├── .last-cleanup              # epoch ms
├── .last-update-result.json
├── backups/                   # .claude.json.backup.<epoch-ms>
├── cache/                     # changelog.md
├── downloads/
├── file-history/<session-id>/ # per-session file backups (edit undo)
├── history.jsonl              # flat prompt history, one line per prompt
├── paste-cache/
├── plugins/
│   ├── installed_plugins.json
│   ├── known_marketplaces.json
│   ├── plugin-catalog-cache.json
│   ├── cache/<marketplace>/<plugin>/<version>/
│   └── marketplaces/<marketplace>/   # git checkouts
├── projects/<encoded-cwd>/<session-uuid>.jsonl   ← the transcripts
├── session-env/<session-id>/
├── sessions/<pid>.json + <pid>.<hash>.key        # live-session locks
├── settings.json
├── shell-snapshots/snapshot-zsh-<epoch-ms>-<rand>.sh
└── telemetry/
~/.claude.json                 # 80 KB global state incl. per-project config
```

### Directories the brief expected that do **not** exist here

`todos/`, `statsig/`, `commands/`, `agents/`, `ide/`, `mcp/`, and the global
`~/.claude/CLAUDE.md`. They exist on other installs / older versions, so the
dashboard treats each as optional and shows an empty state instead of failing.

---

## 2. Project directory name encoding

The directory name is the session `cwd` with **every** `/`, `.` and `_`
replaced by `-`. Verified on all 53 files: `dirname == cwd.replace('/-._' → '-')`
held **53/53 times**.

```
/home/ethan/delivery/MY/claude-app  →  -home-ethan-delivery-MY-claude-app
```

The encoding is **lossy and not reversible**: `-` in the original path, and the
`.`/`_` substitutions, cannot be distinguished from separators. The dashboard
therefore **never decodes the directory name**. It reads the real `cwd` field
from the first entry in the transcript and uses that as the authoritative
project path, falling back to a best-effort decode (leading `-` → `/`, remaining
`-` → `/`) only for directories with no readable entry.

The file name (minus `.jsonl`) is the session UUID and matched `sessionId`
in every file checked (0 mismatches).

---

## 3. JSONL entry types

One JSON object per line. Counts from the 23 563 lines scanned:

| `type` | Count | Role |
| --- | ---: | --- |
| `assistant` | 6 797 | assistant turn (**carries `message.usage`**) |
| `attachment` | 4 710 | injected context, not a real message |
| `user` | 4 337 | user turn **and** tool results |
| `mode` | 1 200 | session state marker |
| `bridge-session` | 1 196 | session state marker |
| `last-prompt` | 1 190 | session state marker |
| `atis-latch` | 1 189 | session state marker |
| `ai-title` | 1 171 | **session title** |
| `permission-mode` | 1 075 | session state marker |
| `system` | 243 | system notices |
| `file-history-snapshot` | 213 | edit-undo bookkeeping |
| `file-history-delta` | 75 | edit-undo bookkeeping |
| `frame-link` | 67 | published artifact link |
| `cost-state` | 42 | **running cost/usage totals** |
| `queue-operation` | 40 | prompt queue bookkeeping |
| `artifact-autoreact-ledger` | 10 | artifact bookkeeping |
| `artifact-comment-monitor` | 8 | artifact bookkeeping |

> **No `summary` entries exist in this data.** Older Claude Code wrote
> `{"type":"summary","summary":...,"leafUuid":...}`. This version writes
> `ai-title` instead. The parser accepts both.

State-marker lines (`mode`, `permission-mode`, `atis-latch`, `bridge-session`,
`ai-title`, `last-prompt`) are rewritten repeatedly throughout a file — the
**last occurrence wins**.

### 3.1 Common envelope (`user`, `assistant`, `attachment`, `system`)

Present on 100% of those lines:

```jsonc
{
  "uuid":        "827550e5-…",        // this entry
  "parentUuid":  "787dcb18-…" | null, // previous entry (thread linkage)
  "sessionId":   "50153522-…",
  "timestamp":   "2026-08-28T17:07:24.902Z",  // ISO-8601 UTC, always "…Z"
  "cwd":         "/home/ethan/Documents/TEK4",
  "gitBranch":   "HEAD",              // literal "HEAD" when detached; may be ""
  "version":     "2.1.246",
  "isSidechain": false,
  "userType":    "external",
  "entrypoint":  "cli"
}
```

Optional, seen in this data: `session_id` (snake-case duplicate, 92–99%),
`slug`, `isMeta`, `promptId`, `requestId`, `effort`, `apiBlockIndex`.

`isSidechain` was `false` on **all 11 140** user/assistant lines here. Sub-agent
sidechains therefore could not be observed live; the viewer still branches on
the flag because other installs produce it.

### 3.2 `user`

```jsonc
{
  "type": "user",
  "message": { "role": "user", "content": <string | block[]> },
  "toolUseResult": <object | string>,        // 91% — the raw tool outcome
  "sourceToolAssistantUUID": "…",            // 91% — links back to the tool_use
  "isMeta": true,                            // 2%  — slash-command caveats etc.
  "isCompactSummary": true                   // rare
}
```

`message.content` is a **plain string** 361 times and a **block list** 3 977
times. A `user` line is a real human turn only when it has no `toolUseResult`
and `isMeta` is falsy; otherwise it is a tool result or an injected notice.

Observed `toolUseResult` shapes (top 5):

| Shape | Count |
| --- | ---: |
| `{stdout, stderr, interrupted, isImage, noOutputExpected}` (Bash) | 3 335 |
| `{type, file}` (Read) | 277 |
| plain string | 143 |
| `{type, filePath, content, structuredPatch, originalFile, userModified}` (Edit) | 72 |
| `{questions, answers, annotations}` (AskUserQuestion) | 17 |

### 3.3 `assistant`

```jsonc
{
  "type": "assistant",
  "requestId": "req_011Ceu…",
  "effort": "high",
  "message": {
    "id": "msg_…", "type": "message", "role": "assistant",
    "model": "claude-opus-5",
    "content": [ …blocks… ],
    "stop_reason": "tool_use" | "end_turn" | "stop_sequence",
    "stop_sequence": null, "stop_details": {…},
    "usage": {…}, "diagnostics": {…},
    "container": {…}, "context_management": {…}   // 22%
  },
  "isApiErrorMessage": true,   // 11 lines
  "error": {…}                 // 8 lines
}
```

`message.content` is **always a list** on assistant lines.

Models seen: `claude-opus-5` (6 092), `claude-fable-5-1` (690),
`<synthetic>` (11 — local error placeholders, must be excluded from cost),
`claude-opus-4-8` (4).

### 3.4 Content blocks

| Block | Count | Fields |
| --- | ---: | --- |
| `tool_use` (assistant) | 3 964 | `id`, `name`, `input`, `caller` |
| `tool_result` (user) | 3 964 | `tool_use_id`, `content`, `is_error` (89%) |
| `thinking` (assistant) | 2 165 | `thinking`, `signature` |
| `text` | 681 | `text` |

*No `image` blocks at the top level.* Images appear **inside** `tool_result`
content: when `tool_result.content` is a list (299 of 3 964), its inner blocks
are `image` (305), `text` (13) and `tool_reference` (11). Otherwise
`tool_result.content` is a plain string (3 666).

`tool_use.caller` was `{"type":"direct"}` on all 3 966 occurrences; a non-direct
caller marks a sub-agent invocation.

#### 3.4.1 Thinking blocks are usually empty

A `thinking` block always carries `thinking` and `signature`, but the text is
almost always blank:

| | count |
| --- | ---: |
| thinking blocks total | 2 235 |
| with `thinking == ""` | **2 195** (98%) |
| with actual text | 40 |

The signature is retained while the reasoning text is not. A viewer that
renders every one of these as an expandable "Thinking" section produces a
wall of empty disclosures, so the dashboard shows a non-expandable marker
when the text is blank and a real collapsible section only for the 2% that
have content. The exports omit blank ones entirely.

Tool-name frequency: `Bash` 3 530, `Read` 292, `Write` 75, `AskUserQuestion` 19,
`WebSearch` 12, `SendUserFile` 11, `ToolSearch` 10, `Skill` 6, `Artifact` 5,
`WebFetch` 3, `TaskStop` 2, `Edit` 1. MCP tools follow the
`mcp__<server>__<tool>` convention and are grouped by server in the dashboard.

### 3.5 `message.usage` — the billing source of truth

Assistant lines **always** carry `usage`. Full observed shape:

```jsonc
{
  "input_tokens": 2,
  "cache_creation_input_tokens": 39125,
  "cache_read_input_tokens": 0,
  "output_tokens": 337,
  "output_tokens_details": { "thinking_tokens": 134 },
  "server_tool_use": { "web_search_requests": 0, "web_fetch_requests": 0 },
  "service_tier": "standard",
  "cache_creation": {                       // 1h vs 5m TTL split
    "ephemeral_1h_input_tokens": 39125,
    "ephemeral_5m_input_tokens": 0
  },
  "inference_geo": "not_available",
  "iterations": [ { …per-API-call breakdown… } ],
  "speed": "standard"
}
```

The dashboard aggregates the four top-level counters per model and ignores
`iterations` (it double-counts the totals).

#### 3.5.1 One API response spans several lines — usage must be deduplicated

**This is the single biggest correctness trap in the format.** Claude Code
writes **one `assistant` line per content block**, and every one of those lines
repeats the *same* `message.usage` object verbatim.

In one 764 KB transcript, 156 `assistant` lines represented only **86** distinct
`message.id` values, with up to three lines sharing one id:

```
msg_011CeZ4LaKcnAN8FWrBoaAvo   in=2 out=683 cw=2151 cr=34146
msg_011CeZ4LaKcnAN8FWrBoaAvo   in=2 out=683 cw=2151 cr=34146   ← same usage
msg_011CeZ4LaKcnAN8FWrBoaAvo   in=2 out=683 cw=2151 cr=34146   ← same usage
```

Summing usage line by line across the whole install overstated cost by **57%**
($900 against the $572 Claude Code recorded). The indexer therefore charges
each API response once, keyed on `message.id` and falling back to `requestId`
then `uuid`. Message *counts* are deduplicated the same way: that transcript
holds 86 assistant messages, not 156.

#### 3.5.2 Cache writes are priced by TTL

`usage.cache_creation` splits cache-creation tokens into
`ephemeral_5m_input_tokens` and `ephemeral_1h_input_tokens`, and the two tiers
cost different amounts. A least-squares fit of the four counters against the
`costUSD` figures in 32 `cost-state` records recovers the rates for
`claude-opus-5[1m]` **exactly**, to 0.00% residual:

| Counter | $ / million tokens |
| --- | ---: |
| input | 5.00 |
| output | 25.00 |
| cache write (1 hour) | 10.00 |
| cache read | 0.50 |

Cache writes at $10 are **2x** the input rate, not the 1.25x of the 5-minute
tier. All 9 631 079 cache-creation tokens on this install used the 1-hour tier,
so pricing them at the 5-minute rate understates cost by about a third. The
default pricing table carries `cache_write_5m` and `cache_write_1h` separately
and is seeded from this fit.

#### 3.5.3 Transcripts are a lower bound on real usage

After deduplication the transcript-derived estimate reaches **93%** of the
`cost-state` total. The gap is real and not a parsing fault:

| Model | cost-state input tokens | in transcripts |
| --- | ---: | ---: |
| `claude-haiku-4-5-20251001` | 245 528 | **0** |
| `claude-opus-5` | 37 280 | 6 642 |

Haiku is used for background work such as title generation and is billed but
**never written as an `assistant` entry**. Retried requests are likewise
charged but not always logged. The dashboard therefore shows the estimate and
Claude Code's own `totalCostUSD` side by side, and labels the estimate a lower
bound rather than pretending to be a bill.

### 3.6 `attachment`

Injected context, rendered separately and collapsed by default. Sub-types found:

`total_tokens_reminder` (3 736), `batching_reminder_sent` (258),
`bash_output_audience_note` (154), `edited_text_file` (126), `environment` (122),
`deferred_tools_delta` (48), `agent_listing_delta` (47), `skill_listing` (46),
`auto_mode` (46), `silent_turn_reminder` (30), `remote_session_change` (29),
`prompt_snapshot` (16), `queued_command` (14), `date` (9), `model` (9),
`session_context` (8), `command_permissions` (6), `instructions` (5),
`deferred_tools_record` (3).

### 3.7 `system`

`subtype` values: `turn_duration` (157, has `durationMs` + `messageCount`),
`away_summary` (67, has `content`), `local_command` (15, slash-command echo),
`informational` (2), `compact_boundary` (1, has `compactMetadata` with
`preTokens`/`postTokens`), `model_refusal_fallback` (1, has `originalModel`,
`fallbackModel`, `apiRefusalCategory`, `retractedMessageUuids`).

### 3.8 `ai-title` and `last-prompt` — session titles

```jsonc
{ "type": "ai-title",    "aiTitle": "CSV des universités Epitech Global Campus",
  "sessionId": "…" }
{ "type": "last-prompt", "lastPrompt": "peut tu me faire un fichier csv…",
  "leafUuid": "…", "sessionId": "…" }
```

Title resolution order used by the indexer:
`summary` (legacy) → `ai-title` → first real user message, truncated → session UUID.

### 3.9 `cost-state` — Claude Code's own cost accounting

```jsonc
{
  "type": "cost-state", "sessionId": "…",
  "totalCostUSD": 2.4256665,
  "totalAPIDuration": 230218, "totalAPIDurationWithoutRetries": 230164,
  "totalToolDuration": 34646, "totalDuration": 10552655,
  "totalLinesAdded": 0, "totalLinesRemoved": 0,
  "startTime": 1787936489365,
  "modelUsage": {
    "claude-opus-5[1m]": {
      "inputTokens": 174, "outputTokens": 15712,
      "cacheReadInputTokens": 2415017, "cacheCreationInputTokens": 82240,
      "webSearchRequests": 0, "costUSD": 2.4235785
    }
  },
  "hasUnknownModelCost": false
}
```

Note `modelUsage` keys carry the **context-window suffix** (`claude-opus-5[1m]`)
while `message.model` does **not** (`claude-opus-5`). The pricing table
normalises by stripping `[…]`.

Only 42 of 53 sessions have a `cost-state` line, so the dashboard computes cost
from `usage` + the editable pricing table and shows `totalCostUSD` alongside as
"reported by Claude Code" when available.

---

## 4. `~/.claude/settings.json`

```json
{
  "model": "opus[1m]",
  "enabledPlugins": { "ui-ux-pro-max@ui-ux-pro-max-skill": true },
  "extraKnownMarketplaces": { "ui-ux-pro-max-skill": { "source": { "source": "github", "repo": "…" } } },
  "theme": "dark",
  "agentPushNotifEnabled": true
}
```

No `permissions` or `hooks` keys on this install — the config viewer renders
whatever keys exist and highlights the well-known ones when present.

## 5. `~/.claude.json`

80 KB, 60 top-level keys. Relevant ones:

- `projects` — map of **absolute path → per-project config**, 11 entries, with
  `allowedTools`, `mcpServers`, `enabledMcpjsonServers`, `hasTrustDialogAccepted`,
  `lastSessionId`, `lastCost`, `lastTotal*Tokens`, `lastModelUsage`,
  `lastSessionMetrics`.
- `oauthAccount`, `userID`, `machineID` — **identity; masked in the UI.**
- `numStartups`, `firstStartTime`, `installMethod`, `autoUpdates`.
- Large caches (`cachedGrowthBookFeatures` 625 keys, `plugin-catalog-cache`)
  that the viewer collapses by default.

MCP servers were `{}` for every project here; the viewer shows an empty state.

## 6. `~/.claude/history.jsonl`

220 lines, one per submitted prompt, unrelated to transcript UUIDs:

```json
{"display":"…prompt text…","pastedContents":{},"timestamp":1787592119020,
 "project":"/home/ethan","sessionId":"a4713f56-…"}
```

`timestamp` is **epoch milliseconds** here, unlike the ISO strings in transcripts.

## 7. Plugins

`installed_plugins.json` → `{version: 2, plugins: {"<plugin>@<marketplace>":
[{scope, installPath, version, installedAt, lastUpdated, gitCommitSha}]}}`.
`known_marketplaces.json` → `{"<name>": {source: {source, repo},
installLocation, lastUpdated}}`. Skills live under
`<installPath>/skills/*/SKILL.md` and marketplace checkouts under
`plugins/marketplaces/<name>/`.

---

## 8. Consequences for the parser

1. **Stream, never slurp.** The largest transcript is 30 MB; the list view must
   only read enough lines to build metadata.
2. **`type` is an open set.** 17 distinct values here, several undocumented.
   Unknown types are counted and skipped, never fatal.
3. **`message.content` is polymorphic** — string or list, and list elements may
   not be dicts. Every access is guarded.
4. **Counting "messages"** means user turns without `toolUseResult`/`isMeta`,
   plus **distinct** assistant API responses. Attachments and state markers are
   excluded, otherwise a 3-turn session reports 200 messages.
5. **Deduplicate assistant usage on `message.id`** before summing anything —
   see 3.5.1. Getting this wrong inflates every token and cost figure by ~57%.
6. **Titles** come from `ai-title`, which appears *late* in the file — so the
   indexer scans the tail as well as the head.
7. **`<synthetic>` model** lines must be skipped when costing.
8. **Deleted project folders are normal.** Two of twelve (`/home/ethan/delivery/MY/portfolio`,
   `/home/ethan/delivery/MY/sites`) no longer exist on disk while their transcripts
   remain, so "Resume" and "Open folder" must degrade gracefully.
9. **Timestamps are ISO-8601 UTC with `Z`** in transcripts but **epoch ms** in
   `history.jsonl` and `cost-state.startTime`.
