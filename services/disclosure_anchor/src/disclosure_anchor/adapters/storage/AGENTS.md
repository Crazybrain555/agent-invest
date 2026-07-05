# adapters/storage — 文件存储（不可变归档 + 原子派生）

```text
path_builder.py        FileStorePathBuilder 是**唯一**路径生成入口（组件消毒、逃逸防护、
                       只产相对路径；实际落盘 = data_root/"data"/<relpath>，注意有 data/ 一层）
raw_document_store.py  原始 PDF 不可变归档：tmp 写入+fsync → hardlink 防覆盖 → 写后重哈希
                       → %PDF- 魔数校验；quarantine 走 runtime/quarantine + manifest
artifact_store.py      派生物原子写（write_json_atomic / write_jsonl_atomic，返回 hash/字节数）
```

硬规则：任何新路径需求先加 path_builder 方法 + 测试，严禁在 store/use case 里拼路径；
raw 只追加不覆盖（provider_document_id 换文件 = 新版本 + supersedes，不是覆盖）；
绝对路径不入库不出 API（DB 里只存 relpath / basename）。
