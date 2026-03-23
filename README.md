# AI量化实验室


# AIQuantInvestment

AIQuantLab 是一个面向量化投资的人工智能实验平台，专注于深度学习驱动的金融时序建模、策略开发与自动化回测。项目采用模块化、工程化设计，支持灵活的数据管道、模型扩展、自动化调度和实验管理。

## 项目背景

随着金融市场的复杂性和数据量的增长，传统的投资方法面临挑战。人工智能，特别是深度学习技术，为量化投资提供了新的机遇。&#8203;:contentReference[oaicite:3]{index=3}

## 项目结构

为了确保项目的有序推进和团队协作，AIQuantInvestment 采用了模块化的目录结构：
AIQuantLab/
├── README.md
├── .gitignore
├── main.py                  # 主程序入口，自动化调度
├── configs/                 # 配置文件（模型、数据库、策略等）
│   ├── db/
│   ├── models/
│   │   └── rnn/gru/dfzq.yaml
│   └── strategies/
├── data/                    # 数据目录（raw/processed/strategies）
│   ├── raw/                 # 原始数据
│   ├── processed/           # 处理后的数据
│   └── strategies/          # 策略相关数据
├── docs/                    # 项目文档
├── logs/                    # 日志输出
├── notebooks/               # Jupyter实验与分析
├── others/                  # 其他资源
├── tests/                   # 单元测试
├── src/
│   ├── backtesting/         # 回测系统
│   │   └── backtest.py      # 回测引擎
│   ├── data_service/        # 数据服务与加载
│   │   ├── data_loading/    # 数据加载模块
│   │   ├── preprocessing/   # 数据预处理模块
│   │   ├── data_engineering/# 数据工程模块
│   │   ├── data_saving/     # 数据保存模块
│   │   └── data_pipeline.py # 数据管道主类
│   ├── dataset/             # 数据集定义
│   │   └── dfzq_dataset.py  # 东方证券数据集
│   ├── evaluations/         # 评估与指标
│   ├── models/              # 模型实现
│   │   ├── base_model.py    # 基础模型类
│   │   ├── model.py         # 通用模型类
│   │   ├── model_factory.py # 模型工厂
│   │   └── rnn/             # RNN类模型
│   │       ├── gru/         # GRU类模型
│   │       │   └── dfzq_gru/# 东方证券GRU模型
│   │       │       ├── dfzq_gru.py    # 模型实现
│   │       │       ├── get_configs.py # 配置管理
│   │       │       └── test_dfzq_gru.py # 模型测试
│   │       └── lstm/        # LSTM类模型
│   ├── scheduler/           # 任务调度与自动化
│   │   ├── job_definitions.py # 任务定义
│   │   ├── job_runner.py    # 调度器
│   │   └── Dfzq_gru_scheduler.py # 东方证券GRU调度器
│   ├── strategies/          # 策略实现
│   │   ├── base_strategy.py # 基础策略类
│   │   └── strategy.py      # 机器学习策略
│   ├── tasks/               # 任务封装
│   │   ├── base.py          # 基础任务类
│   │   ├── etl.py           # ETL任务
│   │   ├── training.py      # 训练任务
│   │   ├── label_generation_task.py # 标签生成任务
│   │   ├── market_price_norm_data_initialization.py # 市场数据初始化
│   │   └── standardization_parameter_generation.py # 标准化参数生成
│   ├── train/               # 训练器与训练脚本
│   │   ├── trainer.py       # 基础训练器
│   │   └── Neural_networks/ # 神经网络训练
│   │       └── RNN/
│   │           └── DFZQ_GRU/
│   │               ├── train_dfzq_gru.py # 训练脚本
│   │               └── dfzq_gru_trainer.py # 训练器
│   └── utils/               # 工具函数
│       ├── logger.py        # 日志工具
│       ├── config_loader.py # 配置加载器
│       ├── db_connection.py # 数据库连接
│       ├── table_schema.py  # 表结构定义
│       └── visualization.py # 可视化工具
└── README.md

## 核心模块说明

### 1. 数据管道（Data Pipeline）

- **数据加载**：`src/data_service/data_loading/` 提供多种数据源的加载接口，支持本地文件、数据库、API等多种方式。
- **数据预处理**：`src/data_service/preprocessing/` 实现数据清洗、标准化、异常检测、缺失值处理等流程，适配金融时序特性。
- **数据工程**：`src/data_service/data_engineering/` 支持特征构造、标签生成、数据转换等高级操作。
- **数据保存**：`src/data_service/data_saving/` 提供多种数据存储方式，支持数据库、文件系统等。
- **数据管道**：`src/data_service/data_pipeline.py` 整合上述模块，提供完整的数据处理流程。

