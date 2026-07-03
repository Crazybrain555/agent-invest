---
id: disclosure_anchor_milestone_00_local-environment-and-parser-validation
project: disclosure_anchor
title: 本地环境与样本 parser 验证
status: complete
created_at: 2026-06-26
---

# Milestone 00: 本地环境与样本 parser 验证

> 实施状态：已完成（acceptance-matrix A01–A05 pass；Phase 00 产出的样本解析 fixtures
> 由 Phase 03/04 测试持续消费，runbook 见 `runbooks/phase00-environment-and-parser-validation.md`）。

## 1. 目标

确认 macOS + 外置盘 + native PostgreSQL + MinerU native 环境可跑，并用少量真实 PDF 验证 parser 输出能被转换成后续 `NormalizedIR` / `document_unit` fixture。

## 2. 范围

本阶段是 pre-v1 门槛，不建设完整应用。

范围内：

- 建立 `/Volumes/AgentSSD/agent_system` 目录骨架。
- 初始化 native PostgreSQL `PGDATA`。
- 安装 MinerU native 环境。
- 将模型缓存指向外置盘。
- 用 3 类 PDF 样本验证：年报、投关记录、短公告。
- 保存样本 parser output、normalized IR fixture、document_units fixture。



## 3. 实施细则

1. 创建外置盘根目录和 sentinel：

```text
/Volumes/AgentSSD/agent_system/MOUNT_SENTINEL_DO_NOT_CREATE_ON_INTERNAL
```

2. 创建共享模型缓存：

```text
/Volumes/AgentSSD/agent_system/shared/model_cache/mineru
/Volumes/AgentSSD/agent_system/shared/model_cache/huggingface
/Volumes/AgentSSD/agent_system/shared/model_cache/modelscope
```

3. 配置环境变量，确保模型不进入 `~/.cache`。
4. 初始化 PostgreSQL cluster，但本阶段不要求建完整 schema。
5. 选 3 类样本 PDF：

```text
annual_report.pdf
ir_activity.pdf
short_announcement.pdf
```

6. 使用 MinerU 生成原始输出。
7. 手工或临时代码转换成 `normalized_ir.v1.json` fixture。
8. 手工或临时代码生成 `document_units.v1.jsonl` fixture。
9. 肉眼检查：章节、表格、Q&A 是否基本可用。


## 4. 检查点

- 外置盘挂载存在。
- `MOUNT_SENTINEL_DO_NOT_CREATE_ON_INTERNAL` 存在。
- PostgreSQL 可以启动和停止。
- MinerU 可以解析 3 类样本。
- 样本输出路径在外置盘或 repo fixtures，不落入系统默认 cache。
- 样本生成：

```text
normalized_ir.v1.json
document_units.v1.jsonl
```

- 年报至少能看到一个 text unit 和一个 table unit。
- 投关记录至少能看到一个 qa unit。


## 5. Definition of Done

- 有一份 `reports/phase00-parser-validation.md`。
- 3 个样本 fixture 可被后续测试使用。
- 外置盘目录和模型缓存路径确认无误。
- PostgreSQL native cluster 可启动。


## 6. 明确不做

- 不接 CNINFO。
- 不建完整 DB schema。
- 不写正式 API。
- 不写 L2 claim。
- 不调优 parser 质量。


## 7. 交付给下一阶段

- 外置盘目录骨架。
- 可用 PostgreSQL native 环境。
- MinerU native 环境。
- 样本 parser output / IR / units fixture。
- parser 质量初步报告。


## 8. 常见失败与处理

- MinerU 无法安装：记录 Python 版本、依赖、完整错误，不进入 Phase 01。
- 模型下载到内置盘：清理并重新设置缓存变量。
- 年报表格完全不可用：保留样本与错误，不调整数据模型，先形成 parser 风险记录。
- PostgreSQL 无法写外置盘：停止，修正权限或路径。
