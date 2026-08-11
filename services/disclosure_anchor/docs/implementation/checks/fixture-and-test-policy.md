# Fixture and test policy

## 1. Authority

Parser、Unit、publication 与 retrieval 行为不能由合成 fixture 或历史 golden 自行定义。
采用顺序固定为：当前产品契约 → 原始 PDF / provider artifact → 精确版本的官方
MinerU 行为 → 代表性真实样本 → 相邻合成正负例。

`normalized_ir` phase00 golden 与其再生成脚本已随旧 writer 删除；需要考古时使用 Git
历史。新测试不得把它们复制回工作树。

## 2. Deterministic default suite

`make agent-check` 在无数据库、无 provider 凭据的环境中必须可运行，包含：

- ruff；
- strict mypy；
- `unittest` no-DB suite；
- composition/import-death 审计；
- `git diff --check`。

测试使用 `unittest`。合成案例只用于闭合一个已由真实样本或明确契约确认的 invariant，
并同时包含相邻 negative case；禁止文档 ID、发行人、具体短语或页码特判。

## 3. Frozen provider corpus

冻结 MinerU 3.4.4 Hybrid-medium bundle 不入 Git。通过
`DISCLOSURE_MEDIUM_FROZEN_ROOT` 显式指向本机只读目录，运行：

```bash
DISCLOSURE_MEDIUM_FROZEN_ROOT=/private/tmp/<bundle-root> \
  .venv/bin/python -m unittest \
  tests.sample_corpus.test_provider_unit_builder_frozen
```

当前最小 source-identity replay 覆盖 Zhongke、Caitong、JiangHai full：

- PDF hash/物理页数与 canonical provider envelope；
- exact provider bundle reread equality；
- outline、logical table owner/stub、physical segment；
- Provider Unit、persisted search binding 与 HTML visible atoms。

页窗 artifact 或 failed run 只能用于 DB-free diagnostic/视觉检查，不得伪造成 admitted
full-PDF succeeded parse。

## 4. Visual review

`scripts/review_mineru_medium_outline.py` 只创建 `/private/tmp` 报告，不改 bundle、DB 或
公开数据。报告必须绑定 source PDF hash、provider bundle manifest、run evidence 和页域，
并对 blocks/headings/units/physical-table segments 做 exactly-once 清点。视觉判断与记录见
`mineru-medium-greenfield-visual-review.md`。

## 5. Opt-in integration and provider tests

数据库、真实 MinerU 或网络测试必须显式 opt-in。缺少以下输入时以具体理由 skip，不能读取
仓库内过期 path reference：

- `DISCLOSURE_MIGRATION_DATABASE_URL` / `DATABASE_URL`；
- `DISCLOSURE_MINERU_BIN`；
- `DISCLOSURE_TEST_<LABEL>_PDF`；
- 完整的 remote runtime identity 与 machine-local credentials。

DB 测试使用 scratch database/schema，清理自己的行，不写 sibling service schema。provider
测试不改变真实 worker、共享 AgentSSD、launchd 或公开 active run。

## 6. Acceptance

行为变更必须说明 failure family、一般 invariant、fail-closed boundary，并至少验证：

1. 一个代表性真实 filing；
2. 一个相邻 negative case；
3. content/source identity 守恒；
4. provider/v4 historical 分支未互相 fallback；
5. 不产生 fuzzy dedupe、cell repair、metadata-title 注入或第二文本 universe。

无法运行真实样本时必须记录准确 blocker，不得用合成绿灯替代完成声明。
