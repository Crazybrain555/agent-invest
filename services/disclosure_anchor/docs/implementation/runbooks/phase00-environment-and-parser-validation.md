---
id: disclosure_anchor_runbook_phase00
project: disclosure_anchor
title: Phase 00 执行清单（环境与样本 parser 验证）
status: complete
relates_to: docs/implementation/milestones/00-local-environment-and-parser-validation.md
created_at: 2026-06-26
---

> 已执行完成：A01–A05 均为 pass，fixtures 与报告见 `tests/fixtures/phase00/`。本文保留为历史执行记录。

# Phase 00 执行清单：环境与样本 parser 验证

本文件是 milestone 00 的**逐步执行 runbook**：milestone 文件讲"做什么/为什么/验收口径"，本文件讲"具体敲什么命令、产物落在哪、每步怎么自检"。

目标只有一个：**证明这台机器能干这件事**，并落出后续阶段可复用的 3 个样本 fixture。本阶段不建 repo 骨架、不建完整 DB schema、不写 app。

> 约定：外置盘根 `AGENT_SYSTEM=/Volumes/AgentSSD/agent_system`；代码仓库 `REPO=/Users/zhang/dev/agent-invest/services/disclosure_anchor`。

---

## 0. 当前已知环境实况（开工前先确认）

| 项 | 现状（2026-06-26 探测） | 本阶段动作 |
|---|---|---|
| `/Volumes/AgentSSD` | 已挂载 | 建骨架 + sentinel |
| PostgreSQL native | 未安装 / 不在 PATH | 安装 + initdb + 起停验证 |
| MinerU | 未安装 / 不在 PATH | 安装 + 模型缓存指外置盘 |
| 样本 PDF | 已在 `tmp/sample_filings/` | 直接选用，不另找 |

如果开工时 `/Volumes/AgentSSD` 没挂载：**停止**，先挂盘。本服务全程 fail-closed，不允许退回内置盘。

---

## 1. 外置盘骨架与挂载哨兵 → 验收 A01

```bash
test -d /Volumes/AgentSSD || { echo "AgentSSD 未挂载，停止"; exit 1; }

mkdir -p /Volumes/AgentSSD/agent_system
touch /Volumes/AgentSSD/agent_system/MOUNT_SENTINEL_DO_NOT_CREATE_ON_INTERNAL

mkdir -p /Volumes/AgentSSD/agent_system/shared/model_cache/{mineru,huggingface,modelscope}
mkdir -p /Volumes/AgentSSD/agent_system/shared/staging/{inbox,downloads,manual_uploads}
mkdir -p /Volumes/AgentSSD/agent_system/shared/tmp
mkdir -p /Volumes/AgentSSD/agent_system/postgres/{pg18-main,sockets,logs}
mkdir -p /Volumes/AgentSSD/agent_system/services/disclosure_anchor/data/{raw_documents,parser_artifacts}
mkdir -p /Volumes/AgentSSD/agent_system/services/disclosure_anchor/data/derived/{normalized_ir,document_unit_snapshots,exports}
mkdir -p /Volumes/AgentSSD/agent_system/services/disclosure_anchor/runtime/{inbox,quarantine,failed,tmp,locks,pid,logs}
mkdir -p /Volumes/AgentSSD/agent_system/services/disclosure_anchor/runtime/reports/{doctor,parse_quality}
```

自检：

- [ ] `MOUNT_SENTINEL_DO_NOT_CREATE_ON_INTERNAL` 在外置盘存在
- [ ] 内置盘 `/` 下**没有**误建 `agent_system`（`ls -d /agent_system 2>/dev/null` 应为空）
- [ ] 上述目录全部可写

---

## 2. 模型缓存环境变量 → 验收 A03

把缓存变量固化进一个可被后续命令 `source` 的文件，**不要**只在临时 shell 里 export：

