# PostgreSQL adapter boundary

- Own only `disclosure_core`, `disclosure_ops`, `disclosure_public`, and their documented least-privilege roles.
  Cross-service consumers use versioned public views/API/change feed, never private tables.
- Applied Alembic revisions are immutable and append-only. Derive the current head from the revision graph; never
  hard-code a historical head in tests, doctor, or instructions.
- Models, constraints, migrations, entity mappers, repositories, public views, grants, and exported contracts stay
  semantically aligned. A public-view change is a versioned contract change with consumer and regression updates.
- Application writes go through the UnitOfWork; repositories translate known database conflicts into typed domain
  errors and do not commit implicitly.
- Queue/ops views expose facts to the app role; scheduling thresholds remain caller policy. Reader/L2 roles receive
  only the documented public surface.
- DB integration runs only through `make test-integration` and its marker-verified scratch database/root set. It
  must fail closed on missing markers and never fall back to `DATABASE_URL`, `invest_engine`, or shared AgentSSD.
- Migration numbers, column counts, revision histories, current grants, and rollout notes belong in Alembic,
  generated contracts, migration milestones, and the contract checklist—not in this file.
