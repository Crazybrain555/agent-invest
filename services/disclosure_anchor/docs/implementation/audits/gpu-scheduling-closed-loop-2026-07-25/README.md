# GPU scheduling evidence — 2026-07-25

本文只保存能重跑的最小证据，不保存大 HTML、截图或派生 CSV。所有数据库采集均为只读；
时区固定为 `Asia/Shanghai`。历史线上基线和未部署候选必须分开解释。

## 1. 身份、窗口与已观察结果

| 证据 | 时间窗（+08:00） | 直接观察 |
|---|---|---|
| DB 波形基线 | 2026-07-25 05:45:05–11:45:05 | parse start 959、finish 946；完成按 `finished_at` |
| vLLM 短窗 | 2026-07-25 11:49:27–11:50:50 | 13 个有效 gauge：running 1–127、waiting 0–151；health 20/20 HTTP 200 |
| eligible backlog | 2026-07-25 11:53:52 | 当时 live worker 谓词为 19,878；这是单点，不是历史序列 |
| 当前补充快照 | 2026-07-25 16:38:41 | raw pending 20,335、候选谓词 eligible 19,669、running 10 |
| active IR 扫描 | DB 16:38:59；扫描结束 16:39:26 | 19,782 true、0 false、0 missing/unreadable |
| vLLM 补充横截面 | 2026-07-25 16:34:20 | health 200/0.316s、running 121、waiting 40、preemptions 0 |
| `dcf7014` 正式安装波次 | 19:43:53–20:21:21 | parsed/built/published 55/55/55；一次 readiness 假阴性停止滚动准入 |
| readiness 放大空谷 | 19:52:29–20:24:37 | 32.14 分钟无新 parse start；eligible backlog 仍为 19,887 |

05:45 基线运行的是 primary commit
`3c7ef821922f39975a4b889738ea823052c1a648`。基础动态调度后来 squash 为
`dcf7014`；19:43:53 的正式安装波次由 startup banner 和真实子进程命令确认运行该代码。
该波次又发现 readiness 连续失败控制尚未闭合，后续修正的发布身份仍必须以最终 commit、
startup banner 和真实进程为准。

本机解析端为 MinerU 3.4.0、`mineru-vl-utils` 1.0.5。已安装源文件校验：

```text
d57a4e17f1f0397247239fc52c1a6b3fa555e7107cb2edda8d6fa0b56e55275c  mineru/cli/client.py
0fadf7a94ae702861b4a1fa7f42358c6687cfc63fbe322c004fb1d3248658390  mineru/backend/vlm/vlm_analyze.py
f7f233d86ae0f5aab6ffe5d8eccef4344c968aeaf879563dae99d4875057ee39  mineru/cli/fast_api.py
```

2026-07-25 的远端 `/version` 返回 vLLM `0.21.0`。该 HTTP 端点没有暴露
`max_num_seqs`；短窗中 running 达 127 且同时有 waiting，支持“约 128 的调度容量”这一
运营判断，但不等于服务启动参数的直接证明。发布时仍须把 128 当作待复核的运营配置。

身份复核命令：

```bash
date '+%FT%T%z'
git rev-parse HEAD
git status --short
source ~/.config/agent-invest/disclosure_anchor/worker.env
"$DISCLOSURE_MINERU_BIN" -v
curl -fsS --noproxy '*' --connect-timeout 3 --max-time 8 \
  "$DISCLOSURE_MINERU_SERVER_URL/version"
```

### 1.1 上线 A/B 新发现：单次 readiness 仍会放大成锯齿

`dcf7014` 已解决 1,600 级请求突发，但真实上线证明“一次 readiness 失败立即结束
refill”仍是独立的锯齿发生器：

- 19:43:58 很快填满 16 个 parse 槽，随后持续滚动启动；
- 19:52:29 后停止新启动，直到 20:24:37 才恢复，一共 32.14 分钟；
- 这段时间不是完全卡死：仍有 16 份成功，最后一份到 20:20:35；55 份成功 run 最终
  `normalized_ir_relpath`、`document_units_relpath`、`is_active=true` 全部齐全；
- round 20:21:21 报告 55/55/55，唯一共享控制错误是
  `parser_readiness_failed`；另一个 `parser_task_failed` 在 19:56:08 才发生，晚于停止
  准入 3 分 39 秒，不能解释停止准入；
