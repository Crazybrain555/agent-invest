# CNINFO WebAPI 用法参考

日期：2026-06-15

本文固化巨潮 WebAPI 页面上与 `disclosure_anchor` 第一阶段最相关的官方用法和接口口径。后续写 API client、downloader、router、同步任务时，优先看本文和 `cninfo-webapi-verification.md`，不要再反复依赖网页登录页面。

来源页面：

- `https://webapi.cninfo.com.cn/#/apiDoc`
- `https://webapi.cninfo.com.cn/#/product/package?packageId=13`
- `https://webapi.cninfo.com.cn/#/product/package?packageId=22`
- `https://webapi.cninfo.com.cn/#/interface?packageId=13&apiId=f712909f097f4dfcb62ec69006439ac6`
- `https://webapi.cninfo.com.cn/#/interface?packageId=13&apiId=337da212118b47b2b3cecda598d4ac43`
- `https://webapi.cninfo.com.cn/#/interface?packageId=22&apiId=89f5d71e8ddd4422bb91d1e89516192b`
- `https://webapi.cninfo.com.cn/#/interface?packageId=22&apiId=a0fec4cde3bf4f83821fb5a769231100`

## 结论

第一阶段只需要四个接口：

| 用途 | 数据包 | 接口 | API code | URL |
| --- | --- | --- | --- | --- |
| 公司维表 | 股票-基本信息 | 公司基本信息 | `p_stock2100` | `https://webapi.cninfo.com.cn/api/stock/p_stock2100` |
| 证券维表 | 股票-基本信息 | 股票基本信息 | `p_stock2101` | `https://webapi.cninfo.com.cn/api/stock/p_stock2101` |
| 公告分类字典 | 股票-公司公告 | 公告分类信息 | `p_info3005` | `https://webapi.cninfo.com.cn/api/info/p_info3005` |
| 公告索引 | 股票-公司公告 | 公告基本信息 | `p_info3015` | `https://webapi.cninfo.com.cn/api/info/p_info3015` |

实现顺序建议：

1. 先封装 token 获取和通用请求。
2. 再封装 `p_stock2101` / `p_stock2100` 维护证券和公司维表。
3. 再封装 `p_info3005` 维护公告分类字典。
4. 最后封装 `p_info3015` 按日期、股票、市场或增量游标同步公告索引。

## 鉴权与调用

凭证只从环境变量读取：

```bash
CNINFO_ACCESS_KEY=...
CNINFO_ACCESS_SECRET=...
CNINFO_ACCESS_TOKEN=... # 可选；优先用 key/secret 换新 token
```

token endpoint：

```text
POST https://webapi.cninfo.com.cn/api-cloud-platform/oauth2/token
grant_type=client_credentials
client_id=${CNINFO_ACCESS_KEY}
client_secret=${CNINFO_ACCESS_SECRET}
```

官方 `用法详解` 页展示的是通用示例，不是每个接口的定制示例。示例逻辑是：

1. 用 `client_credentials`、`client_id`、`client_secret` 请求 token。
2. 从返回 JSON 取 `access_token`。
3. 调用实际 API 时把 `access_token` 放进 query string。
4. 解析返回 JSON 的 `records`。

注意：官方示例代码里的接口 URL 使用了 `api/public/p_public0005?subtype=002`，它只是通用演示，不是本文四个目标接口。实现时必须替换成目标 API code。

最小调用形态：

```text
GET https://webapi.cninfo.com.cn/api/stock/p_stock2101?scode=000002&format=json&access_token=${TOKEN}
```

POST 也可用。对于第一阶段，实现 GET 足够；需要隐藏参数或避免 URL 过长时再补 POST。

## 通用参数

四个目标接口都支持这些通用参数：

| 参数 | 中文 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- | --- |
| `format` | 结果集格式 | string | 否 | 可选 `xml`、`json`、`csv`、`dbf` |
| `@column` | 结果列选择 | string | 否 | 多列用逗号分隔，例如 `@column=a,b` |
| `@limit` | 结果条数限制 | int | 否 | 设置返回条数 |
| `@orderby` | 结果集排序 | string | 否 | 例如 `@orderby=id:desc` 或 `@orderby=id:asc` |

本服务默认用 `format=json`。CSV/DBF 只作为人工导出或兼容测试路径，不作为主同步格式。

## 通用错误码

