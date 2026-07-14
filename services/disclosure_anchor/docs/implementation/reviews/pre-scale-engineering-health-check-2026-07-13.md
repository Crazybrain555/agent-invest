---
id: disclosure_anchor_pre_scale_engineering_health_check_2026_07_13
title: 100–200 股上量前工程体检
date: 2026-07-13
status: implemented-pending-production-activation
scope: services/disclosure_anchor
---

# 100–200 股上量前工程体检（2026-07-13）

## 0. 结论与前序审计关系

本报告是对 `process-classes-review-2026-07-12.md`（040b50f / d715f59 /
33a2927）的**独立后续审计**，不是续写或背书。前序报告只作为事故线索和用户裁决记录；
放行 SQL、主体 resolver、live catalog、候选快照、worker reports、真实 PDF 和 scratch DB
均重新取证。并入本轮的 `vocab-generalization-2026-07-13.md` 也没有被默认采信：独立复审
又实锤两条 absolute noise 误杀，最终升为 filing r8 / class r7，并重算候选层容量。

本轮在 2026-07-13 当时的结论是 **Conditional GO**：代码侧 P0 已封闭，真实 16-PDF 常驻
backlog 验收通过；当时尚待完成 0022 migration、class_map r7 / filing map r8 load-rules、
worker.env 调参与 KeepAlive 安装。上述激活项后来均已完成；当前版本与清库动作以本页 checklist
的 2026-07-14 修订及 live doctor 为准，不能再把本段历史状态当作执行指令。

本轮没有推翻四项用户裁决：register 永远全量；process=下载+解析一个动作；title_noise
绝对排除；文档并发≤8（MinerU window=16）。

### 新发现的共同根因

13 家暴露的不只是几个漏词，而是五类“上量会放大”的工程惯性：

1. 同一三态配置在写入口/API/SQL 各自解释，`[]` 语义漂移；
2. 业务唯一键有 exact UNIQUE，却缺入口规范化和 DB 规范形态约束；
3. worker 把固定节拍误当恢复间隔，掩盖 checkpoint、轮级异常和 GPU cooldown 缺口；
4. 审计只看已下载 survivor，恰好看不见导致未下载的未知码；
5. migration head / public view 清单靠手抄，功能变更后验证钉子必然陈旧。

## 1. 放行谓词正确性

### 复现 / 证伪

live 13 家候选 5,095 份：当前无 NULL/空 title，最长 title 113 字，F006V 最多 8 段，
无空段/首尾空白，且 13 家均无 process override。因此这些边界不能靠现库自然覆盖。

scratch 集成构造了 NULL title、10 万字符 title、空串/纯空白 category、带空格多段码、
`process_classes=[]`、unknown/mixed/non-array 覆盖，并复核 carrier/topic/noise 用例：

| 输入 | 结果 |
|---|---|
| coded + NULL title | 仍按 code 放行 |
| codeless + NULL title | fail-closed |
| 100k title 尾部命中年度报告 | 放行，无截断 |
| `raw_category='   '` | 按无码 title 路由 |
| `013101 || 011301`（段带空格） | trim 后命中 dividend |
| override `[]` | 继承全局，而不是“处理零类” |
| override `['not_a_class']` | fail-closed；正常写入口提前 422/ValueError |
| override `['dividend','not_a_class']` | 整体 fail-closed，不部分放行 dividend |
| override `{}` / 嵌套非字符串 | fail-closed；doctor 告警但不自身 TypeError |
| carrier 与 process 共码 | carrier 自身不在 effective set 时排除 |
| title_topic 命中 | 与 code 资格取并集 |
| title_noise 命中 | code/topic/company override 均不能翻案 |

前序实现中 API/CSV 已把空覆盖折成 NULL，config/README 又写“空=继承”，但 SQL/API 读层
遇到绕过入口持久化的 `[]` 会解释成空集合；`raw_category=' '` 也会误走 coded 分支。

### 根因一句话