- 20:33 复核 broad backlog 19,978、完整 scheduler predicate eligible 19,887，排除任务不足；
- 空谷尾部 vLLM 为 `running=0–17, waiting=0`，说明旧洪峰已被压住；但单次探测失败让
  16 份大文档自然排空，形成逐步下坡，再叠加 120 秒 parse cooldown。

同一窗口 `/health` 既有 200，也有 connect timeout；后端进程没有重启，成功请求计数仍
增长。因此它是 readiness 假阴性/瞬时网络抖动，不是持续 GPU 宕机。修正采用成熟 probe
语义：探测失败时先暂停新 admission，5 秒后原地重试；连续三次才报告 outage，任一成功
清零计数。Kubernetes readiness 的默认 `failureThreshold` 也是 3，且失败后继续探测，
而不是把一次失败升级成进程 liveness 结论。

时间线可用下列只读 SQL 和 worker report 复核：

```sql
BEGIN TRANSACTION READ ONLY;
SET LOCAL TIME ZONE 'Asia/Shanghai';
SELECT min(started_at), max(started_at), count(*)
  FROM disclosure_core.processing_run
 WHERE run_kind = 'parse'
   AND started_at >= timestamptz '2026-07-25 19:43:53+08'
   AND started_at <  timestamptz '2026-07-25 20:24:37+08';
SELECT max(started_at) AS admission_stopped,
       max(finished_at) FILTER (WHERE status = 'succeeded') AS tail_finished,
       count(*) FILTER (WHERE status = 'succeeded') AS succeeded,
       count(*) FILTER (WHERE status = 'failed') AS failed
  FROM disclosure_core.processing_run
 WHERE run_kind = 'parse'
   AND started_at >= timestamptz '2026-07-25 19:43:53+08'
   AND started_at <  timestamptz '2026-07-25 20:24:37+08';
ROLLBACK;
```

```bash
rg -n '^## run|duration_seconds|parsed:|built:|published:|failed:|parser_readiness_failed' \
  /Volumes/AgentSSD/agent_system/services/disclosure_anchor/runtime/logs/worker-20260725.log
```

## 2. DB 波形、在途与失败

完成时间只能使用 `finished_at`。早期把 `created_at` 当完成时间的直方图不再作为证据。

```sql
BEGIN TRANSACTION READ ONLY;
SET LOCAL TIME ZONE 'Asia/Shanghai';
SELECT now() AS db_snapshot;

WITH bounds AS (
  SELECT timestamptz '2026-07-25 05:45:05+08' AS from_ts,
         timestamptz '2026-07-25 11:45:05+08' AS to_ts
), minutes AS (
  SELECT generate_series(
           date_trunc('minute', from_ts),
           date_trunc('minute', to_ts),
           interval '1 minute'
         ) AS minute,
         from_ts,
         to_ts
    FROM bounds
)
SELECT to_char(minute, 'YYYY-MM-DD HH24:MI') AS minute_shanghai,
       (SELECT count(*)
          FROM disclosure_core.processing_run r
         WHERE r.run_kind = 'parse'
           AND r.started_at >= greatest(m.minute, m.from_ts)
           AND r.started_at < least(
                 m.minute + interval '1 minute', m.to_ts
               )) AS started,
       (SELECT count(*)
          FROM disclosure_core.processing_run r
         WHERE r.run_kind = 'parse'
           AND r.finished_at >= greatest(m.minute, m.from_ts)
           AND r.finished_at < least(
                 m.minute + interval '1 minute', m.to_ts
               )) AS finished,
       (SELECT count(*)
          FROM disclosure_core.processing_run r
         WHERE r.run_kind = 'parse'
           AND r.started_at < least(
                 m.minute + interval '1 minute', m.to_ts
               )
           AND (
             r.finished_at IS NULL
             OR r.finished_at >= least(
                  m.minute + interval '1 minute', m.to_ts
                )
           )) AS in_flight_at_end
  FROM minutes m
 ORDER BY minute;

SELECT processing_run_id, document_id, started_at,
       round(extract(epoch FROM (now() - started_at)) / 60, 1) AS age_min
  FROM disclosure_core.processing_run
 WHERE run_kind = 'parse' AND status = 'running'
 ORDER BY started_at;

SELECT date_trunc('hour', finished_at) AS hour,
       coalesce(error->>'error_code', '?') AS error_code,
       coalesce(error->>'retryable', '?') AS retryable,
       count(*)
  FROM disclosure_core.processing_run
 WHERE run_kind = 'parse'
   AND status = 'failed'
   AND finished_at >= timestamptz '2026-07-25 05:45:05+08'
   AND finished_at <  timestamptz '2026-07-25 11:45:05+08'
 GROUP BY 1, 2, 3
 ORDER BY 1, 4 DESC;

-- 这是 raw 事实视图，不等于 scheduler-eligible。
SELECT count(*) AS raw_pending_parse
  FROM disclosure_ops.pending_parse_v1;
ROLLBACK;
```

