# SEC（EDGAR）Filing + XBRL/iXBRL 下载与落盘规范（草案）

> 目的：把“十年+（甚至更久）财报 filing + XBRL 结构化数据”的下载规则理顺，并与本仓库的 `raw/sec/{accession}/...` 目录契约对齐，方便后续 Skill2/Skill3 实现。

---

## 0) 给专家看的「需求描述」（可直接转发）

我们在做一个“公司研究证据池”工程，需要从 SEC EDGAR **长期、可追溯**地获取并落盘：

1. **Filing 本体（人读）**：10-K / 10-Q / 20-F / 6-K 等申报文件的“主文档”和关键附件（exhibits）。
2. **XBRL 数据（机读）**：同一份 filing 附带的结构化可机读数据（传统 XBRL 或 Inline XBRL/iXBRL）。

核心诉求：

- **时间跨度**：至少 10 年（希望支持更久；1994/1995 以后 EDGAR 都可访问）。官方索引从 1994Q3 起有 `/Archives/edgar/full-index` 与 `/Archives/edgar/daily-index`。  
- **可追溯性**：每个 accession 单独目录，保存下载清单（manifest）+ hash，能复现实证（“我当时用的就是这份披露”）。
- **规则要能覆盖 iXBRL 与老式 XBRL**：不同年份/不同发行人文件命名不同；需要一个稳健的“发现→下载→落盘→校验”策略。
- **落盘目录契约**（每个 accession 一个目录）：
  - `primary_document.html`：主文档（人读，通常为 HTML/HTM；iXBRL 时也嵌 XBRL 标签）
  - `primary_document.txt`：主文档纯文本（用于检索/切段/LLM；可由本地 HTML->text 生成）
  - `xbrl/`：XBRL 文件集（instance + taxonomy/linkbases 等，按原文件名保存）
  - `sections/`：从主文档（或 submission）本地解析抽取的 MD&A / Risk Factors / Business（best-effort）
  - `exhibits/`：高价值附件（如 99.1 新闻稿、重大合同等，后续按 VMF 或策略筛选）
  - `meta.yaml`：元数据（ticker/cik/accession/form/filed/report_date/is_ixbrl/primary_doc_original 等）
  - `manifest.yaml`：下载清单（文件名、大小、sha256、源 URL、完整性标记）

我们现在的难点是：**理顺 iXBRL 与传统 XBRL 的文件集合与差异**，并给出“不同发行人/不同年份”都能工作的下载规则（尤其是：哪些文件是必需、哪些可选、zip 是否完整、如何从官方目录可靠发现）。

---

## 1) 官方约束（必须遵守）

SEC 官方要求脚本下载 **声明 User-Agent + 控制频率**，并明确了当前的公平访问限速：

```text
User-Agent: <YourAppName> <ContactEmail@domain>
Max request rate: 10 requests/second
```

（参考：SEC “Accessing EDGAR Data / Fair access / Paths and directory structure”等官方页面。）

---

## 2) EDGAR Archives 的“真实目录结构”（最关键）

对单份 filing（一个 accession），官方目录通常形如：

```text
https://www.sec.gov/Archives/edgar/data/{CIK}/{ACCESSION_NO_NO_DASHES}/
```

并且目录下会有三个“机器友好”的索引文件：

- `index.html`
- `index.xml`
- `index.json`

`index.json` 会列出该目录下所有文件名与大小（非常适合脚本“发现→下载”）。

同一份 filing 还有“无中间目录”的 raw submission 路径（`.../data/{CIK}/{accession}.txt`），以及带中间目录的完整 submission 路径（`.../{accession_no_dashes}/{accession}.txt`）。

---

## 3) “人读 filing” vs “机读 XBRL”到底各是什么文件

### 3.1 人读（Filing 本体）

通常包含：