```bash
cat > /Volumes/AgentSSD/agent_system/config/model_cache.env <<'EOF'
export MINERU_MODEL_CACHE=/Volumes/AgentSSD/agent_system/shared/model_cache/mineru
export HF_HOME=/Volumes/AgentSSD/agent_system/shared/model_cache/huggingface
export MODELSCOPE_CACHE=/Volumes/AgentSSD/agent_system/shared/model_cache/modelscope
EOF
# 注意：上一步若 config/ 不存在先 mkdir -p /Volumes/AgentSSD/agent_system/config
source /Volumes/AgentSSD/agent_system/config/model_cache.env
```

自检：

- [ ] 三个变量都指向外置盘
- [ ] 装 MinerU **之前**已 `source`，确保模型不落 `~/.cache`

---

## 3. PostgreSQL native cluster → 验收 A02

> 目标只是"能起能停、PGDATA 在外置盘"。本阶段**不**建 schema/role/表（那是 milestone 02）。

```bash
# 1) 安装（记录实际版本到报告，本 pack 假定 18.x）
brew install postgresql@18

PG=$(brew --prefix postgresql@18)/bin
PGDATA=/Volumes/AgentSSD/agent_system/postgres/pg18-main
SOCK=/Volumes/AgentSSD/agent_system/postgres/sockets
LOG=/Volumes/AgentSSD/agent_system/postgres/logs

# 2) 初始化 cluster 到外置盘（pg18-main 必须为空目录）
"$PG/initdb" -D "$PGDATA" -E UTF8 --locale=C \
  --username=disclosure_anchor \
  --auth-local=trust \
  --auth-host=scram-sha-256

# 3) 固化网络姿态进 PGDATA/postgresql.conf（第一层防御）：把 localhost-only TCP、55432、AgentSSD
#    socket 写进 cluster 自身配置。这样即使将来有人裸跑 `pg_ctl -D "$PGDATA" start`（不带 -o），
#    也不会漂回 PostgreSQL 默认（5432 / /tmp socket）。幂等：若旧 socket-only block 已存在，原地
#    替换成当前 block；若不存在，则追加一个当前 block。
python3 - "$PGDATA/postgresql.conf" "$SOCK" <<'PY'
import pathlib
import sys

conf = pathlib.Path(sys.argv[1])
sock = sys.argv[2]
lines = conf.read_text(encoding="utf-8").splitlines()

out = []
i = 0
while i < len(lines):
    if "disclosure_anchor hardening" in lines[i]:
        i += 1
        while i < len(lines):
            stripped = lines[i].strip()
            if (
                stripped == ""
                or stripped.startswith("# === disclosure_anchor hardening")
                or stripped.startswith("# === end disclosure_anchor PG settings")
                or stripped.startswith("port =")
                or stripped.startswith("listen_addresses =")
                or stripped.startswith("unix_socket_directories =")
            ):
                i += 1
                continue
            break
        continue
    out.append(lines[i])
    i += 1

block = [
    "",
    "# === disclosure_anchor hardening: persist localhost-only TCP plus AgentSSD socket ===",
    "port = 55432",
    "listen_addresses = 'localhost'",
    f"unix_socket_directories = '{sock}'",
    "# === end disclosure_anchor PG settings ===",
]
conf.write_text("\n".join(out).rstrip() + "\n" + "\n".join(block) + "\n", encoding="utf-8")
PY

# 4) 起 / 查 / 停。-o 里同名参数现在是"启动时再确认"（与 conf 同值，第二层防御），不再是唯一来源。
#    唯一合法启动方式：pg_ctl -D 本 PGDATA；禁止 `brew services start postgresql@18`（它指向内置盘默认 cluster）。
"$PG/pg_ctl" -D "$PGDATA" -l "$LOG/server.log" \
  -o "-k $SOCK -p 55432 -c listen_addresses=localhost" -w start

"$PG/pg_ctl" -D "$PGDATA" status

# 可选：若需要 IDE/DBeaver/VSCode 走 TCP，请从私有环境变量设置本地开发密码，不要写入 repo 文档。
# export DISCLOSURE_DEV_DB_PASSWORD='<set in private env>'
if [ -n "${DISCLOSURE_DEV_DB_PASSWORD:-}" ]; then
  "$PG/psql" -h "$SOCK" -p 55432 -U disclosure_anchor -d postgres \
    -v db_password="$DISCLOSURE_DEV_DB_PASSWORD" \
    -c "alter role disclosure_anchor password :'db_password';"
fi

"$PG/psql" -h "$SOCK" -p 55432 -U disclosure_anchor -d postgres \
  -c "select version(), current_setting('data_directory'), current_setting('listen_addresses');"
"$PG/pg_isready" -h 127.0.0.1 -p 55432 -U disclosure_anchor
"$PG/pg_ctl" -D "$PGDATA" -w stop
```

