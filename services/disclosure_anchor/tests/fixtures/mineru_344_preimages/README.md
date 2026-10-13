# Frozen MinerU patch preimages

These files are exact upstream source preimages for the SHA-pinned MinerU 3.4.4
compatibility-image build and its independent writer tests. They are test
fixtures, not runtime code.

- `mineru/**`: OpenDataLab/MinerU tag `mineru-3.4.4-released`
  (`0dfc9460cd9ab693b9af60ae3fbffd7bc111b062`).
- `mineru_vl_utils/**`: OpenDataLab/mineru-vl-utils tag
  `mineru_vl_utils-1.0.5-released`
  (`cc467faaddb53d8b276cedf88f09302f540a7b83`).

`tests.unit.test_mineru_heap_trim_compat` verifies every patch-target file against
`TARGET_PREIMAGE_SHA256`, applies the real patch, and compiles every generated
source. Updating a fixture therefore requires an explicit source-identity and
patch-contract change.

The sources are byte-exact and intentionally preserve any upstream trailing
whitespace. The service `.gitattributes` disables Git's whitespace diagnostic
only for this fixture tree; changing or broadening that exception is guarded by
the same unit test that verifies the source hashes.

Two additional upstream writer preimages support the independent generated
DataWriter boundary test; they are not patch targets or installed image files.
Both were copied byte-for-byte from the official MinerU 3.4.4 wheel
`mineru-3.4.4-py3-none-any.whl` (SHA256
`d4d678539782a7683d998e2914a52d96b5720676ce65658b29666b1f4d9dfd13`),
under the `mineru/**` upstream provenance above:

- `mineru/data/data_reader_writer/base.py`: SHA256
  `85eac3891bb6dc3be171dc6d5a18abd9a8cb1b592458fd218d68e4c255999803`.
- `mineru/data/data_reader_writer/filebase.py`: SHA256
  `c047bfd6a588095bf68c0c50204f10c9a6bce2d014a6065f26dc241acbe03e2c`.

`tests.unit.test_native_storage_endpoint_independent` verifies these hashes
before executing their class definitions with the generated `common.py` writer.
