# Independent explicit-capacity inputs

`inputs.json` preserves the independently authored literal capacity profiles and
synthetic installer/collector observations used by the existing Windows test.
It is byte-identical to its original fixture, SHA256
`98d6003dc2c28a60eb617b9098d19afd84b9ce192441d7281c6cb131178c91c7`.
The four expected capacity-source digests are independent assertions, not values
regenerated from the implementation under test.

`scripts/windows/test_mineru_explicit_capacity_deployment.ps1` copies its seven
context files from the checkout under test into its disposable output directory.
The capacity codec and file reader come from their canonical application/adapter
paths; the remaining files come from `scripts/windows/mineru_heap_trim_compat`.
No historical patch generator or duplicate source context is stored here. The
explicit `SourceContext` parameter remains available for a separately pinned
test package. These fixtures and mocked tests do not deploy or contact a service.
