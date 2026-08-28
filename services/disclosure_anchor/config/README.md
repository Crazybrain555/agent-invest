# config/ — 运营者旋钮总索引

这里是**运营者日常改的全部东西**。规则词表（class_map 等）不在这里——它们是代码
确定性契约的一部分，位于包内并有版本纪律（见下"两类文件"）。

## 本目录文件

| 文件 | 管什么 | 改完跑什么 |
|---|---|---|
| `watchlist.csv` | 股票池**导入快照**（真源是 DB 的 tracked_company，round22 改判）：一行一只票 + 按公司覆盖（lookback_days / sync_frequency / process_classes，空=继承全局） | 导入：`make track`（自动先 config-check；`DRY_RUN=1` 只看计划；`PRUNE_DRIFT=YES` 全量恢复）；DB 导出必须用 `make track-export OUT=/timestamped/path/watchlist.csv`，不会默认覆盖本文件 |
| `processing_policy.json` | 全局处理策略：`process`=下载+解析；`register_only`=只登记元数据。r4(2026-07-15)：process 20 类，`equity_share_change` 为完整股本/流通/解禁台账进入 process；dividend/related_party/financing 仍 register_only，必要时按公司覆盖拉回。carrier 例外：中介载体即使共码命中 process 也不放行，除非把 intermediary_report 加进生效集合。title_noise 总闸仍是绝对门，r12 只保留 12 个没有新增金融事实的 hard pattern，其余 67 个事实/条件项恢复到 class/process/carrier 正常路径；r13 再恢复 6 条自我标识副本/序次重复项（英文版/（英文）/H股季报年报/ST 退市链第 N 次提示）共 18 条——含事实的主件感知去重仍待可靠关联键后实施 | `make config-check` + `make load-rules`，再重启/kickstart resident worker 并跑 doctor |

池子的增删改查（CSV 导入与 API PUT 是整行 upsert：空可选字段=清除覆盖回继承，响应/输出会回显
`cleared_overrides`；**例外**：`make track CODES=...` 快捷入池是 ensure 语义——已在池的公司
保持 status 与全部覆盖不动，2026-07-14 改判）：

| 操作 | API | CLI |
|---|---|---|
| 增/改 | `PUT /v1/admin/tracked-companies` | `make track CODES=...` 或编辑 CSV + `make track` |
| ↳ 公司解名 | 最多 20 条的 API 请求与最多 20 个 `CODES` 的兼容路径可当场补真名；CSV/bulk 导入固定跳过无 provenance 的 post-commit profile，交给 worker 首次同步按 `source_access` 审计路径补齐。`SKIP_PROFILE_RESOLUTION=YES` 也可关闭 CLI 小范围兼容路径 | |
| 暂停（可逆停） | PUT 里 `status=paused` | CSV 该行 status=paused + `make track` |
| 删（出池，公司与文档留档） | `DELETE /v1/admin/tracked-companies/{code}?exchange=` | `make untrack CODES=...` |
| 按需取证（L6 拉式触发） | `POST /v1/admin/tracked-companies/{code}/sync?exchange=`（body 可选 window_days） | `make sync COMPANY=...` |
| 查 | `GET /v1/tracked-companies`（含级联生效值） | `make track-status` |

暂停 vs 出池：paused 保留配置随时恢复；untrack 删订阅关系但公司、source access、已获取文档、
原始文件和派生谱系全部留档（下载队列只放行有 active 行的公司，出池即停止获取）。运行时不提供
按公司级联删除 corpus/raw/source provenance 的命令；确定性测试数据必须由隔离 scratch runner 清理。

## 级联模型（同一参数，三层，空=继承）

```
参数            全局默认                        按公司覆盖(watchlist.csv)
处理类型        processing_policy.json process   process_classes 列（替换式）
回补窗口        DISCLOSURE_INITIAL_LOOKBACK_DAYS=1280   lookback_days 列
同步频率        DISCLOSURE_SYNC_INTERVAL_SECONDS=86400  sync_frequency 列(hourly/daily/weekly)
```

