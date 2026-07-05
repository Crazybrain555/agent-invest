# envelope-kernel

投研预测引擎 L1 `data_asset` 信封的最小共享核：信封字段模型（协议 §3.2 六组）、asset_kind/payload_kind
枚举与合法组合矩阵（§2.2）、`asset://` URI 规则（§2.3）、source_tier/trace_level 枚举（§2.9）、
`data_asset.v1` JSON schema（从代码导出）与契约校验入口。

语义权威是根 `docs/reference/` 下的引擎协议 v0.7；本包只实现、不发明。运行时依赖仅 pydantic。

```bash
make agent-check        # ruff + mypy + unittest + git diff --check
make export-contracts   # 重新导出 contracts/data_asset.v1.json（契约测试守护逐字节一致）
```

服务以相对路径可编辑依赖引用：`pip install -e ../../packages/envelope_kernel`。
