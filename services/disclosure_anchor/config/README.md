# config/ — 运营者旋钮总索引

这里是**运营者日常改的全部东西**。规则词表（class_map 等）不在这里——它们是代码
确定性契约的一部分，位于包内并有版本纪律（见下"两类文件"）。

## 本目录文件

| 文件 | 管什么 | 改完跑什么 |
|---|---|---|
| `watchlist.csv` | 股票池**导入/快照文件**（真源是 DB 的 tracked_company，round22 改判）：一行一只票 + 按公司覆盖（lookback_days / sync_frequency / process_classes，空=继承全局） | 导入：`make track`（自动先 config-check；`DRY_RUN=1` 只看计划；`PRUNE_DRIFT=YES` 全量恢复）；快照：`make track-export`（DB → 本文件，git 留痕） |
| `processing_policy.json` | 全局处理策略：`process`=下载+解析；`register_only`=只登记元数据。r4(2026-07-15)：process 20 类，`equity_share_change` 为完整股本/流通/解禁台账进入 process；dividend/related_party/financing 仍 register_only，必要时按公司覆盖拉回。carrier 例外：中介载体即使共码命中 process 也不放行，除非把 intermediary_report 加进生效集合。title_noise 总闸仍是绝对门，r12 只保留 12 个没有新增金融事实的 hard pattern，其余 67 个事实/条件项恢复到 class/process/carrier 正常路径；r13 再恢复 6 条自我标识副本/序次重复项（英文版/（英文）/H股季报年报/ST 退市链第 N 次提示）共 18 条——含事实的主件感知去重仍待可靠关联键后实施 | `make config-check` + `make load-rules`，再重启/kickstart resident worker 并跑 doctor |

池子的增删改查（CSV 导入与 API PUT 是整行 upsert：空可选字段=清除覆盖回继承，响应/输出会回显
`cleared_overrides`；**例外**：`make track CODES=...` 快捷入池是 ensure 语义——已在池的公司
保持 status 与全部覆盖不动，2026-07-14 改判）：

| 操作 | API | CLI |
|---|---|---|
| 增/改 | `PUT /v1/admin/tracked-companies` | `make track CODES=...` 或编辑 CSV + `make track` |
| ↳ 入池即解名 | 两条路径都会在有凭据时当场拉公司档案补真名（Miniflux 模式，失败留占位符，首次同步兜底） | |
| 暂停（可逆停） | PUT 里 `status=paused` | CSV 该行 status=paused + `make track` |
| 删（出池，公司与文档留档） | `DELETE /v1/admin/tracked-companies/{code}?exchange=` | `make untrack CODES=...` |
| 按需取证（L6 拉式触发） | `POST /v1/admin/tracked-companies/{code}/sync?exchange=`（body 可选 window_days） | `make sync COMPANY=...` |
| 清除（测试期：连公司/文档/文件一起删） | 无（刻意只留 CLI） | `make purge-company CODE=... PURGE=YES` |
| 查 | `GET /v1/tracked-companies`（含级联生效值） | `make track-status` |

暂停 vs 出池 vs 清除：paused 保留配置随时恢复；untrack 删订阅关系但公司/已获取文档留档
（下载队列只放行有 active 行的公司，出池即停止获取）；purge 是 wipe-test-data 的单公司版，
只用于撤销失误/测试残留。

## 级联模型（同一参数，三层，空=继承）

