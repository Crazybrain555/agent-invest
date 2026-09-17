# Independent explicit-capacity inputs

`inputs.json` preserves the independently authored literal capacity profiles and
synthetic installer/collector observations used by the existing Windows test.
The original fixture SHA256 was
`98d6003dc2c28a60eb617b9098d19afd84b9ce192441d7281c6cb131178c91c7`.
Its source-identity assertions were independently reviewed and updated after the
shared environment/HTTP projection extraction and the earlier pressure-observation
addition. The capacity profiles, expected startup arguments, limits and simulated
results are unchanged. Current fixture SHA256 is
`cbc7d9c57ae5d90207d356bdcb7cc24ddabe226d88d884f179b95bbab12d88f2`.
The four source digests and their aggregate remain literal independent assertions;
the test never regenerates expected values from the implementation under test.

`scripts/windows/test_mineru_explicit_capacity_deployment.ps1` copies its seven
context files from the checkout under test into its disposable output directory.
The capacity codec and file reader come from their canonical application/adapter
paths; the remaining files come from `scripts/windows/mineru_heap_trim_compat`.
No historical patch generator or duplicate source context is stored here. The
explicit `SourceContext` parameter remains available for a separately pinned
test package. These fixtures and mocked tests do not deploy or contact a service.