- **主文档（primary document）**：`.htm` / `.html`（浏览器打开就是你看到的 10-K/10-Q/20-F 正文）
- **submission text**：`{accession}.txt`（一整个“submission 包”，包含多个 `<DOCUMENT>`；用于强追溯/离线解析）
- **附件/Exhibits**：`.htm`、`.pdf`、`.jpg/.png` 等

> 注：很多 filing 目录里会有 `R1.htm`、`R2.htm`…（SEC viewer 拆分的页面/表格片段）以及 `FilingSummary.xml`（viewer 的目录映射）。

### 3.2 机读（XBRL / iXBRL）

XBRL 的核心是“**标签化的事实（facts）+ 上下文（context）+ 单位（unit）+ 维度（dimensions）**”。

常见文件（命名不完全固定，但模式固定）：

- **instance（事实数据）**：`.xml`
  - 传统 XBRL：常见 `{stem}.xml`（对应 EX-101.INS）
  - iXBRL：常见 `*_htm.xml`（从 iXBRL HTML 抽取出的实例；**文件名不一定和 `.xsd` 同 stem**，例如 JD 20-F 的 instance 是 `d871796d20f_htm.xml`，schema 是 `jd-20241231.xsd`）
- **schema（taxonomy）**：`.xsd`（对应 EX-101.SCH）
- **linkbases（结构/标签/计算/维度）**：`.xml`
  - `_pre.xml`（presentation）
  - `_lab.xml`（label）
  - `_cal.xml`（calculation）
  - `_def.xml`（definition）
- （可选）`*-xbrl.zip`：某些 filing 会提供一个 zip；但实际经验是 **zip 可能不包含全部关键文件**（例如缺 `*_htm.xml` 或 `FilingSummary.xml`），因此“以 `index.json` 为准”更稳。

---

## 4) iXBRL vs 传统 XBRL：如何判别 + 为什么要分情况

### 4.1 判别法（工程可落地）

对“主文档 HTML”：

- 若包含 `http://www.xbrl.org/2013/inlineXBRL` 或出现 `<ix:` 标签，则判定为 **iXBRL**
- 否则更可能是 **传统 XBRL**（XBRL 文件作为 exhibits/附件单独存在）

### 4.2 AAPL 示例（同一概念在两种文件里的表现）

以 AAPL 10-Q（2025-06-28）为例：

- 人读/展示（iXBRL HTML）：`aapl-20250628.htm`
  - HTML 里嵌了 `<ix:nonFraction ... name="us-gaap:Assets" scale="6">331,495</ix:nonFraction>`
  - `scale="6"` 表示展示值需要乘以 `10^6`（所以 331,495 → 331,495,000,000）
- 机读/事实（instance XML）：`aapl-20250628_htm.xml`
  - `<us-gaap:Assets contextRef="c-22" unitRef="usd">331495000000</us-gaap:Assets>`

而 AAPL 10-K（2018-09-29，2018-11-05 filed）是传统 XBRL：

- 主文档 `a10-k20189292018.htm` 不包含 `<ix:`（非 iXBRL）
- instance 是 `aapl-20180929.xml`，配套 `aapl-20180929.xsd` + `_pre/_lab/_cal/_def`

---

## 5) “十年+”下载策略（发现→下载→落盘→校验）

### Step A：ticker → CIK

优先用 SEC 官方映射文件：

- `https://www.sec.gov/files/company_tickers.json`

### Step B：CIK → filings（accession 列表）

用 SEC 官方 submissions JSON（公司提交历史）：

- `https://data.sec.gov/submissions/CIK##########.json`

这里能拿到近年 filing 的：

- `accessionNumber`
- `form`（10-K/10-Q/20-F/6-K…）
- `filingDate`
- `reportDate`
- `primaryDocument`（非常关键：主文档文件名）

> 十年+：如果 `recent` 不足以覆盖，可结合 `/Archives/edgar/full-index` / `daily-index` 回溯构建 accession 列表（代价更大，但官方可行）。

### Step C：对每个 accession，基于 Archives 目录下载