```
参数            全局默认                        按公司覆盖(watchlist.csv)
处理类型        processing_policy.json process   process_classes 列（替换式）
回补窗口        DISCLOSURE_INITIAL_LOOKBACK_DAYS=1095   lookback_days 列
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
- **包内词表 = 规则契约**（`adapters/sources/cninfo/class_map.json`、`facet_map.json`、
  `filing_type_map.json`；`adapters/unit_builder/note_key_map.json`、`event_key_map.json`）：
  版本号写进数据库行，改动 = 升版 + `make load-rules`（分类词表）或升 RULES_VERSION +
  `make rebuild-units`（切分词表）。清单与版本见 `docs/architecture/data-dictionary.md` §4。

## 常用环境变量（machine-local，~/.config/agent-invest/disclosure_anchor/*.env）

| 变量 | 默认 | 说明 |
|---|---|---|
| DATABASE_URL / DISCLOSURE_MIGRATION_DATABASE_URL | — | 库连接（socket DSN） |
| DISCLOSURE_DATA_ROOT / SHARED_ROOT / RUNTIME_ROOT | — | 数据/共享/运行时根 |
| DISCLOSURE_INITIAL_LOOKBACK_DAYS | 1095 | 首次回补窗口（三年底线） |
| DISCLOSURE_SYNC_INTERVAL_SECONDS | 86400 | 全局同步间隔 |
| DISCLOSURE_PROCESSING_POLICY | config/processing_policy.json | 策略文件路径 |
| DISCLOSURE_BACKFILL_MAX_PENDING_DOWNLOADS | 2000 | 首回补处理总在途水位（兼容旧变量名）：待下载 + 已下载待解析；单公司原子同步可越线一次 |
| WORKER_BATCH_SYNC | 13 | 每轮到期公司上限；常驻模式零等待轮转，但首回补还受总在途水位约束，不要直接升到 200 |
| WORKER_BATCH_DOWNLOAD | 50 | 每轮下载上限；下载只把工作从 pending-download 搬到 pending-parse，总在途水位避免 GPU 故障时 raw 无界增长 |
| WORKER_BATCH_PARSE | 50 | 直接 `worker once` 的单轮上限；resident 只在有界 refill window 内滚动补槽 |
| WORKER_PARSE_CONCURRENCY | 16（生产模板） | 文档槽，不是 GPU 请求数；本地 CPU backend 必须设 1 |
| WORKER_GPU_REQUEST_BUDGET / MAX_SEQUENCES | 112 / 128 | resident worker 稳态请求包络；16 文档时每份 MinerU `--max-concurrency=7` |
| WORKER_PARSE_*_PAGE_THRESHOLD / SATURATED_SHARE | 80/4、500/1 | regular/heavy/huge 名义份额；lane 空闲时允许借用 |
| WORKER_PARSE_CANDIDATE_WINDOW | 1000 | 每次公平选择的候选前缀；不是第二份耐久队列 |
| WORKER_FINALIZE_CONCURRENCY | 2 | parse 后 build/publish 的有界下游池 |
| WORKER_LOOP_INTERVAL_SECONDS / MAX | 900 / 1800 | 仅空队列使用的 15→30 分钟退避；有进展时不睡眠 |
| MINERU_PROCESSING_WINDOW_SIZE | 16 | GPU 页窗口红线（round22h OOM 后定案） |
| CNINFO_* | — | 凭据（只进环境，绝不进仓） |

## 命令速查

```bash
make config-check          # 离线验证两个配置文件（文件:行号 报错）
make track DRY_RUN=1       # 看导入对账计划（创建/更新/暂停），不写库
make track                 # 导入 watchlist（幂等）
make track CODES=600519    # 快捷入池（DB 直写；想留 git 快照再 track-export）
make track-export          # DB 池子 → config/watchlist.csv 快照（git 留痕）
make track-status          # 全池状态 + 每公司生效配置与来源层
make worker-once           # 手动跑一轮（同步→下载→解析→切分→发布）
make worker-loop           # 常驻自适应排水；积压时零等待，空闲时 15→30 分钟退避
make doctor-full           # 环境+迁移头+分类规则版本 全体检
make worker-status         # 常驻 worker 状态 + 今日报告尾部
make worker-restart        # 仅重载代码/env；不会重载 launchd plist
./scripts/install_launchd.sh  # 仅在 job 已安全 bootout 后安装/更新 plist
```

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
