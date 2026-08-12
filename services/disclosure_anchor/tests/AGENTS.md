# test boundary

- The test framework is `unittest`; do not introduce pytest. `make test` is deterministic discovery without a
  live DB, provider credential, production GPU, remote parser, worker, or AgentSSD runtime dependency.
- `make test-integration` owns scratch-database creation, migration, marker verification, isolated roots, and
  cleanup. Tests never fall back to `invest_engine`, a production DSN, or shared runtime paths.
- Credentialed provider, real parser/MinerU, and smoke checks are explicit opt-in and use disposable DB/artifact
  roots. A skipped optional check is reported with its exact missing precondition; it is not claimed as executed.
- Behavior changes test the failure family and semantic invariant with a representative positive, an adjacent
  negative, and deterministic replay across relevant filing/provider/issuer variation. Do not patch by document ID
  or equate one golden unit count with correctness.
- Assert source-slice/integrity conservation, boundaries, provenance, and output meaning. Published DB state and
  historical fixtures are evidence, not automatic product authority.
- Before adding coverage, inspect adjacent tests and remove/merge obsolete or duplicate assertions. Fixture rules
  are in `docs/implementation/checks/fixture-and-test-policy.md`; the composition ratchet is enforced by
  `scripts/audit_test_composition.py` against `tests/composition_ledger.json`. Refresh the ledger with its
  explicit update mode only after a deliberate composition audit.
- Tests that commit, consume a global queue, rebuild a projection, or exercise migrations stay inside the suite
  scratch DB. Teardown isolation is not a substitute for production safety.
- Current test counts, skip counts, dates, milestone labels, runner internals, and composition baselines belong in
  generated test output/ledgers/checklists, not in agent instructions.
