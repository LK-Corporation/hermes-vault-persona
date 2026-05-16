# vault-persona — Hermes Agent plugin

Load persona-bot profiles from a markdown vault as system-prompt overlays at runtime — explicitly via slash command **or automatically when the user mentions a persona by name**.

```
/persona <slug>          # load vault/<path>/<slug>.md as persistent overlay
/persona default         # deactivate, back to default agent identity
/persona status          # show what's loaded

"what would Hickey say about TypeScript?"     # auto-loads programmer-philosopher
                                              # one-shot — released after the turn
"according to Munger, how should I decide?"
"como vería Naval este libro?"                # (also matches ES patterns)
```

## What it solves

If you keep a vault of persona definitions (character voices, role-bots, custom assistants…) as markdown files, you probably want to **deploy** them — not just store them. A persona file read as RAG context is not the same as a persona file injected as a system-prompt overlay.

Under edge-case questions (out-of-scope, ambiguous), an RAG-loaded persona tends to leak back to the agent's default identity ("As your operator…"). An overlay-loaded persona stays in voice.

v0.2 adds **mention-based auto-loading** so you don't have to remember every slug. Type *"what would Hickey say"* and the plugin resolves `Hickey → programmer-philosopher-clojurist-anglo`, loads the overlay just for that turn, then automatically releases.