| 错误码 | 含义 | 处理建议 |
| --- | --- | --- |
| `-1` | 系统繁忙 | 重试，记录失败 |
| `200` | success | 正常写入本地同步结果 |
| `401` | 未经授权的访问 | 检查 token 是否缺失、IP/权限是否受限 |
| `402` | 不合法的参数 | 记录参数并停止该批次 |
| `403` | 脚本服务器异常 | 重试，必要时降频 |
| `404` | token 无效 | 重新获取 token |
| `405` | token 过期 | 重新获取 token |
| `406` | 用户已被禁用 | 停止同步，人工处理 |
| `407` | 免费试用次数已用完 | 停止或切换正式套餐 |
| `408` | 用户没有余额 | 停止付费下载/导出 |
| `409` | 验证权限错误 | 检查接口权限 |
| `410` | 验证权限异常 | 记录并人工处理 |
| `411` | 获取用户信息失败 | 重试或人工处理 |
| `412` | 包时长已超期 | 检查套餐有效期 |

实现时不要只看 HTTP status。CNINFO 常见形态是 HTTP 200 内部返回 `resultcode`。

## 数据包 13：股票-基本信息

页面地址：

```text
https://webapi.cninfo.com.cn/#/product/package?packageId=13
```

官方页面说明：该包包含上市公司机构名称、证券简称、法人代表、注册地址、办公地址、主营业务、经营范围、中介机构、董秘、证代等机构信息，也包含证券类别、交易市场、上市日期等证券基本信息。

页面显示：

- 更新频率：实时
- 提供方式：GET, POST
- 包含接口：公司基本信息、股票基本信息、股票所属板块、板块成份股数据、股票背景资料

第一阶段只用 `公司基本信息` 和 `股票基本信息`。

### 公司基本信息：p_stock2100

接口页面：

```text
https://webapi.cninfo.com.cn/#/interface?packageId=13&apiId=f712909f097f4dfcb62ec69006439ac6
```

接口元数据：

| 项 | 值 |
| --- | --- |
| API code | `p_stock2100` |
| 中文名 | 公司基本信息 |
| URL | `https://webapi.cninfo.com.cn/api/stock/p_stock2100` |
| 请求方式 | GET, POST |
| 最大记录数 | 20000 |

输入参数：

| 参数 | 中文 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- | --- |
| `scode` | 股票代码 | string | 是 | 不超过 50 只股票代码，用逗号分隔，例如 `000001,600000` |
| `format` | 结果集格式 | string | 否 | `xml`、`json`、`csv`、`dbf` |
| `@column` | 结果列选择 | string | 否 | 多列逗号分隔 |
| `@limit` | 结果条数限制 | int | 否 | 返回条数 |
| `@orderby` | 结果集排序 | string | 否 | 例如 `id:desc` |

输出字段：

