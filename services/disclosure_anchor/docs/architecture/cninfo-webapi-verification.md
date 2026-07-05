# CNINFO WebAPI 验证记录

日期：2026-06-15

## 结论

`https://webapi.cninfo.com.cn/` 的官方 WebAPI 可以作为 `disclosure_anchor` 的 A 股公告索引和股票基础信息来源。

本次已确认：

- `股票-基本信息` 数据包：`packageId=13`
- `股票-公司公告` 数据包：`packageId=22`
- 官方 OAuth token 获取可用。
- `公司基本信息`、`股票基本信息`、`公告分类信息`、`公告基本信息` 四个接口均能用 API 返回 JSON 数据。

需要注意：

- 页面口径是“支持免费在线预览，如下载收费数据将扣除相应费用”，不是“无限免费批量下载”。
- API 文档错误码包含 `407 免费试用次数已用完`、`408 用户没有余额`、`412 包时长已超期` 等状态。
- 所以第一阶段可以把这些接口作为远端索引源使用，但生产同步要记录调用结果、额度/权限错误和失败重试。

## 鉴权方式

官方 `用法详解` 给出的方式是：

1. 用 Access Key / Access Secret 获取 token。
2. API 请求带上 `access_token`。

本地环境变量建议：

```bash
export CNINFO_ACCESS_KEY='...'
export CNINFO_ACCESS_SECRET='...'
export CNINFO_ACCESS_TOKEN='...' # 可选；优先用 key/secret 重新换取
```

Token endpoint：

```text
POST https://webapi.cninfo.com.cn/api-cloud-platform/oauth2/token
grant_type=client_credentials
client_id=${CNINFO_ACCESS_KEY}
client_secret=${CNINFO_ACCESS_SECRET}
```

本次测试结果：

- token endpoint 返回 HTTP 200。
- 返回 JSON 包含 `access_token`、`expires_in`、`refresh_token`。
- `expires_in` 实测为 3599 秒。

`docs/巨潮api.md` 不应保存明文凭证，只保留环境变量占位。

## 股票-基本信息

数据包地址：

```text
https://webapi.cninfo.com.cn/#/product/package?packageId=13
```

页面说明：

- 数据包名称：`股票-基本信息`
- 包含上市公司机构名称、证券简称、法人代表、注册地址、办公地址、主营业务、经营范围、董秘、证代等机构信息，以及证券类别、交易市场、上市日期等证券基本信息。
- 更新频率：实时。
- 提供方式：GET, POST。

### 公司基本信息

```text
API code: p_stock2100
URL: https://webapi.cninfo.com.cn/api/stock/p_stock2100
```

接口特征：

- `scode` 必填。
- `scode` 支持最多 50 只股票代码，用逗号分隔。
- 最大记录数：20000。
- 支持 `format`、`@column`、`@limit`、`@orderby`。

本次实测：

```text
GET /api/stock/p_stock2100?scode=000002&format=json&@limit=2&access_token=...
```

结果：

- HTTP 200
- `resultcode=200`
- `resultmsg=success`
- `total=1`
- 返回字段包括 `ORGID`、`ORGNAME`、`SECCODE`、`SECNAME`、注册地址、办公地址、主营业务、经营范围、行业分类、统一社会信用代码等。

### 股票基本信息

```text
API code: p_stock2101
URL: https://webapi.cninfo.com.cn/api/stock/p_stock2101
```

接口特征：

- `scode` 可选。
- `scode` 支持最多 50 只股票代码，用逗号分隔。
- 最大记录数：20000。
- 支持 `format`、`@column`、`@limit`、`@orderby`。

本次实测：

```text
GET /api/stock/p_stock2101?scode=000001,000002&format=json&@limit=3&access_token=...
```

结果：

- HTTP 200
- `resultcode=200`
- `resultmsg=success`
- `total=2`
- 返回字段包括 `ORGNAME`、`SECCODE`、`SECNAME`、证券类别、交易市场、上市日期、上市状态、ISIN 等。

## 股票-公司公告

数据包地址：

```text
https://webapi.cninfo.com.cn/#/product/package?packageId=22
```

页面说明：

- 数据包名称：`股票-公司公告`
- 数据包介绍：巨潮网上市公司相关公告 PDF 全文，权威、实时、高效。
- 包含接口：`公告分类信息`、`公告基本信息`。
- 更新频率：实时。
- 提供方式：GET, POST。

### 公告分类信息

