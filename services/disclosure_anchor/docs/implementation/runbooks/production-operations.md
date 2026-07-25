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

### 1.1 首次从旧 worker 切换到当前 plist

这条路径只用于旧 job 的有效 `ExitTimeOut` 仍小于 60 秒、且旧代码还不认识
`parser_cancelled` 的首次切换。此时禁止 `kickstart -k` 或让安装脚本自动 bootout；
否则 launchd 会在 5 秒后强杀长文档，既可能留下 MinerU 临时 API，也会消耗业务重试。

1. 先通过本分支全部发布门，再写入 disabled 标记，但不终止当前进程：

   ```bash
   WORKER_DOMAIN="gui/$(id -u)"
   WORKER_LABEL="com.agentinvest.disclosure-worker"
   launchctl disable "$WORKER_DOMAIN/$WORKER_LABEL"
   ```

   这只是安装前置状态，不是互斥保证。2026-07-25 首次切换实测：一个已经 loaded
   的 KeepAlive job 仍可在 Python 子进程退出后、`bootout` 前立即重拉 wrapper。
   因此不得在下文安全零点先单独终止 Python；真正阻止重拉的是对 loaded job 的
   `bootout`。

2. 等旧波次自然排空。三个条件必须同时为零：

   - `disclosure_core.processing_run` 中 `run_kind='parse' AND status='running'`；
   - `pgrep -afil '/bin/mineru -p |mineru.cli.fast_api'` 的旧 MinerU/临时 API；
   - vLLM `/metrics` 的 `vllm:num_requests_running` 和
     `vllm:num_requests_waiting`（先排除其他合法客户端）。

3. 零点可能很短。`launchctl` 显示的是 zsh wrapper PID，不能停它（Python 子进程会继续
   补槽）；必须解析且只接受它唯一的直接 `disclosure_anchor.cli.worker loop` 子进程。
   对 **Python PID** 先 `SIGSTOP` 冻结，再重复核对上述三个条件；若任一非零，
   `SIGCONT` 后继续等。三者仍为零时，保持 Python 为 STOP 并直接移除整个 loaded job：

   ```bash
   WRAPPER_PID="$(launchctl print "$WORKER_DOMAIN/$WORKER_LABEL" |
     awk '/pid =/{print $3; exit}')"
   PYTHON_PID="$(pgrep -P "$WRAPPER_PID" -f \
     'disclosure_anchor.cli.worker loop')"
   case "$PYTHON_PID" in
     ""|*$'\n'*) echo "expected exactly one worker Python child" >&2; exit 1 ;;
   esac
   kill -STOP "$PYTHON_PID"
   # 在进程保持 STOP 时重新核对 PG、MinerU/API、vLLM 三个零条件。
   # 任一非零：kill -CONT "$PYTHON_PID"，继续等待；不得 bootout。
   #
   # 三项仍为零：保持 Python 为 STOP，直接移除整个 loaded job。不要先
   # TERM/CONT Python；否则旧 KeepAlive 可在 bootout 前重拉一轮新任务。
   launchctl bootout "$WORKER_DOMAIN/$WORKER_LABEL"
   while launchctl print "$WORKER_DOMAIN/$WORKER_LABEL" >/dev/null 2>&1; do
     sleep 1
   done
   # 再确认 wrapper/Python/MinerU/API 均不存在，且 PG/vLLM 仍为零。
   ./scripts/install_launchd.sh
   ```

4. 模板请求 `ExitTimeOut=90`；确认新 loaded job 的**有效值至少 60 秒**，再按动态调度
   设计 §8.2 做启动即验。2026-07-25 当前 macOS 对 user LaunchAgent 实测把 plist 的
   90 秒请求报告为 60 秒；安装器校验有效下界而不是假设请求值会原样呈现。
   安装脚本发现 label 仍 loaded，或仍有 MinerU CLI/临时 API 进程，都会退出 75；这是安全
   保护，不得绕过。

完成首次切换后，常规代码/env 重载才使用 `make worker-restart`；新 worker 的取消是
retry-neutral，且有效值至少 60 秒（worker 自身 graceful window 为 35 秒）会给官方
cleanup 路径和 wrapper 回收留出余量。

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
| HUGE lane 长任务 | worker report 的 `parse_huge_dispatched` 与 processing_run 时长；不再有大小排除死信 | 以归档 actual byte_count/页数核对成本；正常长任务继续运行，只在极端 whole-future runaway 时由 launchd 监督重启 |

下载类死信（新增 2026-07-14）：`invalid_candidate_snapshot` / `raw_archive_error` /
`subject_identity_conflict` 等 retryable=false 的下载失败永久出队，证据在
`source_access(status='failed')` 与 quarantine 目录（含 sha256 manifest）。

## 6. TCC / launchd 假死

worker 以 exit 77 自杀 = TCC 拒绝访问外置盘（详见 `scripts/run_worker_once.sh` 头部注释）。
处置：系统设置 → 隐私与安全性 → 完全磁盘访问 给 `/bin/zsh`（或按注释操作），然后
`make worker-restart`。KeepAlive 30 秒节流重启属预期；除 §1.1 首次 staged cutover 外，
不要手工 bootout。

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