`process_classes` 的替换式语义是：非空列表整体替换全局 `process`；DB `NULL`、JSON
空数组 `[]` 与 CSV 空单元格都表示继承。未知类名在写入口整体拒绝，绕过入口写入的异常值
由 worker 安全地不放行并由 doctor 告警。

登记**永远全量**（所有公告元数据入库可查）——所以把某类加进 process 后，
历史文档自动从已登记元数据回补下载，无需重新同步。生效配置与来源层看
`make track-status`（process_classes_source 等列标注 company/global）。

## 两类文件的边界

- **本目录 = 运营策略**：文件改动（或 `make track`）进入审计；resident worker 缓存策略，
  改动或 `make load-rules` 后须重启 launchd job 并再跑 doctor，不能等待自动刷新。
- **包内分类词表 = 规则契约**（`adapters/sources/cninfo/class_map.json`、`facet_map.json`、
  `filing_type_map.json`）：版本号写入加载表，改动 = 升版 + `make load-rules`。Provider Unit
  builder 不再读取章节/事件词表，也不按业务词面改变标题、边界或 payload。清单与版本见
  `docs/architecture/data-dictionary.md` §4。

## 常用环境变量（machine-local，~/.config/agent-invest/disclosure_anchor/*.env）

| 变量 | 默认 | 说明 |
|---|---|---|
| DATABASE_URL | — | worker/pipeline 的 app 写连接；必须是非 superuser `disclosure_app`，不回落 migration owner |
| DISCLOSURE_MIGRATION_DATABASE_URL | — | 仅迁移/建库 owner 连接；worker/pipeline 禁止使用 |
| DISCLOSURE_DATA_ROOT / SHARED_ROOT / RUNTIME_ROOT | — | 数据/共享/运行时根 |
| DISCLOSURE_INITIAL_LOOKBACK_DAYS | 1280 | 首次回补窗口（3.5 年 + 闰日/日历缓冲） |
| DISCLOSURE_SYNC_INTERVAL_SECONDS | 86400 | 全局同步间隔 |
| DISCLOSURE_PROCESSING_POLICY | config/processing_policy.json | 策略文件路径 |
| DISCLOSURE_MINERU_BACKEND | hybrid-http-client | sole writer 固定值；其他 lane 仅可作 DB-free 诊断 |
| DISCLOSURE_MINERU_API_URL | — | Mac→固定 MinerU API；只接受 loopback SSH forward，API 不得暴露到 Funnel/LAN |
| DISCLOSURE_MINERU_OBSERVABILITY_URL | — | Mac→vLLM `/v1` observation/canary/metrics endpoint；与 API URL 分离 |
| DISCLOSURE_MINERU_INFERENCE_UPSTREAM_URL | — | Windows API container→vLLM 的内部 Docker URL；Mac 不直接探测该 hostname |
| DISCLOSURE_MINERU_API_TASK_SLOTS / API_INFERENCE_CONCURRENCY | 1 / 7 | 当前 30 GiB Windows profile 的 whole-PDF task 上限与每 task inference cap；1–3 可配置但必须重新 attestation + 两轮异构验证，不能只改 env |
| DISCLOSURE_GPU_METRICS_URL | — | 可选真实 GPU exporter `/metrics`；Linux 用 DCGM，Windows 用受限 SSH loopback 的 pinned nvidia-smi exporter；未配置时明确 unavailable |
| DISCLOSURE_GPU_EXPECTED_UUID | — | Windows exporter 观测到的唯一 GPU UUID；与安装 receipt 绑定，跨 UUID 指标或换卡会降级为 unavailable |
| DISCLOSURE_DCGM_METRICS_URL | — | 旧 DCGM-only 兼容别名；若与通用 URL 同时配置必须完全相同 |
| WORKER_PROGRESS_METRICS_TIMEOUT_SECONDS | 5 | vLLM/GPU exporter 探针超时；失败只降级观测，不停止健康 worker |
| DISCLOSURE_MINERU_RUNTIME_BUNDLE_IDENTITY_SHA256 | — | 远端 immutable image/model/config 的 operator/provider attested digest；不得用 mutable tag 伪装 |
| DISCLOSURE_MINERU_BIN | — | pinned 本地 MinerU client venv 的精确 executable |
| DISCLOSURE_SEMANTIC_PROVIDERS_JSON | Luna low → Sonnet 5 low | 完整有序、无 secret 的 provider 数组；字段/示例见 `docs/implementation/design/semantic-adjudication-runtime.md` |
| DISCLOSURE_SEMANTIC_FAILOVER_POLICY | availability_only.v1 | 仅已列明的 executable/auth/quota/transport/timeout 可切备用；协议/结果/安全错误 fail closed |
| DISCLOSURE_BACKFILL_MAX_PENDING_DOWNLOADS | 2000 | 首回补处理总在途水位（兼容旧变量名）：待下载 + 已下载待解析；单公司原子同步可越线一次 |
| WORKER_BATCH_SYNC | 13 | 每轮到期公司上限；常驻模式零等待轮转，但首回补还受总在途水位约束，不要直接升到 200 |
| WORKER_BATCH_DOWNLOAD | 50 | 每轮下载上限；下载只把工作从 pending-download 搬到 pending-parse，总在途水位避免 GPU 故障时 raw 无界增长 |
| WORKER_BATCH_PARSE | 50 | 仅为直接 `worker once` 的单轮文档上限；production resident 常驻补槽，不把该数当报告/排空边界 |
| WORKER_PARSE_CONCURRENCY | 16（生产模板） | 文档槽，不是 GPU 请求数；本地 CPU backend 必须设 1 |
| WORKER_GPU_REQUEST_BUDGET / MAX_SEQUENCES | 7 / 128 | resident worker 稳态 active inference 包络；本地候选可达 16 文档，但 API-facing outstanding 固定为 1，其余只留在 PostgreSQL durable queue |
| WORKER_PARSE_*_PAGE_THRESHOLD / SATURATED_SHARE | 80/4、500/1 | regular/heavy/huge 名义份额；lane 空闲时允许借用 |
| CNINFO_OVERSIZED_KB | 10240 | 兼容旧名；以归档 actual byte_count 判定 HUGE lane，不是下载/解析上限 |
| WORKER_PARSE_CANDIDATE_WINDOW | 1000 | 每次公平选择的候选前缀；不是第二份耐久队列 |
| WORKER_FINALIZE_CONCURRENCY | 2 | parse 后 build/publish 的有界下游池 |
| WORKER_REPORT_INTERVAL_SECONDS | 300 | resident 观测快照周期；只轮换 report 对象，绝不关闭 admission 或排空 future |
| DISCLOSURE_PARSE_TIMEOUT_* | 3600 / 12-per-page / 14400 | 页数感知的软预期耗时，只告警、不终止正常长文档 |
| DISCLOSURE_PARSE_RUNAWAY_TIMEOUT_SECONDS | 86400 | 极端 live-but-stuck 进程保护；整本文档默认可运行 24 小时 |
| WORKER_LOOP_INTERVAL_SECONDS / MAX | 900 / 1800 | acquisition/project maintenance 的空闲/故障退避；parse 空队列由下载事件或 5 秒 fail-safe poll 唤醒 |
| MINERU_PROCESSING_WINDOW_SIZE | 16 | GPU 页窗口红线（round22h OOM 后定案） |
| DISCLOSURE_MINERU_SMOKE_RECEIPT / CANARY_CACHE | — | runtime bundle v6 的 DB-free smoke/canary v2 PASS 对；resident 在连 DB 前强制校验 |
| DISCLOSURE_MINERU_STAGED_LOAD_RECEIPT / STAGED_LOAD_CONFIRMATION_RECEIPT | — | 同一 persistent identity 和真实异构 corpus 的两次独立完整 4/8/16 文档回放 PASS；客户端 API-facing window 固定为 1、huge 独占，并从进程外持续验证容器 epoch/restart/OOM/RSS/Docker VM 内存 |
| DISCLOSURE_MINERU_STAGED_CORPUS_SHA256 | — | 两次 staged run 共用、至少 16 个不同 hash 且覆盖 regular/heavy/huge 的有序真实 PDF corpus 身份 |
| DISCLOSURE_MINERU_DOCKER_MEMORY_RESERVE_BYTES | 0（未配置） | 当前 Docker VM 的 operator-calibrated 最低可用内存；parse-capable gate 要求正值，不能写死为跨机器常量 |
| DISCLOSURE_MINERU_CANARY_MAX_AGE_SECONDS | 2592000 | 静态 smoke/staged-load 的进程启动租约（30 天）；启动后每 300 秒继续核 live API/model，incident 立即关闭 admission |
| DISCLOSURE_MINERU_LIVE_PROBE_INTERVAL_SECONDS | 300 | parse admission 限频复核 `/v1/models` 唯一 served-model；首次入场必查 |
| CNINFO_* | — | 凭据（只进环境，绝不进仓） |

