# vault-persona — Hermes Agent plugin

Load persona-bot profiles from a markdown vault as system-prompt overlays at runtime.

```
/persona <slug>          # load vault/<path>/<slug>.md as overlay
/persona default         # deactivate, back to default agent identity
/persona status          # show what's loaded
```

## What it solves

If you keep a vault of persona definitions (character voices, role-bots, custom assistants…) as markdown files, you probably want to **deploy** them — not just store them. A persona file read as RAG context is not the same as a persona file injected as a system-prompt overlay.

Under edge-case questions (out-of-scope, ambiguous), an RAG-loaded persona tends to leak back to the agent's default identity ("As your operator…"). An overlay-loaded persona stays in voice.

This plugin closes that gap. It hooks into [Hermes Agent](https://github.com/NousResearch/hermes-agent)'s `pre_llm_call` event to inject your persona as context on every turn until you deactivate it. Tracks [hermes-agent#643](https://github.com/NousResearch/hermes-agent/issues/643).

## How it works

1. `/persona <slug>` resolves to `<PERSONA_PATH_TEMPLATE>.format(slug=slug)` within your vault.
2. The plugin calls `<PERSONA_TOOL>` via Hermes' tool registry to read that file.
3. YAML frontmatter is stripped; the body is stored globally.
4. A `pre_llm_call` hook appends the body to every user message as overlay context.
5. `/persona default` (or `/new` / `/reset`) clears the active persona.

The vault stays the source of truth — adding or editing personas in the vault is reflected on next load without restarting Hermes.

## Requirements

- **Hermes Agent** (tested on v0.12.0).
- **A way for Hermes to read files from your vault.** Out of the box, `PERSONA_TOOL` defaults to `mcp_vault_bridge_vault_read_page` — the Hermes registry name for a `vault.read_page` MCP tool that takes `{"path": "<relative>"}` and returns `{"content": "...", "path": "..."}`. Any MCP tool with that contract works (a custom vault-bridge, `mcp_obsidian_read_note`, the built-in `read_file` tool with a path prefix, etc.). Adjust `PERSONA_TOOL` to match what you have.

## Install

```bash
git clone https://github.com/<owner>/hermes-vault-persona ~/.hermes/plugins/vault-persona
hermes plugins enable vault-persona
systemctl --user restart hermes-gateway.service   # or restart your CLI session
```

Verify:

```bash
hermes plugins list | grep vault-persona   # should show 'enabled', source 'user'
```

## Config

Two environment variables, read at plugin import time:

| Var | Default | Purpose |
|---|---|---|
| `PERSONA_TOOL` | `mcp_vault_bridge_vault_read_page` | Hermes registry tool name to read the persona file. Must accept `{"path": "<relative>"}` and return JSON with a `content` field |
| `PERSONA_PATH_TEMPLATE` | `conceptos/perfiles-bot/{slug}.md` | Path within the vault, with `{slug}` placeholder |

Example for an Obsidian vault with personas under `Personas/`:

```bash
# in ~/.hermes/.env
PERSONA_TOOL=mcp_obsidian_read_note
PERSONA_PATH_TEMPLATE=Personas/{slug}.md
```

## Demo

A persona-bot file in the vault (`conceptos/perfiles-bot/programmer-philosopher-clojurist-anglo.md`):

```markdown
---
type: perfil-bot
tldr: "Engineer-philosopher in the Hickey vein. Separate simple from easy…"
based_on: rich-hickey
---

# Programmer-philosopher (Clojurist anglo) — persona overlay

## Tone
Slow, etymological, dense. Distinguishes simple ("one fold") from easy
("familiar, ready-at-hand"). Anti-OOP, anti-PLOP, pro-thinking-off-computer.

## Testimonial anchors
| Anchor | Citation |
|---|---|
| Hammock-driven development | "Think hard about the problem, not about the keyboard" |
| Simple Made Easy talk | Strange Loop 2011 |
…

## How it rejects out-of-scope questions
Acknowledge, anchor in domain limit, redirect to adjacent vault profile…
```

In Telegram:

```
You: /persona programmer-philosopher-clojurist-anglo
Bot: Persona 'programmer-philosopher-clojurist-anglo' loaded as overlay
     (14518 chars). Active until /persona default.

You: I'm deciding between Redis and PostgreSQL for my SaaS. What lens?
Bot: [Hickey-voice response: simple-vs-easy, complecting, cite Antirez's
     ruthlessly-cut-complexity…]

You: When do you think AGI arrives — 2026 or 2030?            (out-of-scope)
Bot: [Stays in Hickey voice] That's outside the corpus this persona draws
     from. If you want futurist takes on AGI timelines, I'd point you to
     Amodei or LeCun in your vault — those are the lenses to apply, not
     this one.
```

Before the plugin, that last question would drop back to the default agent identity ("As your operator…").

## Caveats (current 0.1.0)

- **Single-user / global state.** One active persona per Hermes instance, not per session. For Telegram bots with one user that's fine. Multi-user needs refactor to a dict keyed by `session_id` (which the handler doesn't receive directly — would need caching from `on_session_start`).
- **Honcho / persistent memory leaks possible.** If your Hermes is also injecting conversational memory (e.g. via Honcho), that's part of the system prompt and the overlay is appended to the user message — so the model sees both. The overlay text explicitly tells the model to ignore prior memory, but residual leaks may show in long conversations. Workaround: `/new` to clear session memory before `/persona`. Proper fix would be to toggle the memory provider per-session, which Hermes doesn't expose programmatically today.
- **YAML frontmatter assumption.** Plugin strips `^---\n.*?\n---\n` from the file. If your personas have a different header convention, edit `_FRONTMATTER_REGEX`.

## Roadmap

- Per-session state (when Hermes exposes session_id to slash-command handlers)
- Optional auto-`/new` on persona load to mitigate memory leak
- Config-driven overlay text (currently hardcoded English instruction)
- `/persona list` subcommand (would need MCP `vault.list_personas` tool or filesystem glob)

PRs welcome.

## Acknowledgments

- [Nous Research](https://nousresearch.com) for Hermes Agent and the plugin system that made this a 173-line plugin instead of a fork.
- The `pre_llm_call` hook + `register_command` API is the right surface for this kind of session-level customization.

## License

MIT — see [LICENSE](LICENSE).