```text
API code: p_info3005
URL: https://webapi.cninfo.com.cn/api/info/p_info3005
```

接口特征：

- `sortcode`：分类编码，只能查询一个分类代码。
- `parentcode`：父类编码；顶级分类为 `01`。
- 最大记录数：20000。
- 支持 `format`、`@column`、`@limit`、`@orderby`。

本次实测：

```text
GET /api/info/p_info3005?parentcode=01&format=json&@limit=5&access_token=...
```

结果：

- HTTP 200
- `resultcode=200`
- `resultmsg=success`
- `total=31`
- 返回字段包括 `SORTCODE`、`PARENTCODE`、`SORTNAME`、`F001D`、`F002D`。

用途：

- 本地库应保存公告分类字典，用于解释 `公告基本信息.F006V` 中的分类编码串。

### 公告基本信息

```text
API code: p_info3015
URL: https://webapi.cninfo.com.cn/api/info/p_info3015
```

接口特征：

- `scode`：股票代码。
- `sdate`：开始查询时间。
- `edate`：结束查询时间。
- `market`：市场编码，例如上交所 `012001`、科创板 `012029`、深交所主板 `012002`、深交所创业板 `012015`。
- `maxid`：增量起始 ID。
- `textid`：正文 ID。
- `page`、`pagesize`：分页。
- 支持 `format`、`@column`、`@limit`、`@orderby`。

接口说明里明确提到：

- 为保证响应时间，暂定每次最多返回 20000 条记录。
- 因公告数量较多，同一个类别公告一次只能请求一天的数据。
- 如果当天公告数量超过 20000 条，可以保存结果集最大 `OBJECTID`，下次用 `maxID` 增量提取。

文档矛盾点：

- 文档表格中的“最大记录数”显示为 `1`。
- 但接口说明、页面预览和本次 API 实测均表明它可以返回记录集，并支持分页/增量参数。
- 实现时以接口说明和实测为准，同时记录异常。

本次实测：

```text
GET /api/info/p_info3015?scode=000002&sdate=20200101&edate=20200105&page=1&pagesize=3&format=json&access_token=...
```

结果：

- HTTP 200
- `resultcode=200`
- `resultmsg=success`
- `total=2`
- 返回字段包括 `TEXTID`、`RECID`、`SECCODE`、`SECNAME`、`F001D`、`F002V`、`F003V`、`F004V`、`F005N`、`F006V`、`F007V`、`F008V`、`F009V`、`F010V`、`OBJECTID`、`RECTIME`。

字段含义：

- `TEXTID`：正文 ID。
- `RECID`：主体 ID。
- `SECCODE` / `SECNAME`：证券代码 / 证券简称。
- `F001D`：公告日期。
- `F002V`：公告标题。
- `F003V`：公告地址，实测为 `http://static.cninfo.com.cn/finalpage/.../*.PDF`。
- `F004V`：公告格式。
- `F005N`：公告大小。
- `F006V`：信息分类编码串。
- `F007V` / `F008V`：证券类别编码 / 名称。
- `F009V` / `F010V`：证券市场编码 / 名称。
- `OBJECTID`：增量游标。
- `RECTIME`：发布时间。

## 下载/扣费链路测试

用户已授权可以用新用户赠送额度做一次小额下载测试。本次没有成功触发扣费，原因是下载链路还缺少网页登录态或本地环境变量凭证。

已测试：

- 在 WebAPI 页面 `公司基本信息 / p_stock2100` 里点击 `数据导出`。
- 页面没有直接导出文件，而是弹出登录框并提示 `请先登录！`。
- 价格页只显示按次扣费 / 包时长的通用说明，未在未登录状态下展示明确单价。
- 不带 `access_token` 直接请求 `format=csv` 会返回 `resultcode=401`，提示未经授权访问。
- `docs/巨潮api.md` 已改为环境变量占位，不再保存明文 key；因此在没有 shell 环境变量的状态下，不能继续用 API 方式完成一次真实导出。

结论：

- 普通 JSON API 查询已经确认可用。
- 网页 `数据导出` 需要 CNINFO Web 平台登录态，不能只靠当前页面匿名状态完成。
- API 方式的 CSV/下载需要有效 `access_token`，后续应通过 `CNINFO_ACCESS_KEY` / `CNINFO_ACCESS_SECRET` 换取 token，或使用 `CNINFO_ACCESS_TOKEN`。
- 下一次要真正花一小笔额度测试下载，需要先满足二选一：
  - 用户在 in-app Browser 里登录 CNINFO WebAPI 平台。
  - 用户把 `CNINFO_ACCESS_KEY`、`CNINFO_ACCESS_SECRET` 或 `CNINFO_ACCESS_TOKEN` 注入当前 shell 环境。