1) 先拉：

- `.../{accession_no_dashes}/index.json`

2) 再按规则下载：

- `primaryDocument`（落盘为 `primary_document.html`，并在 `meta.yaml` 记录原文件名）
- `{accession}.txt`（落盘为 `primary_submission.txt` 或直接放 `primary_document.txt` 的来源；实现可选）
- `xbrl/`：按 `index.json` 发现并下载：
  - `.xsd`（schema）
  - instance（**优先按模式找**：传统 XBRL 常见 `{stem}.xml`；iXBRL 常见 `*_htm.xml`，且不一定与 `.xsd` 同名；用 `index.json` 过滤 `_pre/_lab/_cal/_def` 后挑出 instance）
  - linkbases（`_pre/_lab/_cal/_def`）
  - `FilingSummary.xml`（建议存，方便定位报表/表格）

### Step D：落盘与完整性

- 每个 accession 写 `manifest.yaml`（文件列表、大小、sha256、源 URL、下载时间）
- 写 `meta.yaml`（ticker/cik/form/filed/report_date/is_ixbrl/primary_doc_original/xbrl_instance_file 等）
- 生成 `primary_document.txt`（本地 HTML→text；用于搜索、切段、LLM）
- `sections/`（本地解析抽取；best-effort）

---

## 6) 与本仓库 Phase 1 目录契约的对齐（建议）

将上述规则对齐到：

```text
company/{TICKER}/raw/sec/{accession}/
  meta.yaml
  manifest.yaml
  primary_document.html
  primary_document.txt
  sections/
  xbrl/
  exhibits/
```

并在 `current/filings_index.yaml` 中记录每个 accession 的元信息（form/filed/report_date/period_end/is_ixbrl/has_xbrl 等），供 Skill3 映射 period→accession。

---

## 7) Skill2 / Skill3 分工（推荐路线）

- Skill2（或 downloader 模块）负责 **as-filed 落盘**：
  - 以 SEC Archives `index.json` 为准，下载并落盘：
    - `primary_document.html` / `primary_document.txt`
    - `raw/sec/{accession}/xbrl/`：instance + `.xsd` + linkbases（优先不保留 `*-xbrl.zip`）
    - `sections/`：从 `primary_document` 本地 best-effort 抽取（MD&A / Risk Factors / Business 等）
    - `meta.yaml` / `manifest.yaml`：来源 URL + hash + 完整性标记
- Skill3（`extract-xbrl-timeseries`）负责 **消费本地 XBRL → 构建 Statement Atlas**：
  - 读取 `raw/sec/{accession}/xbrl/` 解析 instance + linkbases
  - 产出 `current/xbrl_atlas/*`（facts/nodes/edges/paths/periods），并保留 `accession` 溯源
- 可选 fallback（仅兜底）：当本地 XBRL 缺失/异常时，可临时用 SEC “已抽取”的 XBRL（例如 `sec_edgar_mcp.get_financials` / `companyfacts`）bootstrap，但必须在结果中明确标记为降级路径

---

## 8) 参考官方链接（集中放这里，方便审阅）

```text
SEC Accessing EDGAR Data:
  https://www.sec.gov/os/accessing-edgar-data

SEC Developer:
  https://www.sec.gov/developer

SEC company tickers mapping:
  https://www.sec.gov/files/company_tickers.json

SEC submissions (CIK JSON):
  https://data.sec.gov/submissions/CIK##########.json

SEC extracted XBRL (company facts / concept):
  https://data.sec.gov/api/xbrl/companyfacts/CIK##########.json
  https://data.sec.gov/api/xbrl/companyconcept/CIK##########/us-gaap/Assets.json

SEC Interactive Data C&DIs (Inline XBRL; EX-101.* / exhibit 101/104 guidance):
  https://www.sec.gov/rules-regulations/staff-guidance/compliance-disclosure-interpretations/interactive-data-cdi
```