### 2. 模型开发与管理

- **模型实现**：`src/models/` 支持 RNN、GRU、LSTM、Transformer 等主流结构，采用 PyTorch 实现，便于自定义扩展。
- **东方证券GRU**：`src/models/rnn/gru/dfzq_gru/dfzq_gru.py` 为典型金融时序GRU模型，内置注意力机制和特征相关性约束。
- **模型配置**：所有模型参数、训练参数、优化器参数等均可通过 YAML 文件（如 `configs/models/rnn/gru/dfzq.yaml`）集中管理，支持实验复现。

### 3. 训练与评估

- **训练器**：`src/train/` 提供基础训练器和特定模型训练脚本，支持早停、学习率调度、自动保存、断点续训等功能。
- **评估指标**：`src/evaluations/` 支持 IC、RankIC、Sharpe、回撤等金融常用指标，便于策略效果量化。
- **日志记录**：训练过程自动记录 loss、IC、相关性等关键指标，支持 tensorboard/wandb 等可视化。

### 4. 策略与回测

- **策略实现**：`src/strategies/` 支持多种投资策略开发，便于与模型输出对接。
- **回测系统**：`src/backtesting/` 提供高效的回测引擎，支持多周期、多标的、滑点、手续费等真实市场细节。

### 5. 自动化调度与任务管理

- **任务定义**：`src/scheduler/job_definitions.py` 定义系统任务和调度配置。
- **调度器**：`src/scheduler/job_runner.py` 实现任务调度和执行。
- **特定模型调度**：`src/scheduler/Dfzq_gru_scheduler.py` 提供东方证券GRU模型的特定调度逻辑。
- **任务封装**：
  - **基础任务**：`src/tasks/base.py` 定义任务接口和通用功能。
  - **ETL任务**：`src/tasks/etl.py` 实现数据ETL流程。
  - **训练任务**：`src/tasks/training.py` 实现模型训练流程。
  - **标签生成**：`src/tasks/label_generation_task.py` 实现标签生成流程。
  - **市场数据初始化**：`src/tasks/market_price_norm_data_initialization.py` 实现市场数据初始化。
  - **标准化参数生成**：`src/tasks/standardization_parameter_generation.py` 实现标准化参数生成。

### 6. 日志与监控

- **日志系统**：`src/utils/logger.py` 提供统一日志接口，支持多级别日志、文件轮转、异常追踪，所有关键任务均有详细日志输出。
- **监控与告警**：可扩展集成邮件、钉钉等告警方式，便于生产环境监控。

### 7. 测试与开发规范

- **单元测试**：`tests/` 覆盖数据、模型、调度等核心模块，保障主干稳定。
- **开发规范**：遵循 PEP8，采用模块化、注释清晰、配置分离、版本控制等最佳实践。

## 快速上手

### 1. 环境准备

建议 Python 3.8+，安装依赖：

```bash
pip install -r requirements.txt
```

### 2. 配置管理

- 编辑 `configs/models/rnn/gru/dfzq.yaml` 或其他模型配置文件，调整模型结构、训练参数等。
- 数据路径、日志路径等可在 `configs/` 下统一管理。

### 3. 训练流程

def main():
    # 1. 初始化数据服务
    data_service = DataService()

    # 2. 获取原始数据
    raw_data = data_service.fetch_data()

    # 3. 数据预处理
    preprocessed_data = preprocess_data(raw_data)

    # 4. 特征工程
    features, labels = generate_features(preprocessed_data)

    # 5. 创建自定义数据集
    dataset = CustomDataset(features, labels)

    # 6. 使用 DataLoader 加载数据
    data_loader = DataLoader(dataset, batch_size=32, shuffle=True)

    # 7. 初始化模型
    model = InvestmentModel()

    # 8. 初始化策略
    strategy = InvestmentStrategy(model)

    # 9. 训练模型
    strategy.train(data_loader)

    # 10. 初始化回测器
    backtester = Backtester(strategy)

    # 11. 进行回测
    backtest_results = backtester.run_backtest()

    # 12. 输出回测结果
    print(backtest_results)

if __name__ == "__main__":
    main()
请根据您的需求修改和扩展上述代码。

## 4.GRU的部分训练代码的项目结构

