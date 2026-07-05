"""Integration tests (reserved for Phase 02+).

This layer is intentionally empty in Phase 01. Phase 02 fills it with PostgreSQL
repository CRUD, UnitOfWork rollback, and `make migrate` idempotence tests; later
phases add RawDocumentStore, the MinerU adapter, and the document_unit builder.
Tests here must skip cleanly when the local database/runtime is absent so the
suite stays green without external services.
"""
