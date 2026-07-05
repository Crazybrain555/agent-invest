# envelope_kernel - Agent Operating Contract

Shared L1 `data_asset` envelope kernel for the 投研预测引擎 monorepo. Semantic authority is the engine
protocol at `../../docs/reference/投研预测引擎顶层框架协议_v0.7修订最终版本.md` — this package implements
§3.2 (envelope), §2.2 (kind matrix), §2.3 (`asset://` URI), §2.9 (source_tier / trace_level); it decides
nothing on its own. Cross-service norms live in the root `AGENTS.md`.

## Scope

In: envelope field model (`DataAsset`), kind enums + legal combination matrix, `asset://` URI build/parse,
`data_asset.v1` JSON schema export, and `validate_envelope()` for services to reuse in contract tests.

Out (deliberately): parsers, DB/API/storage code, evidence_record (L2 envelope), change-feed event models,
speculative abstraction. Runtime dependency is pydantic only; `jsonschema` is test-extra only. Keep it that way.

## Layout

```text
src/envelope_kernel/kinds.py      asset_kind / payload_kind / source_tier / trace_level + combination matrix
src/envelope_kernel/envelope.py   DataAsset model (six groups flattened; minimal required core; extra=forbid)
src/envelope_kernel/uri.py        asset:// URI rules (§2.3 hard rules)
src/envelope_kernel/contracts.py  schema export (`make export-contracts`) + validate_envelope()
contracts/data_asset.v1.json      exported artifact — never hand-edit; regenerate and let the contract test guard it
tests/                            unittest only (no pytest, no DB)
```

## Rules

1. Field/enum names and values follow the protocol and the disclosure_anchor public contract
   (e.g. `tier_0a`, `G0`); new fields go through the registered-extension discipline (§2.1), never ad hoc.
2. `extra="forbid"` on the model is the enforcement of that discipline — adding a field means editing the
   model, regenerating the contract, and updating tests in the same change.
3. Breaking changes to the envelope = new `data_asset.v2` contract per §2.7; do not mutate v1 semantics.
4. Validation gate: `make agent-check` (ruff + mypy + unittest + `git diff --check`). Services consume this
   package via a relative-path editable install (`pip install -e ../../packages/envelope_kernel`).
