# Filing API boundary

- Public readers return only `disclosure_public.*_v1` data or documented derived fields. Do not expose private
  table/model shapes, absolute paths, stack traces, raw parser internals, bbox/page internals, or private roles.
- Public DTO names and meaning follow the versioned views/contracts. Any public field, cursor, error envelope, or
  contract-version change updates schemas, export, consumers, tests, and the contract checklist together.
- Evidence responses expose only requestable locators and integrity metadata authorized by the published unit;
  missing or hash/size/media drift fails closed and never falls back to an arbitrary file.
- Read routers use the reader engine; admin composition uses the app engine. A public route must not rely on
  write privileges or an admin/private-table shortcut.
- Admin routes remain local/loopback and authenticated, fail closed when required configuration is missing, and
  orchestrate existing use cases rather than duplicating domain/application logic.
- This service remains L1: do not add claim/forecast semantics, multi-tenant policy, MCP wrappers, or vector/full-
  text retrieval to the API layer without a governing contract revision.
- Current endpoints, derived-field allowlists, error-code inventories, migration provenance, and rollout history
  belong in generated contracts and `docs/implementation/checks/contract-checklist.md`.