| 字段 | 中文 | 类型 | 说明 |
| --- | --- | --- | --- |
| `ORGID` | 机构ID | varchar(11) |  |
| `ORGNAME` | 机构名称 | varchar(100) |  |
| `SECCODE` | 证券代码 | varchar(10) |  |
| `SECNAME` | 证券简称 | varchar(40) |  |
| `F001V` | 英文名称 | varchar(100) |  |
| `F002V` | 英文简称 | varchar(40) |  |
| `F003V` | 法人代表 | varchar(40) |  |
| `F004V` | 注册地址 | varchar(100) |  |
| `F005V` | 办公地址 | varchar(150) |  |
| `F006V` | 邮政编码 | varchar(10) |  |
| `F007N` | 注册资金 | numeric(14,4) |  |
| `F008V` | 货币编码 | varchar(12) |  |
| `F009V` | 货币名称 | varchar(60) |  |
| `F010D` | 成立日期 | DATE |  |
| `F011V` | 机构网址 | varchar(80) |  |
| `F012V` | 电子信箱 | varchar(80) |  |
| `F013V` | 联系电话 | varchar(60) |  |
| `F014V` | 联系传真 | varchar(60) |  |
| `F015V` | 主营业务 | varchar(500) |  |
| `F016V` | 经营范围 | varchar(4000) |  |
| `F017V` | 机构简介/公司成立概况 | varchar(2000) |  |
| `F018V` | 董事会秘书 | varchar(40) |  |
| `F019V` | 董秘联系电话 | varchar(60) |  |
| `F020V` | 董秘联系传真 | varchar(60) |  |
| `F021V` | 董秘电子邮箱 | varchar(80) |  |
| `F022V` | 证券事务代表 | varchar(40) |  |
| `F023V` | 上市状态编码 | varchar(12) |  |
| `F024V` | 上市状态 | varchar(60) |  |
| `F025V` | 所属省份编码 | varchar(12) |  |
| `F026V` | 所属省份 | varchar(60) |  |
| `F027V` | 所属城市编码 | varchar(12) |  |
| `F028V` | 所属城市 | varchar(60) |  |
| `F029V` | 中上协一级行业编码 | varchar(12) |  |
| `F030V` | 中上协一级行业名称 | varchar(60) |  |
| `F031V` | 中上协二级行业编码 | varchar(60) |  |
| `F032V` | 中上协二级行业名称 | varchar(60) |  |
| `F033V` | 申万行业分类一级编码 | varchar(60) |  |
| `F034V` | 申万行业分类一级名称 | varchar(60) |  |
| `F035V` | 申万行业分类二级编码 | varchar(60) |  |
| `F036V` | 申万行业分类二级名称 | varchar(60) |  |
| `F037V` | 申万行业分类三级编码 | varchar(60) |  |
| `F038V` | 申万行业分类三级名称 | varchar(60) |  |
| `F039V` | 会计师事务所 | varchar(200) |  |
| `F040V` | 律师事务所 | varchar(200) |  |
| `F041V` | 董事长 | varchar(60) |  |
| `F042V` | 总经理 | varchar(60) |  |
| `F043V` | 公司独立董事(现任) | varchar(100) | 多名 |
| `F044V` | 入选指数 | varchar(1000) | 多个 |
| `F045V` | 最新报告预约日期 | varchar(50) |  |
| `F046V` | 保荐机构 | varchar(500) | 多个 |
| `F047V` | 主承销商 | varchar(500) |  |
| `F048V` | PEVC标记 | varchar(12) |  |
| `F049V` | 注册国家 | varchar(200) |  |
| `F050V` | 统一社会信用代码 | varchar(60) |  |
| `F051V` | 工商ID | varchar(60) |  |
| `F052V` | 可转债 | varchar(100) |  |
| `F053V` | CDR | varchar(100) |  |
| `F054V` | 企业规模 | varchar(20) |  |

### 股票基本信息：p_stock2101

接口页面：

```text
https://webapi.cninfo.com.cn/#/interface?packageId=13&apiId=337da212118b47b2b3cecda598d4ac43
```

接口元数据：

| 项 | 值 |
| --- | --- |
| API code | `p_stock2101` |
| 中文名 | 股票基本信息 |
| URL | `https://webapi.cninfo.com.cn/api/stock/p_stock2101` |
| 请求方式 | GET, POST |
| 最大记录数 | 20000 |

输入参数：

| 参数 | 中文 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- | --- |
| `scode` | 股票代码 | string | 否 | 不超过 50 只股票代码，用逗号分隔，例如 `000001,600000` |
| `format` | 结果集格式 | string | 否 | `xml`、`json`、`csv`、`dbf` |
| `@column` | 结果列选择 | string | 否 | 多列逗号分隔 |
| `@limit` | 结果条数限制 | int | 否 | 返回条数 |
| `@orderby` | 结果集排序 | string | 否 | 例如 `id:desc` |

输出字段：

| 字段 | 中文 | 类型 | 说明 |
| --- | --- | --- | --- |
| `ORGNAME` | 机构名称 | varchar |  |
| `SECCODE` | 证券代码 | varchar |  |
| `SECNAME` | 证券简称 | varchar |  |
| `F001V` | 拼音简称 | varchar |  |
| `F002V` | 证券类别编码 | varchar |  |
| `F003V` | 证券类别 | varchar |  |
| `F004V` | 交易市场编码 | varchar |  |
| `F005V` | 交易市场 | varchar |  |
| `F006D` | 上市日期 | datetime |  |
| `F007N` | 初始上市数量 | decimal | 单位：股 |
| `F008V` | 代码属性编码 | varchar |  |
| `F009V` | 代码属性 | varchar |  |
| `F010V` | 上市状态编码 | varchar |  |
| `F011V` | 上市状态 | varchar |  |
| `F012N` | 面值 | decimal | 单位：元 |
| `F013V` | ISIN | varchar |  |

## 数据包 22：股票-公司公告

页面地址：

```text
https://webapi.cninfo.com.cn/#/product/package?packageId=22
```

官方页面说明：该包提供巨潮网上市公司相关公告 PDF 全文，页面描述其特点为权威、实时、高效。

页面显示：

