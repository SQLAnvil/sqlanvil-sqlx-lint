"""Configuration model and TOML loading.

Precedence: .sqlx-lint.toml in the working directory, else the
[tool.sqlx-lint] table of pyproject.toml, else built-in defaults.

The target warehouse is resolved separately (see `detect_warehouse`):
an explicit `warehouse` config key wins, then `workflow_settings.yaml`
in the working directory, then "postgres".
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

#: Rules that run unless disabled.
DEFAULT_ENABLED = {
    "E001",
    "E002",
    "E003",
    "E004",
    "E006",
    "E007",
    "E010",
    "S101",
    "S102",
    "S103",
    "S104",
    "S105",
    "S106",
    "S108",
}
#: Opt-in rules (house-style checks): enable via `enable = [...]`.
OPT_IN = {"E005", "W008"}

#: Warehouse names sqlanvil accepts in workflow_settings.yaml.
WAREHOUSES = ("postgres", "supabase", "mysql", "bigquery")
DEFAULT_WAREHOUSE = "postgres"


@dataclass(frozen=True)
class DirPolicy:
    """Naming/type policy applied to files whose path contains a substring."""

    path_contains: str
    require_prefix: str | None = None
    require_types: tuple[str, ...] | None = None
    severity: str = "error"


@dataclass(frozen=True)
class Config:
    #: Target warehouse; None = detect from workflow_settings.yaml, else postgres.
    warehouse: str | None = None
    schema_suffixes: list[str] = field(
        default_factory=lambda: ["_prod", "_dev", "_test"]
    )
    documented_types: set[str] = field(
        default_factory=lambda: {"table", "view", "incremental", "declaration"}
    )
    #: E010 applies only to files whose path contains one of these; empty = all.
    coverage_paths: list[str] = field(default_factory=list)
    #: Schemas E006 may reference directly (e.g. system catalogs, extensions).
    allowed_schemas: list[str] = field(default_factory=list)
    dir_policies: list[DirPolicy] = field(default_factory=list)
    enabled_extra: set[str] = field(default_factory=set)
    disabled: set[str] = field(default_factory=set)

    def rule_on(self, code: str) -> bool:
        if code in self.disabled:
            return False
        return code in DEFAULT_ENABLED or code in self.enabled_extra

    @property
    def effective_warehouse(self) -> str:
        return (self.warehouse or DEFAULT_WAREHOUSE).lower()

    def __eq__(self, other):
        if not isinstance(other, Config):
            return NotImplemented
        return (
            self.warehouse == other.warehouse
            and self.schema_suffixes == other.schema_suffixes
            and self.documented_types == other.documented_types
            and self.coverage_paths == other.coverage_paths
            and self.allowed_schemas == other.allowed_schemas
            and self.dir_policies == other.dir_policies
            and self.enabled_extra == other.enabled_extra
            and self.disabled == other.disabled
        )


_KNOWN_KEYS = {
    "warehouse",
    "schema_suffixes",
    "documented_types",
    "coverage_paths",
    "allowed_schemas",
    "dir_policies",
    "enable",
    "disable",
}


def _from_dict(raw: dict) -> Config:
    unknown = set(raw) - _KNOWN_KEYS
    if unknown:
        raise ValueError(
            f"unknown sqlx-lint config key(s): {', '.join(sorted(unknown))}"
        )
    policies = [
        DirPolicy(
            path_contains=p["path_contains"],
            require_prefix=p.get("require_prefix"),
            require_types=tuple(p["require_types"]) if p.get("require_types") else None,
            severity=p.get("severity", "error"),
        )
        for p in raw.get("dir_policies", [])
    ]
    kwargs = {}
    if "warehouse" in raw:
        wh = str(raw["warehouse"]).lower()
        if wh not in WAREHOUSES:
            raise ValueError(
                f"unknown warehouse {wh!r}; expected one of {', '.join(WAREHOUSES)}"
            )
        kwargs["warehouse"] = wh
    if "schema_suffixes" in raw:
        kwargs["schema_suffixes"] = list(raw["schema_suffixes"])
    if "documented_types" in raw:
        kwargs["documented_types"] = set(raw["documented_types"])
    if "coverage_paths" in raw:
        kwargs["coverage_paths"] = list(raw["coverage_paths"])
    if "allowed_schemas" in raw:
        kwargs["allowed_schemas"] = list(raw["allowed_schemas"])
    return Config(
        dir_policies=policies,
        enabled_extra=set(raw.get("enable", [])),
        disabled=set(raw.get("disable", [])),
        **kwargs,
    )


def load_config_file(path: str | Path) -> Config:
    """Load a standalone TOML config file (the whole document is the config)."""
    return _from_dict(tomllib.loads(Path(path).read_text(encoding="utf-8")))


def detect_warehouse(root: str | Path = ".") -> str | None:
    """Read `warehouse:` from <root>/workflow_settings.yaml without PyYAML.

    Accepts both the scalar form (`warehouse: postgres`) and the mapping form
    (`warehouse:` followed by an indented `kind: postgres`). Returns None when
    the file is absent or carries no recognisable value.
    """
    settings = Path(root) / "workflow_settings.yaml"
    if not settings.is_file():
        return None
    try:
        lines = settings.read_text(encoding="utf-8").split("\n")
    except OSError:
        return None
    for i, line in enumerate(lines):
        m = re.match(r"^warehouse\s*:\s*(?:[\"']?([A-Za-z]+)[\"']?)?\s*(?:#.*)?$", line)
        if not m:
            continue
        if m.group(1):
            return m.group(1).lower()
        for nxt in lines[i + 1 :]:
            if not nxt.strip() or nxt.lstrip().startswith("#"):
                continue
            if not nxt.startswith((" ", "\t")):
                break
            km = re.match(r"^\s+kind\s*:\s*[\"']?([A-Za-z]+)", nxt)
            if km:
                return km.group(1).lower()
        return None
    return None


def load_config(root: str | Path = ".") -> Config:
    root = Path(root)
    standalone = root / ".sqlx-lint.toml"
    cfg = None
    if standalone.is_file():
        cfg = load_config_file(standalone)
    else:
        pyproject = root / "pyproject.toml"
        if pyproject.is_file():
            data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
            tool = data.get("tool", {}).get("sqlx-lint")
            if tool is not None:
                cfg = _from_dict(tool)
    if cfg is None:
        cfg = Config()
    return with_detected_warehouse(cfg, root)


def with_detected_warehouse(cfg: Config, root: str | Path = ".") -> Config:
    """Fill in `warehouse` from workflow_settings.yaml when the config leaves it unset."""
    if cfg.warehouse is not None:
        return cfg
    detected = detect_warehouse(root)
    if detected is None:
        return cfg
    return Config(
        warehouse=detected,
        schema_suffixes=cfg.schema_suffixes,
        documented_types=cfg.documented_types,
        coverage_paths=cfg.coverage_paths,
        allowed_schemas=cfg.allowed_schemas,
        dir_policies=cfg.dir_policies,
        enabled_extra=cfg.enabled_extra,
        disabled=cfg.disabled,
    )
