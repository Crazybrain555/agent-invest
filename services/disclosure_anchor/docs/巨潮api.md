# 巨潮 CNINFO 凭据与代码落位

本文件只记录变量名和落位约定，不保存真实凭据。

## 环境变量

```bash
CNINFO_ACCESS_KEY=<set_in_private_env>
CNINFO_ACCESS_SECRET=<set_in_private_env>
CNINFO_ACCESS_TOKEN=<optional_set_in_private_env>
```

推荐把真实值放在仓库外：

```text
~/.config/disclosure_anchor/cninfo.env
```

运行未来服务命令前加载：

```bash
set -a
source ~/.config/disclosure_anchor/cninfo.env
set +a
```

也可以使用 shell profile、macOS Keychain、外置盘私有 config 或其他 secret provider 注入同名环境变量。

## 未来代码落位

- `src/disclosure_anchor/settings.py`：只从环境变量读取 `CNINFO_ACCESS_KEY`、`CNINFO_ACCESS_SECRET`、可选 `CNINFO_ACCESS_TOKEN`。
- `src/disclosure_anchor/application/ports/disclosure_source.py`：定义 provider-neutral 的 `DisclosureSourcePort`。
- `src/disclosure_anchor/adapters/sources/cninfo/`：实现 CNINFO token、HTTP client、mapper、rate limit、retry。
- domain 和 use case 不直接依赖 CNINFO 字段、HTTP、token 或本地 env 文件。

## 接口资料

CNINFO API code、参数、字段、result envelope 以这些文档为准：

```text
docs/architecture/cninfo-webapi-usage-reference.md
docs/architecture/cninfo-interfaces.schema.json
```
