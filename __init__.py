"""vault-persona — Hermes Agent plugin.

v0.1: /persona <slug> slash command — loads a persona-bot profile from
your markdown vault (via any MCP tool that can read a file by path) and
injects it as an overlay to each LLM call until /persona default clears.

v0.2: adds **auto-load on mention** (one-shot). When the user mentions
a persona by name in natural language ("what would Hickey say about X",
"like Naval would put it", "según Munger"…), the plugin detects the name,
resolves it to a perfil-bot slug via a mapping file + optional aliases,
and loads the overlay JUST for that turn. Next turn returns to the
default agent identity. Manual `/persona <slug>` is still persistent.

v0.3: adds **semantic routing** (OPT-IN, disabled by default). When the
user message has no explicit mention but its meaning matches a persona
in the vault, the plugin can suggest one (or up to 3) via RAG over a
Qdrant collection + cross-encoder reranking. Conservative UX: the LLM
sees the suggestions inline and decides whether to OFFER them at the
end of its response — NEVER auto-loads. Requires HERMES_ROUTING_ENABLED=true
and a Qdrant + Ollama (for embeddings) + optional rerank-server stack.

v0.3 also adds a **post_tool_call hook** that observes when the LLM loads
a persona directly via the configured PERSONA_TOOL (i.e. tool-driven
loads without /persona or mention) and emits a structured log line so
external telemetry can correlate routing suggestions with tool-driven
acceptances.

Auto-load is **optional** — if no mapping file is present, the plugin
behaves exactly like v0.1 (slash commands only). Routing is also
**optional** — without HERMES_ROUTING_ENABLED=true the plugin behaves
exactly like v0.2.

Mechanics:
  - /persona <slug>             → persistent manual load
  - /persona default            → deactivate
  - /persona status             → state info
  - "what would X say about Y"  → one-shot auto-load of X, replies, releases
  - manual active wins over mention; mention wins over routing
  - routing only fires when there's no manual + no mention + message ≥ N chars

Config via environment variables (read at plugin import time):
  PERSONA_TOOL           MCP tool name to read the vault file. Default:
                         "mcp_vault_bridge_vault_read_page". The tool must
                         accept {"path": "<relative>"} and return JSON with
                         a "content" field.
  PERSONA_PATH_TEMPLATE  Path template within the vault. Default:
                         "conceptos/perfiles-bot/{slug}.md".
  PERSONA_MAPPING_FILE   JSON file with name→slug mapping for auto-load.
                         Default: "<plugin_dir>/name_to_slug.json".
                         If absent, auto-load is disabled (slash-only mode).
  PERSONA_ALIASES_FILE   YAML file with manual aliases (apellidos, nicknames).
                         Default: "<plugin_dir>/aliases.yaml". Optional;
                         merged on top of mapping aliases.

  HERMES_ROUTING_ENABLED    Opt-in switch for v0.3 semantic routing.
                            Default "false". Set "true" to activate.
  HERMES_ROUTING_QDRANT_URL Qdrant base URL. Default http://localhost:6333.
  HERMES_ROUTING_OLLAMA_URL Ollama base URL (for embeddings). Default
                            http://localhost:11434.
  HERMES_ROUTING_RERANK_URL Optional rerank-server URL (cross-encoder).
                            If unreachable, falls back to bi-encoder scores.
                            Default http://localhost:6335.
  HERMES_ROUTING_COLLECTION Qdrant collection name. Default "personas-routing".
  HERMES_ROUTING_EMBED_MODEL  Ollama embedding model name. Default "bge-m3".
  HERMES_ROUTING_MIN_LEN    Min message chars to fire routing. Default 20.
  HERMES_ROUTING_TIMEOUT_S  Pipeline timeout (graceful fallback). Default 5.
  HERMES_ROUTING_TOP_MIN    Min top-1 score to suggest. Default 0.50.
  HERMES_ROUTING_TOP_STRONG Top-1 strong threshold (suggest only 1). Default 0.55.
  HERMES_ROUTING_GAP        Min gap top-1 vs top-3 for "strong". Default 0.05.

Build mapping: run `python build_mapping.py --vault <path>` (included).

State model: single-user global (one active persona per Hermes instance).
For multi-user gateways, refactor _ACTIVE_PERSONA to a dict keyed by
session_id (note: register_command handlers don't receive session_id;
you would need to cache it from on_session_start).

Tracks: https://github.com/NousResearch/hermes-agent/issues/643
"""
from __future__ import annotations

