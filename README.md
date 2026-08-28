# fb-dump

**English** · [Русский](README.ru.md)

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Releases](https://img.shields.io/github/v/release/deliciousNesquik/fb-dump?sort=semver)](https://github.com/deliciousNesquik/fb-dump/releases)
[![Firebird 3/4/5](https://img.shields.io/badge/Firebird-3%20%7C%204%20%7C%205-orange)](https://firebirdsql.org/)

`fb-dump` reads the schema of a live **Firebird** database and writes it as a tree of `.sql` files — **one object, one file**, where the file contains the *complete* definition of the object: DDL, constraints, comments, and grants.

The tool does one thing. It reads the system catalog via [`firebird-lib`](https://pypi.org/project/firebird-lib/) (no `isql`, no text parsing), writes deterministic output, and either creates a complete tree or creates nothing at all.

## Contents

- [Quick start](#quick-start)
- [Installation](#installation)
- [Connection settings](#connection-settings)
- [Modes](#modes)
- [Options](#options)
- [What a file contains](#what-a-file-contains)
- [Directory layout and file names](#directory-layout-and-file-names)
- [Guarantees](#guarantees)
- [Exit codes](#exit-codes)
- [Isolation](#isolation)
- [Character sets](#character-sets)
- [Recreating a schema from the tree](#recreating-a-schema-from-the-tree)
- [Limitations](#limitations)
- [Development](#development)
- [License](#license)

## Quick start

```bash
export ISC_USER=SYSDBA ISC_PASSWORD=masterkey          # standard Firebird client variables

fb-dump -d localhost:employee -o schema                  # full dump into ./schema
fb-dump -d localhost:employee --list                     # what is inside?
fb-dump -d localhost:employee CUSTOMER                   # one object, printed to stdout
fb-dump -d localhost:employee CUSTOMER -o schema         # ...or refresh the tree
fb-dump -d localhost:employee > employee.sql             # entire schema as a single script
```

A dump looks like this:

```text
schema/
├── .fb-dump.toml               ← the tree structure (and fb-dump safety marker)
├── DATABASE.sql                ← dialect, default charset, database-level access rights
├── 01_ROLES/
├── 04_GENERATORS/
├── 06_DOMAINS/
├── 07_TABLES/
│   └── CUSTOMER.sql            ← CREATE TABLE + constraints + comments + grants
├── 08_INDICES/
├── 10_VIEWS/
├── 11_PROCEDURES/
└── 13_TRIGGERS/
```

## Installation

### Prebuilt binaries (no Python installed)

Download the executable for your platform from the [Releases](https://github.com/deliciousNesquik/fb-dump/releases) page.

```bash
chmod +x fb-dump-linux-x64 && ./fb-dump-linux-x64 --help
```

The binary still needs the **Firebird client library** (`fbclient.dll` / `libfbclient.so` / `libfbclient.dylib`) at runtime — `firebird-driver` loads it. It is present wherever a Firebird client or server is installed. On macOS, the binary is unsigned; allow it once in *System Settings → Privacy & Security*.

### From source

```bash
git clone https://github.com/deliciousNesquik/fb-dump.git && cd fb-dump
uv run fb-dump --help            # uv builds the environment based on pyproject.toml
```

Python 3.11+, dependencies `firebird-driver` and `firebird-lib` (2.x). Without `uv`: install the package into a virtual environment and run `fb-dump` or `python -m fb_dump`.

## Connection settings

Flags take precedence over environment variables. The password is **never** accepted from the command line (it would leak via `ps`); set `ISC_PASSWORD` in the environment.

| Flag | Environment variable | Default | Meaning |
| --- | --- | --- | --- |
| `-d`, `--database DSN` | `FB_DATABASE` | *required* | Alias, file path, or `HOST:ALIAS_OR_PATH`. |
| `-u`, `--user USER` | `ISC_USER` | driver default | Database user. |
| — | `ISC_PASSWORD` | — | Password. |
| `-r`, `--role ROLE` | `FB_ROLE` | — | SQL role to connect with. |
| `--isolation LEVEL` | `FB_ISOLATION` | `read-committed` | Isolation of the catalog-reading transaction: `read-committed`, `read-consistency` or `snapshot` (see [Isolation](#isolation)). |
| `--charset CS` | `FB_CHARSET` | `UTF8` | Connection character set (see [Character sets](#character-sets)). |
| `--fallback-charset CS` | — | — | Second charset for mixed-encoding metadata. |

`ISC_USER` and `ISC_PASSWORD` are standard variables that the Firebird client library reads itself; `fb-dump` only passes them through. `fb-dump` does not read `.env` files — use the shell (`set -a; . ./.env; set +a`), `direnv`, or `uv run --env-file .env fb-dump …`. The variables are shown in `.env.example`.

## Modes

**Full dump** — no object names. Reads the entire schema and writes a complete tree (`--out`) or prints every file to stdout in directory order, with a `-- ===== path =====` header before each. The tree is replaced entirely and only if every object was read (see [Guarantees](#guarantees)).

```bash
fb-dump -d DSN -o schema
fb-dump -d DSN -o schema --layout plain          # directories without numbers
fb-dump -d DSN > schema.sql
```

**Targeted export** — one or more object names. Names are matched case-insensitively; a name found in multiple categories yields multiple files unless `--type` narrows the selection. Without `--out`, SQL is printed to stdout; with `--out`, only the affected files are overwritten (nothing is deleted — cleaning up obsolete files is the full dump's job). The tool's own tree layout is used.

```bash
fb-dump -d DSN ACCOUNT                        # every ACCOUNT (table, procedure, …)
fb-dump -d DSN ACCOUNT --type table           # just the table
fb-dump -d DSN ACCOUNT CALC_TOTAL -o schema   # refresh two files in the tree
```

**List** — `--list` prints `type<TAB>name` for each object, sorted by category, then by name:

```bash
fb-dump -d DSN --list                         # everything
fb-dump -d DSN --list --type procedure        # one category: procedures
fb-dump -d DSN --list | cut -f2 | sort        # pipe-friendly
```

## Options

| Option | Applies to | Description |
| --- | --- | --- |
| `NAME…` | targeted | Object names. Their presence selects targeted mode. |
| `-o`, `--out DIR` | full, targeted | Write files here instead of stdout. |
| `--layout PRESET\|FILE` | full, targeted | `numbered` (default), `plain`, `flat`, or a TOML file. |
| `--print-layout` | — | Print the effective layout as TOML and exit (no database connection). |
| `--allow-partial` | full | Write the tree even if some objects could not be dumped. |
| `--force` | `--out` | Full dump: replace a directory that has no `.fb-dump.toml` or that contains foreign entries; targeted export: write into such a directory. |
| `--type TYPE` | targeted, list | Restrict to one category. |
| `--list` | list | List objects and exit. |
| `-q`, `--quiet` / `-v`, `--verbose` | all | Errors only / debug output (stderr). |
| `--version`, `-h` | — | |

**`--type` values:** `table`, `index`, `view`, `procedure` (`proc`), `function`, `external-function` (`external_function`, `udf`), `trigger`, `exception`, `domain`, `generator` (`sequence`), `role`, `package`, `collation` — the same words `--list` prints.

Diagnostics go to **stderr**; stdout carries only data.

## What a file contains

Each file is the whole object, in this order: definition, comments, grants.

| Category | Definition | Also in the file |
| --- | --- | --- |
| role | `CREATE ROLE` | `COMMENT ON ROLE`; role memberships: `GRANT role TO user [WITH ADMIN OPTION]`, `GRANT DEFAULT role TO …` |
| collation | `CREATE COLLATION` | comment |
| external function (UDF) | `DECLARE EXTERNAL FUNCTION` | comment, `GRANT EXECUTE` |
| generator | `CREATE SEQUENCE` with the declared `START WITH` / `INCREMENT BY` (Firebird 4+) | comment, `GRANT USAGE`. **No current value** — that is data, not schema. |
| exception | `CREATE OR ALTER EXCEPTION` | comment, `GRANT USAGE` |
| domain | `CREATE DOMAIN` | comment |
| table | `CREATE TABLE` (with `ON COMMIT …` for temporary tables), `ALTER TABLE … SET GENERATED ALWAYS` / `SET INCREMENT BY` for Firebird 4 identity columns, `ALTER TABLE … ALTER SQL SECURITY`, then every constraint as a named `ALTER TABLE … ADD CONSTRAINT` (PK, UNIQUE, CHECK, FK) | table and column comments, `GRANT SELECT, INSERT, UPDATE (cols) …` |
| index | `CREATE INDEX` (+ `WHERE …` for Firebird 5 partial indices, `ALTER INDEX … INACTIVE` if the index is inactive) | comment |
| function | `CREATE OR ALTER FUNCTION` (`DETERMINISTIC` kept; UDR routines as `EXTERNAL NAME … ENGINE …`) | comment, `GRANT EXECUTE` |
| view | `CREATE OR ALTER VIEW` | view and column comments, grants |
| procedure | `CREATE OR ALTER PROCEDURE` (UDR routines as `EXTERNAL NAME … ENGINE …`) | procedure and parameter comments, `GRANT EXECUTE` |
| package | `CREATE OR ALTER PACKAGE` + `RECREATE PACKAGE BODY` | comment, `GRANT EXECUTE` |
| trigger | `CREATE OR ALTER TRIGGER` (ACTIVE/INACTIVE state; DML, database and DDL triggers; UDR triggers) | comment |

`DATABASE.sql` holds `SET SQL DIALECT n`, the default character set (as a comment), the database comment, changed default collations (`ALTER CHARACTER SET … SET DEFAULT COLLATION`), database-level privileges (`GRANT CREATE TABLE TO …`, `GRANT ALTER ANY PROCEDURE TO …`, `GRANT ALTER DATABASE TO …`) and memberships of system roles (`GRANT RDB$ADMIN TO USER …`).

Grantees are always qualified — `TO USER JOE`, `TO ROLE READERS`, `TO PROCEDURE P` — so a user and a role with the same name cannot be confused on replay, and a privilege granted by someone other than the object's owner carries `GRANTED BY`.

Not dumped: system objects (`RDB$…`, constraint-enforcing indices and triggers, identity generators), packaged procedures/functions (they are inside the package body), the owner's implicit privileges on its own objects, generator values, users, shadows. Privileges on system objects are counted and reported, not written. PSQL objects are wrapped in `SET TERM ^ ;` … `SET TERM ; ^`; everything else ends with `;`. The output order is fixed (categories, then names), so repeated dumps of the same schema are byte-identical.

## Directory layout and file names

The directory structure is **data, not code**. Three presets are included:

| Preset | Table `ACCOUNT` ends up at | Note |
| --- | --- | --- |
| `numbered` (default) | `07_TABLES/ACCOUNT.sql` | Numbers give a coarse dependency order when files are concatenated. |
| `plain` | `TABLES/ACCOUNT.sql` | Same directories without numbers. |
| `flat` | `ACCOUNT.table.sql` | Everything in one directory; the type is in the file name. |

Anything else is a TOML file: start from a preset and change what you need:

```bash
fb-dump --layout plain --print-layout > my-layout.toml
```

```toml
base = "plain"                  # preset to start from
file = "{name}.sql"             # file name template; placeholders: {name}, {type}
database = "DATABASE.sql"       # database-level file

[dirs]                          # category -> directory; "" is the directory root, "/" is for subdirectories
table  = "Tables"
index  = "Tables/Indices"
view   = "Views"
role   = ""

[files]                         # file name overrides for specific categories
index  = "{name}.index.sql"
```

```bash
fb-dump -d DSN -o schema --layout my-layout.toml
```

Categories you do not mention retain values from `base`. Directory names can be anything your file system accepts, including non-Latin names; they must stay inside the tree (no absolute paths, drive letters or `..`) and must not contain characters Windows rejects (`: * ? " < > |`). Templates take the bare `{name}` and `{type}` placeholders only. Object names are sanitized the same way (plus reserved names like `CON`).

Every tree stores its effective layout in **`.fb-dump.toml`**. This file makes the tree self-describing for any consumer, allows targeted exports to find the right files without knowing the layout in advance, and marks the directory as belonging to fb-dump — a non-empty directory without it will not be touched unless you pass `--force`.

## Guarantees

- **Full dump — all or nothing.** The tree is assembled in a staging directory next to the target and swapped in via rename. If even one object fails to read, *nothing* is written, and the exit code is 3: a half-written tree would appear in version control as objects being *deleted*, which is a false record. `--allow-partial` disables this behavior.
- **Your directory is safe.** `--out` must be empty, non-existent, or a tree written by fb-dump — and even then a full dump refuses to delete entries the layout does not account for (a `.git` directory, a README) unless you pass `--force`. Keep the tree in its own directory: a repository subdirectory, not its root.
- **The swap adapts.** Normally the new tree is built next to the target and renamed into place. When the target cannot be renamed — it is a mount point (a Docker volume), your current directory, or Windows has a file in it open — the tree is rebuilt in place: new files first, old entries removed last, never a half-written file.
- **Determinism.** One schema → the same bytes. No timestamps in the output.
- **Read-only.** fb-dump only ever reads the database catalog; it never writes to the database.
- **Object-level resilience.** A single object that fails to render (no permissions, strange metadata) is logged and counted; it never aborts the run. A collection that cannot be read entirely is an infrastructure error (code 1).

Transactions are **read-only and NO WAIT**: fb-dump never waits on a lock, and never writes to the database. What the run guarantees about consistency depends on `--isolation` — see below.

## Exit codes

| Code | Meaning |
| :---: | --- |
| `0` | Success. |
| `1` | Infrastructure: cannot connect, cannot read a collection, or cannot write the output. |
| `2` | Usage: bad arguments, bad layout file, no database given. |
| `3` | Incomplete result: some objects were skipped or (in targeted mode) some names were not found. In a full dump, nothing is written unless `--allow-partial` is specified. |

## Isolation

The catalog is read in one read-only transaction that never waits on a lock. What that transaction guarantees is your choice:

| `--isolation` | What you get | What it costs |
| --- | --- | --- |
| `read-committed` (default) | Record-version read committed. | The dump is **not** a point-in-time snapshot: DDL committed while it runs may land in one collection and not in another. |
| `read-consistency` (Firebird 4+) | Read committed where every statement sees a stable view. firebird-lib loads a collection with one query, so each collection is internally consistent. | Different collections still come from different moments. Rejected with a clear message on Firebird 3. |
| `snapshot` | One consistent view of the catalog for the whole run, so the tree is a true snapshot of the schema as of its start. | Record versions cannot be garbage-collected while the dump runs. On a large dump against a busy database, that defers cleanup for the duration. |

Firebird's `SNAPSHOT` is *concurrency* isolation — pure MVCC that blocks neither readers nor writers. The level that does take table locks is `SNAPSHOT TABLE STABILITY`, and fb-dump never uses it.

Two levels the driver offers are deliberately absent. `SNAPSHOT TABLE STABILITY` (`SERIALIZABLE` in the driver) takes table locks and would block every writer touching the same system tables — the opposite of what a dumper should do. Read committed *without* record version fails on the first record another transaction is modifying, which under `NO WAIT` is a spurious error with nothing gained.

A dump of a 13,000-object schema takes about two minutes here, so `snapshot` is usually the better choice; `read-committed` remains the default because it is the safer neighbour on a busy OLTP server. `--fallback-charset` reads its collection through a second connection, hence in a second transaction — with `snapshot` that part is consistent in itself but not with the primary connection's view.

## Character sets

The connection is opened with `--charset` (default `UTF8`). Legacy databases whose metadata is stored in a single-byte charset need it set explicitly, e.g., `--charset WIN1251`; otherwise, reading fails with a decode error and exit code 1.

In some legacy databases, metadata is stored in **mixed** encodings: some rows decode only as WIN1251, while others contain characters outside WIN1251 and decode only as UTF8. A single connection cannot read both, and firebird-lib loads the entire collection with one query, so one bad row fails the whole collection. `--fallback-charset WIN1251` lazily opens a second connection and re-reads such a collection through it; a warning names every collection that took this path. Rows from the fallback connection are read in a different transaction, so the consistency caveat above applies doubly.

## Recreating a schema from the tree

The tree is a *description*; turning it back into a database is a separate task. Two things require ordering that files alone cannot express:

1. **Cross-object dependencies within a single category** — a view built on another view, a procedure calling another procedure, a foreign key pointing at a table that sorts later alphabetically. Numbered directories only order *categories*.
2. **Grants to PSQL objects** (`GRANT … TO PROCEDURE P`) require the grantee to exist.

Concatenating files in directory order (`fb-dump -d DSN > all.sql` does exactly this) gives a readable single-file view of the schema, but it is not a reliable way to build a database. A foreign key only has to reference a table that sorts later — in one real 1,014-table schema, 627 of them do — and the script stops there. Recreating a database is the job of an *applier* that works in phases: domains and generators, then tables without constraints, then constraints, then indices, then views and PSQL in repeated passes until a pass adds nothing new, and finally grants and comments. Definitions use `CREATE OR ALTER` / `RECREATE` where Firebird supports it, so reapplying a file is harmless — which is what makes the repeated passes safe.

## Limitations

- Not byte-identical to `isql -x`: firebird-lib formats DDL differently (semantically equivalent).
- A dump is a point-in-time snapshot only with `--isolation snapshot` (see [Isolation](#isolation)).
- Comments on function *parameters* are not dumped (firebird-lib does not provide `COMMENT ON` for them); procedure parameters, columns, and comments on all objects are dumped.
- `SQL SECURITY DEFINER|INVOKER` (Firebird 4) is dumped for tables only; firebird-lib does not read it for procedures, functions, triggers and packages. Role system privileges (`ALTER ROLE … SET SYSTEM PRIVILEGES`) are not dumped either.
- Shadows, BLOB filters, users, and mappings are not the schema objects that fb-dump knows about.
- Firebird 2.5 is out of scope (different driver).
- Full dump reads some object metadata lazily; over a slow connection, a large database takes a long time to dump. Run it closer to the server.

## Development

```bash
uv sync --dev
uv run pyright fb_dump         # 0 errors expected
uv run pytest -q               # offline suite with coverage gate (≥ 80%; currently ~97%)
```

Tests run on fakes of firebird-lib objects — neither a database nor `fbclient` is needed. CI runs both on every push; a `vX.Y.Z` tag builds standalone binaries.

## License

[MIT](LICENSE)