## 命令速查

```bash
make config-check          # 离线验证默认 watchlist、screen sidecar 和 processing policy
make track DRY_RUN=1       # 看导入对账计划（创建/更新/暂停），不写库
make track                 # 导入 watchlist（幂等；固定跳过 post-commit profile）
make track CODES=600519    # 小范围快捷入池（最多20只保留即时解名兼容）
make track CODES=600519 SKIP_PROFILE_RESOLUTION=YES  # 仅入池，解名交给 worker
make track-export OUT=/timestamped/path/watchlist.csv  # DB 池子 → 独立复核快照
make track-status          # 全池状态 + 每公司生效配置与来源层
make mineru-smoke RUNTIME_MANIFEST=/path/runtime.json RECEIPT=/path/receipt.json CANARY_CACHE=/path/canary.json
                           # 无 DB/队列/业务凭据下传：三次多模态 canary + 固定单页 full-PDF，进程/临时树差集归零
make mineru-staged-load RUNTIME_MANIFEST=/path/runtime.json CORPUS_MANIFEST=/path/frozen-corpus.json EXPECTED_CORPUS_SHA256=<sha256> RECEIPT=/path/new-load-receipt.v6.json SSH_HOST=<host> SSH_USER=<user> SSH_IDENTITY=/private/key SSH_KNOWN_HOSTS=/private/known_hosts DOCKER_MEMORY_RESERVE_BYTES=<bytes> DOCUMENT_RUNAWAY_TIMEOUT_SECONDS=86400 API_DRAIN_TIMEOUT_SECONDS=86400
                           # smoke PASS 后固定4/8/16文档数；两值最低86400，私有settings更高时必须同步；API-facing window=1、huge独占；每阶段都含regular/heavy/huge；API/vLLM/外部Docker epoch-OOM-RSS-memory/清理任一越界立即停止
make worker-once           # 手动跑一轮（同步→下载→解析→切分→发布）
make worker-loop           # 常驻自适应排水；积压时零等待，空闲时 15→30 分钟退避
make worker-status         # 单次只读快照：公司/文档两条进度、队列、当前任务、vLLM 与真实 GPU exporter
# Agent/脚本可直接读取：python -m disclosure_anchor.cli.worker status --format json
make doctor-full           # 环境+迁移头+分类规则版本 全体检
make worker-status         # 常驻 worker 状态 + 今日报告尾部
make worker-restart        # 仅重载代码/env；不会重载 launchd plist
./scripts/install_launchd.sh  # 仅在 job 已安全 bootout 后安装/更新 plist
```