`eligible pending` 必须复用被审计 source commit 的完整
`queries.pending_parse` 谓词；raw view 会包含 oversized、不可重试或预算耗尽项。服务根目录
可用：

```bash
source ~/.config/agent-invest/disclosure_anchor/worker.env
PYTHONPATH=src .venv/bin/python - <<'PY'
from sqlalchemy import text
from disclosure_anchor.adapters.db.postgres.connection import (
    app_database_url,
    create_db_engine,
)
from disclosure_anchor.application.worker import queries
from disclosure_anchor.cli.worker import _process_scope_classes
from disclosure_anchor.settings import load_settings

s = load_settings()
engine = create_db_engine(app_database_url(s))
with engine.connect() as conn:
    conn.execute(text("SET TRANSACTION READ ONLY"))
    print("db_snapshot", conn.execute(text("SELECT now()")).scalar_one())
    rows = queries.pending_parse(
        conn,
        max_retries=s.disclosure_max_parse_retries,
        limit=2147483647,
        scope_classes=_process_scope_classes(s),
    )
    print("eligible", len(rows))
engine.dispose()
PY
```

500+ 页 attempt 和 invocation failure 占槽分钟使用同一完成窗，并对当时 946 个带 raw path 的
attempt 重放物理页数；结果为 500+ 页 succeeded 7、failed 9，10 个
`parser_invocation_failed` 合计 359.3 分钟，另有 1 个历史 raw path 已不可读：

```bash
source ~/.config/agent-invest/disclosure_anchor/worker.env
PYTHONPATH=src .venv/bin/python - <<'PY'
from collections import Counter
from pathlib import Path
from sqlalchemy import text
from disclosure_anchor.adapters.db.postgres.connection import (
    app_database_url,
    create_db_engine,
)
from disclosure_anchor.adapters.parsers.pdf_page_probe import count_pdf_pages
from disclosure_anchor.settings import load_settings

s = load_settings()
engine = create_db_engine(app_database_url(s))
with engine.connect() as conn:
    conn.execute(text("SET TRANSACTION READ ONLY"))
    rows = conn.execute(text("""
        SELECT pr.status, pr.started_at, pr.finished_at,
               coalesce(pr.error->>'error_code', '') AS error_code,
               d.raw_file_relpath
          FROM disclosure_core.processing_run pr
          JOIN disclosure_core.document d
            ON d.document_id = pr.document_id
         WHERE pr.run_kind = 'parse'
           AND pr.finished_at >= timestamptz '2026-07-25 05:45:05+08'
           AND pr.finished_at <  timestamptz '2026-07-25 11:45:05+08'
           AND d.raw_file_relpath IS NOT NULL
    """)).mappings().all()
engine.dispose()

counts = Counter()
invocation_minutes = 0.0
for row in rows:
    path = Path(s.disclosure_data_root) / "data" / row["raw_file_relpath"]
    try:
        pages = count_pdf_pages(path)
    except (FileNotFoundError, OSError, ValueError, RuntimeError):
        counts["unreadable_path"] += 1
        continue
    if pages >= 500:
        counts[f"500_plus_{row['status']}"] += 1
    if row["error_code"] == "parser_invocation_failed":
        counts["invocation_failed"] += 1
        invocation_minutes += (
            row["finished_at"] - row["started_at"]
        ).total_seconds() / 60
print(dict(counts), round(invocation_minutes, 1))
PY
```

## 3. vLLM queue 与 health

