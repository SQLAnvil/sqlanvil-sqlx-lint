"""Core linter: parses the .sqlx config block and SQL body, emits Findings.

Rules (codes are stable; suppress with `-- sqlx-lint: disable=E006` on the
offending line or `-- sqlx-lint: disable-file=E006` anywhere in the file):

  E001  config {} block missing or unbalanced
  E002  columns: {} missing or empty on documented types
        (declarations may carry columnTypes: {} instead)
  E003  schema: hardcodes an environment suffix (suffix-doubling trap)
  E004  name: redundantly matches the filename (non-declarations)
  E005  schema: set on operations/assertion configs        [opt-in]
  E006  hardcoded table path instead of ${ref()} (warehouse-aware)
  E007  directory policy violation (configured prefix/type per path)
  W008  post_operations block appears before the main SELECT [opt-in]
  E010  columns:{} does not cover every determinable output column

sqlanvil-specific rules (the deltas that bite when Dataform habits carry over):

  S101  BigQuery-only config (bigquery:{}, partitionBy, clusterBy, ...) on a
        non-BigQuery warehouse — silently ignored, never applied
  S102  `;` used to separate statements in operations / pre_operations /
        post_operations — sqlanvil splits on `---`, so the block runs as one
        statement and fails at run time
  S103  postgres.indexes[].method given as a string — it is a numeric enum
  S104  incrementalStrategy on a non-BigQuery warehouse
  S105  ADD PRIMARY KEY / ADD CONSTRAINT in an incremental's operations block
        not wrapped in when(!incremental(), ...) — errors on the second run
  S106  assertions: sets both uniqueKey and uniqueKeys
  S108  .jitCode() / jitData() — no runtime in sqlanvil
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .config import Config

NO_SCHEMA_TYPES = {"operations", "assertion", "test"}

#: Schemas that are never a hardcoded *source* (system catalogs).
SYSTEM_SCHEMAS = {"pg_catalog", "information_schema", "pg_temp", "mysql", "sys",
                  "performance_schema"}

#: Config keys that only BigQuery understands (S101).
BIGQUERY_ONLY_KEYS = ("bigquery", "partitionBy", "clusterBy", "bigqueryPolicyTags")


@dataclass
class Finding:
    code: str
    severity: str  # "error" | "warning"
    line: int
    message: str


def _line_of(text, pos):
    return text.count("\n", 0, pos) + 1


def _balanced_block(text, open_brace_pos):
    """Return (content, end_pos) of the brace block at open_brace_pos, else None."""
    depth = 0
    in_str = None
    i = open_brace_pos
    while i < len(text):
        ch = text[i]
        if in_str:
            if ch == "\\":
                i += 2
                continue
            if ch == in_str:
                in_str = None
        elif ch in "\"'`":
            in_str = ch
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[open_brace_pos + 1 : i], i + 1
        i += 1
    return None


def _config_value(config, key):
    m = re.search(rf"\b{key}\s*:\s*([\"'])(.*?)\1", config)
    return m.group(2) if m else None


def _config_key_line(config, key, config_line):
    m = re.search(rf"\b{key}\s*:", config)
    return config_line + config[: m.start()].count("\n") if m else config_line


def _strip_comments(text):
    text = re.sub(
        r"/\*.*?\*/", lambda m: re.sub(r"[^\n]", " ", m.group(0)), text, flags=re.S
    )
    return re.sub(r"--[^\n]*", lambda m: " " * len(m.group(0)), text)


def _split_top_level(text, sep=","):
    """Split text on sep occurring outside (), [], {}, and string literals."""
    parts, depth, start, in_str, i = [], 0, 0, None, 0
    while i < len(text):
        ch = text[i]
        if in_str:
            if ch == "\\":
                i += 2
                continue
            if ch == in_str:
                in_str = None
        elif ch in "\"'`":
            in_str = ch
        elif ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        elif ch == sep and depth == 0:
            parts.append(text[start:i])
            start = i + 1
        i += 1
    parts.append(text[start:])
    return parts


def _scan_sql(text):
    """Walk SQL text and return (semicolons, js_regions).

    semicolons: positions of `;` that sit outside string literals, dollar-quoted
    bodies, and `${ ... }` JavaScript regions.
    js_regions: (start, end) spans of `${ ... }` regions, so callers can tell
    whether a match is wrapped in a template expression.
    """
    semis, regions = [], []
    i, n = 0, len(text)
    in_str = None
    while i < n:
        ch = text[i]
        if in_str:
            if ch == "\\":
                i += 2
                continue
            if ch == in_str:
                in_str = None
            i += 1
            continue
        if ch == "$" and text.startswith("$$", i):
            end = text.find("$$", i + 2)
            i = n if end < 0 else end + 2
            continue
        if ch == "$" and text.startswith("${", i):
            start = i
            depth, j, js_str = 0, i + 1, None
            while j < n:
                c = text[j]
                if js_str:
                    if c == "\\":
                        j += 2
                        continue
                    if c == js_str:
                        js_str = None
                elif c in "\"'`":
                    js_str = c
                elif c == "{":
                    depth += 1
                elif c == "}":
                    depth -= 1
                    if depth == 0:
                        break
                j += 1
            regions.append((start, j + 1))
            i = j + 1
            continue
        if ch in "\"'":
            in_str = ch
        elif ch == ";":
            semis.append(i)
        i += 1
    return semis, regions


def _statement_blocks(body):
    """Yield (kind, start, content) for each pre_operations/post_operations block."""
    for m in re.finditer(r"\b(pre_operations|post_operations)\s*({)", body):
        block = _balanced_block(body, m.start(2))
        if block:
            yield m.group(1), m.start(2) + 1, block[0]


def _main_projection(clean_body):
    """Return (projection_text, from_clause_text, select_pos) of the first
    top-level SELECT (CTE bodies and sub-selects sit inside parens/braces),
    or None if no top-level SELECT exists."""
    depth, in_str, i = 0, None, 0
    select_pos = None
    while i < len(clean_body):
        ch = clean_body[i]
        if in_str:
            if ch == "\\":
                i += 2
                continue
            if ch == in_str:
                in_str = None
        elif ch in "\"'`":
            in_str = ch
        elif ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        elif depth == 0 and ch in "sSfF":
            word = re.match(r"(select|from)\b", clean_body[i:], re.I)
            if word and (
                i == 0 or not (clean_body[i - 1].isalnum() or clean_body[i - 1] in "_$")
            ):
                if word.group(1).lower() == "select" and select_pos is None:
                    select_pos = i
                elif word.group(1).lower() == "from" and select_pos is not None:
                    return (clean_body[select_pos + 6 : i], clean_body[i:], select_pos)
        i += 1
    if select_pos is not None:  # SELECT without FROM (constants)
        return (clean_body[select_pos + 6 :], "", select_pos)
    return None


_SIMPLE_IDENT = re.compile(r"^`?[A-Za-z_]\w*`?(?:\.`?([A-Za-z_]\w*)`?)?$")
_TRAILING_ALIAS = re.compile(r"\bas\s+`?([A-Za-z_]\w*)`?\s*$", re.I | re.S)
_STAR_ITEM = re.compile(
    r"^(?:[A-Za-z_]\w*\.)?\*\s*(?:except\s*\(([^)]*)\))?\s*(?:replace\s*\(.*\))?$",
    re.I | re.S,
)
_SINGLE_REF_FROM = re.compile(
    r"^from\s+\$\{\s*ref\(\s*(?:\"[^\"]*\"\s*,\s*)?\"([^\"]+)\"\s*\)\s*\}\s*"
    r"(?:as\s+\w+\s*)?"
    r"(?:where\b|group\b|order\b|qualify\b|limit\b|window\b|post_operations\b|$)",
    re.I | re.S,
)


def _known_output_columns(clean_body, resolver, _depth=0, _seen=None):
    """Best-effort set of output column names of a file's main SELECT.
    Unparseable items are silently skipped; `select *` is followed through a
    single plain ${ref()} when a resolver is provided. Never raises."""
    parsed = _main_projection(clean_body)
    if parsed is None:
        return set()
    projection, from_clause, _ = parsed
    names, star_except = set(), None
    items = _split_top_level(projection)
    if items:
        items[0] = re.sub(r"^\s*(distinct|all)\b", "", items[0], flags=re.I)
    for item in items:
        item = item.strip()
        if not item:
            continue
        sm = _STAR_ITEM.match(item)
        if sm:
            star_except = {
                n.strip().strip("`").lower()
                for n in (sm.group(1) or "").split(",")
                if n.strip()
            }
            continue
        am = _TRAILING_ALIAS.search(item)
        if am:
            names.add(am.group(1).lower())
            continue
        im = _SIMPLE_IDENT.match(item)
        if im:
            names.add((im.group(1) or item.strip("`")).lower())
    if star_except is not None and resolver is not None and _depth < 3:
        rm = _SINGLE_REF_FROM.match(from_clause.strip())
        if rm:
            ref_name = rm.group(1)
            _seen = _seen or set()
            if ref_name not in _seen:
                _seen.add(ref_name)
                upstream = resolver(ref_name)
                if upstream is not None:
                    up_names = _known_output_columns(
                        _strip_comments(upstream), resolver, _depth + 1, _seen
                    )
                    names |= up_names - star_except
    return names


def _block_keys(config, key):
    """Top-level keys of the `<key>: {}` block, lowercased; empty set if none."""
    cm = re.search(rf"\b{key}\s*:\s*({{)", config)
    block = _balanced_block(config, cm.start(1)) if cm else None
    if not block:
        return set()
    keys = set()
    for item in _split_top_level(block[0]):
        km = re.match(r'\s*"?([A-Za-z_]\w*)"?\s*:', item)
        if km:
            keys.add(km.group(1).lower())
    return keys


def _documented_keys(config):
    return _block_keys(config, "columns")


# --- E006: hardcoded table paths, per warehouse -------------------------------

_IDENT = r'(?:"[^"\n]+"|`[^`\n]+`|[A-Za-z_]\w*)'
#: `schema.table` (any quoting per part) right after a table-introducing keyword.
_TWO_PART_AFTER_KEYWORD = re.compile(
    rf"\b(?:from|join|into|update)\s+({_IDENT})\s*\.\s*({_IDENT})(?![\w.\"`(])",
    re.I,
)
#: BigQuery-style whole-path in one pair of backticks: `proj.dataset.table`
_BQ_BACKTICK_3 = re.compile(r"`([A-Za-z][\w-]*\.[A-Za-z]\w*\.[A-Za-z]\w*)`")
#: `dataset.table` in one pair of backticks (BigQuery shorthand)
_BQ_BACKTICK_2 = re.compile(r"`([A-Za-z][\w-]*\.[A-Za-z]\w*)`")


#: An open extract/substring/trim/overlay/position call with no `)` yet.
_FUNCTION_FROM = re.compile(
    r"\b(?:extract|substring|trim|overlay|position)\s*\([^()]*$", re.I
)


def _unquote(ident):
    return ident.strip('"`').lower()


def _hardcoded_paths(clean_body, warehouse, allowed_schemas):
    """Yield (pos, path) for table references that bypass ${ref()}."""
    allowed = {s.lower() for s in allowed_schemas} | SYSTEM_SCHEMAS
    for pm in _BQ_BACKTICK_3.finditer(clean_body):
        yield pm.start(), f"`{pm.group(1)}`"
    if warehouse == "bigquery":
        for pm in _BQ_BACKTICK_2.finditer(clean_body):
            yield pm.start(), f"`{pm.group(1)}`"
    for pm in _TWO_PART_AFTER_KEYWORD.finditer(clean_body):
        first, second = pm.group(1), pm.group(2)
        schema = _unquote(first)
        if schema in allowed or schema.startswith("pg_temp"):
            continue
        # `extract(dow from d.date)`, `substring(x from 1)`, `trim(both from x)`:
        # FROM as function syntax, inside an open call paren.
        if _FUNCTION_FROM.search(clean_body[max(0, pm.start() - 200) : pm.start()]):
            continue
        yield pm.start(1), f"{first}.{second}"


def lint_text(text, path, config: Config | None = None, resolver=None):
    cfg = config or Config()
    warehouse = cfg.effective_warehouse
    findings: list[Finding] = []
    path = path.replace("\\", "/")
    stem = re.sub(r"\.sqlx$", "", path.rsplit("/", 1)[-1])
    file_disabled = set(re.findall(r"sqlx-lint:\s*disable-file=([EWS]\d+)\b", text))
    lines = text.split("\n")

    def suppressed(code, line):
        if code in file_disabled:
            return True
        if not (0 < line <= len(lines)):
            return False
        src = lines[line - 1]
        return "sqlx-lint:" in src and re.search(rf"disable={code}\b", src) is not None

    def add(code, severity, line, message):
        if cfg.rule_on(code) and not suppressed(code, line):
            findings.append(Finding(code, severity, line, message))

    # --- S108: JIT compilation has no runtime in sqlanvil (checked pre-config) ---
    for jm in re.finditer(r"\.jitCode\s*\(|\bjitData\s*\(", text):
        add(
            "S108",
            "error",
            _line_of(text, jm.start()),
            "jitCode()/jitData() have no runtime in sqlanvil (compile error since "
            "1.30) — generate the SQL at compile time instead",
        )

    # --- E001: locate and parse config block ---
    m = re.search(r"\bconfig\s*({)", text)
    if not m:
        add("E001", "error", 1, "no config {} block found")
        return findings
    block = _balanced_block(text, m.start(1))
    if block is None:
        add(
            "E001",
            "error",
            _line_of(text, m.start()),
            "config {} block braces are unbalanced",
        )
        return findings
    config_body, config_end = block
    config_line = _line_of(text, m.start())
    body = text[config_end:]
    body_offset = config_end

    ctype = _config_value(config_body, "type") or ""
    schema = _config_value(config_body, "schema") or _config_value(config_body, "dataset")
    name = _config_value(config_body, "name")
    documented = _documented_keys(config_body)
    column_types = _block_keys(config_body, "columnTypes")

    # --- E002: columns documentation ---
    # Declarations scaffolded by `sqlanvil introspect` carry columnTypes instead.
    if ctype in cfg.documented_types and not documented:
        if not (ctype == "declaration" and column_types):
            add(
                "E002",
                "error",
                config_line,
                f'type "{ctype}" requires a non-empty columns: {{}} block',
            )

    # --- E003: schema suffix (declarations exempt: raw datasets may carry one) ---
    suffix_re = "|".join(re.escape(s) for s in cfg.schema_suffixes)
    if (
        schema
        and cfg.schema_suffixes
        and ctype != "declaration"
        and re.search(rf"(?:{suffix_re})$", schema)
    ):
        base = re.sub(rf"(?:{suffix_re})$", "", schema)
        key = "schema" if _config_value(config_body, "schema") is not None else "dataset"
        add(
            "E003",
            "error",
            _config_key_line(config_body, key, config_line),
            f'{key}: "{schema}" hardcodes an environment suffix; use the base '
            f'name ("{base}") and let --schema-suffix or the environment\'s '
            "schemaSuffix append it",
        )

    # --- E004: redundant name (declarations conventionally repeat it) ---
    if name and ctype != "declaration" and name == stem:
        add(
            "E004",
            "error",
            _config_key_line(config_body, "name", config_line),
            f'name: "{name}" matches the filename and is redundant — remove it',
        )

    # --- E005 (opt-in): schema on operations/assertions ---
    # hasOutput: true operations are exempt: schema+name define ${self()}.
    has_output = re.search(r"\bhasOutput\s*:\s*true\b", config_body) is not None
    if schema and ctype in NO_SCHEMA_TYPES and not has_output:
        add(
            "E005",
            "error",
            config_line,
            f'type "{ctype}" must not set schema: — '
            "it uses the workflow_settings.yaml default",
        )

    # --- E006: hardcoded table paths in the SQL body ---
    clean_body = _strip_comments(body)
    for pos, table_path in _hardcoded_paths(clean_body, warehouse, cfg.allowed_schemas):
        add(
            "E006",
            "error",
            _line_of(text, body_offset + pos),
            f"hardcoded table path {table_path} — declare a source and use ${{ref()}}",
        )

    # --- E007: directory policies ---
    for policy in cfg.dir_policies:
        if policy.path_contains not in path:
            continue
        if policy.require_prefix and not stem.startswith(policy.require_prefix):
            add(
                "E007",
                policy.severity,
                1,
                f"files in {policy.path_contains} must be prefixed "
                f'"{policy.require_prefix}" (got "{stem}")',
            )
        if policy.require_types and ctype not in policy.require_types:
            add(
                "E007",
                policy.severity,
                config_line,
                f"files in {policy.path_contains} must be type "
                f'{" or ".join(policy.require_types)} (got "{ctype}")',
            )

    # --- E010: columns coverage (skip when E002 already owns the file) ---
    in_scope = not cfg.coverage_paths or any(
        p in path for p in cfg.coverage_paths
    )
    if ctype in ("table", "incremental", "view") and documented and in_scope:
        known = _known_output_columns(clean_body, resolver)
        missing = sorted(known - documented)
        if missing:
            parsed = _main_projection(clean_body)
            sel_line = (
                _line_of(text, body_offset + parsed[2]) if parsed else config_line
            )
            shown = ", ".join(f'"{n}"' for n in missing[:10])
            more = f" (+{len(missing) - 10} more)" if len(missing) > 10 else ""
            add(
                "E010",
                "error",
                sel_line,
                f"columns: {{}} is missing documentation for output "
                f"column(s): {shown}{more}",
            )

    # --- W008 (opt-in): post_operations placement ---
    pm = re.search(r"\bpost_operations\s*{", body)
    if pm:
        sm = re.search(r"(?im)^\s*select\b", _strip_comments(body[: pm.start()]))
        if sm is None:
            add(
                "W008",
                "warning",
                _line_of(text, body_offset + pm.start()),
                "post_operations {} placed before the main SELECT statement",
            )

    # --- S101 / S104: BigQuery-only config on another warehouse ---
    if warehouse != "bigquery":
        # Keys nested inside a bigquery: {} block are reported once, via the block.
        bqm = re.search(r"\bbigquery\s*:\s*({)", config_body)
        bq_block = _balanced_block(config_body, bqm.start(1)) if bqm else None
        scan = config_body
        if bq_block:
            s0, s1 = bqm.start(1), bqm.start(1) + len(bq_block[0]) + 2
            scan = config_body[:s0] + re.sub(r"[^\n]", " ", config_body[s0:s1]) + config_body[s1:]
        for key in BIGQUERY_ONLY_KEYS:
            if re.search(rf"\b{key}\s*:", scan):
                add(
                    "S101",
                    "error",
                    _config_key_line(config_body, key, config_line),
                    f"{key}: is BigQuery-only and is ignored on {warehouse} — "
                    "use the postgres: {} block (indexes, partition) instead",
                )
        if re.search(r"\bincrementalStrategy\s*:", config_body):
            add(
                "S104",
                "error",
                _config_key_line(config_body, "incrementalStrategy", config_line),
                f"incrementalStrategy: is BigQuery-only (compile error on {warehouse}) "
                "— use the default merge with uniqueKey, or delete the range in "
                "pre_operations",
            )

    # --- S103: postgres index method must be the numeric enum ---
    pgm = re.search(r"\bpostgres\s*:\s*({)", config_body)
    pg_block = _balanced_block(config_body, pgm.start(1)) if pgm else None
    if pg_block:
        for mm in re.finditer(r"\bmethod\s*:\s*([\"'])(\w+)\1", pg_block[0]):
            add(
                "S103",
                "error",
                config_line + config_body[: pgm.start() + 1 + mm.start()].count("\n"),
                f'postgres.indexes[].method: "{mm.group(2)}" must be the numeric enum '
                "(BTREE=0, HASH=1, GIN=2, GIST=3, BRIN=4); omit it for btree",
            )

    # --- S106: uniqueKey and uniqueKeys are mutually exclusive in assertions ---
    am = re.search(r"\bassertions\s*:\s*({)", config_body)
    a_block = _balanced_block(config_body, am.start(1)) if am else None
    if a_block:
        keys = _block_keys(config_body, "assertions")
        if "uniquekey" in keys and "uniquekeys" in keys:
            add(
                "S106",
                "error",
                _config_key_line(config_body, "assertions", config_line),
                "assertions: sets both uniqueKey and uniqueKeys — they are mutually "
                "exclusive; fold the single key into uniqueKeys",
            )

    # --- S102 / S105: statement blocks (operations body, pre/post_operations) ---
    blocks = list(_statement_blocks(body))
    if ctype == "operations":
        blocks.append(("operations", 0, body))

    for kind, start, content in blocks:
        semis, regions = _scan_sql(content)
        for sp in semis:
            rest = content[sp + 1 :]
            # Ignore a trailing `;` — only `;` followed by another statement is a
            # separator. Comments after it do not count as a statement.
            if re.search(r"\S", _strip_comments(rest)):
                add(
                    "S102",
                    "error",
                    _line_of(text, body_offset + start + sp),
                    f"`;` separates statements in {kind} — sqlanvil splits on a "
                    "`---` line, so this runs as one statement and fails at run time",
                )
                break
        if ctype == "incremental" and kind != "operations":
            for dm in re.finditer(
                r"\bADD\s+(?:PRIMARY\s+KEY|CONSTRAINT)\b", content, re.I
            ):
                guarded = any(
                    s <= dm.start() < e
                    and re.search(
                        r"when\s*\(\s*!\s*incremental\s*\(\s*\)", content[s:e]
                    )
                    for s, e in regions
                )
                if not guarded:
                    add(
                        "S105",
                        "error",
                        _line_of(text, body_offset + start + dm.start()),
                        f"{dm.group(0)} in {kind} of an incremental runs on every "
                        "append and fails the second time — wrap it in "
                        "${when(!incremental(), `...`)}",
                    )

    return findings


def lint_file(path, config: Config | None = None, resolver=None):
    with open(path, encoding="utf-8") as fh:
        return lint_text(fh.read(), path, config=config, resolver=resolver)
