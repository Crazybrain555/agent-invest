# Frozen MinerU patch preimages

These files are exact upstream source preimages used by the SHA-pinned MinerU
3.4.4 compatibility-image build. They are test fixtures, not runtime code.

- `mineru/**`: OpenDataLab/MinerU tag `mineru-3.4.4-released`
  (`0dfc9460cd9ab693b9af60ae3fbffd7bc111b062`).
- `mineru_vl_utils/**`: OpenDataLab/mineru-vl-utils tag
  `mineru_vl_utils-1.0.5-released`
  (`cc467faaddb53d8b276cedf88f09302f540a7b83`).

`tests.unit.test_mineru_heap_trim_compat` verifies every file against
`TARGET_PREIMAGE_SHA256`, applies the real patch, and compiles every generated
source. Updating a fixture therefore requires an explicit source-identity and
patch-contract change.