覆盖值与 provider 字符串没有单一规范化契约，边界层各自解释原始表示。

### 修复 / 不修理由

- 定案：只有**全部元素都是已知 class 的非空 JSON array**替换全局；NULL、`[]`、CSV 空格
  均继承。mixed unknown、非 array、嵌套值整体 fail-closed，并由 doctor 告警。
- category 判空及每个 F006V segment 均 `btrim`；NULL title 沿 PostgreSQL 三值逻辑安全
  fail-closed。
- 0022 同时恢复 public view 与 worker 的路由同义：broad `title` fallback 只用于无码行，
  coded 行只接 class ∪ narrow `title_topic`。不改变 public 列集。
- absolute noise 重新逐 TEXTID 复核：裸“中期票据计划”误杀 pid 1225149847 的金额/期限/票息，
  “预计满足赎回条件”误杀 pid 1225006849 的首次预警。filing r8 收窄前者、删除后者；两个
  must-survive 候选均进入 download 回归。绝对总闸不接受“多数低值”作为宽词理由。
- 注册期 report_period 改按 code + title_topic priority argmax 的主类推导：偿付能力摘要不再
  伪造 Q1，多段码 `012111||010301` 的年度报告不再漏年度 period。
- 不给 unknown override “容错放行”：绕过正常入口写私表本身就是异常，安全行为必须是
  不处理 + doctor，而不是静默扩大 GPU 面。

## 2. 主体解析与本地注册防重复

### 复现 / 证伪

证伪“没有唯一约束”：`uq_security_code_exchange` 自 0001 已存在；live catalog 13 条 security
全部规范（SSE=5、SZSE=8），normalized duplicate=0、contested identifier=0。因此不新增
重复 UNIQUE migration。

但 fake resolver 构造证明 exact UNIQUE 不防语义别名：修复前 `SZSE/szse` 或
`000333/'000333 '` 各会创建 2 company + 2 security。同一发行人 A/B 两个代码在没有 USCC
时也会先建两家；以后补同一 USCC，第二个 identifier 会 contested，而不会安全自动合并。
旧交易所推断还把 `900901`（沪 B）和 `430047/830799`（北交所）都路由到 SZSE。

CNINFO live 1,386 个 provider_document_id 全是数字，长度 10/11/12 位分别 1363/18/5。
构造的“年度报告目录”“local-deadbeef”此前均被 RegisterLocalPdfCommand 接受，正是 RCA
中目录名污染去重键的可执行路径。

### 根因一句话

resolver 用未规范化的外部字符串做 exact lookup，而 CLI 还主动为 CNINFO 伪造 local hash identity。

### 修复 / 不修理由

- 新增共享 security identity：code strip、exchange strip+uppercase；所有 track/register/
  resolver/repository/query 入口复用。0022 增 `ck_security_code_canonical` 与
  `ck_security_exchange_canonical`，防绕过应用写入别名。
- 推断显式支持 BSE（92/4/8）、SSE（6/9，含沪 B）、SZSE（0/2/3，含深 B）；未知前缀
  fail-closed。显式错误的 code/exchange 也拒绝；0022 CHECK 使用与 Python strip 对齐的 Unicode
  空白集合并校验大陆前缀。缺 exchange 的 legacy candidate 先按代码推断（600519→SSE、
  830001→BSE）再查 LOCAL，不再从错误 SZSE 探测开始。
- `provider=cninfo` 只接收 1–128 位 ASCII 数字 TEXTID；API=422，CLI=exit 2；文件名自动
  推导只认 `__<数字TEXTID>.pdf` 尾段，删除 hash/目录名 fallback。
- 暂不做 A/B 双证券 placeholder company 自动合并：它涉及 tracked_company 唯一归属、
  security 重挂、company/outbox 合并的原子公共契约，局部 resolver 修补风险更大。上量清单
  暂以“一发行人只入一个代码”控制，并由 doctor 的 contested identifier 告警兜底。

## 3. 动态调度与容量模型

