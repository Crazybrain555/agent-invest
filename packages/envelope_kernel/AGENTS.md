# envelope_kernel package boundary

- This package owns the shared `data_asset` envelope, kind matrix, `asset://` URI rules, and exported schemas.
- Keep it independent of providers, parsers, databases, storage engines, service settings, and runtime state.
- Reject unknown envelope fields (`extra="forbid"`). A new field follows the registered-extension discipline;
  an incompatible semantic change creates a new contract version.
- A breaking change creates a new contract version and updates every affected service, export, test, and doc.
- Generated schema/contract artifacts are rebuilt from canonical models and are never hand-edited.
- Keep runtime dependencies minimal and tests deterministic without PostgreSQL or external services.
- Derive current layout and consumer usage from the package code, exported contract, and tests; do not freeze a
  file map in agent instructions.
