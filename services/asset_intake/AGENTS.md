# asset_intake service boundary

- This service registers L1 `dataset_snapshot` and `tool_result` assets; do not absorb disclosure parsing or
  downstream claim/forecast semantics.
- Reuse `packages/envelope_kernel`; do not fork its envelope, kind, URI, or schema definitions.
- Write only asset_intake-owned schemas/roles. Read disclosure data only through versioned public contracts or
  explicit source references, never private tables.
- Provider access is injected through ports. Preserve provider identity, request/access provenance, raw hashes,
  asset lineage, and explicit failure state; do not invent missing source facts.
- Secrets come from environment/private configuration. Runtime files stay under the service's AgentSSD root and
  never enter Git.
- Default tests are deterministic and no-DB. Credentialed provider checks require explicit opt-in. The existing
  live-DB gate uses `invest_engine`, so it may touch only `intake_*` objects and must assert that all
  `disclosure_*` objects are unchanged; do not describe it as a scratch runner.
- Before provider, registry, public-view, envelope, or provenance changes, inspect a representative payload and,
  when the DB is available, use read-only SQL to confirm the actual `intake_public.*_v1` schema and representative
  provider/kind distribution. Published rows and historical fixtures are evidence, not the semantic authority.
- Current setup commands, DSNs, ports, provider status, and directory maps are verified from Makefile/settings/
  code and observed environment; they do not belong in agent instructions.
