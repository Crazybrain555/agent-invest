# 生产运维 Runbook（disclosure_anchor，单机单人）

2026-07-14 上线加固批次（batch 4）建立。读者是三个月后忘光细节的运维者本人。
配置生效矩阵见 `config/README.md`；健康一眼看 `make doctor-full` + `make worker-status`。

## 1. 开机 / 重启顺序

正常情况全自动：`com.agentinvest.postgres`（launchd 一次性 pg_ctl start，等 AgentSSD 挂载）
→ `com.agentinvest.disclosure-worker`（KeepAlive 常驻）。人工核对：

```bash
launchctl list | grep agentinvest     # 三个 job：postgres(一次性)/doctor(定时)/worker(常驻)
make pg-status && make doctor-full    # exit 0 才算活
make worker-status
```

手工恢复（自动链路失效时）：`make pg-start` → `make worker-restart` → `make doctor-full`。
launchd job 丢失时重装：`make install-ops-launchd`（postgres+doctor）、
`./scripts/install_launchd.sh`（worker）。

## 2. 告警通道

- 每日 18:30 `com.agentinvest.disclosure-doctor` 跑 `scripts/doctor_daily.sh`：
  doctor FAIL、交易日 18:00 后 24h 零新增（freshness）→ macOS 通知。
- worker 每轮：source 断供或单轮失败 ≥5 → macOS 通知（每小时同题限流）。
- 通知历史落 `$DISCLOSURE_RUNTIME_ROOT/notify-markers/alerts.log`（错过弹窗看这里）。

## 3. MinerU 端点故障（实案：2026-07-12，45 个 parse 失败）

症状：worker 报告 parse 失败堆积，`processing_run.error` 为
`parser_invocation_failed` + `httpx.ConnectTimeout`（远端 VLM 端点，如 100.107.19.82:30000）。
处置：确认端点恢复（`curl -m 5 <server_url>/health` 或问 GPU 机器）→ 什么都不用做，
worker 按重试预算自动重解析。恢复核对（应为 0 且失败文档最终 published）：

```sql
SELECT count(*) FROM disclosure_ops.pending_parse_v1 WHERE failed_parse_count > 0;
```

## 4. CNINFO 配额 / 封禁

症状：报告 `sync_quota_break: True`（配额熔断，next round 冷却 30→120 分钟自适应）或
`source_outage_break: True`（HTTP 层故障）。处置：配额熔断不用动，等冷却；
持续 outage 先 `curl webapi.cninfo.com.cn` 判断网络/封禁，凭据问题看
`~/.config/agent-invest/disclosure_anchor/cninfo.env`（轮换后要 `make worker-restart`）。
兜底：`make sync COMPANY=x` 走 `--channel web` 免凭据通道验证是否仅 WebAPI 侧故障。

## 5. 三类死信处置

| 死信 | 找到它 | 处置 |
|---|---|---|
| parse 重试耗尽 / 不可重试 | doctor `parse dead letters` WARN；`pending_parse_v1.last_failed_retryable=false` | 查 `processing_run.error` 根因；修复后 `make process DOC=<id>` 手动重跑 |
| 空发布（0 unit） | doctor `empty publish dead letters`（实存案例：美的 3 篇「日常关联交易预计」，疑似表格型盲区） | 人工看原 PDF：确属无正文可切 → `make publish RUN=<id> ALLOW_EMPTY=1 REASON=...`；是切分盲区 → 修规则后 `make rebuild-units DOC=<id>` |
| 超大文件排除 | doctor `oversized parse exclusions`（实存案例：万科A 2023 年报、招商银行 2025 年报） | 年报是核心文件，不应长期滞留：确认 `CNINFO_OVERSIZED_KB` 上限（worker.env）→ 提高后 `make worker-restart`，或单独 `make process DOC=<id>` 试解析并观察 MinerU 内存 |

下载类死信（新增 2026-07-14）：`invalid_candidate_snapshot` / `raw_archive_error` /
`subject_identity_conflict` 等 retryable=false 的下载失败永久出队，证据在
`source_access(status='failed')` 与 quarantine 目录（含 sha256 manifest）。

## 6. TCC / launchd 假死

worker 以 exit 77 自杀 = TCC 拒绝访问外置盘（详见 `scripts/run_worker_once.sh` 头部注释）。
处置：系统设置 → 隐私与安全性 → 完全磁盘访问 给 `/bin/zsh`（或按注释操作），然后
`make worker-restart`。KeepAlive 30 秒节流重启属预期，不要手工 bootout。

## 7. 磁盘与产物治理

- doctor 有双卷剩余空间检查（<10% WARN）。
- 孤儿解析产物：`make gc-orphans`（dry-run 盘点，2026-07-14 实测 8,174 文件 / 1.48 GiB，
  全为被 supersede 的旧 parse run 产物）；确认后 `make gc-orphans APPLY=YES`
  （删除前自动写 manifest 到 audit/gc/）。原始 PDF 永不在 GC 范围内。

## 8. 数据质量巡检（周节律）

`make audit-weekly` = 未映射码 + 样板公告 + 标题吞没三项审计，任一非零退出即有真 finding。
词表升级流程：改 JSON + 升版本 + `make load-rules`（见 adapters/sources/cninfo 的词表工程原则）。

## 9. 备份与恢复（占位，待新备份盘）

当前 PG 集群与 raw 档案同在 AgentSSD——单盘故障即全损，这是已知的最大风险敞口
（用户决定：等新盘到位再做每日 pg_dump + raw rsync + 恢复演练；本节到位后补全步骤）。

## 10. 危险边界（不要做的事）

- `make purge-company` / `make wipe-test-data`：测试期工具，级联删行+删文件，生产禁用。
- `untrack` 是退订（保留全部文档档案），`paused` 是可逆暂停——想停采集永远先用 paused。
- 已应用迁移一律冻结；改视图/约束开新迁移。
- admin API 需要 `DISCLOSURE_ADMIN_TOKEN`（Bearer）且仅回环可用；token 在 worker.env，
  轮换用 `openssl rand -hex 32` 换值后 `make worker-restart` + 重启 API。