```bash
source ~/.config/agent-invest/disclosure_anchor/worker.env
for i in {1..20}; do
  date '+%FT%T%z'
  curl -fsS --noproxy '*' --connect-timeout 3 --max-time 8 \
    "$DISCLOSURE_MINERU_SERVER_URL/metrics" |
    rg '^vllm:(num_requests_running|num_requests_waiting|kv_cache_usage_perc|num_preemptions_total)(\{|[[:space:]])'
  curl -sS --noproxy '*' --connect-timeout 3 --max-time 8 \
    -o /dev/null -w 'health=%{http_code} latency=%{time_total}s\n' \
    "$DISCLOSURE_MINERU_SERVER_URL/health"
  sleep 3
done
```

health 200 只说明服务能应答，不说明没有排队；running/waiting 也不是硬件 GPU 百分比。
若要引用另一时点的 vLLM 版本、waiting 401+ 或 `--max-num-seqs=128`，必须另附该时点
服务启动命令/日志，不能混进上面的 2026-07-25 短窗。

## 4. active IR `full_pdf` 审计

DB 范围是所有 active、succeeded、带 `normalized_ir_relpath` 的 run；既包括普通 parse，
也包括复用原 IR 的 `rebuild_units` active run。

```bash
source ~/.config/agent-invest/disclosure_anchor/worker.env
PYTHONPATH=src .venv/bin/python - <<'PY'
import json
from collections import Counter
from pathlib import Path
from sqlalchemy import text
from disclosure_anchor.adapters.db.postgres.connection import (
    app_database_url,
    create_db_engine,
)
from disclosure_anchor.settings import load_settings

s = load_settings()
engine = create_db_engine(app_database_url(s))
with engine.connect() as conn:
    conn.execute(text("SET TRANSACTION READ ONLY"))
    snapshot = conn.execute(text("SELECT now()")).scalar_one()
    rows = conn.execute(text("""
        SELECT document_id, normalized_ir_relpath
          FROM disclosure_core.processing_run
         WHERE is_active
           AND status = 'succeeded'
           AND normalized_ir_relpath IS NOT NULL
         ORDER BY document_id
    """)).mappings().all()
engine.dispose()

counts = Counter()
for row in rows:
    path = (
        Path(s.disclosure_data_root)
        / "data"
        / row["normalized_ir_relpath"]
    )
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        value = payload.get("parsed_pages", {}).get("full_pdf", "missing")
        key = str(value).lower() if isinstance(value, bool) else str(value)
        counts[key] += 1
    except Exception:
        counts["unreadable"] += 1
print("db_snapshot", snapshot)
print("rows", len(rows), "counts", dict(sorted(counts.items())))
PY
```

这只排除 active IR 合同中记录在案的 range parse。resident worker 不会自动分页或拼接；
admin 契约仍允许显式 `start_page` / `end_page`。因此 `false=0` 不能证明 MinerU 的跨页表和
reading order 绝对正确，但足以说明目前没有“已发布页段产物被外部拼坏”的证据。

## 5. MinerU 临时目录生命周期

2026-07-25 16:59:46 的只读盘点为 131 个 `mineru-api-client-*` 目录、1.309 GiB。它只证明
历史 cleanup 泄漏，不证明结果被分页或拼接。复核命令不会删除文件：

```bash
date '+%Y-%m-%dT%H:%M:%S%z'
find "${TMPDIR%/}" -maxdepth 1 -type d \
  -name 'mineru-api-client-*' -exec du -sk {} + 2>/dev/null |
  awk '{n+=1; kb+=$1}
       END {printf "dirs=%d kib=%d gib=%.3f\n", n, kb, kb/1048576}'
```

## 6. 证明边界

- baseline 是旧 live commit，不是候选 A/B；
- backlog 是单点，不能重建此前每分钟队列长度；
- 500+ 页失败率按 attempt，不能冒充 unique-document 失败率；
- lane 公平只发生在 `ORDER BY q.document_id LIMIT 1000` 的当前候选前缀内；
- `16×7=112` 是一个持有 singleton lock 的 resident worker 的正常稳态配置包络，不是
  共享 GPU 的分布式令牌；
- 仓外直连、lock session 异常、shutdown/retry 的短暂交叠都在静态乘法证明之外；
- 多生产者成为合法拓扑时，应把 admission/fencing 上移到统一 GPU 入口，而不是继续堆
  本地 worker 状态。