### 复现 / 证伪

生产形态是 `StartInterval=7200 + run_worker_once.sh once`。按最终 class_map r7、
filing_type_map r8、processing_policy r2 重算，
13 家三年候选 5,095、可下载 1,044、扣 2 个 oversized 后可解析 1,042。线性外推 200 家
约需 321 个 parse batch（50/轮）：旧 2h 节拍仅等待就约 640h；方案 b 的 300s 节拍仍
增加 26.7h，不能称为 GPU-limited。

另一个真实放大器已在 live 复现：13 个 checkpoint 的 cursor `synced_at` 已到 7 月 13 日，
但行 `updated_at` 停在 7 月 9 日，故 13/13 每轮都 due。repository 只改 cursor，due SQL
却看 insert-only timestamp。对历史 `\:` 写法另做了证伪：production `pg_get_viewdef` 实际存的是
`|| ':p_info3015'`，`sync_due_v1` 的 13/13 行均匹配 checkpoint；反斜杠被 SQLAlchemy 当作
bind 转义消费，并不是生产 join 缺陷。当前 Python 查询仍改用 `chr(58)` 消除 `SyntaxWarning`
与三层转义歧义，但没有把它包装成行为修复或改写历史 migration。

### 根因一句话

固定 2h tick 同时承担调度、故障冷却和公平轮转，掩盖了“有工作也睡”与“时间戳不推进”两个独立错误。

### 修复

选择方案 a：单进程 resident loop + launchd KeepAlive，不引入 Celery/三阶段线程：

- 有 sync/download/parse/build/publish 进展：下一轮 sleep=0；
- 空队列：900→1800s 退避；item/system error：60s 指数退避；每轮 report 照发；
- `sync_quota_break`：仅 sync 冷却 30m→60m→120m，先让本地 download/parse 立即排水；
- 可重试 CNINFO 网络/5xx/坏响应：首错立即停止剩余 sync/download，本地 parse 继续，随后 source
  60s 指数 cooldown；底层 error_code/retryable 穿透 use-case wrapper，本地 download 成功不重置
  quota 退避；`make track` 的 profile 解名遇 quota 也首错即停，保留 placeholder 给 worker 修复；
- parser identity 在 dequeue 前做进程级缓存 preflight；失败不创建文档 run，version probe 自身有
  10s timeout/进程组终止。GPU infra failure 仅冷却 parse 120s，避免 73s 重启窗内耗完 item retry；
- 共享 DB/存储 build failure 停止 refill，并要求 cooldown 后 build-only 健康探针通过才恢复 parse；
  `IR_MISSING/IR_CONTRACT_TOO_OLD` 等单文档 build poison 继续隔离并受自身 attempt cap；
- 每轮从 AUTOCOMMIT dedicated connection 复核 singleton advisory lock；锁/连接丢失 fail-closed；
- checkpoint update 刷新 `updated_at`；fresh-checkpoint 集成用例同时钉住 worker due 查询和
  public lifecycle view，确保新鲜 checkpoint 不 due 且 `last_synced_at/synced_through` 可见；失败
  同步写 60s 调度标记并排到未尝试公司之后，前 13 个毒丸不能饿死后 187 家；CNINFO
  client/bucket/token cache 跨轮复用，零等待不破 1QPS；
- backfill cap 统计 pending-download + 全部 pending-parse/raw。每轮只做一次全局精确扫描，
  同轮按 candidate_count 保守累计，单公司最多越水位一次。GPU outage 时不会下载完整 1.6 万份，
  代价是 200 家 checkpoint 受 GPU 排水约束，不再承诺 16 轮全齐。

真实 backlog 验收（scratch DB/root，生产 PDF 只读复制）：16 个已发布 PDF、1,326,079 bytes，
并发 8，首轮 36.340s 完成 parsed/built/published=16/16/16、failed=0（1,585.0 docs/h）；
第二轮在同一秒立即启动，0.017s 判空并进入 900s sleep；idle 10s 前后进程 CPU time 均为
`0:00.57`。SIGTERM 正常退出，16 个 active succeeded run 一致，scratch 自动清理。
这批刻意是小 PDF，只证明节拍已消失；容量外推仍用大轮保守吞吐 255 docs/h。