import json
import logging
import os
import re
import unicodedata
from pathlib import Path
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

# === Single-user global state ===
_ACTIVE_PERSONA: Optional[str] = None
_ACTIVE_SLUG: Optional[str] = None
_ONE_SHOT_ACTIVE: bool = False

# === Configurable via env (read at import time) ===
_PERSONA_TOOL = os.environ.get("PERSONA_TOOL", "mcp_vault_bridge_vault_read_page")
_PERSONA_PATH_TEMPLATE = os.environ.get(
    "PERSONA_PATH_TEMPLATE", "conceptos/perfiles-bot/{slug}.md"
)
_PERSONA_MAPPING_FILE = os.environ.get("PERSONA_MAPPING_FILE")  # default: plugin_dir/name_to_slug.json
_PERSONA_ALIASES_FILE = os.environ.get("PERSONA_ALIASES_FILE")  # default: plugin_dir/aliases.yaml

# Path prefix derived from template — used by post_tool_call observer to detect
# when the LLM loaded a perfil-bot directly via PERSONA_TOOL (skipping mention).
_PERFIL_PATH_PREFIX = _PERSONA_PATH_TEMPLATE.split("{slug}")[0]

# === v0.3 Semantic routing (OPT-IN) ===
ROUTING_ENABLED = os.environ.get("HERMES_ROUTING_ENABLED", "false").lower() == "true"
ROUTING_QDRANT_URL = os.environ.get("HERMES_ROUTING_QDRANT_URL", "http://localhost:6333")
ROUTING_OLLAMA_URL = os.environ.get("HERMES_ROUTING_OLLAMA_URL", "http://localhost:11434")
ROUTING_RERANK_URL = os.environ.get("HERMES_ROUTING_RERANK_URL", "http://localhost:6335")
ROUTING_COLLECTION = os.environ.get("HERMES_ROUTING_COLLECTION", "personas-routing")
ROUTING_EMBED_MODEL = os.environ.get("HERMES_ROUTING_EMBED_MODEL", "bge-m3")
ROUTING_MIN_LEN = int(os.environ.get("HERMES_ROUTING_MIN_LEN", "20"))
ROUTING_TIMEOUT_S = float(os.environ.get("HERMES_ROUTING_TIMEOUT_S", "5"))
ROUTING_GAP_THRESHOLD = float(os.environ.get("HERMES_ROUTING_GAP", "0.05"))
ROUTING_TOP_THRESHOLD = float(os.environ.get("HERMES_ROUTING_TOP_MIN", "0.50"))
ROUTING_STRONG_THRESHOLD = float(os.environ.get("HERMES_ROUTING_TOP_STRONG", "0.55"))
_HTTP_CLIENT: Optional[httpx.Client] = None

# === Mapping name→perfil_slug (loaded at register) ===
# Shape: {normalized_alias: {"persona_slug": str, "perfil_slug": str, "nombre_canonico": str}}
_ALIAS_INDEX: dict[str, dict] = {}

_FRONTMATTER_REGEX = re.compile(r"^---\n.*?\n---\n", re.DOTALL)
_FILLER_PREFIX_RE = re.compile(r"^(?:el\s+de\s+|la\s+de\s+|the\s+|el\s+|la\s+)")

