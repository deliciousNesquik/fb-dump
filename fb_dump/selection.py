"""Resolve object names given on the command line (targeted export).

Firebird folds unquoted identifiers to upper case, so matching is
case-insensitive. Without ``--type`` a name that exists in several categories
yields several matches — by design.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from . import log
from .categories import CATEGORIES, CATEGORY_BY_ALIAS, CATEGORY_ORDER, Category


@dataclass(frozen=True)
class Resolved:
    matches: list[tuple[Category, Any]]  # in category order, deduplicated by (key, name)
    missing: list[str]                   # requested names with no match


def resolve(schema: Any, names: list[str], type_alias: str | None = None) -> Resolved:
    cats = [CATEGORY_BY_ALIAS[type_alias]] if type_alias else list(CATEGORIES)
    # Materialise each candidate collection once.
    cat_objects = [(c, list(c.objects(schema))) for c in cats]

    matches: list[tuple[Category, Any]] = []
    missing: list[str] = []
    seen: set[tuple[str, str]] = set()

    for name in names:
        target = name.strip()
        upper = target.upper()
        per_name: list[tuple[Category, Any]] = []
        found = False       # a repeated name still matched, it is just already collected
        for cat, objs in cat_objects:
            for obj in objs:
                obj_name = cat.name_of(obj)
                if obj_name == target or obj_name.upper() == upper:
                    found = True
                    key = (cat.key, obj_name)
                    if key not in seen:
                        seen.add(key)
                        per_name.append((cat, obj))
        if not found:
            missing.append(name)
            continue
        if not per_name:
            continue        # duplicate of a name already resolved
        if len({c.key for c, _ in per_name}) > 1:
            hit = ", ".join(sorted(c.key for c, _ in per_name))
            log.info(f"{target!r} matched several categories: {hit}")
        matches.extend(per_name)

    matches.sort(key=lambda cm: (CATEGORY_ORDER[cm[0].key], cm[0].name_of(cm[1])))
    return Resolved(matches=matches, missing=missing)