自检：

- [ ] `initdb` 把 PGDATA 建在 `pg18-main`，不在内置盘
- [ ] 三项（`port=55432` / `listen_addresses='localhost'` / `unix_socket_directories`）已写进 `postgresql.conf`
- [ ] 裸 `pg_ctl -D "$PGDATA" start`（不带 -o）也仍 localhost-only TCP + 55432 + AgentSSD socket（用 `show port/listen_addresses/unix_socket_directories` 验；`pg_isready -h 127.0.0.1 -p 55432` accepting，`pg_isready -h 127.0.0.1 -p 5432` no response）
- [ ] `pg_ctl start/stop` 均成功
- [ ] `psql` 能连上并打印版本
- [ ] 不使用、不依赖 `brew services start postgresql@18`
- [ ] 实际 PG 版本号已记入报告（若非 18.x，更新 `pg18-main` 命名约定的备注）

---

## 4. MinerU native 安装与样本解析 → 验收 A03 / A04 前置

> 安装步骤以 **MinerU 上游当前文档为准**（版本/后端/依赖变化频繁），本 runbook 不钉死命令；必须把"实际用的安装命令 + MinerU 版本 + 后端(pipeline / vlm-mlx)"如实写进报告。

要点（不可省）：

```bash
source /Volumes/AgentSSD/agent_system/config/model_cache.env   # 先加载缓存变量

# 按上游装好 MinerU（独立 venv，记录 Python 版本与 venv 路径）
# 验证缓存确实落外置盘：解析一份后检查
ls -la /Volumes/AgentSSD/agent_system/shared/model_cache/mineru
du -sh ~/.cache 2>/dev/null   # 不应因 MinerU 暴涨
```

选 3 类样本（仓库里已有，直接用）：

| 角色 | 文件 |
|---|---|
| `annual_report.pdf`（年报：heading + 长文本 + 表格） | `tmp/sample_filings/002484_江海股份/2026-04-10__periodic__002484__江海股份：2025年年度报告__1225087169.pdf` |
| `ir_activity.pdf`（投关：完整 Q&A） | `tmp/sample_filings/000333_美的集团/2025-04-11__investor_relations__000333__美的集团：2025年4月11日投资者关系活动记录表__1223071887.pdf` |
| `short_announcement.pdf`（短公告：少量事项型 text/table） | `tmp/sample_filings/002484_江海股份/2026-06-18__risk_or_forecast__002484__江海股份：……股票交易异常波动的公告__1225376481.pdf` |

对每个样本跑一遍 MinerU，原始输出落**外置盘**（体积大，不进 git）：

```text
/Volumes/AgentSSD/agent_system/services/disclosure_anchor/data/parser_artifacts/_phase00/<sample>/
```

自检：

- [ ] 3 个样本都能跑完，无崩溃
- [ ] 模型缓存确认落在外置盘，`~/.cache` 未暴涨
- [ ] 解析失败或超时的样本：记录完整错误，**不**调数据模型，转入第 8 节风险记录

