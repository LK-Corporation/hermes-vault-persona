#!/usr/bin/env python3
"""build_mapping.py — construye name_to_slug.json para el router auto-load.

Itera vault/conceptos/perfiles-bot/*.md, extrae basado_en (regex sobre frontmatter
porque yaml.safe_load no parsea bien `[[../../entidades/personas/X]]` Obsidian-style),
mapea a vault/entidades/personas/<slug>.md, saca nombre canónico (primer H1 del body)
y construye el JSON.

Output: infra/kali/vault-persona-plugin/name_to_slug.json

Uso:
    py infra/kali/vault-persona-plugin/build_mapping.py
    py infra/kali/vault-persona-plugin/build_mapping.py --vault /path/to/vault
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---", re.DOTALL)
# Match `entidades/personas/<slug>` en cualquier sintaxis Obsidian/markdown
PERSONA_REF_RE = re.compile(r"entidades/personas/([a-z0-9][a-z0-9-]*)")
H1_RE = re.compile(r"^# (.+)$", re.MULTILINE)
TLDR_RE = re.compile(r'^tldr:\s*"([^"]+)"', re.MULTILINE)
SECTOR_RE = re.compile(r"sector/([a-z][a-z-]*)")


def parse_frontmatter(text: str) -> str:
    """Devuelve el bloque de frontmatter (entre `---`s)."""
    m = FRONTMATTER_RE.match(text)
    return m.group(1) if m else ""


def extract_persona_slugs(perfil_md_text: str) -> list[str]:
    """Extrae los slugs de personas referenciadas en `basado_en` del perfil-bot."""
    fm = parse_frontmatter(perfil_md_text)
    # Limita a la sección basado_en (de la línea con "basado_en:" hasta siguiente key top-level o ---)
    in_section = False
    section_lines = []
    for line in fm.splitlines():
        if re.match(r"^basado_en\s*:", line):
            in_section = True
            continue
        if in_section:
            if re.match(r"^[a-z_]+\s*:", line) and not line.startswith(" "):
                # Nueva key top-level → fin de basado_en
                break
            section_lines.append(line)
    section_text = "\n".join(section_lines)
    return PERSONA_REF_RE.findall(section_text)


def extract_tldr(perfil_md_text: str) -> str:
    fm = parse_frontmatter(perfil_md_text)
    m = TLDR_RE.search(fm)
    return (m.group(1).strip() if m else "")[:160]


def extract_sectors(perfil_md_text: str) -> list[str]:
    fm = parse_frontmatter(perfil_md_text)
    # Solo línea "sector: [...]"
    sector_line = ""
    for line in fm.splitlines():
        if line.startswith("sector:"):
            sector_line = line
            break
    seen = []
    for s in SECTOR_RE.findall(sector_line):
        if s not in seen:
            seen.append(s)
    return seen[:3]


def extract_nombre_canonico(persona_md_path: Path) -> str | None:
    """Lee la página de persona, devuelve el H1 del body."""
    if not persona_md_path.exists():
        return None
    text = persona_md_path.read_text(encoding="utf-8")
    # Skipea frontmatter
    body = FRONTMATTER_RE.sub("", text, count=1).lstrip()
    m = H1_RE.search(body)
    return m.group(1).strip() if m else None


def build(vault: Path) -> dict:
    perfiles_dir = vault / "conceptos" / "perfiles-bot"
    personas_dir = vault / "entidades" / "personas"

    if not perfiles_dir.exists():
        sys.exit(f"No existe {perfiles_dir}")
    if not personas_dir.exists():
        sys.exit(f"No existe {personas_dir}")

    # mapping: persona_slug → {perfil_slug, nombre_canonico, tldr, sector, aliases}
    mapping: dict[str, dict] = {}
    perfiles_sin_basado = []  # perfiles que no tienen basado_en a persona específica

    for perfil_path in sorted(perfiles_dir.glob("*.md")):
        if perfil_path.name.endswith(".compact.md"):
            continue
        perfil_slug = perfil_path.stem
        text = perfil_path.read_text(encoding="utf-8")
        personas_refs = extract_persona_slugs(text)
        tldr = extract_tldr(text)
        sectors = extract_sectors(text)

        if not personas_refs:
            perfiles_sin_basado.append(perfil_slug)
            continue

        # Si hay >1 persona basado_en (perfiles antología), usar primera + nota
        for persona_slug in personas_refs:
            persona_md = personas_dir / f"{persona_slug}.md"
            nombre_canonico = extract_nombre_canonico(persona_md) or persona_slug.replace("-", " ").title()

            # Si esta persona ya tiene un perfil mapeado, advertir
            existing = mapping.get(persona_slug)
            if existing:
                # Mantener el primero, anotar conflicto
                existing.setdefault("perfil_alternativos", []).append(perfil_slug)
                continue

            mapping[persona_slug] = {
                "perfil_slug": perfil_slug,
                "nombre_canonico": nombre_canonico,
                "tldr": tldr,
                "sector": sectors,
                "aliases": [],  # se rellenarán desde aliases.yaml manual
            }

    return {
        "mapping": mapping,
        "perfiles_sin_basado": sorted(perfiles_sin_basado),
        "total_perfiles": sum(1 for _ in perfiles_dir.glob("*.md") if not _.name.endswith(".compact.md")),
        "total_mapeados": len(mapping),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--vault",
        default="C:/Users/victo/vault",
        help="Path al vault root",
    )
    parser.add_argument(
        "--output",
        default=None,
        help="Output JSON path (default: junto a este script)",
    )
    parser.add_argument("--print", action="store_true", help="Print resumen a stdout")
    args = parser.parse_args()

    vault = Path(args.vault).resolve()
    result = build(vault)

    out_path = Path(args.output or Path(__file__).resolve().parent / "name_to_slug.json")
    out_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Escrito {out_path} ({out_path.stat().st_size} bytes)", file=sys.stderr)
    print(
        f"  total perfiles: {result['total_perfiles']} | mapeados: {result['total_mapeados']} | "
        f"sin basado_en: {len(result['perfiles_sin_basado'])}",
        file=sys.stderr,
    )

    if args.print:
        for slug, info in sorted(result["mapping"].items()):
            print(f"  {slug:30} → {info['perfil_slug']:55} ({info['nombre_canonico']})")
        if result["perfiles_sin_basado"]:
            print("\nPerfiles SIN basado_en (no auto-routeables por mención):")
            for p in result["perfiles_sin_basado"]:
                print(f"  - {p}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
