"""Load the posting/search vocabulary from backend/seeds/field_definitions/*.yaml.

Usage:
    python backend/scripts/seed_fields.py           # validate, then sync into Postgres
    python backend/scripts/seed_fields.py --check   # validate only (no database needed; use in CI)

The YAML files are the source of truth. Syncing is idempotent:
  * areas, kinds, fields and prompts are upserted
  * kinds/prompts missing from the YAML are deactivated, fields are retired (never deleted,
    because notes may reference them)
  * field_assignments are replaced wholesale (pure config, nothing references them)
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import psycopg
import yaml
from psycopg.types.json import Jsonb

from _env import FIELD_SEEDS_DIR, database_url

SLUG = re.compile(r"^[a-z][a-z0-9_]*$")
FIELD_KEY = re.compile(r"^[a-z][a-z0-9_]{1,40}$")
TYPES = {"single", "multi", "int", "money", "bool", "text", "text_list"}
ROLES = {"hard", "soft", "rank", "text", "display"}
STEPS = {"details", "verdict", "settings"}
TEMPLATES = {"product", "place", "stay", "experience", "recipe", "other"}
COMMON_FILE = "_common.yaml"


class SeedError(Exception):
    def __init__(self, errors: list[str]):
        self.errors = errors
        super().__init__("seed validation failed:\n  - " + "\n  - ".join(errors))


@dataclass
class Seed:
    areas: list[dict[str, Any]] = field(default_factory=list)
    kinds: list[dict[str, Any]] = field(default_factory=list)
    fields: dict[str, dict[str, Any]] = field(default_factory=dict)
    prompts: list[dict[str, Any]] = field(default_factory=list)
    assignments: list[dict[str, Any]] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Loading and validation
# ---------------------------------------------------------------------------

def _read_yaml(path: Path, errors: list[str]) -> dict[str, Any]:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        errors.append(f"{path.name}: invalid YAML: {exc}")
        return {}
    if not isinstance(data, dict):
        errors.append(f"{path.name}: top level must be a mapping")
        return {}
    return data


def _synonyms(raw: Any, where: str, errors: list[str]) -> list[str]:
    if raw is None:
        return []
    if not isinstance(raw, list) or not all(isinstance(s, str) and s.strip() for s in raw):
        errors.append(f"{where}: synonyms must be a list of non-empty strings")
        return []
    out = []
    for s in raw:
        norm = " ".join(s.split())
        if norm != norm.lower():
            errors.append(f"{where}: synonym {s!r} must be lowercase")
        out.append(norm.lower())
    if len(set(out)) != len(out):
        errors.append(f"{where}: repeated synonyms")
    return out


def _parse_field(raw: dict[str, Any], src: str, area: str | None, errors: list[str]) -> dict[str, Any] | None:
    key = raw.get("key")
    where = f"{src}: field {key!r}"
    if not isinstance(key, str) or not FIELD_KEY.match(key):
        errors.append(f"{where}: key must match {FIELD_KEY.pattern}")
        return None
    dtype, role = raw.get("type"), raw.get("role")
    if dtype not in TYPES:
        errors.append(f"{where}: type must be one of {sorted(TYPES)}")
        return None
    if role not in ROLES:
        errors.append(f"{where}: role must be one of {sorted(ROLES)}")
        return None
    if not raw.get("label"):
        errors.append(f"{where}: label is required")

    personal = bool(raw.get("personal", False))
    facet = raw.get("facet", role in {"hard", "soft", "rank"})
    if role in {"hard", "soft"} and not facet:
        errors.append(f"{where}: {role} fields must be facetable")
    if personal and role not in {"soft", "display"}:
        errors.append(f"{where}: personal fields can only be soft or display (never a hard filter)")

    lo, hi = raw.get("min"), raw.get("max")
    if dtype == "money" and lo is None:
        lo = 0
    for name, val in (("min", lo), ("max", hi)):
        if val is not None and (not isinstance(val, int) or isinstance(val, bool)):
            errors.append(f"{where}: {name} must be an integer")
    if isinstance(lo, int) and isinstance(hi, int) and lo > hi:
        errors.append(f"{where}: min is greater than max")
    if dtype not in {"int", "money"} and (raw.get("min") is not None or raw.get("max") is not None):
        errors.append(f"{where}: min/max only apply to int and money fields")

    options_raw = raw.get("options") or []
    if not isinstance(options_raw, list):
        errors.append(f"{where}: options must be a list")
        options_raw = []
    options: list[dict[str, Any]] = []
    if dtype in {"single", "multi"}:
        if not options_raw:
            errors.append(f"{where}: {dtype} fields need options")
        seen_values: set[str] = set()
        seen_syn: dict[str, str] = {}
        for opt in options_raw:
            val = opt.get("value") if isinstance(opt, dict) else None
            owhere = f"{where} option {val!r}"
            if not isinstance(val, str) or not SLUG.match(val):
                errors.append(f"{owhere}: value must be a lowercase slug")
                continue
            if val in seen_values:
                errors.append(f"{owhere}: duplicate value")
            seen_values.add(val)
            if not opt.get("label"):
                errors.append(f"{owhere}: label is required")
            syns = _synonyms(opt.get("synonyms"), owhere, errors)
            for s in syns:
                if s in seen_syn and seen_syn[s] != val:
                    errors.append(f"{where}: synonym {s!r} used by both {seen_syn[s]!r} and {val!r}")
                seen_syn[s] = val
            entry: dict[str, Any] = {"value": val, "label": str(opt["label"]) if opt.get("label") else val,
                                     "synonyms": syns}
            for scope_key in ("areas", "kinds"):
                if opt.get(scope_key) is not None:
                    entry[scope_key] = list(opt[scope_key])
            options.append(entry)
        clash = sorted(set(seen_syn) & seen_values)
        for s in clash:
            if seen_syn[s] != s:
                errors.append(f"{where}: synonym {s!r} equals another option's value")
    elif dtype in {"int", "money"}:
        for opt in options_raw:
            val = opt.get("value") if isinstance(opt, dict) else None
            if not isinstance(val, int) or isinstance(val, bool):
                errors.append(f"{where}: preset values must be integers")
                continue
            if (isinstance(lo, int) and val < lo) or (isinstance(hi, int) and val > hi):
                errors.append(f"{where}: preset {val} is outside min/max")
            options.append({"value": val, "label": str(opt.get("label") or val)})
    elif options_raw:
        errors.append(f"{where}: {dtype} fields cannot have options")

    return {
        "key": key, "label": str(raw.get("label") or key), "help": raw.get("help"),
        "data_type": dtype, "options": options,
        "unit": raw.get("unit", "INR" if dtype == "money" else None),
        "min_value": lo, "max_value": hi, "search_role": role,
        "facetable": bool(facet), "personal": personal,
        "_src": src, "_area": area,
    }


def load_seed(directory: Path = FIELD_SEEDS_DIR) -> Seed:
    """Parse and validate every seed file; raises SeedError listing all problems."""
    errors: list[str] = []
    seed = Seed()
    common_path = directory / COMMON_FILE
    if not common_path.exists():
        raise SeedError([f"missing {COMMON_FILE} in {directory}"])
    common = _read_yaml(common_path, errors)
    area_files = sorted(p for p in directory.glob("*.yaml") if p.name != COMMON_FILE)

    # Areas
    area_ids: set[str] = set()
    for i, a in enumerate(common.get("areas") or []):
        aid = a.get("id")
        if not isinstance(aid, str) or not SLUG.match(aid):
            errors.append(f"{COMMON_FILE}: area id {aid!r} must be a lowercase slug")
            continue
        if aid in area_ids:
            errors.append(f"{COMMON_FILE}: duplicate area {aid!r}")
        area_ids.add(aid)
        seed.areas.append({
            "id": aid, "label": a.get("label") or aid, "sort_order": (i + 1) * 10,
            "is_launch": bool(a.get("launch", False)), "is_active": bool(a.get("active", True)),
        })
    if not seed.areas:
        errors.append(f"{COMMON_FILE}: no areas defined")

    # Prompts
    seen_prompts: set[str] = set()
    for i, p in enumerate(common.get("prompts") or []):
        key = p.get("key")
        if not isinstance(key, str) or not FIELD_KEY.match(key):
            errors.append(f"{COMMON_FILE}: prompt key {key!r} is invalid")
            continue
        if key in seen_prompts:
            errors.append(f"{COMMON_FILE}: duplicate prompt {key!r}")
        seen_prompts.add(key)
        template = p.get("template")
        if template is not None and template not in TEMPLATES:
            errors.append(f"{COMMON_FILE}: prompt {key!r} has unknown template {template!r}")
        seed.prompts.append({"key": key, "label": p.get("label") or key, "template": template,
                             "sort_order": (i + 1) * 10})

    # Kinds and fields from every file
    kinds_by_area: dict[str, dict[str, dict[str, Any]]] = {}
    raw_assign: list[tuple[str, str | None, dict[str, Any]]] = []
    for f in common.get("fields") or []:
        parsed = _parse_field(f, COMMON_FILE, None, errors)
        if parsed:
            if parsed["key"] in seed.fields:
                errors.append(f"{COMMON_FILE}: field {parsed['key']!r} defined twice")
            seed.fields[parsed["key"]] = parsed
    for a in common.get("assign") or []:
        raw_assign.append((COMMON_FILE, a.get("area"), a))

    for path in area_files:
        data = _read_yaml(path, errors)
        area = data.get("area")
        if area not in area_ids:
            errors.append(f"{path.name}: area {area!r} is not listed in {COMMON_FILE}")
            continue
        if area in kinds_by_area:
            errors.append(f"{path.name}: area {area!r} has more than one seed file")
        kinds = kinds_by_area.setdefault(area, {})
        for i, k in enumerate(data.get("kinds") or []):
            kid = k.get("id")
            where = f"{path.name}: kind {kid!r}"
            if not isinstance(kid, str) or not SLUG.match(kid):
                errors.append(f"{where}: id must be a lowercase slug")
                continue
            if kid in kinds:
                errors.append(f"{where}: duplicate kind")
            if k.get("template") not in TEMPLATES:
                errors.append(f"{where}: template must be one of {sorted(TEMPLATES)}")
            kinds[kid] = {
                "id": f"{area}.{kid}", "area_id": area, "label": k.get("label") or kid,
                "template": k.get("template"), "synonyms": _synonyms(k.get("synonyms"), where, errors),
                "sort_order": (i + 1) * 10,
            }
        for f in data.get("fields") or []:
            parsed = _parse_field(f, path.name, area, errors)
            if parsed:
                if parsed["key"] in seed.fields:
                    errors.append(f"{path.name}: field {parsed['key']!r} already defined in {seed.fields[parsed['key']]['_src']}")
                seed.fields[parsed["key"]] = parsed
        for a in data.get("assign") or []:
            if a.get("area") not in (None, area):
                errors.append(f"{path.name}: assignments may only target area {area!r}")
            raw_assign.append((path.name, area, a))

    for area_kinds in kinds_by_area.values():
        seed.kinds.extend(area_kinds.values())
    for aid in area_ids:
        if next((a for a in seed.areas if a["id"] == aid), {}).get("is_active") and aid not in kinds_by_area:
            errors.append(f"area {aid!r} is active but has no seed file with kinds")

    # Option-level scopes (areas / kinds) must point at real things
    for fdef in seed.fields.values():
        for opt in fdef["options"]:
            for aid in opt.get("areas", []):
                if aid not in area_ids:
                    errors.append(f"{fdef['_src']}: field {fdef['key']!r} option {opt['value']!r} names unknown area {aid!r}")
            if "kinds" in opt:
                resolved = []
                for kid in opt["kinds"]:
                    full = kid if "." in kid else (f"{fdef['_area']}.{kid}" if fdef["_area"] else kid)
                    area_part, _, short = full.partition(".")
                    if short not in kinds_by_area.get(area_part, {}):
                        errors.append(f"{fdef['_src']}: field {fdef['key']!r} option {opt['value']!r} names unknown kind {kid!r}")
                    resolved.append(full)
                opt["kinds"] = resolved

    # Assignments
    seen_scopes: set[tuple[Any, ...]] = set()
    used_fields: set[str] = set()
    for i, (src, area, a) in enumerate(raw_assign):
        fkey, step, template = a.get("field"), a.get("step"), a.get("template")
        where = f"{src}: assign {fkey!r}"
        if fkey not in seed.fields:
            errors.append(f"{where}: unknown field")
            continue
        used_fields.add(fkey)
        if step not in STEPS:
            errors.append(f"{where}: step must be one of {sorted(STEPS)}")
        if template is not None and template not in TEMPLATES:
            errors.append(f"{where}: unknown template {template!r}")
        if area is not None and area not in area_ids:
            errors.append(f"{where}: unknown area {area!r}")
        config = a.get("config") or {}
        if not isinstance(config, dict):
            errors.append(f"{where}: config must be a mapping")
            config = {}
        bands = config.get("price_bands")
        if bands is not None and (not isinstance(bands, list) or bands != sorted(set(bands))
                                  or not all(isinstance(b, int) and b > 0 for b in bands)):
            errors.append(f"{where}: price_bands must be increasing positive integers")
        kinds = a.get("kinds")
        targets: list[str | None] = [None]
        if kinds is not None:
            if area is None:
                errors.append(f"{where}: kinds need an area")
                continue
            targets = []
            for kid in kinds:
                k = kinds_by_area.get(area, {}).get(kid)
                if k is None:
                    errors.append(f"{where}: unknown kind {kid!r} in area {area!r}")
                    continue
                if template is not None and k["template"] != template:
                    errors.append(f"{where}: kind {kid!r} is a {k['template']}, not a {template}")
                targets.append(k["id"])
        for kind_id in targets:
            scope = (fkey, area, template, kind_id)
            if scope in seen_scopes:
                errors.append(f"{where}: assigned twice to the same scope {scope[1:]}")
            seen_scopes.add(scope)
            seed.assignments.append({
                "field_key": fkey, "area_id": area, "template": template, "kind_id": kind_id,
                "step": step, "sort_order": (i + 1) * 10,
                "default_ticked": bool(a.get("default_ticked", False)), "config": config,
            })

    for fkey, fdef in seed.fields.items():
        if fkey not in used_fields:
            errors.append(f"{fdef['_src']}: field {fkey!r} is defined but never assigned")

    if errors:
        raise SeedError(errors)
    return seed


# ---------------------------------------------------------------------------
# Syncing into Postgres
# ---------------------------------------------------------------------------

def apply_seed(conn: psycopg.Connection, seed: Seed) -> dict[str, int]:
    """Sync the seed into the database in one transaction. Returns row counts."""
    with conn.transaction():
        # Refuse to change the data type of a field that notes already use
        for key, fdef in seed.fields.items():
            row = conn.execute(
                "SELECT fd.data_type::text, EXISTS (SELECT 1 FROM note_fields nf WHERE nf.field_key = fd.key)"
                " FROM field_definitions fd WHERE fd.key = %s", (key,)).fetchone()
            if row and row[0] != fdef["data_type"] and row[1]:
                raise SeedError([f"field {key!r} is used by notes; changing its type from {row[0]} "
                                 f"to {fdef['data_type']} needs a data migration, not a seed change"])

        for a in seed.areas:
            conn.execute(
                "INSERT INTO areas (id, label, sort_order, is_launch, is_active) VALUES (%s, %s, %s, %s, %s)"
                " ON CONFLICT (id) DO UPDATE SET label = EXCLUDED.label, sort_order = EXCLUDED.sort_order,"
                " is_launch = EXCLUDED.is_launch, is_active = EXCLUDED.is_active",
                (a["id"], a["label"], a["sort_order"], a["is_launch"], a["is_active"]))

        for k in seed.kinds:
            conn.execute(
                "INSERT INTO kinds (id, area_id, label, template, synonyms, sort_order, is_active)"
                " VALUES (%s, %s, %s, %s::template_kind, %s, %s, true)"
                " ON CONFLICT (id) DO UPDATE SET label = EXCLUDED.label, template = EXCLUDED.template,"
                " synonyms = EXCLUDED.synonyms, sort_order = EXCLUDED.sort_order, is_active = true",
                (k["id"], k["area_id"], k["label"], k["template"], k["synonyms"], k["sort_order"]))
        conn.execute("UPDATE kinds SET is_active = false WHERE NOT (id = ANY (%s))",
                     ([k["id"] for k in seed.kinds],))

        for f in seed.fields.values():
            conn.execute(
                "INSERT INTO field_definitions (key, label, help, data_type, options, unit, min_value, max_value,"
                " search_role, facetable, personal, status)"
                " VALUES (%s, %s, %s, %s::field_data_type, %s, %s, %s, %s, %s::field_search_role, %s, %s, 'active')"
                " ON CONFLICT (key) DO UPDATE SET label = EXCLUDED.label, help = EXCLUDED.help,"
                " data_type = EXCLUDED.data_type, options = EXCLUDED.options, unit = EXCLUDED.unit,"
                " min_value = EXCLUDED.min_value, max_value = EXCLUDED.max_value,"
                " search_role = EXCLUDED.search_role, facetable = EXCLUDED.facetable,"
                " personal = EXCLUDED.personal, status = 'active'",
                (f["key"], f["label"], f["help"], f["data_type"], Jsonb(f["options"]), f["unit"],
                 f["min_value"], f["max_value"], f["search_role"], f["facetable"], f["personal"]))
        conn.execute("UPDATE field_definitions SET status = 'retired'"
                     " WHERE status = 'active' AND NOT (key = ANY (%s))", (list(seed.fields),))

        for p in seed.prompts:
            conn.execute(
                "INSERT INTO prompt_definitions (key, label, template, sort_order, is_active)"
                " VALUES (%s, %s, %s::template_kind, %s, true)"
                " ON CONFLICT (key) DO UPDATE SET label = EXCLUDED.label, template = EXCLUDED.template,"
                " sort_order = EXCLUDED.sort_order, is_active = true",
                (p["key"], p["label"], p["template"], p["sort_order"]))
        conn.execute("UPDATE prompt_definitions SET is_active = false WHERE NOT (key = ANY (%s))",
                     ([p["key"] for p in seed.prompts],))

        conn.execute("DELETE FROM field_assignments")
        with conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO field_assignments (field_key, area_id, template, kind_id, step, sort_order,"
                " default_ticked, config) VALUES (%s, %s, %s::template_kind, %s, %s::posting_step, %s, %s, %s)",
                [(a["field_key"], a["area_id"], a["template"], a["kind_id"], a["step"], a["sort_order"],
                  a["default_ticked"], Jsonb(a["config"])) for a in seed.assignments])

    return {"areas": len(seed.areas), "kinds": len(seed.kinds), "fields": len(seed.fields),
            "prompts": len(seed.prompts), "assignments": len(seed.assignments)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--check", action="store_true", help="validate the YAML only")
    args = parser.parse_args()
    try:
        seed = load_seed()
    except SeedError as exc:
        print(exc, file=sys.stderr)
        return 1
    if args.check:
        print(f"seed OK: {len(seed.areas)} areas, {len(seed.kinds)} kinds, {len(seed.fields)} fields, "
              f"{len(seed.prompts)} prompts, {len(seed.assignments)} assignments")
        return 0
    with psycopg.connect(database_url(), autocommit=True) as conn:
        try:
            counts = apply_seed(conn, seed)
        except SeedError as exc:
            print(exc, file=sys.stderr)
            return 1
    print("seeded: " + ", ".join(f"{v} {k}" for k, v in counts.items()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