## 2026-06-15 API 复测与落盘

用户已将 `CNINFO_ACCESS_KEY`、`CNINFO_ACCESS_SECRET`、`CNINFO_ACCESS_TOKEN` 注入项目根目录 `.env`。

本次复测从 `.env` 读取凭证，重新调用 OAuth token endpoint，并把 API 响应保存到项目根目录。第一次在沙盒内请求被本机网络权限拦截；随后经用户授权用非沙盒网络重跑成功。

复测结果：

- OAuth token endpoint：HTTP 200，返回 `access_token`，`expires_in=2685`。
- `p_stock2100` JSON：HTTP 200，`resultcode=200`，`total=1`。
- `p_stock2101` JSON：HTTP 200，`resultcode=200`，`total=2`。
- `p_info3005` JSON：HTTP 200，`resultcode=200`，`total=31`，本次保存 5 条。
- `p_info3015` JSON：HTTP 200，`resultcode=200`，`total=2`。
- `p_stock2100` CSV：HTTP 200，返回 2 行文本，已保存。
  - 响应头带计量字段：`api-count: 1`、`api-size: 2209`、`api-status: 200`。
  - `Content-Type` 标的是 xlsx 的 mime，但落地内容是纯 CSV；`Content-Disposition: attachment;filename=CSV_<uuid>.csv`。

根目录样例文件：

```text
cninfo_api_download_summary.json
cninfo_stock2100_000002.json
cninfo_stock2101_000001_000002.json
cninfo_info3005_parent01.json
cninfo_info3015_000002_20200101_20200105.json
cninfo_stock2100_000002.csv
```

Chrome 页面复核：

- 在 Chrome 已登录状态下打开 `https://webapi.cninfo.com.cn/#/admin/use`。
- 进入左侧 `使用情况` 后，页面显示 `API使用情况`，日期为 `2026-06-15`。
- 表格显示 `暂无数据`。
- 账户页可见赠送可用仍为 `100` 元。

当前解释：

- API 调用和数据落盘已经成功。
- 这些最小 JSON/CSV API 调用没有在 `使用情况` 页面即时形成可见记录，也没有扣减赠送余额。
- 这可能表示免费额度/未计费接口不会即时进入该页面，或平台使用统计存在延迟；实现层不能依赖该页面作为调用成功的唯一证据。
- 响应头 `api-count` 与面板赠送余额（仍为 100 元）对不上：`api-count=1` 更可能是“本次返回记录条数”的元数据，而非即时扣费金额。是否计费、是否延迟入账仍需在余额可见时用一次已知 `api-count` 的导出做前后差值校准。

## 对 disclosure_anchor 的影响

第一阶段可以不从网页 HTML 抓公告列表，优先使用 CNINFO WebAPI 做公告索引：

1. 定时同步 `p_stock2101` 或按需同步 `p_stock2100`，维护本地 `company/security` 基础表。
2. 定时同步 `p_info3005`，维护公告分类字典。
3. 按市场、日期、股票代码或分类调用 `p_info3015`，维护本地公告索引表。
4. 对重要公告或命中规则的公告，再下载 `F003V` 指向的 PDF 到本地 raw 文件库。
5. 本地数据库保存 `TEXTID`、`OBJECTID`、PDF URL、标题、发布时间、公告日期、分类编码、同步批次和下载/解析状态。

这样服务的第一阶段可以变成：

- 远端 WebAPI 作为权威索引源。
- 本地库保存同步快照、游标、重要公告原文和解析产物。
- HTML/front-end crawler 只作为 WebAPI 不可用时的备选方案。

## 风险和待确认

- 需要确认账号长期授权、调用额度、是否有正式套餐或余额要求。
- 需要在同步任务里处理 `401`、`404`、`405`、`407`、`408`、`412` 等权限/额度/过期错误。
- 需要验证“单日 + 分类”的实际限制：文档提到同一类别一次只能请求一天，但本次未按分类全量压测。
- 需要确认 `F005N` 的公告大小单位。
- 需要确认 PDF 地址是否长期稳定，以及是否存在 H5/非 PDF 附件需要另行接口处理。
