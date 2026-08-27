"""Output layout: which directory each object category goes to and how files are named.

The layout is data, not code. A layout is either a preset name (``numbered``,
``plain``, ``flat``) or a TOML file the user writes; every tree fb-dump produces
carries its *effective* layout in ``.fb-dump.toml``, so the tree is
self-describing and a later targeted export into it lands in the right places.

TOML format (every key optional)::

    base = "plain"              # preset to start from (default: numbered)
    file = "{name}.sql"         # file-name template: {name}, {type}
    database = "DATABASE.sql"   # database-level file (dialect, database grants)

    [dirs]                      # category -> directory ("" = tree root, "/" nests)
    table = "Таблицы"
    index = "Таблицы/Индексы"

    [files]                     # per-category file-name template overrides
    index = "{name}.index.sql"
"""

from __future__ import annotations

import json
import string
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

# Canonical category order: order of a full dump and of the numbered preset.
CATEGORY_KEYS: tuple[str, ...] = (
    "role", "collation", "external_function", "generator", "exception", "domain",
    "table", "index", "function", "view", "procedure", "package", "trigger",
)

MANIFEST = ".fb-dump.toml"
DEFAULT_PRESET = "numbered"
DEFAULT_FILE = "{name}.sql"
DEFAULT_DATABASE = "DATABASE.sql"

_PLAIN_DIRS: dict[str, str] = {
    "role": "ROLES",
    "collation": "COLLATIONS",
    "external_function": "EXTERNAL_FUNCTIONS",
    "generator": "GENERATORS",
    "exception": "EXCEPTIONS",
    "domain": "DOMAINS",
    "table": "TABLES",
    "index": "INDICES",
    "function": "FUNCTIONS",
    "view": "VIEWS",
    "procedure": "PROCEDURES",
    "package": "PACKAGES",
    "trigger": "TRIGGERS",
}

PRESETS: dict[str, dict[str, Any]] = {
    "numbered": {"dirs": {k: f"{i:02d}_{d}" for i, (k, d) in enumerate(_PLAIN_DIRS.items(), 1)}},
    "plain": {"dirs": dict(_PLAIN_DIRS)},
    "flat": {"dirs": {k: "" for k in CATEGORY_KEYS}, "file": "{name}.{type}.sql"},
}

_ALLOWED_KEYS = {"version", "base", "file", "database", "dirs", "files"}
_PLACEHOLDERS = {"name", "type"}
_UNSAFE_CHARS = set('/\\:*?"<>|')
_RESERVED_STEMS = {"CON", "PRN", "AUX", "NUL"} | {f"COM{i}" for i in range(1, 10)} | {f"LPT{i}" for i in range(1, 10)}


class LayoutError(Exception):
    """Invalid layout specification."""


def safe_component(name: str) -> str:
    """Make an object name safe as a single file-name component.

    Firebird identifiers are usually ``[A-Z0-9_$]``, but quoted identifiers may
    contain anything. Path separators and characters Windows rejects become
    ``_``; trailing dots/spaces are dropped; Windows reserved device names get
    a ``_`` suffix so ``CON.sql`` never has to be created.
    """
    safe = "".join("_" if (ch in _UNSAFE_CHARS or ord(ch) < 32) else ch for ch in name.strip())
    safe = safe.rstrip(". ") or "_"
    if safe.upper() in _RESERVED_STEMS:
        safe += "_"
    return safe


@dataclass(frozen=True)
class Layout:
    dirs: dict[str, str]
    file: str = DEFAULT_FILE
    files: dict[str, str] = field(default_factory=dict)
    database: str = DEFAULT_DATABASE

    def path_for(self, key: str, name: str) -> str:
        """Tree-relative path (POSIX separators) of the file for object ``name`` of category ``key``."""
        template = self.files.get(key, self.file)
        filename = template.format(name=safe_component(name), type=key)
        directory = self.dirs[key]
        return f"{directory}/{filename}" if directory else filename

    def to_toml(self) -> str:
        """Serialise as the manifest written into every tree (deterministic, no timestamps)."""
        lines = [
            "# Written by fb-dump: the layout of this tree. Pass this file to --layout",
            "# to produce the same structure elsewhere.",
            "version = 1",
            f"file = {_toml_str(self.file)}",
            f"database = {_toml_str(self.database)}",
            "",
            "[dirs]",
        ]
        lines += [f"{k} = {_toml_str(self.dirs[k])}" for k in CATEGORY_KEYS]
        if self.files:
            lines += ["", "[files]"]
            lines += [f"{k} = {_toml_str(v)}" for k, v in self.files.items() if k in CATEGORY_KEYS]
        return "\n".join(lines) + "\n"