---

## 5. 反推 fixture：normalized_ir.v1.json + document_units.v1.jsonl → 验收 A04 / A05

本阶段允许用**临时脚本或手工**把 MinerU 原始输出转成两个 fixture（正式 mapper 是 milestone 04/05 的事）。

fixture 落**仓库内**（小、可 git 跟踪、后续测试复用）：

```text
$REPO/tests/fixtures/phase00/annual_report/
$REPO/tests/fixtures/phase00/ir_activity/
$REPO/tests/fixtures/phase00/short_announcement/
```

每个目录至少产出：

```text
normalized_ir.v1.json          parser-neutral IR（本阶段先定一个能自洽的最小结构）
document_units.v1.jsonl         每行一个 text/table/qa unit
manual_review.md               肉眼复核记录（格式见 checks/fixture-and-test-policy.md 第 5 节）
parser_artifacts_ref.txt       指向外置盘原始输出目录的相对说明（不拷大文件进 repo）
```

> 重要：本阶段顺便把 `NormalizedIR v1` 和 `document_unit` payload 的**最小字段形状**从真实输出里定下来——这是当前 pack 里唯一没给模板的空白，Phase 00 正是定它的地方。定完后续 `contracts/normalized_ir/normalized_ir.v1.json` 以此为基。

肉眼复核（milestone 00 检查点）：

- [ ] 年报：至少 1 个 text unit + 1 个 table unit 基本可用
- [ ] 投关：至少 1 个 qa unit 基本可用
- [ ] 短公告：有 text/表格事项可见
- [ ] 章节 heading、表格完整性、Q&A 边界肉眼过得去（过不去就如实标 issue，不强行美化）

---

## 6. Phase 00 报告

写一份 `phase00-parser-validation.md`，建议落**仓库内**便于评审：

```text
$REPO/tests/fixtures/phase00/phase00-parser-validation.md
```

内容至少：

```text
环境：macOS 版本、Python 版本、PostgreSQL 实际版本、MinerU 版本与后端
3 个样本各自的 manual_review 结论（pass / needs_rule_adjustment / parser_unusable）
parser 已知风险（表格跨页、扫描件、公式等）
NormalizedIR v1 / document_unit 最小字段形状的初版定义或链接
未解决项与对 milestone 04/05 的影响
```

---

## 7. Phase 00 退出判据（全绿才进 milestone 01）

对照 `docs/implementation/checks/acceptance-matrix.md`，把以下项从 `pending` 改为 `pass`：

- [ ] **A01** 外置盘挂载 + sentinel 存在
- [ ] **A02** PostgreSQL native 可起停，PGDATA 在外置盘
- [ ] **A03** 模型缓存不落内置盘默认 cache
- [ ] **A04** 样本可生成 `normalized_ir.v1.json`
- [ ] **A05** 样本可生成 `document_units.v1.jsonl`

附加 DoD（milestone 00）：

- [ ] 3 个样本 fixture 齐备，可被后续测试引用
- [ ] `phase00-parser-validation.md` 存在
- [ ] 外置盘目录与缓存路径确认无误

退出后按 `003-agent-execution-rules.md` 第 3 节输出统一交付摘要，并更新 acceptance-matrix。

---

## 8. 常见失败与处理（照 milestone 00 第 8 节，不要绕过）

- MinerU 装不上 → 记录 Python 版本/依赖/完整错误，**不**进 milestone 01。
- 模型下到内置盘 → 清理 `~/.cache` 对应内容，重设缓存变量后重跑。
- 年报表格完全不可用 → 保留样本与错误，形成 parser 风险记录，**不**改数据模型。
- PostgreSQL 写不进外置盘 → 停止，修权限/路径，不退回内置盘。

> 任何一项失败都"停在本阶段、不向下游扩散"——这是 roadmap 第 5 节的硬规则。