`worker loop` 启动即输出一次进度，之后随 `WORKER_REPORT_INTERVAL_SECONDS` 更新。公司同步分母是
active 股票池；文档发布分母是“当前已发现且应处理”的文档，会随新公告发现而增长，因此显式标记
dynamic total，不能把二者拼成一个虚假的总体百分比。每个快照同时追加到
`$DISCLOSURE_RUNTIME_ROOT/reports/progress/YYYY-MM-DD.jsonl`（`worker_progress.v2`、0600，含
producer instance + 单调 sequence/event ID）；终端、
未来前端/SSE adapter 与 Agent 都应消费这一份事件。固定 API queued/processing/completed/failed、
vLLM running/waiting/KV 与 GPU exporter 的真实 compute utilization 始终分栏；Windows 使用
UUID 绑定的 nvidia-smi exporter，Linux 可使用 DCGM，未配置时不得用 KV cache 代替 GPU 占用。
探针失败还区分 `endpoint_unreachable` 与 `metric_contract_unsatisfied`，便于告警和升级处置。

默认 `config/watchlist.csv` 是 1,463 行的
`a_share_research_priority.v11-candidate.phase1` 部署候选；精确 Pro 结论是 `STOP`、
`best attainable phase-1 candidate / still not GO`，不能把它表述成已经证明的“未来最优”股票池。
`config/watchlist-screen.v1.json` 是部署侧 sidecar；CSV 注释里的 `watchlist-v11-manifest.json` 是
Pro 原始附件的逻辑文件名，两者字节完全相同。sidecar 把 CSV 绑定到 5,548 行全量审计、显式硬门槛、
排除计数、CSV hash 和有序 membership hash。它是研究优先级 universe，不是投资推荐或收益排序；审计意见、
处罚历史、长期流动性、管理层诚信、治理评分、会计重述风险、前瞻一致预期、行业空间/催化剂、
估值和价格动量目前均明确标记为未覆盖。所有信号都来自已经实现的后视年度会计数据，未做行业
周期调整；当前成分股快照也不是 point-in-time 数据，不能据此做无偏历史回测。
V11 候选只覆盖沪主板、深主板、科创板、创业板；板块身份取自完整
CNINFO 行的 `F004V`，BSE/NEEQ 不进入默认研究池，但通用 tracking contract 仍可正确表达 BSE。
约 1,500 是首轮研究资源规模，不是强行补齐的名额。上市日须不晚于 2023-02-23，按 2026-08-23
冻结时点留足 42 个自然月；市值至少 20 亿元，并要求正常 A 股状态、非风险警示、非定义中的持续
亏损。通过硬门后按财务质量、增长、现金质量、行业策略和异常惩罚进行确定性 Decimal 评分；阈值
自然产生 1,463 家并落在 1,400–1,600 的人工复审闸门内，禁止为卡死 1,500 强制补入。
A/B/C 只是入选证据强度分层和确定性审计展示顺序；CSV 导入数据库后所有成员地位相同，行序不是
运行时调度优先级。规则对象必须逐字段精确匹配 V11 phase-1，不能只改
说明而继续沿用同一规则版本。