- 更新频率：实时
- 提供方式：GET, POST
- 包含接口：公告分类信息、公告基本信息

这两个接口都要用：`p_info3005` 做分类字典，`p_info3015` 做公告索引。

投资者关系活动记录表、投资者关系管理信息、业绩说明会等投资者交流类 PDF 也通过 `p_info3015`
返回。实测这类记录可能使用 `012001` 等分类编码，不一定完全落在普通公告分类树的 `010...`
编码下；同步器不能只保留年报或常规公告分类。

### 公告分类信息：p_info3005

接口页面：

```text
https://webapi.cninfo.com.cn/#/interface?packageId=22&apiId=89f5d71e8ddd4422bb91d1e89516192b
```

接口元数据：

| 项 | 值 |
| --- | --- |
| API code | `p_info3005` |
| 中文名 | 公告分类信息 |
| URL | `https://webapi.cninfo.com.cn/api/info/p_info3005` |
| 请求方式 | GET, POST |
| 最大记录数 | 20000 |

输入参数：

| 参数 | 中文 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- | --- |
| `sortcode` | 分类编码 | string | 否 | 只能查询一个分类代码 |
| `parentcode` | 父类编码 | string | 否 | 传入父类编码可查询所属分类编码，顶级分类为 `01` |
| `format` | 结果集格式 | string | 否 | `xml`、`json`、`csv`、`dbf` |
| `@column` | 结果列选择 | string | 否 | 多列逗号分隔 |
| `@limit` | 结果条数限制 | int | 否 | 返回条数 |
| `@orderby` | 结果集排序 | string | 否 | 例如 `id:desc` |

输出字段：

| 字段 | 中文 | 类型 | 说明 |
| --- | --- | --- | --- |
| `SORTCODE` | 类目编码 | VARCHAR |  |
| `PARENTCODE` | 父类编码 | VARCHAR |  |
| `SORTNAME` | 类目名称 | VARCHAR |  |
| `F001D` | 启用时间 | DATE |  |
| `F002D` | 停用时间 | DATE |  |

分类预览里可见顶层及公司公告相关分类，例如 `010112` 深市公司公告、`010113` 沪市主板公告、`010115` 创业板公司公告、`010123` 科创板公司公告、`010124` 深市主板注册制等。实现时应保存完整分类字典，不要把这些分类硬编码死。

### 公告基本信息：p_info3015

接口页面：

```text
https://webapi.cninfo.com.cn/#/interface?packageId=22&apiId=a0fec4cde3bf4f83821fb5a769231100
```

接口元数据：

| 项 | 值 |
| --- | --- |
| API code | `p_info3015` |
| 中文名 | 公告基本信息 |
| URL | `https://webapi.cninfo.com.cn/api/info/p_info3015` |
| 请求方式 | GET, POST |
| 最大记录数 | 页面表格显示 `1`，但接口说明写每次最多 20000；实测可返回多条记录 |

接口说明要点：

- 用于获取公告信息。
- 为保证响应时间，接口说明称每次最多返回 20000 条。
- 公告数量较多时，同一个类别的公告一次只能请求一天的数据。
- 如果当天公告数量超过 20000 条，应保存结果集里的最大 `OBJECTID`，下次用 `maxID`/`maxid` 传入实现增量提取。
- 页面“最大记录数”表格显示 `1`，与说明和实测结果矛盾；实现以实测和说明为准，但保留告警。

输入参数：

| 参数 | 中文 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- | --- |
| `scode` | 股票代码 | string | 否 | 输入 1 个股票；`scode` 和 `edate` 同时为空时默认返回最近 100 条 |
| `sdate` | 开始查询时间 | string | 否 | 支持 `20161101`、`2016-11-01`、`2016/11/01` |
| `edate` | 结束查询时间 | string | 否 | `scode` 和 `edate` 同时为空时默认最近 100 条；`scode` 为空但 `edate` 不为空时取 `edate` 当天数据 |
| `market` | 市场 | string | 否 | 上交所 `012001`，科创板 `012029`，深交所主板 `012002`，深交所创业板 `012015` |
| `maxid` | 增量起始ID | int | 否 | 用于增量提取 |
| `textid` | 正文ID | string | 否 | 按正文 ID 查询 |
| `page` | page | int | 否 | 分页页码 |
| `pagesize` | pagesize | int | 否 | 分页大小 |
| `format` | 结果集格式 | string | 否 | `xml`、`json`、`csv`、`dbf` |
| `@column` | 结果列选择 | string | 否 | 多列逗号分隔 |
| `@limit` | 结果条数限制 | int | 否 | 返回条数 |
| `@orderby` | 结果集排序 | string | 否 | 例如 `id:desc` |

