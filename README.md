# sqlanvil-sqlx-lint

A convention linter for [SQLAnvil](https://sqlanvil.com) `.sqlx` files. SQL
linters (sqlfluff) check the SQL body; `sqlanvil compile` checks syntax and
`sqlanvil validate` checks against the warehouse. None of them see the
**config-block and project conventions** that keep a sqlanvil repo healthy, and
several Dataform habits that sqlanvil silently ignores or fails on at run time.
This tool does, in milliseconds, with no warehouse connection.

Zero dependencies (Python ≥ 3.11 standard library only). Designed for
[pre-commit](https://pre-commit.com). Warehouse-aware: PostgreSQL, Supabase,
MySQL/MariaDB, and BigQuery-target projects.

```bash
pip install sqlanvil-sqlx-lint
```

Forked from [acuantia/dataform-sqlx-lint](https://github.com/acuantia/dataform-sqlx-lint)
(MIT), which was extracted from a production Dataform repository.

## Rules

### Conventions

| Code | Default | Checks |
|------|---------|--------|
| E001 | on | `config {}` block present and balanced |
| E002 | on | non-empty `columns: {}` on tables/views/incrementals/declarations (declarations may carry `columnTypes: {}` from `sqlanvil introspect` instead) |
| E003 | on | `schema:` must not hardcode an environment suffix (`_prod`/`_dev`/`_test` by default) — `--schema-suffix` and `environments.<name>.schemaSuffix` append it, so a literal doubles up (`analytics_test_test`) |
| E004 | on | `name:` matching the filename is redundant (declarations exempt) |
| E005 | opt-in | operations/assertions must not set `schema:` (`hasOutput: true` operations exempt — schema+name define `${self()}`) |
| E006 | on | hardcoded table paths instead of `${ref()}` — these silently break the dependency graph. Warehouse-aware: `public.orders`, `"schema"."table"`, `` `db`.`table` ``, `` `project.dataset.table` `` |
| E007 | on* | configurable per-directory naming/type policies (*no-op until policies are configured) |
| W008 | opt-in | `post_operations {}` placed before the main SELECT (style preference) |
| E010 | on | every determinable output column appears in `columns: {}` — parses the main SELECT conservatively (unparseable expressions are skipped, never false-flagged) and follows `select *` through a single plain `${ref()}` into the upstream file |

Why E002/E010 matter: sqlanvil writes `description:` and `columns: {}` into the
warehouse catalog (`COMMENT ON TABLE` / `COMMENT ON COLUMN` on Postgres, table
and column descriptions on BigQuery). That is the metadata data catalogs, BI
tools, and AI analytics agents read. Partial blocks leave silent gaps.

### sqlanvil deltas

The places where a Dataform/BigQuery habit produces a sqlanvil project that
compiles but does the wrong thing. The first three are failures no compiler
catches.

| Code | Default | Checks | `sqlanvil compile` catches it? |
|------|---------|--------|-------------------------------|
| S101 | on | `bigquery: {}`, `partitionBy`, `clusterBy`, `bigqueryPolicyTags` on a non-BigQuery warehouse — ignored silently, never applied | no |
| S102 | on | `;` separating statements in `operations` / `pre_operations` / `post_operations` — sqlanvil splits on a `---` line, so the block runs as one statement and fails at run time | no |
| S105 | on | `ADD PRIMARY KEY` / `ADD CONSTRAINT` in an incremental's operations block not wrapped in `${when(!incremental(), …)}` — runs on every append and errors the second time | no |
| S103 | on | `postgres.indexes[].method` given as a string — it is a numeric enum (`BTREE=0, HASH=1, GIN=2, GIST=3, BRIN=4`) | yes |
| S104 | on | `incrementalStrategy` on a non-BigQuery warehouse | yes (≥1.29) |
| S106 | on | `assertions:` sets both `uniqueKey` and `uniqueKeys` | yes |
| S108 | on | `.jitCode()` / `jitData()` — no runtime in sqlanvil | yes (≥1.30) |

## Usage

```bash
sqlanvil-sqlx-lint definitions/outputs/my_table.sqlx [...]
# exit 0 = clean or warnings only; 1 = errors; 2 = bad config
```

Run from the project root. The target warehouse is read from
`workflow_settings.yaml` there (`warehouse: postgres|supabase|mysql|bigquery`),
and `--definitions-root` (default `./definitions`) is indexed so `${ref()}`
targets resolve for E010's star-resolution. Override with `--warehouse` or the
`warehouse` config key; the default with nothing configured is `postgres`.

### pre-commit

```yaml
repos:
  - repo: https://github.com/SQLAnvil/sqlanvil-sqlx-lint
    rev: v0.2.0
    hooks:
      - id: sqlanvil-sqlx-lint
```

### Configuration

`.sqlx-lint.toml` in the project root, or a `[tool.sqlx-lint]` table in
`pyproject.toml` (the standalone file wins). All keys optional:

```toml
warehouse = "postgres"                     # else workflow_settings.yaml, else postgres
schema_suffixes = ["_prod", "_dev", "_test"]  # E003 suffix list ([] disables)
documented_types = ["table", "view", "incremental", "declaration"]  # E002
coverage_paths = ["definitions/outputs/"]  # E010 scope; empty = everywhere
allowed_schemas = ["extensions"]           # E006 may reference these directly
enable = ["E005", "W008"]                  # switch on opt-in rules
disable = ["E004"]                         # switch off default rules

[[dir_policies]]                           # E007 (repeatable)
path_contains = "definitions/outputs/"
require_prefix = "rpt_"
require_types = ["table", "incremental"]
severity = "error"                         # or "warning"
```

See [`examples/sqlx-lint.toml`](examples/sqlx-lint.toml) for a complete
sources → intermediate → outputs layout. System catalogs (`pg_catalog`,
`information_schema`, `mysql`, `sys`, …) are always allowed.

### Adopting on an existing project

A migrated repository will be loud on the first run: E002 on every undocumented
model and E010 wherever documentation is partial. Adopt in layers rather than
suppressing:

1. Start with `disable = ["E002", "E010"]` so the S-series and E006 findings,
   which are actual defects, land first.
2. Re-enable E002 and scope E010 with `coverage_paths` to the BI-facing layer.
3. Widen `coverage_paths` as documentation catches up.

### Suppressing findings

```sql
from public.legacy_events -- sqlx-lint: disable=E006 (declaration repoints at cutover)
```

or file-wide, anywhere in the file:

```sql
-- sqlx-lint: disable-file=E006
```

Suppress with a reason, sparingly — the convention is usually the fix.

## Design notes

- **Conservative by construction**: the SQL projection parser only claims
  column names it can determine (aliases, simple identifiers, resolvable
  `select *`); anything ambiguous is skipped, so E010 never false-flags.
- **E006 looks only at table positions** (`FROM`, `JOIN`, `INTO`, `UPDATE`),
  ignores `FROM` used as function syntax (`extract(dow from d.date)`), function
  calls (`public.my_func(1)`), and anything inside `${…}`.
- **S102 understands sqlanvil's text**: `;` inside string literals, `$$`
  dollar-quoted PL/pgSQL bodies, and `${…}` JavaScript regions never count; a
  single trailing `;` is fine.
- **Declarations are exempt** from E003/E004 deliberately: raw source schemas
  legitimately carry environment-suffixed names, and `name:` is required.
- Rule codes are stable; gaps in the numbering are historical.

## Agent Skill

The `sqlanvil-sqlx-lint` Agent Skill teaches AI coding agents (Claude Code,
Codex CLI, Cursor, or any tool supporting the open
[Agent Skills](https://agentskills.io/) format) to run this linter on every
`.sqlx` file they create or modify. It lives with the other SQLAnvil skills
in [SQLAnvil/agent-skills](https://github.com/SQLAnvil/agent-skills).

The skill runs the `sqlanvil-sqlx-lint` command, so install the linter too:
`pip install sqlanvil-sqlx-lint`, into the virtualenv your sqlanvil project
uses.

### Claude Code

Run one of these from your sqlanvil project. They use the
[skills CLI](https://skills.sh), which needs Node.js.

**For this project, shared with your team:**

```bash
npx skills add SQLAnvil/agent-skills --skill sqlanvil-sqlx-lint -a claude-code
```

This writes `.claude/skills/sqlanvil-sqlx-lint/SKILL.md`, plus a
`skills-lock.json` that records where the skill came from. Commit both.
Everyone who clones the repo gets the skill, and so do Claude Code sessions
on the web, which can't see the skills in your home directory.

**For you, in every project:**

```bash
npx skills add SQLAnvil/agent-skills --skill sqlanvil-sqlx-lint -a claude-code -g
```

This writes `~/.claude/skills/sqlanvil-sqlx-lint/SKILL.md`.

Use `--skill '*'` instead to also install `sqlanvil-engineering-fundamentals`,
which teaches the agent how sqlanvil projects differ from Dataform.
The two skills work together.

**Without Node.js**, copy the skill folder yourself:

```bash
git clone --depth 1 https://github.com/SQLAnvil/agent-skills /tmp/sas
mkdir -p .claude/skills    # or ~/.claude/skills for every project
cp -r /tmp/sas/skills/sqlanvil-sqlx-lint .claude/skills/
```

**Check that it loaded.** Claude Code picks up new skills without a restart.
One exception: if `.claude/skills/` didn't exist when your session started,
run `/reload-skills` once. Then:

1. Run `/skills`. `sqlanvil-sqlx-lint` should be in the list.
2. Ask *"lint definitions/my_table.sqlx for sqlanvil conventions"*. Claude
   should run `sqlanvil-sqlx-lint` and report findings by rule code. From
   then on it lints every `.sqlx` file it creates or edits, without being
   asked.

You can also run the skill yourself by typing `/sqlanvil-sqlx-lint`.

**Updating.** The skill is updated with every SQLAnvil release.
`npx skills update sqlanvil-sqlx-lint` pulls the latest version of an install
made with the skills CLI. For a copied folder, copy it again.

### Other agents

`npx skills add SQLAnvil/agent-skills` without `-a` installs into every agent
it detects on your machine (Codex CLI, Cursor, and others). To copy by hand,
put `skills/sqlanvil-sqlx-lint/` in your agent's skills directory.

## Development

```bash
python3 -m venv .venv && .venv/bin/pip install -e . pytest
.venv/bin/pytest        # 103 tests
```

## License

MIT — see [LICENSE](https://github.com/SQLAnvil/sqlanvil-sqlx-lint/blob/main/LICENSE).