### 200 股 × 3 年容量模型

| 项 | 13 家实测 | 200 家点估计 | 口径 |
|---|---:|---:|---|
| 去重候选 | 5,095 | 78,385 | source snapshot 按 TEXTID 取最新 |
| 可下载 | 1,044 | 16,062 | r8 gate（池内 process1044/register3444/noise607） |
| 可解析 | 1,042 | 16,031 | 扣 oversized 2→约31 |
| `make track` WebAPI | — | 200 | 有 key+secret 时逐票 profile 解名；不计 dry-run |
| worker 首同步 WebAPI | — | 7,601 | 200×(37 index chunk+1 profile)+进程内 category cache 1次 |
| wipe→全链 WebAPI | — | 7,801 | track + worker；受 CNINFO resultcode 日配额 |
| 全部共享 1QPS 请求 | — | 约23,863 | WebAPI 7,801 + 静态 PDF 16,062；下界 6.63h |
| GPU 理论 | — | 40.6h | 73s/份 ÷ 并发8 |
| GPU 保守 | — | 62.9h | live 大轮 785/11,077.809s=255 docs/h |
| 全部公司获 checkpoint | — | GPU-only约54h；全链约60h | 排水约13,862份；后者另含此前WebAPI/PDF 1QPS下界 |
| 保守总时长 | — | **约69.5h（2.9天）** | 1QPS 顺序阶段 + 保守 GPU，不含 quota 跨日等待 |

公司分布很宽：本 13 家 proposed eligible 30–201/家，线性值只用于排班，不是承诺。
CNINFO 日配额只计 WebAPI resultcode 信封，不计静态 PDF；wipe→track→首同步至少
`ceil(7801/Q)` 个配额日（另加 retry/restart）。Q 低于约 2.7k/日时会超过 2.9 天 GPU/1QPS
排班，实操以约 3k/日作为预警线。配额中断不推进该公司 checkpoint，下轮重做该公司完整
三年窗口（正确但会重复请求）；暂不扩为 chunk checkpoint，因为 37 chunk/公司可控。

瓶颈排序：GPU；CNINFO 日配额（额度低时升为第一）；1QPS；DB/build/publish。

## 4. Worker 韧性与毒丸

### 复现 / 证伪

- live 39 文档发生 45 次 `parser_invocation_failed`（33×1、6×2），39/39 后来成功：逐文档
  retry 基础有效，但旧 2h tick 隐式提供了恢复时间。
- live 有 3 个真实永久 publish poison：美的日常关联交易预计公告 build succeeded、unit=0，
  `EMPTY_RUN` 连续多轮重试；不是理论场景。
- live 有 2 个 oversized，SQL 已在 LIMIT 前排除；旧 report 的 skipped 计数却永远看不到它们。
- scratch 坏 PDF 与正常 PDF 同轮：坏件持久化 failed，正常件继续走完整链，证实 item isolation。
- 构造 parser version 探针挂起、build 和 publish 异常：修复前 version probe 无 timeout/不可被
  SIGTERM registry 杀死，且每文档重复探针；systemic build 又可把 raw 全部变为 build_failed。
- 构造 source_factory 永远失败、wrapped CNINFO 503、实际 `parse_timeout`：本地已有 parse 仍发布；
  503 首错即停剩余 source batch，下一轮 source 被冷却；quota→本地排水→再 quota 保持30→60m。
- API 成功信封的 records 非数组/含非对象、count/total 不完整此前会静默推进 checkpoint；现均
  fail-closed，37 个 chunk 只有全验证通过才推进。

### 根因一句话

异常隔离只包住 parse use case 的一部分，且固定节拍代替了显式基础设施退避和 dead-letter 观测。

