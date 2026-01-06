# Weights & Biases (W&B) 使用指南

本指南介绍如何通过 Weights & Biases 监控 DFZQ GRU 模型的训练过程，特别是如何在手机端查看训练进度。

## 基本设置和使用

### 1. 安装和登录

首次使用前，需要安装 wandb 并登录您的账户：

```bash
# 安装 wandb 包
pip install wandb

# 登录到您的 W&B 账户
wandb login
```

登录后，系统会要求您输入 API 密钥，可以从 [wandb.ai/settings](https://wandb.ai/settings) 获取。

### 2. 代码集成

我们已经将 W&B 集成到训练代码中，替代了原来的 TensorBoard。主要变化包括：

- 使用 `wandb.init()` 创建一个新运行
- 使用 `wandb.watch(model)` 自动跟踪模型梯度和参数
- 使用 `wandb.log()` 记录训练指标
- 训练结束时用 `wandb_run.finish()` 关闭 W&B 会话

### 3. 核心功能

W&B 提供了比 TensorBoard 更丰富的功能：

- **实时指标追踪**：自动更新的图表和表格
- **超参数管理**：记录并比较不同运行的超参数
- **梯度和权重可视化**：自动记录梯度和权重分布
- **跨运行比较**：比较不同训练运行的性能
- **实验组织**：按项目组织实验

## 移动设备查看训练进度

W&B 的一大优势是可以在手机或平板电脑上轻松查看训练进度。

### 1. 通过移动浏览器查看

1. 在手机浏览器中访问 [wandb.ai](https://wandb.ai) 并登录
2. 导航到您的项目（例如 "dfzq_gru"）
3. 选择当前运行查看实时指标

W&B 的网页界面对移动设备做了响应式设计，大部分图表可以正常查看。

### 2. 使用 PWA (Progressive Web App)

为了更好的移动体验，可以将 W&B 网站添加到主屏幕：

1. 在 Chrome 浏览器中访问 wandb.ai
2. 点击浏览器菜单
3. 选择"添加到主屏幕"
4. 现在您可以像本地应用一样使用 W&B

### 3. 使用第三方移动应用

有第三方移动应用可用于查看 W&B 运行：

- 在应用商店搜索 "WandB for Mobile" 或类似应用
- 这些应用通常提供更流畅的移动体验

## 常见问题与解决方案

### 无法连接 W&B 服务器

如果您的训练环境无法直接连接互联网，可以使用离线模式：

```python
import wandb
wandb.init(mode="offline")
```

训练完成后，可以使用以下命令上传数据：

```bash
wandb sync wandb/offline-run-*
```

### 日志过多导致性能问题

如果您发现手机查看时图表渲染缓慢，可以考虑：

1. 减少 `wandb.log()` 的频率，例如每 10 个批次记录一次
2. 减少每次记录的指标数量
3. 使用更简化的图表视图

### 查看最佳模型

W&B 允许您轻松找到性能最好的模型：

1. 在运行页面中，查看"Summary"部分
2. 我们记录了关键指标如 "test/combined_ic"
3. 您可以通过这些指标快速识别最佳模型

## 与团队协作

W&B 使团队协作变得简单：

1. 邀请团队成员到您的 W&B 项目
2. 共享运行链接以进行讨论
3. 为运行添加注释和说明
4. 比较团队成员的不同运行

## 进一步资源

- [W&B 官方文档](https://docs.wandb.ai/)
- [W&B 教程](https://wandb.ai/site/tutorials)
- [W&B 社区论坛](https://community.wandb.ai/) 