# Invocation prefixes — each ends with its own \s+ (the concat does NOT add more).
# Bilingual ES/EN — extend as needed for your locale.
_INVOCATION_PREFIXES = [
    # ES: qué/cómo + verb + persona
    r"qu[éeèë]\s+(?:dir[íi]a|piensa|opina|dice|opinar[íi]a|piensar[íi]a|pensar[íi]a|dijo|pens[óo]|opin[óo]|critic[óo]|criticar[íi]a|reaccionar[íi]a|reaccion[óo]|reacciona|hace|har[íi]a|hizo|haria)\s+",
    r"c[óo]mo\s+(?:lo\s+)?(?:dir[íi]a|opinar[íi]a|ver[íi]a|piensa|pensar[íi]a|reaccionar[íi]a|criticar[íi]a|encarar[íi]a|enfocar[íi]a|abordar[íi]a|analizar[íi]a|verbaliz[óo])\s+",
    r"como\s+",
    r"seg[úu]n\s+",
    r"(?:al\s+)?estilo\s+(?:de\s+)?",
    r"perspectiva\s+de\s+",
    r"opini[óo]n\s+de\s+",
    r"voz\s+de\s+",
    r"qu[éeèë]\s+(?:te\s+)?parece\s+a\s+",
    # EN: what/how + verb + person
    r"what\s+would\s+",
    r"how\s+would\s+",
    r"according\s+to\s+",
    r"in\s+the\s+style\s+of\s+",
    r"the\s+view\s+of\s+",
    r"like\s+",  # broad — keep last
]


