# config/ — 运营者旋钮总索引

这里是**运营者日常改的全部东西**。规则词表（class_map 等）不在这里——它们是代码
确定性契约的一部分，位于包内并有版本纪律（见下"两类文件"）。

## 本目录文件

| 文件 | 管什么 | 改完跑什么 |
|---|---|---|
| `watchlist.csv` | 股票池唯一真源：一行一只票 + 按公司覆盖（lookback_days / sync_frequency / process_classes，空=继承全局） | `make track`（自动先 config-check；`DRY_RUN=1` 只看计划不写库；`PRUNE_DRIFT=YES` 把删掉的行置 paused） |
| `processing_policy.json` | 全局处理策略：`process`=下载+解析；`register_only`=只登记元数据 | 无需命令，下次 worker 启动生效；改前 `make config-check` 验证 |

## 级联模型（同一参数，三层，空=继承）

```
参数            全局默认                        按公司覆盖(watchlist.csv)
处理类型        processing_policy.json process   process_classes 列（替换式）
回补窗口        DISCLOSURE_INITIAL_LOOKBACK_DAYS=1095   lookback_days 列
同步频率        DISCLOSURE_SYNC_INTERVAL_SECONDS=86400  sync_frequency 列(hourly/daily/weekly)
```

登记**永远全量**（所有公告元数据入库可查）——所以把某类加进 process 后，
历史文档自动从已登记元数据回补下载，无需重新同步。生效配置与来源层看
`make track-status`（process_classes_source 等列标注 company/global）。

## 两类文件的边界

- **本目录 = 运营策略**：改动即生效（或 make track），git 提交即审计。
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
| DISCLOSURE_BACKFILL_MAX_PENDING_DOWNLOADS | 2000 | 回补背压阈值 |
| CNINFO_* | — | 凭据（只进环境，绝不进仓） |

## 命令速查

```bash
make config-check          # 离线验证两个配置文件（文件:行号 报错）
make track DRY_RUN=1       # 看对账计划（创建/更新/暂停），不写库
make track                 # 应用 watchlist（幂等）
make track-status          # 全池状态 + 每公司生效配置与来源层
make worker-once           # 手动跑一轮（同步→下载→解析→切分→发布）
make doctor-full           # 环境+迁移头+分类规则版本 全体检
```
