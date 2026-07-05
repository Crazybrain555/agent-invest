"""Two-layer provider configuration (framework v1.2 §4).

- ``registry/datasets/<dataset_key>.yaml``  — semantic dataset contracts.
- ``registry/providers/<provider>.catalog.yaml`` — physical provider catalogs
  (table aliases with candidates + activation, endpoints, safety defaults).

Everything is Git-managed configuration; this module loads, validates and
cross-checks it. Fail-fast discipline (F12): any missing reference, uncovered
required field, unknown transform, or failed live preflight raises
``RegistryError`` naming exactly what is missing — never silent degradation.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any, Literal, Optional, Protocol

import yaml
from pydantic import BaseModel, ConfigDict, model_validator

SERVICE_ROOT = Path(__file__).resolve().parents[3]
REGISTRY_ROOT = SERVICE_ROOT / "registry"

KNOWN_TRANSFORM_TYPES = frozenset({"multiply", "yyyymmdd_to_date"})


class RegistryError(RuntimeError):
    """Configuration is inconsistent or fails preflight; message lists specifics."""


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


# --------------------------------------------------------------------------- datasets


class QueryParamSpec(_Model):
    type: str
    required: bool = False
    default: Any = None
    allowed: list[str] | None = None
    format: str | None = None
    description: str | None = None


class TransformSpec(_Model):
    type: str
    factor: float | None = None
    target_unit: str | None = None

    @model_validator(mode="after")
    def _check(self) -> "TransformSpec":
        if self.type not in KNOWN_TRANSFORM_TYPES:
            raise ValueError(f"unknown transform type '{self.type}'")
        if self.type == "multiply" and self.factor is None:
            raise ValueError("multiply transform requires factor")
        return self


class FieldSpec(_Model):
    name: str
    dtype: str
    unit: str | None = None
    description: str | None = None
    required: bool = False
    as_of_sensitive: bool = False
    group: str | None = None
    derived: bool = False


class FieldMap(_Model):
    column: str | None = None
    endpoint: str | None = None
    provider_unit: str | None = None
    transform: TransformSpec | None = None

    @model_validator(mode="after")
    def _check(self) -> "FieldMap":
        if self.column is None:
            raise ValueError("field_map entry requires column")
        return self


class SqlTemplateParam(_Model):
    type: str
    source: str | None = None
    default: Any = None
    max: int | None = None


class SqlTemplate(_Model):
    statement: str
    params: dict[str, SqlTemplateParam]
    required_predicates: list[str]
    max_rows: int
    timeout_seconds: int
    result_order: list[str] = []


class EndpointJoin(_Model):
    left: str
    right: str
    join_on: list[str]
    join_type: Literal["inner"] = "inner"


class MappingVariant(_Model):
    when: dict[str, str]
    table_alias: str | None = None
    endpoints: list[str] | None = None
    endpoints_join: EndpointJoin | None = None
    field_map: dict[str, FieldMap]
    sql_templates: dict[str, SqlTemplate] = {}
    scope_filter: dict[str, list[str]] | None = None

    @model_validator(mode="after")
    def _check(self) -> "MappingVariant":
        if len(self.when) > 1:
            raise ValueError("variant 'when' must have at most one param")
        return self


class ProviderMapping(_Model):
    table_alias: str | None = None
    endpoints: list[str] | None = None
    endpoints_join: EndpointJoin | None = None
    field_map: dict[str, FieldMap] = {}
    sql_templates: dict[str, SqlTemplate] = {}
    variants: list[MappingVariant] = []

    @model_validator(mode="after")
    def _check(self) -> "ProviderMapping":
        flat = bool(self.field_map)
        if flat == bool(self.variants):
            raise ValueError("mapping must be either flat (field_map) or variants, not both/neither")
        if any(not v.when for v in self.variants):
            raise ValueError("declared variants must each have a 'when' param")
        return self


class TimeSemantics(_Model):
    event_time_from: str | None = None
    published_at_rule: str
    provider_as_of_required: bool = True


class SubjectFrom(_Model):
    field: str
    subject_kind: str


class SubjectSemantics(_Model):
    subject_candidates_from: list[SubjectFrom]


class SemanticContract(_Model):
    description: str
    asset_kind: Literal["dataset_snapshot"]
    payload_kind: Literal["recordset"]
    material_type: str
    source_tier: str
    trace_level: str
    query_params: dict[str, QueryParamSpec]
    primary_key: list[str]
    time_semantics: TimeSemantics
    subject_semantics: SubjectSemantics
    scope_semantics: dict[str, str] = {}
    fields: list[FieldSpec]

    @model_validator(mode="after")
    def _check(self) -> "SemanticContract":
        names = [f.name for f in self.fields]
        if len(names) != len(set(names)):
            raise ValueError("duplicate field names")
        unknown_pk = set(self.primary_key) - set(names)
        if unknown_pk:
            raise ValueError(f"primary_key not in fields: {sorted(unknown_pk)}")
        return self


class DuplicatePolicy(_Model):
    keys: list[str]
    action: Literal["fail", "keep_latest"]


class ValidationSpec(_Model):
    row_checks: list[str] = []
    duplicate_policy: DuplicatePolicy


class ContentHashSpec(_Model):
    sort_by: list[str]
    include_fields: list[str]


class DedupSpec(_Model):
    semantic_key_fields: list[str]
    content_hash: ContentHashSpec


class DatasetEntry(_Model):
    schema_version: Literal["dataset_registry.v1"]
    dataset_key: str
    dataset_contract_version: int
    status: Literal["active", "draft", "deprecated"]
    semantic_contract: SemanticContract
    providers: dict[str, ProviderMapping]
    validation: ValidationSpec
    dedup: DedupSpec

    @model_validator(mode="after")
    def _check(self) -> "DatasetEntry":
        field_names = {f.name for f in self.semantic_contract.fields}
        bad = set(self.dedup.content_hash.include_fields) - field_names
        if bad:
            raise ValueError(f"dedup include_fields not in fields: {sorted(bad)}")
        return self


# --------------------------------------------------------------------------- provider catalogs


class SafetySpec(_Model):
    readonly_required: bool = True
    single_statement_only: bool = True
    require_bound_params: bool = True
    forbid_select_star: bool = True
    deny_tokens: list[str] = []
    default_timeout_seconds: int = 30
    default_max_rows: int = 10000


class TableCandidate(_Model):
    table: str
    role: str
    key_columns: list[str] = []
    date_column: str | None = None
    required_columns: list[str] = []
    note: str | None = None


class FreshnessCheck(_Model):
    sql: str | None = None
    endpoint: str | None = None


class TableAlias(_Model):
    semantic_datasets: list[str]
    candidates: list[TableCandidate]
    active_table: str | None = None
    activation_rule: list[str] = []
    freshness_check: FreshnessCheck | None = None

    @model_validator(mode="after")
    def _check(self) -> "TableAlias":
        tables = {c.table for c in self.candidates}
        if self.active_table is not None and self.active_table not in tables:
            raise ValueError(f"active_table '{self.active_table}' not among candidates")
        return self


class EndpointSpec(_Model):
    api_name: str
    note: str | None = None


class RateLimit(_Model):
    min_interval_ms: int
    retry_backoff_seconds: list[int] = []


class IndexDiscipline(_Model):
    required_prefix_predicates: list[str] = []
    date_columns_are_varchar8: bool = False
    note: str | None = None


class ProviderCatalog(_Model):
    schema_version: Literal["provider_catalog.v1"]
    provider: str
    provider_kind: str
    connection_profile: str
    base_url: str | None = None
    default_safety: SafetySpec
    index_discipline: IndexDiscipline | None = None
    table_aliases: dict[str, TableAlias] = {}
    endpoints: dict[str, EndpointSpec] = {}
    rate_limit: RateLimit | None = None


# --------------------------------------------------------------------------- loading


def _load_yaml(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as handle:
        data = yaml.safe_load(handle)
    if not isinstance(data, dict):
        raise RegistryError(f"{path}: top level must be a mapping")
    return data


def load_dataset_entries(root: Path | None = None) -> dict[str, DatasetEntry]:
    directory = (root or REGISTRY_ROOT) / "datasets"
    entries: dict[str, DatasetEntry] = {}
    for path in sorted(directory.glob("*.yaml")):
        entry = DatasetEntry.model_validate(_load_yaml(path))
        if path.stem != entry.dataset_key:
            raise RegistryError(f"{path.name}: filename must equal dataset_key '{entry.dataset_key}'")
        entries[entry.dataset_key] = entry
    return entries


def load_provider_catalogs(root: Path | None = None) -> dict[str, ProviderCatalog]:
    directory = (root or REGISTRY_ROOT) / "providers"
    catalogs: dict[str, ProviderCatalog] = {}
    for path in sorted(directory.glob("*.catalog.yaml")):
        catalog = ProviderCatalog.model_validate(_load_yaml(path))
        expected = f"{catalog.provider}.catalog"
        if path.name != f"{expected}.yaml":
            raise RegistryError(f"{path.name}: filename must be '{expected}.yaml'")
        catalogs[catalog.provider] = catalog
    return catalogs


@lru_cache(maxsize=1)
def load_registry() -> tuple[dict[str, DatasetEntry], dict[str, ProviderCatalog]]:
    entries = load_dataset_entries()
    catalogs = load_provider_catalogs()
    validate_cross_references(entries, catalogs)
    return entries, catalogs


# --------------------------------------------------------------------------- cross validation


def _iter_mappings(mapping: ProviderMapping) -> list[MappingVariant]:
    if mapping.variants:
        return mapping.variants
    return [
        MappingVariant(
            when={},
            table_alias=mapping.table_alias,
            endpoints=mapping.endpoints,
            endpoints_join=mapping.endpoints_join,
            field_map=mapping.field_map,
            sql_templates=mapping.sql_templates,
        )
    ]


def _required_fields_for(contract: SemanticContract, when: dict[str, str]) -> set[str]:
    group = next(iter(when.values()), None)
    out: set[str] = set()
    for spec in contract.fields:
        if spec.derived or not spec.required:
            continue
        if spec.group is None or (group is not None and spec.group == group):
            out.add(spec.name)
    return out


def validate_cross_references(
    entries: dict[str, DatasetEntry], catalogs: dict[str, ProviderCatalog]
) -> None:
    problems: list[str] = []
    for key, entry in entries.items():
        contract = entry.semantic_contract
        field_names = {f.name for f in contract.fields}
        for provider_name, mapping in entry.providers.items():
            catalog = catalogs.get(provider_name)
            if catalog is None:
                problems.append(f"{key}: provider '{provider_name}' has no catalog")
                continue
            for variant in _iter_mappings(mapping):
                label = f"{key}/{provider_name}" + (f"{variant.when}" if variant.when else "")
                for param, value in variant.when.items():
                    spec = contract.query_params.get(param)
                    if spec is None or (spec.allowed and value not in spec.allowed):
                        problems.append(f"{label}: when-param '{param}={value}' not a declared enum value")
                if variant.table_alias is not None:
                    alias = catalog.table_aliases.get(variant.table_alias)
                    if alias is None:
                        problems.append(f"{label}: table_alias '{variant.table_alias}' missing from catalog")
                    elif key not in alias.semantic_datasets:
                        problems.append(
                            f"{label}: catalog alias '{variant.table_alias}' does not list dataset '{key}'"
                        )
                endpoint_refs = set(variant.endpoints or [])
                if variant.endpoints_join is not None:
                    endpoint_refs |= {variant.endpoints_join.left, variant.endpoints_join.right}
                for fm in variant.field_map.values():
                    if fm.endpoint is not None:
                        endpoint_refs.add(fm.endpoint)
                for ref in sorted(endpoint_refs):
                    if ref not in catalog.endpoints:
                        problems.append(f"{label}: endpoint '{ref}' missing from catalog")
                unknown = set(variant.field_map) - field_names
                if unknown:
                    problems.append(f"{label}: field_map keys not in semantic fields: {sorted(unknown)}")
                missing = _required_fields_for(contract, variant.when) - set(variant.field_map)
                if missing:
                    problems.append(f"{label}: required fields unmapped: {sorted(missing)}")
                for template_name, template in variant.sql_templates.items():
                    for issue in _template_issues(template, catalog.default_safety):
                        problems.append(f"{label}: template '{template_name}': {issue}")
    if problems:
        raise RegistryError("registry cross-validation failed:\n- " + "\n- ".join(problems))


def _template_issues(template: SqlTemplate, safety: SafetySpec) -> list[str]:
    from asset_intake.providers.sql_template import validate_template

    return validate_template(template, safety)


# --------------------------------------------------------------------------- request validation


def validate_request(entry: DatasetEntry, query_params: dict[str, Any]) -> dict[str, Any]:
    spec = entry.semantic_contract.query_params
    unknown = set(query_params) - set(spec)
    if unknown:
        raise RegistryError(f"{entry.dataset_key}: unknown query params {sorted(unknown)}")
    normalized: dict[str, Any] = {}
    missing: list[str] = []
    for name, param in spec.items():
        value = query_params.get(name, param.default)
        if value is None:
            if param.required:
                missing.append(name)
            continue
        if param.allowed is not None and value not in param.allowed:
            raise RegistryError(
                f"{entry.dataset_key}: param '{name}'='{value}' not in allowed {param.allowed}"
            )
        normalized[name] = value
    if missing:
        raise RegistryError(f"{entry.dataset_key}: missing required params {sorted(missing)}")
    return normalized


# --------------------------------------------------------------------------- preflight (F12)


class TableProber(Protocol):
    def table_exists(self, table: str) -> bool: ...
    def columns(self, table: str) -> set[str]: ...
    def max_date(self, table: str, date_column: str) -> Optional[str]: ...


def resolve_active_table(alias_name: str, alias: TableAlias, prober: TableProber) -> str:
    """Fail-fast activation: qualify candidates, honor a pinned active_table, else pick freshest."""

    failures: list[str] = []
    qualified: list[TableCandidate] = []
    for candidate in alias.candidates:
        if not prober.table_exists(candidate.table):
            failures.append(f"{candidate.table}: table missing")
            continue
        missing = set(candidate.required_columns) - prober.columns(candidate.table)
        if missing:
            failures.append(f"{candidate.table}: missing required columns {sorted(missing)}")
            continue
        qualified.append(candidate)

    if alias.active_table is not None:
        if any(c.table == alias.active_table for c in qualified):
            return alias.active_table
        raise RegistryError(
            f"alias '{alias_name}': pinned active_table '{alias.active_table}' failed preflight"
            f" ({'; '.join(failures) or 'not qualified'}) — fix the catalog or the source"
        )

    if not qualified:
        raise RegistryError(f"alias '{alias_name}': no candidate qualifies ({'; '.join(failures)})")

    if "max_date_freshness" in alias.activation_rule:
        dated = [c for c in qualified if c.date_column]
        if dated:
            freshest = max(dated, key=lambda c: prober.max_date(c.table, c.date_column or "") or "")
            return freshest.table
    return qualified[0].table


def preflight_provider(
    catalog: ProviderCatalog, entries: dict[str, DatasetEntry], prober: TableProber
) -> dict[str, str]:
    """Resolve every alias referenced by active datasets; raise listing all failures (F12)."""

    used_aliases: set[str] = set()
    for entry in entries.values():
        if entry.status != "active":
            continue
        mapping = entry.providers.get(catalog.provider)
        if mapping is None:
            continue
        for variant in _iter_mappings(mapping):
            if variant.table_alias:
                used_aliases.add(variant.table_alias)

    resolved: dict[str, str] = {}
    problems: list[str] = []
    for alias_name in sorted(used_aliases):
        alias = catalog.table_aliases.get(alias_name)
        if alias is None:
            problems.append(f"alias '{alias_name}' missing from catalog")
            continue
        try:
            resolved[alias_name] = resolve_active_table(alias_name, alias, prober)
        except RegistryError as exc:
            problems.append(str(exc))
    if problems:
        raise RegistryError(
            f"provider '{catalog.provider}' preflight failed:\n- " + "\n- ".join(problems)
        )
    return resolved