V10 的后视信号门槛和生成器仍保留作历史兼容与测试基线，但不再描述默认部署池，也不能用其输出
与当前 `config/watchlist.csv` / `config/watchlist-screen.v1.json` 做相等比较。

V11 默认候选的部署字节与完整审计证据检查（不联网、不读数据库）是：

```bash
WATCHLIST_V11_ROOT="$DISCLOSURE_DATA_ROOT/evidence/watchlist/a_share_research_priority.v11-candidate.phase1/2026-08-23"
cmp config/watchlist.csv "$WATCHLIST_V11_ROOT/watchlist-v11-candidate.csv"
cmp config/watchlist-screen.v1.json "$WATCHLIST_V11_ROOT/watchlist-v11-manifest.json"
shasum -a 256 \
  config/watchlist.csv \
  config/watchlist-screen.v1.json \
  "$WATCHLIST_V11_ROOT/watchlist-v11-selection-audit.csv" \
  "$WATCHLIST_V11_ROOT/generate_watchlist_v11_candidate.py"
make config-check
```

`make config-check` 校验 Git CSV 与 sidecar 的闭合形状、规则/限制、CSV hash、membership hash、数量、
exchange、board、日期、市值下限、5,548 行审计闭合与 V10 变更计数，但不会重新执行归档的 Pro
生成器。完整来源/投影证明必须运行上述 `cmp`、核对四个已固定 SHA，并检查完整 selection audit；
不能把 `config-check` 单独理解为前瞻治理和行业证据已经补齐。