### 修复 / 不修理由

- parser identity 在 item dequeue 前 preflight，进程内锁保护缓存版本并注入每个 fresh parser；
  探针 Popen 纳入活跃进程组、10s timeout 与 SIGTERM kill，失败不消耗任何文档 retry。parse pool
  从一次提交 50 改为最多 K 个 in-flight，SIGTERM 停止 refill、取消未开始 future。
- MinerU 活跃子进程组全局登记；SIGINT/SIGTERM 终止进程组，parse thread 把非零退出持久化
  为 retryable failure，再安全退出。singleton lock 的 kill -9 自动释放集成测试仍通过。
- BuildUnits 把 IR/rule preparation 纳入 failure marking；expected/OSError 持久化，unknown
  先记结构化失败再 re-raise，现有 attempt cap 不再被绕过。
- automatic pending_publish 必须 `EXISTS document_unit`；真实 3 个空 run 不再占槽，仍可人工
  `--allow-empty --reason`。doctor 显示 empty publish、oversized、parse dead letter。
- quota resultcode 407/408/412/429 同归 quota breaker；GPU outage 用 stage-local cooldown。
- parse/build/publish 每轮每项最多一次；parser/publish infra failure 与 systemic build failure 停止
  refill，最多影响已在途 K 项；build 恢复须先过 build-only probe，item-local IR poison 不触发全局
  cooldown。report 磁盘失败进入 system backoff，不制造 KeepAlive 重启风暴。
- 暂不增加 publish_attempt_count migration：当前实证 poison 是可确定识别的 empty run，已从
  自动队列隔离且有 doctor；完整 publish failure ledger/人工 requeue 是后续公共 ops 契约，
  不应为上量窗口扩大迁移面。非空 publish 系统错误目前仅有 stage cooldown（到期后最多再试
  K 项），没有持久 attempt cap；这是明确残余风险，需人工盯 doctor/report。

## 5. F006V 覆盖与 unknown 风险

### 复现 / 证伪

旧 `audit_unmapped_codes.py` 只扫 document，存在 survivor bias：未知码导致不下载，反而永远
不进审计。候选层去重审计先发现 011711 担保 145、011713 财务资助 3，以及 generic
0123/012399 家族内的减值、问询、说明会等码盲区；池外泛化审计又证明北交所大量依赖
generic code + title。

合并 vocab r6/r7 后，本轮候选层 17,490 个 F006V segment、下载层 5,219 个 segment 对账，
只剩 1 条 `0115`：美的 `1219254862`“解除股份限售提示”。0115 的 provider 名就是股权变动，
且其全部已知子码同属 `equity_share_change`，故 class_map 升 r7 映射父级；该类仍 register-only。
修复后两层均为 `(none beyond accepted generic misc buckets)`。

### 根因一句话

覆盖审计在处理 gate 之后取样，天然把最需要告警的未知候选过滤掉了。

### 修复 / 不修理由

- 新审计以 candidate snapshot 为主，TEXTID 去重后拆 F006V；document 只作 secondary check。
- doctor 每次统计 candidate unknown，并告警异常 process override；generic misc 只有
  0123/012399/01239999/352399 被显式接受，窄 title_topic 仍可救高值主题。
- `other` 永不处理只对**显式 generic bucket 且无 title_topic**可接受；任何新实码不进 generic
  白名单，必须进入 doctor/audit 并人工裁决。
- 当前 13 家没有科创板/BSE/B股；池外晶科能源、大地熊等688xxx语料已验证STAR词表泛化，
  BSE也已验证标题通道，但生产identity/下载全链仍未覆盖。正式200池须放STAR、BSE、沪B、
  深B各一只canary，并在首轮观察交易所路由与unknown码。

## 6. 验证基座防陈旧

### 复现 / 证伪

基线测试曾手写 Alembic revision（新增 migration 后必改），`PUBLIC_VIEWS` 又漏过 0019
新增视图；这两类绿灯都依赖实现者记得同步另一份常量。0022 初稿 revision 超过 ops 表
varchar(32) 时，新的 scratch migration gate 立即真实失败，也证明不能只做 import test。