```README.md
<code_block_to_apply_changes_from>
├── src/                    # 源代码目录
│   ├── models/            # 模型定义
│   │   ├── rnn/          # RNN类模型
│   │   │   ├── gru/      # GRU类模型
│   │   │   │   └── dfzq_gru/  # 东方证券GRU模型
│   │   │   │       ├── config.py      # 模型配置
│   │   │   │       ├── dfzq_gru.py    # 模型实现
│   │   │   │       ├── test_dfzq_gru.py # 模型测试
│   │   │   │       └── models/        # 模型参数存储
│   │   │   └── lstm/     # LSTM类模型
│   │   └── transformer/  # Transformer类模型
│   ├── data_engineering/  # 数据工程
│   ├── data_preprocessing/# 数据预处理
│   ├── data_service/     # 数据服务
│   ├── strategies/       # 策略实现
│   ├── backtesting/     # 回测系统
│   └── utils/           # 工具函数
├── train/                # 训练相关代码
│   ├── trainer.py       # 基础训练器
│   ├── dfzq_gru_trainer.py  # 东方证券GRU训练器
│   └── train_dfzq_gru.py   # 训练脚本
└── README.md
```



### 5. 手动训练与评估（以东方证券GRU为例）模型分类
- RNN类模型
  - GRU
    - 东方证券GRU模型
  - LSTM
- Transformer类模型

### 训练模块
训练模块(`train/`)包含：
1. 基础训练器(`trainer.py`)：提供通用的训练逻辑
2. 具体模型训练器：继承基础训练器，实现特定模型的训练逻辑
3. 训练脚本：用于启动训练任务

### 配置管理
每个具体模型都有自己的配置文件，用于管理：
- 模型参数
- 训练参数
- 优化器参数
- 数据参数
- 其他配置

## 使用说明

### 训练新模型
1. 在`src/models/`下创建新的模型实现
2. 在`train/`下创建对应的训练器
3. 创建训练脚本
4. 准备数据集
5. 配置模型参数
6. 运行训练脚本

### 示例：训练东方证券GRU模型
```python
from src.models.rnn.gru.dfzq_gru.get_configs import DFZQGRUConfig
from src.train.Neural_networks.RNN.DFZQ_GRU.train_dfzq_gru import train_dfzq_gru

# 加载配置
config = DFZQGRUConfig.default()

# 准备数据
dats = ...  # pd.DataFrame
sample_dates = ...  # List[int]
begin_year, end_year = 2018, 2023

# 训练
train_dfzq_gru(dats, sample_dates, config, begin_year, end_year)
```

## 贡献指南

欢迎提交 Pull Request 来改进项目。在提交之前，请确保：

1. 代码符合项目的编码规范
2. 添加了必要的测试用例
3. 更新了相关文档
4. 所有测试都能通过

## 许可证

本项目采用 MIT 许可证。详见 LICENSE 文件。

---

## WSL 下挂载 Space/NAS 共享（可选）

在 WSL/Linux 下无法直接访问 Windows 的 UNC 路径（例如 `\\space\forbid`），需要先把共享挂载成 Linux 路径，再通过环境变量让代码使用挂载路径。

### 1）一次性准备（首次）

```bash
sudo apt-get update && sudo apt-get install -y cifs-utils
sudo mkdir -p /mnt/space/forbid

# 写凭据文件（账号/密码可参考 configs/nas_disk/nas_config.yaml）
sudo tee /etc/cifs-space-cred >/dev/null <<'EOF'
domain=space
username=bsshare
password=YOUR_PASSWORD_HERE
EOF
sudo chmod 600 /etc/cifs-space-cred
```

### 2）挂载 + 运行（WSL 重启/Windows 重启后需要）

```bash
sudo umount /mnt/space/forbid 2>/dev/null || true
sudo mount -t cifs //space/forbid /mnt/space/forbid \
  -o credentials=/etc/cifs-space-cred,vers=3.0,sec=ntlmssp,iocharset=utf8,uid=$(id -u),gid=$(id -g)

export NAS_BASE_PATH=/mnt/space/forbid
python master_scheduler.py
```

说明：
- 仅重启 VS Code/Cursor 通常不会导致挂载丢失；但**新开一个终端**需要重新 `export NAS_BASE_PATH=...`（建议写到 `~/.bashrc`）。
- 如果 `mount` 报协议/认证错误，可尝试把 `vers=3.0` 改为 `vers=2.1`，或查看 `dmesg | tail` 的 CIFS 日志定位原因。

## Codex MCP 依赖说明

### 3.1 MCP 清单表格