def _normalize(s: str) -> str:
    """Lowercase + strip accents + collapse whitespace + strip filler prefix.

    Stripping filler ('el de', 'la de', 'el', 'la', 'the') from the alias key
    is critical so the regex composes properly — the filler is added separately
    in _detect_mention. Without this, an alias like 'el de Almanack' would
    never match because the pattern would be "(filler)?(el de almanack)" — a
    double-filler that won't fit "la de almanack".
    """
    s = s.lower().strip()
    s = unicodedata.normalize("NFD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = re.sub(r"\s+", " ", s)
    s = _FILLER_PREFIX_RE.sub("", s)
    return s


def _load_mapping_files(plugin_dir: Path) -> None:
    """Load name_to_slug.json + aliases.yaml into _ALIAS_INDEX. Tolerant:
    if files are absent, auto-load stays disabled (slash-only mode)."""
    global _ALIAS_INDEX
    _ALIAS_INDEX = {}

    json_path = Path(_PERSONA_MAPPING_FILE) if _PERSONA_MAPPING_FILE else plugin_dir / "name_to_slug.json"
    aliases_path = Path(_PERSONA_ALIASES_FILE) if _PERSONA_ALIASES_FILE else plugin_dir / "aliases.yaml"

    if not json_path.exists():
        logger.info(
            "vault-persona: no mapping file at %s — auto-load disabled (slash-only mode)",
            json_path,
        )
        return

    try:
        data = json.loads(json_path.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.exception("vault-persona: error reading %s: %s", json_path, exc)
        return

    mapping: dict[str, dict] = data.get("mapping", {})

    # Minimal YAML parser (avoids pyyaml dependency for plugin runtime)
    aliases_extra: dict[str, list[str]] = {}
    if aliases_path.exists():
        try:
            current_slug = None
            for raw_line in aliases_path.read_text(encoding="utf-8").splitlines():
                line = raw_line.rstrip()
                if not line or line.lstrip().startswith("#"):
                    continue
                if not line.startswith(" ") and ":" in line:
                    current_slug = line.split(":", 1)[0].strip()
                    if current_slug and not aliases_extra.get(current_slug):
                        aliases_extra[current_slug] = []
                elif current_slug and line.lstrip().startswith("-"):
                    alias = line.lstrip()[1:].strip()
                    if alias.startswith(('"', "'")) and alias.endswith(('"', "'")):
                        alias = alias[1:-1]
                    if alias:
                        aliases_extra.setdefault(current_slug, []).append(alias)
        except Exception as exc:
            logger.exception("vault-persona: error parsing %s: %s", aliases_path, exc)

    for persona_slug, info in mapping.items():
        perfil_slug = info.get("perfil_slug")
        nombre = info.get("nombre_canonico", persona_slug)
        if not perfil_slug:
            continue
        candidates = [nombre]
        candidates.extend(info.get("aliases", []) or [])
        candidates.extend(aliases_extra.get(persona_slug, []))
        candidates.append(persona_slug.replace("-", " "))  # slug-as-name fallback

        for cand in candidates:
            if not isinstance(cand, str):
                continue
            key = _normalize(cand)
            if len(key) < 3:
                continue
            if key in _ALIAS_INDEX:  # first writer wins
                continue
            _ALIAS_INDEX[key] = {
                "persona_slug": persona_slug,
                "perfil_slug": perfil_slug,
                "nombre_canonico": nombre,
            }

    logger.info(
        "vault-persona: loaded %d aliases for %d personas",
        len(_ALIAS_INDEX), len(mapping),
    )


def _detect_mention(user_message: str) -> Optional[dict]:
    """Detect if user_message invokes a mapped persona. Returns dict with
    persona_slug / perfil_slug / nombre_canonico / trigger or None."""
    if not _ALIAS_INDEX:
        return None

    text_norm = _normalize(user_message)
    for alias, info in _ALIAS_INDEX.items():
        alias_re = re.escape(alias)
        filler = r"(?:el\s+de\s+|la\s+de\s+|the\s+|el\s+|la\s+)?"
        for prefix in _INVOCATION_PREFIXES:
            # NOTE: prefix already ends with \s+ — don't add more here.
            pattern = rf"\b{prefix}{filler}{alias_re}\b"
            m = re.search(pattern, text_norm)
            if m:
                logger.info(
                    "vault-persona auto-mention: '%s' → %s",
                    alias, info["perfil_slug"],
                )
                return {**info, "trigger": alias, "prefix_match": m.group(0)}
    return None


def _strip_frontmatter(text: str) -> str:
    m = _FRONTMATTER_REGEX.match(text)
    return text[m.end():].lstrip() if m else text


def _extract_content(raw) -> Optional[str]:
    """Extract content from vault.read_page result (multiple shapes)."""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            return raw
    if not isinstance(raw, dict):
        return None
    if "result" in raw:
        return _extract_content(raw["result"])
    content = raw.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list) and content:
        first = content[0]
        if isinstance(first, dict):
            inner_text = first.get("text") or first.get("content")
            if isinstance(inner_text, str):
                return _extract_content(inner_text)
    return None


def _load_perfil_body(slug: str) -> Optional[str]:
    """Read persona file from vault via the configured MCP tool. Returns
    body without frontmatter, or None on failure."""
    path = _PERSONA_PATH_TEMPLATE.format(slug=slug)
    try:
        from tools.registry import registry
        raw_result = registry.dispatch(_PERSONA_TOOL, {"path": path})
    except Exception as exc:
        logger.exception(
            "vault-persona: %s dispatch failed for slug=%s: %s",
            _PERSONA_TOOL, slug, exc,
        )
        return None
    content = _extract_content(raw_result)
    if not content:
        return None
    body = _strip_frontmatter(content)
    return body if body.strip() else None


def _build_overlay(body: str) -> str:
    return (
        "[ACTIVE PERSONA — ignore your default agent identity and any "
        "long-term memory continuity for this turn. Adopt this persona "
        "completely until /persona default.\n\n"
        "CRITICAL OPERATIONAL RULES (override default LLM tendencies):\n"
        "- FORMAT: if the persona declares format rules (e.g. 'prose only, "
        "no bullets/headers', 'etymology in opening', 'poetic cadence'), they "
        "are NON-NEGOTIABLE, NOT stylistic preferences. Default LLM bullet-spam "
        "with ### headers + numbered lists violates most philosopher personas. "
        "If unsure between listicle vs prose: prose.\n"
        "- ANCHORS: use the 'testimonial anchors' / 'frases-ancla' listed in "
        "the persona LITERALLY (do not paraphrase). MAXIMUM 1 anchor per "
        "response unless the persona explicitly says otherwise — more anchors "
        "= boastful, breaks the voice. If the persona requires 'closing with "
        "frase-ancla', the closing must be a literal quote.\n"
        "- OUT-OF-SCOPE: follow the persona's 'rejection policy' to the "
        "letter. If it says 'stop without apologizing + offer adjacent + "
        "redirect', do that — DO NOT elaborate on the rejected topic, DO NOT "
        "give concrete recommendations on it. Recommending something out-of-"
        "scope concretely violates the persona's discipline.\n"
        "- VOICE: stay in FIRST PERSON always. Don't say 'the bot would say' "
        "or 'this persona thinks' — respond AS the persona. Don't fall back to "
        "default agent identity even for meta-comments.\n"
        "- Do not reference previous turns not visible in THIS conversation]\n\n"
        f"{body}"
    )


# === v0.3 Semantic routing (opt-in via HERMES_ROUTING_ENABLED=true) ===
def _semantic_route(user_message: str) -> Optional[list[dict]]:
    """Suggest personas semantically relevant to the user message via RAG.

    Pipeline: embed (Ollama bge-m3) → Qdrant query top-10 → cross-encoder rerank
    (rerank-server HTTP, optional with graceful fallback) → gap+threshold filter.

    Returns list of {slug, basado_en, tldr, score} or None if disabled / too short /
    below threshold / pipeline failure. Failures are graceful: never break the chat.
    """
    if not ROUTING_ENABLED or _HTTP_CLIENT is None:
        return None
    if not user_message:
        return None
    msg = user_message.strip()
    if len(msg) < ROUTING_MIN_LEN:
        return None
    if msg.startswith("/"):  # slash command, not a semantic query
        return None

    try:
        # 1. Embed via Ollama
        r = _HTTP_CLIENT.post(
            f"{ROUTING_OLLAMA_URL}/api/embed",
            json={"model": ROUTING_EMBED_MODEL, "input": msg},
            timeout=ROUTING_TIMEOUT_S,
        )
        r.raise_for_status()
        embs = r.json().get("embeddings") or []
        if not embs:
            return None
        vec = embs[0]

        # 2. Qdrant query top-10
        r = _HTTP_CLIENT.post(
            f"{ROUTING_QDRANT_URL}/collections/{ROUTING_COLLECTION}/points/query",
            json={"query": vec, "limit": 10, "with_payload": True},
            timeout=ROUTING_TIMEOUT_S,
        )
        r.raise_for_status()
        points = (r.json().get("result") or {}).get("points") or []
        if not points:
            return None

        # 3. Optional cross-encoder rerank (graceful fallback to bi-encoder)
        docs = [(p.get("payload") or {}).get("text", "") for p in points]
        try:
            r = _HTTP_CLIENT.post(
                f"{ROUTING_RERANK_URL}/rerank",
                json={"query": msg, "documents": docs, "normalize": True},
                timeout=ROUTING_TIMEOUT_S,
            )
            r.raise_for_status()
            ranked = r.json().get("results") or []
            if ranked:
                order = [item["index"] for item in ranked]
                points = [points[i] for i in order]
                for p, item in zip(points, ranked):
                    p["_routing_score"] = float(item["score"])
        except Exception as exc:
            logger.warning("vault-persona routing: rerank unavailable (%s) — using bi-encoder", exc)
            for p in points:
                p["_routing_score"] = float(p.get("score", 0))

        # 4. Threshold + gap (top-1 vs top-3)
        top_score = points[0].get("_routing_score", points[0].get("score", 0))
        third_score = (
            points[2].get("_routing_score", points[2].get("score", 0))
            if len(points) >= 3 else 0
        )
        gap = top_score - third_score

        if top_score < ROUTING_TOP_THRESHOLD:
            return None

        if gap >= ROUTING_GAP_THRESHOLD and top_score >= ROUTING_STRONG_THRESHOLD:
            picks = points[:1]
        else:
            picks = points[:3]

        suggestions = []
        for p in picks:
            payload = p.get("payload") or {}
            suggestions.append({
                "slug": payload.get("slug"),
                "basado_en": payload.get("basado_en") or [],
                "tldr": payload.get("tldr") or "",
                "score": p.get("_routing_score", p.get("score", 0)),
            })
        return suggestions if suggestions else None

    except Exception as exc:
        logger.warning("vault-persona routing: pipeline failed: %s", exc)
        return None


def _build_routing_context(suggestions: list[dict]) -> str:
    """Inline context inserted into the LLM call describing the routing suggestions.

    The LLM is INSTRUCTED to offer them at the END of its default reply (not to
    auto-load). Conservative UX: the user decides whether to accept.
    """
    lines = [
        "[PERSONA ROUTER — the user's message semantically matches personas in",
        "the vault that were NOT explicitly mentioned. If your default reply",
        "would benefit from one specific perspective from the list below, OFFER",
        "the user to load it at the END of your reply (do NOT auto-load — only",
        "suggest). If your default reply is already enough or the suggestion",
        "doesn't actually fit the case, IGNORE these options and reply normally.",
        "",
        "Suggested wording when applicable:",
        "  💡 *This fits the perspective of [Name]. Reply 'yes' or invoke",
        "  `/persona <slug>` if you want to load it.*",
        "",
        "If 2-3 suggestions are relevant, present them briefly:",
        "  💡 *Several perspectives apply: [A] / [B] / [C]. Which one? Reply",
        "  the slug or ignore.*",
        "",
        "Relevant personas (ranked by relevance):",
    ]
    for s in suggestions:
        nombre = ", ".join(s.get("basado_en") or []) or s.get("slug", "?")
        score = s.get("score", 0)
        tldr = (s.get("tldr") or "")[:140]
        slug = s.get("slug", "?")
        lines.append(f"- score={score:.2f} | slug=`{slug}` | {nombre} — {tldr}")
    lines.append("]")
    return "\n".join(lines)


# === v0.3 post_tool_call observer (telemetry only) ===
def _on_tool_call(
    tool_name: str = "",
    args=None,
    result=None,
    task_id: str = "",
    session_id: str = "",
    tool_call_id: str = "",
    duration_ms: int = 0,
    **kwargs,
) -> None:
    """post_tool_call observer — emit a structured log line whenever the LLM
    loads a persona directly via PERSONA_TOOL (i.e. tool-driven load, no
    mention, no /persona). External telemetry can correlate this with prior
    routing_suggestion events to compute true acceptance rate.

    Silent no-op if tool_name doesn't match or path is outside the perfil dir.
    """
    if tool_name != _PERSONA_TOOL:
        return
    if not isinstance(args, dict):
        return
    path = args.get("path") or ""
    if not isinstance(path, str) or not path.startswith(_PERFIL_PATH_PREFIX):
        return
    slug = path[len(_PERFIL_PATH_PREFIX):]
    if slug.endswith(".md"):
        slug = slug[:-3]
    slug = slug.strip().strip("/")
    if not slug:
        return
    logger.info(
        "vault-persona: tool-load perfil=%s (duration_ms=%d)",
        slug, duration_ms,
    )


# === Slash command handler ===
def _handle_persona(raw_args: str) -> Optional[str]:
    """/persona handler. Signature: (raw_args: str) -> str | None."""
    global _ACTIVE_PERSONA, _ACTIVE_SLUG, _ONE_SHOT_ACTIVE

    slug = (raw_args or "").strip()

    if not slug or slug.lower() == "default":
        previous = _ACTIVE_SLUG
        _ACTIVE_PERSONA = None
        _ACTIVE_SLUG = None
        _ONE_SHOT_ACTIVE = False
        if previous:
            return f"Persona '{previous}' deactivated. Back to default agent identity."
        return "No persona was active."

    if slug.lower() == "status":
        if _ACTIVE_SLUG:
            mode = "one-shot (auto)" if _ONE_SHOT_ACTIVE else "manual (persistent)"
            return f"Active persona: '{_ACTIVE_SLUG}' [{mode}] ({len(_ACTIVE_PERSONA or '')} chars)."
        if _ALIAS_INDEX:
            return f"No active persona. Auto-load available for {len(_ALIAS_INDEX)} alias(es)."
        return "No active persona. /persona <slug> to load one."

    body = _load_perfil_body(slug)
    if not body:
        return (
            f"Persona '{slug}' not found via {_PERSONA_TOOL} at path "
            f"'{_PERSONA_PATH_TEMPLATE.format(slug=slug)}'. Check the slug and "
            "your PERSONA_TOOL / PERSONA_PATH_TEMPLATE config."
        )

    _ACTIVE_PERSONA = body
    _ACTIVE_SLUG = slug
    _ONE_SHOT_ACTIVE = False  # manual is NEVER one-shot
    return (
        f"Persona '{slug}' loaded as manual overlay ({len(body)} chars). "
        "Active until /persona default."
    )


# === Hooks ===
def _inject_persona(session_id: str, user_message: str, **kwargs):
    """pre_llm_call hook — 3 levels of persona invocation:
    1. Manual active persona → inject full overlay
    2. Mention detection → one-shot auto-load with overlay
    3. v0.3 semantic routing (opt-in) → inject suggestion context (LLM decides
       whether to offer; never auto-loads)
    """
    global _ACTIVE_PERSONA, _ACTIVE_SLUG, _ONE_SHOT_ACTIVE

    # 1. If a persona is already active (manual or residual one-shot), inject
    if _ACTIVE_PERSONA:
        return {"context": _build_overlay(_ACTIVE_PERSONA)}

    # 2. Try to detect a mention for one-shot auto-load
    detected = _detect_mention(user_message or "")
    if detected:
        body = _load_perfil_body(detected["perfil_slug"])
        if not body:
            logger.warning(
                "vault-persona: detected '%s' but failed loading persona %s",
                detected["trigger"], detected["perfil_slug"],
            )
        else:
            _ACTIVE_PERSONA = body
            _ACTIVE_SLUG = detected["perfil_slug"]
            _ONE_SHOT_ACTIVE = True
            logger.info(
                "vault-persona: auto-load one-shot perfil=%s (trigger='%s')",
                detected["perfil_slug"], detected["trigger"],
            )
            return {"context": _build_overlay(body)}

    # 3. v0.3 semantic routing — suggest relevant personas without auto-loading
    suggestions = _semantic_route(user_message or "")
    if suggestions:
        slugs_log = ",".join(s.get("slug", "?") for s in suggestions)
        logger.info(
            "vault-persona routing: %d sugerencias (%s) top_score=%.2f",
            len(suggestions), slugs_log, suggestions[0].get("score", 0),
        )
        return {"context": _build_routing_context(suggestions)}

    return None


def _reset_one_shot(session_id: str, **kwargs):
    """post_llm_call observer — clears _ONE_SHOT_ACTIVE after the auto-load turn."""
    global _ACTIVE_PERSONA, _ACTIVE_SLUG, _ONE_SHOT_ACTIVE
    if _ONE_SHOT_ACTIVE:
        slug_was = _ACTIVE_SLUG
        _ACTIVE_PERSONA = None
        _ACTIVE_SLUG = None
        _ONE_SHOT_ACTIVE = False
        logger.info("vault-persona: one-shot persona %s auto-cleared", slug_was)


def _on_session_reset(session_id: str, **kwargs):
    """Clear persona on /new or /reset."""
    global _ACTIVE_PERSONA, _ACTIVE_SLUG, _ONE_SHOT_ACTIVE
    _ACTIVE_PERSONA = None
    _ACTIVE_SLUG = None
    _ONE_SHOT_ACTIVE = False


# === Plugin registration ===
def register(ctx) -> None:
    global _HTTP_CLIENT
    plugin_dir = Path(__file__).resolve().parent
    _load_mapping_files(plugin_dir)

    if ROUTING_ENABLED:
        _HTTP_CLIENT = httpx.Client(timeout=ROUTING_TIMEOUT_S)
        logger.info(
            "vault-persona: routing semántico habilitado (qdrant=%s, collection=%s)",
            ROUTING_QDRANT_URL, ROUTING_COLLECTION,
        )
    else:
        logger.info(
            "vault-persona: semantic routing disabled (set HERMES_ROUTING_ENABLED=true to enable)"
        )

    auto_label = (
        f"Auto-load available for {len(_ALIAS_INDEX)} alias(es) by mention."
        if _ALIAS_INDEX
        else "Auto-load disabled (no mapping file)."
    )

    ctx.register_command(
        "persona",
        handler=_handle_persona,
        description=(
            "Load a persona-bot from your vault as overlay: "
            f"/persona <slug> | default | status. {auto_label}"
        ),
        args_hint="<slug> | default | status",
    )
    ctx.register_hook("pre_llm_call", _inject_persona)
    ctx.register_hook("post_llm_call", _reset_one_shot)
    ctx.register_hook("on_session_reset", _on_session_reset)
    ctx.register_hook("post_tool_call", _on_tool_call)