输出字段：

| 字段 | 中文 | 类型 | 说明 |
| --- | --- | --- | --- |
| `TEXTID` | 正文ID | VARCHAR | 后续下载/去重可用 |
| `RECID` | 主体ID | VARCHAR |  |
| `SECCODE` | 证券代码 | VARCHAR |  |
| `SECNAME` | 证券简称 | VARCHAR |  |
| `F001D` | 公告日期 | DATE |  |
| `F002V` | 公告标题 | VARCHAR |  |
| `F003V` | 公告地址 | VARCHAR | 实测为 CNINFO PDF URL |
| `F004V` | 公告格式 | VARCHAR | 通常为 PDF |
| `F005N` | 公告大小 | DECIMAL | 单位待确认 |
| `F006V` | 信息分类 | VARCHAR | 分类编码串，需用 `p_info3005` 解释 |
| `F007V` | 证券类别编码 | VARCHAR |  |
| `F008V` | 证券类别名称 | VARCHAR |  |
| `F009V` | 证券市场编码 | VARCHAR |  |
| `F010V` | 证券市场名称 | VARCHAR |  |
| `OBJECTID` | OBJECTID | BIGINT | 增量游标 |
| `RECTIME` | 发布时间 | DATETIME |  |

## 本地实现口径

### 本地表/对象建议

> 注（2026-07）：本节是 canonical 收敛前的建模草案。实际落地对象以 `service-purpose.md` §10–§11 为准（source_access / document / source_checkpoint 等）；`cninfo_request_log` 对应 `source_access`，公告索引字段作为 `document` / `source_access` 的元数据与 payload 保存，不另建 `cninfo_*` 镜像表。CNINFO 字段名保持 provider 原名。

最小本地对象：

- `cninfo_security`：来自 `p_stock2101`。
- `cninfo_company_profile`：来自 `p_stock2100`。
- `cninfo_announcement_category`：来自 `p_info3005`。
- `cninfo_announcement_index`：来自 `p_info3015`。
- `cninfo_request_log`：本地请求日志，记录接口、参数 hash、HTTP status、`resultcode`、行数、耗时、token 刷新、错误信息。

### 同步策略

建议默认策略：

1. 每天先刷新 `p_stock2101`，必要时按股票刷新 `p_stock2100`。
2. 每天或每周刷新一次 `p_info3005`。
3. `p_info3015` 按 `market + date` 或 `scode + date` 同步公告索引。
4. 对全市场同步，优先用单日窗口，避免跨日大窗口。
5. 保存每批最大 `OBJECTID`，遇到当天记录过多时用 `maxid` 增量补拉。
6. 对重要公告再下载 `F003V` 指向的 PDF，PDF 下载和索引同步分开。

### 本地字段映射

公告索引必须至少保存：

- `TEXTID`
- `RECID`
- `SECCODE`
- `SECNAME`
- `F001D`
- `F002V`
- `F003V`
- `F004V`
- `F005N`
- `F006V`
- `F009V`
- `F010V`
- `OBJECTID`
- `RECTIME`
- 本地同步批次、下载状态、解析状态、原始响应摘要

公司/证券维表至少保存：

- 证券代码、证券简称、机构名称、证券类别、交易市场、上市日期、上市状态、ISIN。
- 机构 ID、注册地址、办公地址、主营业务、经营范围、行业分类、董秘/联系方式、统一社会信用代码。

投资者交流类记录建议先作为公告索引的一种 `filing_type` / `bucket` 保存，例如
`investor_relations`，后续解析时再提取活动日期、参与机构、接待人员、问答段落和主题标签。

### 注意事项

- 官方页面的 `用法详解` 示例是通用代码，不是目标接口专用代码。
- API 成功与否以 API response 为准，不以 Web 后台 `使用情况` 是否即时出现记录为准。
- 不要把真实凭证写进 repo；`docs/巨潮api.md` 只能保留环境变量占位。
- 未来测试生成的 `cninfo_*` 样例文件不要长期留在 repo root；需要 fixtures 时应放到明确的测试目录并去敏。
- `p_info3015` 的 `maxID` / `maxid` 大小写在页面说明和参数表里不完全一致；实际参数表是 `maxid`，实现可优先用小写并在必要时兼容大写。
