---
id: disclosure_anchor_design_review_note
project: disclosure_anchor
title: 附件版本与当前最终版的差异判断
status: final-note
created_at: 2026-06-26
---

# 附件版本与当前最终版的差异判断

## 1. 判断结论

附件版和上一版的架构判断基本一致，不存在需要推翻的方向性差异。

最终版采用附件版作为正文基底，因为它更适合作为第一份实施框架：

- 结论先行，读者更容易抓住已锁定决策；
- 服务边界、对外契约、数据所有权、部署位置都写得集中；
- milestone 顺序已经接近后续 agent 执行路径；
- 对成熟项目的参考只落到少量工程调整，没有把项目带向重型架构。

但是附件版仍保留了较多“备选方案表”。在用户已经确认 PostgreSQL、MinerU、PG cluster 三项主路径后，这些备选表对实施 agent 的价值较低，反而容易引发不必要的分叉。因此最终版删去备选方案表，只保留：

```text
已锁定决策
+ 不做事项
+ 实施边界
+ 后续什么信号出现时再调整
```

## 2. 采纳附件版的地方

最终版采纳附件版中的以下内容：

1. `runtime/inbox`、`runtime/quarantine`、`runtime/failed` 的分工。
2. `doctor` / sanity 检查作为最小运维闭环。
3. `processing_run` 不回改，`document.current_processing_run_id` 只是当前默认视图。
4. `contracts/` 与 `disclosure_public.*_v1` 从第一天版本化。
5. FastAPI app / PostgreSQL / batch worker / MinerU 的进程边界。
6. `document + processing_run + document_unit + source_ref + change_event` 的对外契约。
7. `raw_file_hash / artifact_hash / content_hash / structure_hash / content_hash_aggregate` 分离。
8. Phase 0 到 Phase 8 的实施顺序。

## 3. 从上一版保留并强化的地方

最终版从上一版保留以下更硬的工程约束：

1. `application/ports/unit_of_work.py` 明确作为事务边界，不使用含糊的 `transaction.py`。
2. `FileStorePathBuilder` 是唯一生成 raw / parser / derived 路径的组件，业务代码不得手写路径。
3. DB 只存相对路径、hash、状态、payload snapshot 和 artifact locator，不对外暴露绝对路径。
4. 启动时如果 `/Volumes/AgentSSD` 未挂载，必须 fail closed，不允许退回内置盘。
5. 兄弟服务只读 API 或 `disclosure_public` 视图，不得读写 `disclosure_core`。

## 4. 删减的内容

最终版主动删减：

- Docker PG、每服务独立 PG instance 等备选路线的展开；
- Airflow / Superset 等成熟项目的长篇介绍；
- 过早的性能优化、物化视图、全文索引、常驻 MinerU HTTP 服务；
- L2-L6 数据结构细节；
- 备份、恢复、离线存储设计。

这些内容不是错，而是当前实施文件不需要。

## 5. 最终收口

最终工程文件的目标不是证明方案“万无一失”，而是让后续 agent 可以按阶段实现，并且每一步都能被检查。

因此本包采用：

```text
总框架文件
+ roadmap
+ agent 执行规则
+ 9 个 milestone 文件
+ 4 个检查清单
```