### 根因一句话

验证代码复制实现清单，而不是向 Alembic graph / PostgreSQL catalog 查询事实。

### 修复

- `migration_state.py` 用官方 `ScriptDirectory.get_heads()`；doctor 与集成测试共享同一解析器，
  单 head 不是字符串常量。
- `catalog.py` 从 `information_schema.views` 枚举实际 public views；doctor 与 `PUBLIC_VIEWS`
  双向对账，并对实际每个 view 动态检查 app/reader/future_l2_reader SELECT 权限。
- schema integration 仍保留 `PUBLIC_VIEWS == actual catalog`，这样“新增视图忘加契约常量”和
  “常量写了但 migration 没建”都会失败，而不是删掉契约清单。
- migration roundtrip 从“只测某一 revision”改为所有 post-0008：head→0008→head，并断言
  0022 canonical constraints 消失/恢复；public column set 保持不变。

## 7. 验证记录

最终联合 diff 已完成以下门禁，并由未参与实现的只读审查路径复放故障注入后给出 GO：

- scratch PostgreSQL 全量 integration：85/85，248.517s（含真实 MinerU 3 样本、fresh
  checkpoint due/lifecycle、0022
  downgrade/upgrade、public view/权限、坏 PDF、单例锁）；
- 全量集成暴露的 kill -9 测试夹具 pipe `ResourceWarning` 已补 close/reap；全新 scratch
  定向复跑 1/1，0.822s，无 warning，advisory lock 仍归零；
- 真实 16-PDF resident backlog：16/16 parse/build/publish，36.340s（1,585.0 docs/h）；0.017s
  零等待第二轮；idle CPU `0:00.57` 维持10s不增长；
- candidate F006V audit：17,490 segment；0115 r7 修复后无未接受 gap；
- `make agent-check`：ruff、strict mypy（124 source files）、469 no-DB tests（80按环境skip）、
  `git diff --check` 全绿；JSON/plist/shell/config parse 另行全绿；
- 生产只读 doctor：candidate coverage、canonical identity、contested、public catalog/grants PASS；
  精确激活 blocker 为 migration 0021→0022、class r6→r7、title/topic/noise r7→r8；
- 生产只读 catalog/CLI：`sync_due_v1` 13/13 checkpoint join 匹配；改后 `track-status` 13/13
  `last_synced_at` 非空且 `synced_through=2026-07-13`，证伪历史 `\:` 是行为 bug；
- 独立审查复放 provider outage、parser probe hang、unknown/systemic/item-local build、build-only
  recovery 与并发错序（最坏 bounded K+1），无 material finding；
- 生产 DB 未 migrate/load-rules/wipe/track，launchd 未安装或重启。

## 8. 上量前一页 checklist

### 参数（先改 machine-local worker.env）

| 参数 | 上量值 | 理由 |
|---|---:|---|
| WORKER_BATCH_SYNC | 13 | 保持；限制单轮 source 时长，失败标记保证跨公司公平；checkpoint 仍受水位约束 |
| WORKER_BATCH_DOWNLOAD | **50** | 生产现值300要降；与parse配平、缩短kill/故障粒度、降低网络与磁盘突发 |
| WORKER_BATCH_PARSE | 50 | 保持；只是停止粒度，有进展不再睡 |
| WORKER_PARSE_CONCURRENCY | **8** | 仅 `*-http-client` + 有效 server URL；否则保持1；硬顶不上调 |
| MINERU_PROCESSING_WINDOW_SIZE | **16** | round22h OOM 红线，不上调 |
| DISCLOSURE_BACKFILL_MAX_PENDING_DOWNLOADS | 2000 | 保持并显式写env；实际是pending-download+全部raw/pending-parse总水位 |
| WORKER_LOOP_INTERVAL_SECONDS / MAX | 900 / 1800 | 只作用于空队列 |
| build / publish batch | 10 / 10 | 保持；parse 主链已内联二者，仅排中断残留 |

