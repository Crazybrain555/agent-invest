# storage-adapter boundary

- The path builder is the single authority for relative service paths. Reject traversal/escape and do not build
  runtime paths ad hoc in stores, use cases, API, tests, or scripts.
- Raw documents are immutable and append-only: write through a temporary file, make publication atomic, verify
  format/hash after write, and represent changed bytes as a new version with lineage rather than overwrite.
- Derived artifacts are atomic and content-addressed/integrity-checked according to their contract. Partial files
  never become published success.
- Database/public contracts store only approved relative locators or basenames. Absolute AgentSSD paths never enter
  API responses, tracked fixtures, exports, or logs.
- Quarantine preserves evidence and reason without turning untrusted content into an active/published artifact.
- Current directory layouts and method inventories belong in the storage design/runbook and code tests.
