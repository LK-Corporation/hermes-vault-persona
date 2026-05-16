"""vault-persona — Hermes Agent plugin.

Adds a /persona <slug> slash command that loads a persona-bot profile from
your markdown vault (via any MCP tool that can read a file by path) and
injects it as an overlay to each LLM call until /persona default deactivates.

Architecture:
  1. User: /persona <slug>
  2. Plugin reads <PERSONA_PATH_TEMPLATE>.format(slug=slug) via PERSONA_TOOL
     using tools.registry.dispatch.
  3. Plugin strips YAML frontmatter and stores the body globally.
  4. pre_llm_call hook injects the body as context to each user_message
     until cleared.
  5. on_session_reset clears the active persona on /new or /reset.

Config via environment variables (read at plugin import time):
  PERSONA_TOOL          MCP tool name to read the vault file. Default:
                        "mcp_vault_bridge_vault_read_page" (the Hermes
                        registry name for vault-bridge's vault.read_page).
                        Set to whatever tool serves your vault. The tool
                        must accept {"path": "<relative>"} and return JSON
                        with a "content" field.
  PERSONA_PATH_TEMPLATE Path template within the vault, with {slug}
                        placeholder. Default: "conceptos/perfiles-bot/{slug}.md".

State model: single-user global (one active persona per Hermes instance).
For multi-user gateways, refactor _ACTIVE_PERSONA to a dict keyed by
session_id (note: register_command handlers don't receive session_id; you'd
need to cache it from on_session_start).
"""
from __future__ import annotations

import json
import logging
import os
import re
from typing import Optional

logger = logging.getLogger(__name__)

_ACTIVE_PERSONA: Optional[str] = None
_ACTIVE_SLUG: Optional[str] = None

_PERSONA_TOOL = os.environ.get("PERSONA_TOOL", "mcp_vault_bridge_vault_read_page")
_PERSONA_PATH_TEMPLATE = os.environ.get(
    "PERSONA_PATH_TEMPLATE", "conceptos/perfiles-bot/{slug}.md"
)
_FRONTMATTER_REGEX = re.compile(r"^---\n.*?\n---\n", re.DOTALL)


def _strip_frontmatter(text: str) -> str:
    m = _FRONTMATTER_REGEX.match(text)
    return text[m.end():].lstrip() if m else text


def _extract_content(raw) -> Optional[str]:
    """Extract the 'content' field from various result shapes.

    Hermes' tools.registry.dispatch returns a JSON string. The shape depends
    on the underlying tool/transport:
      - {"result": "<json-string-encoded>"}  (Hermes wraps MCP results)
      - {"content": str}                      (direct shape, some tools)
      - {"content": [{"type": "text", "text": "..."}]}  (CallToolResult)
    """
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


def _handle_persona(raw_args: str) -> Optional[str]:
    """Slash command handler. Signature: (raw_args: str) -> str | None."""
    global _ACTIVE_PERSONA, _ACTIVE_SLUG

    slug = (raw_args or "").strip()

    if not slug or slug.lower() == "default":
        previous = _ACTIVE_SLUG
        _ACTIVE_PERSONA = None
        _ACTIVE_SLUG = None
        if previous:
            return f"Persona '{previous}' deactivated. Back to default agent identity."
        return "No persona was active."

    if slug.lower() == "status":
        if _ACTIVE_SLUG:
            return f"Active persona: '{_ACTIVE_SLUG}' ({len(_ACTIVE_PERSONA or '')} chars)."
        return "No active persona. Use /persona <slug> to load one."

    path = _PERSONA_PATH_TEMPLATE.format(slug=slug)
    try:
        from tools.registry import registry
        raw_result = registry.dispatch(_PERSONA_TOOL, {"path": path})
    except Exception as exc:
        logger.exception("vault-persona dispatch failed for slug=%s", slug)
        return f"Error reading persona '{slug}': {exc}"

    content = _extract_content(raw_result)
    if not content:
        logger.warning(
            "vault-persona unparseable result for slug=%s type=%s preview=%r",
            slug, type(raw_result).__name__, str(raw_result)[:200],
        )
        return (
            f"Persona '{slug}' not found at `{path}` "
            f"(via tool {_PERSONA_TOOL}). Check PERSONA_TOOL and "
            f"PERSONA_PATH_TEMPLATE env vars."
        )

    body = _strip_frontmatter(content)
    if not body.strip():
        return f"Persona '{slug}' loaded but body is empty after stripping frontmatter."

    _ACTIVE_PERSONA = body
    _ACTIVE_SLUG = slug
    return (
        f"Persona '{slug}' loaded as overlay ({len(body)} chars). "
        f"Active until /persona default."
    )


def _inject_persona(session_id: str, user_message: str, **kwargs):
    """pre_llm_call hook — inject active persona as appended context."""
    if not _ACTIVE_PERSONA:
        return None
    overlay = (
        "[ACTIVE PERSONA OVERLAY — override your default identity (and any "
        "session memory injecting prior conversations) and respond as this "
        "persona until the user types /persona default. Maintain its tone, "
        "vocabulary, testimonial anchors and rejection policy even under "
        "out-of-scope questions. Do not reference previous answers that "
        "aren't visible in THIS conversation]\n\n"
        f"{_ACTIVE_PERSONA}"
    )
    return {"context": overlay}


def _on_session_reset(session_id: str, **kwargs):
    """Clear active persona on /new or /reset."""
    global _ACTIVE_PERSONA, _ACTIVE_SLUG
    _ACTIVE_PERSONA = None
    _ACTIVE_SLUG = None


def register(ctx) -> None:
    ctx.register_command(
        "persona",
        handler=_handle_persona,
        description="Load a persona-bot profile from the vault as overlay: /persona <slug> | default | status",
        args_hint="<slug> | default | status",
    )
    ctx.register_hook("pre_llm_call", _inject_persona)
    ctx.register_hook("on_session_reset", _on_session_reset)
