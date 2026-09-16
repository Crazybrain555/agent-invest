# MinerU 动态准入与有限 campaign 边界

动态准入控制正式 staged worker 的新远端解析责任。它根据观测压力调整
`C_target`，不修改解析语义、已接受任务、启动时的 N/P/H 或模型参数。
持续补位、动态准入、正式 M6 合格产出是三个不同的验证边界。

## 配置和身份

默认关闭。显式容量由 `DISCLOSURE_MINERU_CAPACITY_CONFIG` 与
`DISCLOSURE_MINERU_CAPACITY_CONFIG_SHA256` 成对提供；未配置时保留旧容量路径。
配置文件通过既有安全文件读取器核对绝对路径、文件所有权、权限和精确字节哈希。
显式容量的 H 是同一 API loop 的共享 HTTP 上限，不再乘以 N。
进程部署证据使用 `mineru.process-profile.v2`；缺少实际引擎证据的
`vllm_max_num_batched_tokens` 保持 null，不从编译批次或其他参数猜测。

动态控制另由 `DISCLOSURE_MINERU_STREAM_PRESSURE_CONFIG` 与
`DISCLOSURE_MINERU_STREAM_PRESSURE_CONFIG_SHA256` 成对启用；需要 staged 模式、
显式容量、实际 API/GPU 观测 URL 和独立选定的 runtime identity。
`mineru.stream-activation.v1` 是闭合配置，要求显式给出：

- runtime/capacity 哈希、完整 API owner、cgroup identity/max、GPU UUID；
- API/GPU 各自最大样本年龄；
- 全部策略字段：qualified_max、runtime/owner 哈希、GPU pause/reduce/recover bytes、
  host pause/recover bytes、sample_max_age_seconds、missing_pause_seconds、
  recovery_seconds、reduction_interval_seconds。

具体字段和严格类型见 `adapters/runtime/mineru_stream_activation.py`。
策略样本年龄不得小于任一数据源允许的年龄，qualified_max 不得超过启动容量 N。
配置中的 qualified_max 是已验证包络的声明；加载器不为该数字提供实机资格。
N/P/H、模型和 runtime/owner 变化需要新的完整绑定，不能拿旧文件热改后继续发任务。

## 观测和生命周期

API 的私有只读 `/agent/telemetry/pressure/v1` 返回自身 cgroup、VM 余量和 OOM
计数，并在有限读取前后核对进程/cgroup 身份。`ancestor_visibility=not_observed`
明确表示没有观察祖先 cgroup；本地 `memory.max=null` 也不代表整机无限内存。
实际部署仍须独立核实宿主及祖先限制，不能把声明的上限当作可分配内存保证。

worker 拥有 API 和 GPU 两个非 daemon reader，各用一个线程内持久 HTTP client。
调度器只读缓存，不在调度回路执行网络请求。初始有效样本在有限等待内取得；身份、
协议或启动失败不会悄悄退回关闭模式。相同 exporter 时间戳不会被重复 GET 续鲜。
普通传输失败、格式有效但 GPU 采集未成功或超过 30 秒的陈旧样本标为 unknown，
保留旧值及其原始时效，不补零或续鲜，reader 继续等待新样本。持续缺测依现有策略
暂停新提交，同一 reader 获得新鲜有效样本后可以恢复。身份、协议、成功样本时钟或
事件回退及内部错误仍为 unsafe；暂时不可用不能掩盖这些独立错误。原严格 capacity
sampler 仍拒绝陈旧或采集失败的样本，只有持续压力控制将其分类为可恢复缺测。

原始观测保存到 runtime root 下每个 owner 私有的 bounded rolling journal。
它只保留最近的有限片段，不能替代有限 M6 campaign 的完整测量证据。
关闭次序为 staged runtime 收尾、停止并回收 readers、关闭 journal；部分启动失败
也回收已启动线程。文件或线程错误保持可见。

## 准入和收尾

初始 C 为 0，第一份有效、未触发压力条件的样本才能放行。已知的任一 hard-low
优先触发 C0，即使另一数据源 unknown；降额只阻止新增，恢复需要完整新鲜观测及
配置的迟滞间隔。恢复只以新鲜的 GPU/host 内存迟滞为条件；H pending 既不否决恢复，
也不作为超过 qualified_max 的上调依据；pending 缺失仍是 unknown。identity/clock/OOM 等 unsafe 会锁住新增直到重新核对和恢复运行。

每次新增 remote_waits grant 核对 durable + provisional + 本次申请不超过当前 target。
降额后实际责任可以暂时高于 target；不能撤销已有 permit 来伪造立即达标。
HTTP 仍先查询幂等状态；仅在证明 absence 后、POST 真正开始前再检查一次准入。
已接受任务的查询、续租、下载、ACK、absence/disposal 不受新提交暂停影响。

普通压力暂停保留原 durable intent 和 credit，恢复后继续同一责任。
unsafe 且已证明未提交的 intent 被单独停放并继续续租；已接受任务和本地 tail
继续排空。最终以可见的 open-circuit 状态结束，保留未提交责任供恢复，不能把它们
记作业务成功或悄悄重建任务。进度输出分别报告 target、实际责任、原因和观测依据。

## 有限 campaign 与验收

`V4CampaignAdmissionScope` 绑定 M6 scope 和 corpus manifest，SQL 在排序和 LIMIT
之前过滤本轮普通/预备候选；新副作用前再次验证 scope。全局未闭合责任恢复仍不加
corpus 过滤，既有 carry-in 不得被计入本轮新增合格产出。
`build_staged_worker_v4_campaign_runtime` 是显式 scoped 构造入口，不能用未限定的
ordinary resident CLI 冒充有限 campaign。
薄入口 `cli/staged_campaign.py` 读取 SHA 钉扎的冻结 manifest/scope，复用单例锁、部署门、显式
stream activation、全局恢复与七 lane；准入上限就是冻结的普通成员本身，不另设配额，也不为此在协调器上加
计数钩子；截止时间或停止文件只关闭新准入，已接受任务照常排空。收据 `staged-v4-campaign.v1` 只投影
协调器的持久结果（admitted/completed/final_states/errors/credits）与 activation 角色（candidate/production
只是标签）；哪些成员被认领由持久 attempt 行核对，收据不自造文档级准入清单。
它不生成正式 M6 owner 事件、发布 credit 或资格。

既有 `staged_commission` 小批量入口也接入同一 owned pressure context，并将同一个
control 交给真实 staged runtime；未配置时保持关闭。它仍只接受显式的 1–8 个文档，
保留原部署检查、全局恢复边界与运行收尾，用于真实入库功能检查。此入口不生成正式
M6 owner 事件或公开消费者资格。

正式 M6 还需要有限运行入口把真实 PG attempt/commit 事实、完整原 owner 证据和
独立公开消费者检查接通。诊断 runner 的完整 PDF/ACK 结果、内存数据库 SQL shim、
单元测试和动态 target 曲线都不能替代这项验收。已有公开视图与解析合同不改变；
不因准入改动重开未受影响的全面解析质量验收。

现场顺序是：精确安装并验证 API pressure endpoint 与身份、核实资源包络、
有限 scoped worker 的暂停/恢复/异常收尾验证、再做规定的完整 M6 运行。
只有上一边界实际通过才进入下一边界；新 CLI、SQL 或运行入口必须有独立测试和
对应 scratch integration 验证。当前运行是否已通过记录在 HANDOFF/RUNTIME，
不由本文的实现说明推定。