## Capacity observation 与默认关闭的 Auto profile

Observation v1 仍只有旁路采样权限。另有默认 `legacy` 的 exact-source capacity pipeline：受控
`candidate` 试验按文档 admission 冻结 profile；`auto` 只选择经过 A-B-B-A receipt 晋升且在页面/源文件
envelope 内的 profile，不能在运行中改 knob。两条路径都复用 machine-local `worker.env` 中已存在的
API/vLLM/GPU URL、runtime bundle identity、GPU UUID、MinerU client 和 Docker memory reserve；
不新增数据库 DSN，也不在 tracked config 写 SSH host/user/key。

Operator 运行时显式传入 v6 `RUNTIME_MANIFEST`、`DURATION_SECONDS` 和与 staged load 相同的
`SSH_HOST` / `SSH_USER` / `SSH_IDENTITY` / `SSH_KNOWN_HOSTS`。这些参数只用于 pinned read-only
collector；端点、hostname、username 与 container ID 不进入 evidence。命令和复算方法见
`docs/implementation/runbooks/production-operations.md` §1.1d，设计见
`docs/implementation/design/capacity-observation.md`。

历史 V10 的 assemble/build 脚本都使用 mode 0444 的 new-only 输出。若要刷新公开来源，必须先把新日期目录作为独立目标运行
`scripts/fetch_research_watchlist_inputs.py --output <new-sources-dir>`，再人工核验完整来源回执；不能覆盖
当前冻结证据，也不能在一次“重建”里静默联网换观察时点。

`make track FILE=/path/to/other.csv` 会校验该自定义文件，不会误检默认 CSV；自定义导入若也要
screen hash 约束，可同时传 `SCREEN_MANIFEST=/path/to/manifest.json`。默认 CSV 则始终要求 sidecar
精确匹配；`./`、`..` 或 symlink 等路径别名不能绕过。pipeline 从同一份内存 bytes 做校验与导入，
不会在 config-check 后重新读取另一份内容。DB 导入成功后 `tracked_company` 仍是真源；`track-export`
强制显式 `OUT`，导入前后都写时间戳独立路径。复核时比较规范化后的 code/exchange/status/覆盖参数和
active membership，不用导出 CSV 与生成 CSV 的字节相等作为正确性断言。

`PRUNE_DRIFT` 只接受空或 `YES`，`DRY_RUN` 只接受空或 `1`，
`SKIP_PROFILE_RESOLUTION` 只接受空或 `YES`；`NO`、`0`、`false` 和拼写错误一律拒绝，
绝不按“非空即真”解释。FILE 与 CODES 互斥。

## 生效矩阵：改什么 → 跑什么 → 是否需重启（2026-07-14）

常驻 worker 的 env/policy 只在进程启动时读一次；改了不重启 = 文件里的值 ≠ 进程里的值。
worker 启动时会打印 `[versions]` 行（policy/builder/分类规则版本），核对生效用它。

| 改动对象 | 生效命令 | 需要 worker-restart? |
|---|---|---|
| 股票池（增删/参数覆盖） | `make track` / `PUT /v1/admin/tracked-companies` | 否（DB 即时） |
| 分类词表 JSON（class/facet/filing_type_map） | `make load-rules` | 否（视图现算） |
| `processing_policy.json` | `make config-check` 后 | **是** |
| `~/.config/.../worker.env`（含 DB/凭据/批量/并发） | — | **是** |
| 代码（src/） | `make agent-check` 后 | **是** |
| `watchlist.csv`（仅文件） | `make track`（导入才生效） | 否 |

`worker-restart` 只适用于已安装 plist 就是当前版本的常规代码/env 重载。plist 变化或首次从
旧 worker 切换，必须按生产 runbook 的 staged drain 执行；安装脚本发现 job 仍 loaded 会
以 75 fail closed。重启后固定动作：`make doctor-full` 退出 0 + 观察一轮报告
（`make worker-status`）。