| 名称 | 用途 | 类型 | 安装来源 | Phase 1 必需 |
| --- | --- | --- | --- | --- |
| context7 | 库文档查询 | HTTP | `https://mcp.context7.com/mcp` | 可选 |
| sec_edgar_mcp | SEC EDGAR 文件检索 | stdio | `python -m sec_edgar_mcp.server`（aiquantlab 环境） | 必需 |
| fs | MCP 文件系统访问 | stdio | `npx -y @modelcontextprotocol/server-filesystem` | 必需 |
| fetch | 网页抓取 | stdio | `.venvs/mcp-fetch` + `python -m mcp_server_fetch` | 可选 |
| alpaca | Alpaca 市场数据/交易 | stdio | `alpaca-mcp-server` 本地可执行文件 | 必需 |
| rss | RSS 新闻订阅 | stdio | 本地 `rss-mcp` Node 服务 | 可选 |
| gdelt | GDELT 新闻/事件搜索 | stdio | 本地 `GDELT-mcp` Node 服务 | 可选 |
| trading_mcp | 股票筛选/基本面指标 | stdio | 本地 `trading-mcp` Node 服务 | 必需 |
| search | DuckDuckGo 搜索/网页提取 | stdio | `.venvs/mcp-search` + `mcp-search-server` | 可选 |
| openalex | 学术论文检索 | stdio | 本地 `openalex-research-mcp` Node 服务 | 可选 |
| crossref | 学术文献 DOI 查询 | stdio | WSL 本地固定安装 `@botanicastudios/crossref-mcp` | 可选 |
| pubmed | 医学文献检索 | stdio | `.venvs/pubmed-mcp` + `pubmed_server.py` | 可选 |
| arxiv | arXiv 论文检索 | stdio | 本地 `arxiv-mcp-server` 可执行文件 | 可选 |
| yfinance | Yahoo Finance 数据 | stdio | `uv --directory ... run server.py` | 必需 |
| github | GitHub API 操作 | stdio | WSL 本地固定安装 `@modelcontextprotocol/server-github` | 可选 |
| git | 本地 Git 操作 | stdio | `.venvs/mcp-git` + `python -m mcp_server_git` | 可选 |
| playwright | 浏览器自动化 | stdio | `npx @playwright/mcp@latest` | 可选 |

### 3.2 环境变量说明

完整环境变量快照见项目根目录 `.env.template`（包含 Alpaca/GitHub/Proxy 的可直接参考值）。

- `ALPACA_API_KEY` / `ALPACA_SECRET_KEY` / `ALPACA_PAPER_TRADE`：Alpaca MCP 认证与纸盘开关。
- `GITHUB_PERSONAL_ACCESS_TOKEN` / `GITHUB_HOST`：GitHub MCP 所需凭据与域名。
- `HTTP_PROXY` / `HTTPS_PROXY` / `NO_PROXY` 及小写版本：所有 MCP 的代理透传变量。
- 其余写在 `.codex/config.toml` 的 `[mcp_servers.xxx.env]`（如 `SEC_EDGAR_USER_AGENT`、`OPENALEX_EMAIL`、`GDELT_USER_AGENT`），不需要额外 `export`。

### 3.3 安装注意事项

1. Python/conda 路径一致性：`/home/help/miniconda3/envs/aiquantlab/bin/python` 和各 `.venvs/*` 路径要存在。
2. Node.js/npx 路径确认：`/home/help/mcp/tools/node/bin/node` 和 `/home/help/mcp/tools/bin/npx` 需可执行。
3. uv 工具安装：`/home/help/mcp/tools/uv/uv` 需可执行（`yfinance` 依赖）。
4. 代理变量配置：有代理时同时设置大写/小写变量；无代理可留空。对于 Windows Codex App 的 `wsl.exe` bridge，Node HTTPS MCP（如 `github`/`crossref`）不要直接用 `npx -y`，优先走本地固定安装 + 代理 launcher。
5. `config.toml` 路径存在性检查：`command`、`args`、`cwd` 中所有绝对路径都要落地。
6. `enabled_tools` 与 Skill 依赖对齐：若工具裁剪，需确认不会影响 Phase 1 技能链。

### 3.4 Phase 1 技能链 -> MCP 映射

- `company-foundation -> sec_edgar_mcp, alpaca, trading_mcp, yfinance`
- `collect-company-facts -> sec_edgar_mcp, fs`
- `extract-xbrl-timeseries -> sec_edgar_mcp, fs`
- `recast-economic-statements -> fs`
- `valuation-and-margin-of-safety -> fs, yfinance, alpaca`