预计：无 quota 跨日时约 **69h**；排班按 3 天，另给 quota/disk/人工处置余量。不要用
16 个小 PDF 的 1,585 docs/h 做 ETA。

### 清库重跑顺序

1. **停旧 worker**：unload 当前 launchd；确认无 worker/MinerU 子进程且 singleton lock 已释放。
2. 保存需要的 watchlist 快照；确认 200 行无同发行人 A/B 双代码，至少含科创板688xxx、
   BSE、沪B、深B各一只 canary。
3. 在新代码上跑 `make agent-check` 与 `make doctor-full`。doctor 必须显示 migration head=0022；
   classification rules PASS（class=2026-07-r7、facet=2026-07-r1、title/topic/noise=2026-07-r11，
   预期 107/16/18/65/79）。只有 doctor 显示 migration 或规则落后时才分别执行 `make migrate` /
   `make load-rules` 后复跑 doctor；wipe 不清 migration/rules，已在目标版本时不要无条件重跑。
4. worker 仍停止时执行用户已决定的 `make wipe-test-data WIPE=YES`；核对 company/document/unit/
   access 均为 0。该命令会删业务表与 raw/derived/parser artifacts，不删 classification rules。
5. `make config-check` → `make track DRY_RUN=1` → `make track` → `make track-status`；确认200 active
   与exchange路由，随即在任何worker前跑`make doctor-full`，要求canonical security identity PASS、contested
   company identifiers none；科创板/BSE/B股均不得被路由成错误交易所。
6. 先手工 `make worker-once`：首批**最多**13家，实际会被candidate_count保守水位压到更少；
   观察 `synced_companies/deferred_backfill`。无 auth/quota/identity 异常后运行
   `scripts/install_launchd.sh`，确认 plist 为 KeepAlive + wrapper `loop`。
7. 安装、代码/config/env改动或`make load-rules`后都须显式
   `launchctl kickstart -k gui/$(id -u)/com.agentinvest.disclosure-worker`（或卸载重装）并再跑
   doctor。观察有进展轮是否 sleep=0；checkpoint 受 GPU 排水约束，GPU-only 约54h、
   连同此前 1QPS 下界约60h，不再期待16轮全齐；排空后应出现900→1800s sleep。

### 必盯信号 / 停线条件

- worker report：synced/downloaded/parsed、duration、`sync_quota_break/source_outage_break`、failure stage；
  `deferred_backfill>0` 在水位生效时是预期，随 processing 排水应下降；大轮持续低于约200 docs/h
  需查GPU/文档体量，不先加并发。当前没有单独 backlog CLI，须联合这些计数判断。
- `make track-status`：统计输出中 `last_synced_at=null`（或`synced_through=null`）的行数，应随排水
  单调趋零；该命令没有名为`never_synced`的聚合字段。已获checkpoint的`last_synced_at`必须推进。
- `make doctor-full`：candidate F006V、invalid override、canonical/contested identity、empty publish、
  parse dead letter、oversized、public catalog/grants。
- GPU/vLLM：OOM、EngineCore reset、connection-reset 成批出现；发生时保持 concurrency8/window16，
  让 parse cooldown 生效，排除其他显存负载。
- CNINFO：407/408/412/429；确认 quota 后 sync 冷却但 parsed 仍增长，checkpoint 未越过失败公司。
- 盘：raw + parser artifacts 增长和剩余空间；按约 17× artifact amplification 留余量。
- 首周词表：noise 单规则暴增、other top50、新 topic（担保/减值/重整/问询/回购）信噪；详见
  `vocab-generalization-2026-07-13.md` §7。

任一出现“同公司重复主体、checkpoint 错进、noise 被 override 翻案、并发>8、OOM 连续两批、
unknown F006V 实码”均停止扩池，先修事实边界；不要靠增 batch/retry 掩盖。
