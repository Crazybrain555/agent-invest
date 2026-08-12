# disclosure_anchor source boundary

- Dependency direction is `domain <- application <- adapters/api/cli`; domain never imports framework, IO,
  settings, SQLAlchemy, FastAPI, providers, or storage.
- Application defines ports, use cases, transactions, and orchestration. Adapters implement external mechanisms;
  API/CLI compose them without bypassing ports or duplicating business rules.
- Inject settings, clocks, providers, parsers, stores, repositories, and UoW dependencies. Do not read global
  configuration inside domain or use cases.
- Domain entities/value objects stay pure, closed vocabularies require a contract decision, IDs are opaque, and
  ordering uses explicit semantic keys. Domain events are created only by domain factories.
- Application writes go through an injected UnitOfWork with default rollback and explicit commit. Reuse shared
  registration/resolution/build/run services; entrypoints do not reimplement their invariants.
- Preserve typed failures and provenance across layer boundaries. Unexpected failures remain visible and do not
  become empty success, guessed facts, or unstructured strings.
- Worker scheduling, lanes, leases, advisory locks, and recovery behavior are operational mechanisms; change them
  only against their design/runbook with focused concurrency and restart validation.
- Public/provider contract changes update canonical DTO/schema exports, consumers, tests, and docs together.
- Source topology, current parser versions, worker architecture, and milestones belong in architecture/design/
  runbook documents, not in this instruction file.