Tracks [hermes-agent#643](https://github.com/NousResearch/hermes-agent/issues/643).

## How it works

**Slash command** (`/persona <slug>` — persistent):

1. Resolves to `<PERSONA_PATH_TEMPLATE>.format(slug=slug)` within your vault.
2. Calls `<PERSONA_TOOL>` via Hermes' tool registry to read that file.
3. YAML frontmatter is stripped; body is stored globally.
4. A `pre_llm_call` hook prepends the body to every user message as overlay context until you `/persona default` (or `/new` / `/reset`).

**Auto-load on mention** (`v0.2`, optional — one-shot):

1. At plugin load time, builds an alias index from `name_to_slug.json` + optional `aliases.yaml`.
2. Each turn, `pre_llm_call` checks the user message against patterns like `"qué diría <NAME>"`, `"what would <NAME>"`, `"according to <NAME>"`, `"in the style of <NAME>"`, etc.
3. If a match resolves to a known persona, the plugin loads it as a **one-shot** overlay.
4. After the LLM responds, `post_llm_call` clears the one-shot — next turn returns to the default identity.
5. Manual `/persona <slug>` (persistent) **always wins** — a mention during a manual session is ignored.

Auto-load is purely additive: if you don't provide a `name_to_slug.json`, the plugin runs in slash-only mode (v0.1 behavior).

## Requirements

- **Hermes Agent** (tested on v0.12.0).
- **A way for Hermes to read files from your vault.** Out of the box, `PERSONA_TOOL` defaults to `mcp_vault_bridge_vault_read_page` — the Hermes registry name for a `vault.read_page` MCP tool that takes `{"path": "<relative>"}` and returns `{"content": "...", "path": "..."}`. Any MCP tool with that contract works (a custom vault-bridge, `mcp_obsidian_read_note`, the built-in `read_file` tool with a path prefix, etc.). Adjust `PERSONA_TOOL` to match what you have.

## Install

```bash
git clone https://github.com/LK-Corporation/hermes-vault-persona ~/.hermes/plugins/vault-persona
hermes plugins enable vault-persona
systemctl --user restart hermes-gateway.service   # or restart your CLI session
```

Verify:

```bash
hermes plugins list | grep vault-persona   # should show 'enabled', source 'user'
```

**To enable auto-load on mention** (optional):

```bash
cd ~/.hermes/plugins/vault-persona

# Build name→slug mapping from your vault (one-shot, regenerable)
python build_mapping.py --vault ~/your-vault

# Optionally add coloquial aliases
cp aliases.example.yaml aliases.yaml
$EDITOR aliases.yaml

# Restart to load the new mapping
systemctl --user restart hermes-gateway.service
```

`build_mapping.py` reads `vault/conceptos/perfiles-bot/*.md` and extracts each persona-bot's `basado_en` reference (assuming Obsidian-style `[[../../entidades/personas/<slug>]]`) plus the canonical name from `vault/entidades/personas/<slug>.md`. Adapt the regex/paths inside if your vault uses different conventions.

## Config

Four environment variables, read at plugin import time:

| Var | Default | Purpose |
|---|---|---|
| `PERSONA_TOOL` | `mcp_vault_bridge_vault_read_page` | Hermes registry tool name to read the persona file. Must accept `{"path": "<relative>"}` and return JSON with a `content` field |
| `PERSONA_PATH_TEMPLATE` | `conceptos/perfiles-bot/{slug}.md` | Path within the vault, with `{slug}` placeholder |
| `PERSONA_MAPPING_FILE` | `<plugin_dir>/name_to_slug.json` | JSON file with name→slug mapping for auto-load. **If absent, auto-load is disabled** (slash-only mode) |
| `PERSONA_ALIASES_FILE` | `<plugin_dir>/aliases.yaml` | YAML with manual aliases / nicknames. Optional; merged on top of mapping aliases |

Example for an Obsidian vault with personas under `Personas/`:

```bash
# in ~/.hermes/.env
PERSONA_TOOL=mcp_obsidian_read_note
PERSONA_PATH_TEMPLATE=Personas/{slug}.md
PERSONA_MAPPING_FILE=/home/me/vault-mapping/name_to_slug.json
PERSONA_ALIASES_FILE=/home/me/vault-mapping/aliases.yaml
```

## Mention patterns recognized

Bilingual ES/EN out of the box. Edit `_INVOCATION_PREFIXES` in `__init__.py` to extend:

| Pattern | Example |
|---|---|
| `qué/cómo + dir[íi]a / piensa / opina / haría / reaccionaría / pensaría …` | "qué diría Hickey", "cómo pensaría Buterin" |
| `según / como / estilo / al estilo de / perspectiva de / opinión de / voz de` | "según Munger", "al estilo de Naval" |
| `what / how + would …` | "what would Hickey say" |
| `according to / in the style of / the view of / like` | "according to Munger" |

Plus a **filler matcher** for fronting: `"the [PERSON]"`, `"el de [PERSON]"`, `"la de [PERSON]"` all dispatch correctly. So `"what would the Clojure guy say"` works if `clojurist`/`Clojure guy` is in your aliases.

## Demo

A persona-bot file in the vault (`conceptos/perfiles-bot/programmer-philosopher-clojurist-anglo.md`):

```markdown
---
type: perfil-bot
tldr: "Engineer-philosopher in the Hickey vein. Separate simple from easy…"
basado_en:
  - [[../../entidades/personas/rich-hickey]]
---

# Programmer-philosopher (Clojurist anglo) — persona overlay

## Tone
Slow, etymological, dense. Distinguishes simple ("one fold") from easy
("familiar, ready-at-hand"). Anti-OOP, anti-PLOP, pro-thinking-off-computer.
…
```

In Telegram (slash command, persistent):

```
You: /persona programmer-philosopher-clojurist-anglo
Bot: Persona 'programmer-philosopher-clojurist-anglo' loaded as manual overlay
     (14518 chars). Active until /persona default.

You: I'm deciding between Redis and PostgreSQL for my SaaS. What lens?
Bot: [Hickey-voice response: simple-vs-easy, complecting, cite Antirez's
     ruthlessly-cut-complexity…]
```

Or just mention by name (one-shot, automatic — v0.2):

```
You: what would Hickey say about TypeScript strict mode?
Bot: [Hickey voice — etymologies, simple-vs-easy, complecting…]

You: and what does Munger think about long-term VC commitments?
Bot: [Munger voice — multidisciplinary mental models, inversion…]
     ^^ Hickey was auto-released after the previous turn

You: how's your day?
Bot: [default agent voice — no persona]
```

Out-of-scope handling (works in both modes):

```
You: what would Hickey say about AGI in 2030?
Bot: [Hickey voice] That's outside the corpus this persona draws
     from. If you want futurist takes on AGI timelines, I'd point you to
     Amodei or LeCun in your vault — those are the lenses to apply, not
     this one.
```

Before the plugin, that last question would drop back to the default agent identity ("As your operator…").

## Caveats (current 0.2.0)

- **Single-user / global state.** One active persona per Hermes instance, not per session. For Telegram bots with one user that's fine. Multi-user needs refactor to a dict keyed by `session_id` (which the handler doesn't receive directly — would need caching from `on_session_start`).
- **Auto-load is opportunistic.** The regex catches common patterns but isn't exhaustive. False negatives (mention without trigger like *"Hickey says X"* without "what would"/"según"/etc) are expected — by design, to avoid false positives when the user is just discussing a person rather than requesting their voice.
- **Mapping file must be rebuilt when your vault changes.** `build_mapping.py` is one-shot. Wire it as a pre-commit hook or a cron if your vault evolves fast.
- **Honcho / persistent memory leaks possible.** If your Hermes is also injecting conversational memory (e.g. via Honcho), that's part of the system prompt and the overlay is appended to the user message — so the model sees both. The overlay text explicitly tells the model to ignore prior memory, but residual leaks may show in long conversations. Workaround: `/new` to clear session memory before `/persona`.
- **YAML frontmatter assumption.** Plugin strips `^---\n.*?\n---\n` from the file. If your personas have a different header convention, edit `_FRONTMATTER_REGEX`.

## Roadmap

- Per-session state (when Hermes exposes session_id to slash-command handlers)
- Optional auto-`/new` on persona load to mitigate memory leak
- `/persona list` subcommand (would need MCP `vault.list_personas` tool or filesystem glob)
- Fuzzy-resolution for `/persona <ambiguous>` (currently exact slug only)
- Suggestion footer on default-identity responses ("for more in this voice: /persona X") — blocked by lack of an outbound-message hook in Hermes (`post_llm_call` is observer-only)

PRs welcome.

## Acknowledgments

- [Nous Research](https://nousresearch.com) for Hermes Agent and the plugin system that made this a small plugin instead of a fork.
- The `pre_llm_call` + `post_llm_call` + `register_command` API is the right surface for this kind of session-level customization.

## License

MIT — see [LICENSE](LICENSE).