def _toml_str(value: str) -> str:
    # A JSON string literal is a valid TOML basic string.
    return json.dumps(value, ensure_ascii=False)


def preset(name: str) -> Layout:
    if name not in PRESETS:
        raise LayoutError(f"unknown layout preset {name!r}; choose from {', '.join(PRESETS)}")
    return from_dict(dict(PRESETS[name], base=None), source=f"preset {name}")


def load(spec: str) -> Layout:
    """``spec`` is a preset name or a path to a TOML layout file."""
    if spec in PRESETS:
        return preset(spec)
    path = Path(spec)
    if not path.is_file():
        raise LayoutError(f"layout {spec!r} is neither a preset ({', '.join(PRESETS)}) nor an existing file")
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
        raise LayoutError(f"{path}: {exc}") from exc
    return from_dict(data, source=str(path))


def load_manifest(tree: Path) -> Layout | None:
    """Layout recorded in an existing tree, or None if the tree has no manifest."""
    manifest = tree / MANIFEST
    if not manifest.is_file():
        return None
    try:
        data = tomllib.loads(manifest.read_text(encoding="utf-8"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
        raise LayoutError(f"{manifest}: {exc}") from exc
    return from_dict(data, source=str(manifest))


def from_dict(data: Mapping[str, Any], source: str) -> Layout:
    unknown = set(data) - _ALLOWED_KEYS
    if unknown:
        raise LayoutError(f"{source}: unknown keys: {', '.join(sorted(unknown))}")

    base = data.get("base", DEFAULT_PRESET)
    if base is None:
        dirs: dict[str, str] = {k: "" for k in CATEGORY_KEYS}
        file, files, database = DEFAULT_FILE, {}, DEFAULT_DATABASE
    else:
        if base not in PRESETS:
            raise LayoutError(f"{source}: unknown base preset {base!r}; choose from {', '.join(PRESETS)}")
        b = PRESETS[base]
        dirs = dict(b["dirs"])
        file, files, database = b.get("file", DEFAULT_FILE), dict(b.get("files", {})), b.get("database", DEFAULT_DATABASE)

    if "dirs" in data:
        dirs.update(_str_table(data["dirs"], f"{source}: [dirs]"))
        for k, v in dirs.items():
            dirs[k] = _normalize_dir(v, f"{source}: dirs.{k}")
    if "files" in data:
        files.update(_str_table(data["files"], f"{source}: [files]"))
    if "file" in data:
        file = data["file"]
    if "database" in data:
        database = data["database"]

    _check_template(file, f"{source}: file")
    for k, v in files.items():
        _check_template(v, f"{source}: files.{k}")
    if not isinstance(database, str) or not database or "/" in database or "\\" in database or database.strip(". ") != database:
        raise LayoutError(f"{source}: database must be a plain file name")
    return Layout(dirs=dirs, file=file, files=files, database=database)


def _str_table(value: Any, where: str) -> dict[str, str]:
    if not isinstance(value, Mapping):
        raise LayoutError(f"{where} must be a table")
    out: dict[str, str] = {}
    for k, v in value.items():
        if k not in CATEGORY_KEYS:
            raise LayoutError(f"{where}: unknown category {k!r}; known: {', '.join(CATEGORY_KEYS)}")
        if not isinstance(v, str):
            raise LayoutError(f"{where}: {k} must be a string")
        out[k] = v
    return out


def _normalize_dir(value: str, where: str) -> str:
    parts = [p for p in value.replace("\\", "/").split("/") if p not in ("", ".")]
    if any(p == ".." for p in parts) or value.startswith(("/", "\\")) or (len(value) > 1 and value[1] == ":"):
        raise LayoutError(f"{where}: directory must be relative to the tree and must not contain '..'")
    return "/".join(parts)


def _check_template(template: Any, where: str) -> None:
    if not isinstance(template, str) or not template:
        raise LayoutError(f"{where} must be a non-empty string")
    try:
        fields = {f for _, f, _, _ in string.Formatter().parse(template) if f is not None}
    except ValueError as exc:
        raise LayoutError(f"{where}: {exc}") from exc
    if "name" not in fields:
        raise LayoutError(f"{where}: template must contain {{name}}")
    if fields - _PLACEHOLDERS:
        raise LayoutError(f"{where}: unknown placeholders {', '.join(sorted(fields - _PLACEHOLDERS))}; allowed: {{name}}, {{type}}")
    if "/" in template or "\\" in template:
        raise LayoutError(f"{where}: template must not contain path separators (use [dirs])")
