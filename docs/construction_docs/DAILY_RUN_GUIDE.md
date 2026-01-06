# Space Signals 日常运行指南

## 📋 目录

1. [快速开始](#快速开始)
2. [初始化数据](#初始化数据)
3. [日常运行](#日常运行)
4. [Windows任务计划配置](#windows任务计划配置)
5. [故障排查](#故障排查)

---

## 🚀 快速开始

### 脚本使用方式

批处理脚本 `run_daily_space_signals.bat` 支持三种模式：

```batch
# 1. 初始化模式（从2006年开始）
run_daily_space_signals.bat init

# 2. 日常模式（更新最近5天）- 默认模式
run_daily_space_signals.bat daily
# 或直接双击运行
run_daily_space_signals.bat

# 3. 测试模式（小规模测试）
run_daily_space_signals.bat test
```

---

## 📦 初始化数据

### 首次运行（从2006年开始）

用于机器学习训练，需要从2006年开始处理所有历史数据：

```batch
run_daily_space_signals.bat init
```

**重要提示：**
- ⏰ **耗时很长**：处理2006年至今约19年的数据，预计需要 **数小时到数天**
- 💾 **数据量大**：约391个因子 × 19年 × 5000+只股票，总数据量可能达到数亿行
- 🌙 **建议时间**：周末或夜间运行
- 📊 **监控进度**：可以查看日志文件 `logs/init_run.log` 和 `space_pipeline_YYYYMMDD.log`

### 初始化策略建议

#### **方案1：一次性全量初始化（推荐周末运行）**

```batch
# 直接运行，从2006年至今
run_daily_space_signals.bat init
```

#### **方案2：分年份批次初始化（更安全）**

如果担心一次性运行时间太长或中途失败，可以分批次处理：

```powershell
# 激活环境
& "F:/AIQuantLab/.venv/Scripts/Activate.ps1"

# 2006-2010
python run_space_data_pipeline.py --latest --start-date 20060101 --end-date 20101231

# 2011-2015
python run_space_data_pipeline.py --latest --start-date 20110101 --end-date 20151231

# 2016-2020
python run_space_data_pipeline.py --latest --start-date 20160101 --end-date 20201231

# 2021-至今
python run_space_data_pipeline.py --latest --start-date 20210101
```

#### **方案3：按因子批次初始化（最灵活）**

先处理重要因子，逐步完善：

```powershell
# 1. 处理成长类因子（79个）
python run_space_data_pipeline.py --latest --start-date 20060101 | findstr /C:"growth"

# 2. 处理价值类因子（26个）
# ... 依此类推
```

---

## 🔄 日常运行

### 手动运行

每天手动执行一次：

```batch
run_daily_space_signals.bat daily
```

或直接双击 `run_daily_space_signals.bat`

**执行内容：**
- 更新最近5天的因子数据
- 使用UPSERT模式，自动处理重复数据
- 覆盖周末和节假日的数据空白

### 为什么是5天？

- ✅ 覆盖周末（周五-下周一）
- ✅ 覆盖节假日空白期
- ✅ 保证数据完整性
- ✅ 运行时间短（通常5-15分钟）

---

## ⏰ Windows任务计划配置

### 方法1：通过图形界面配置

#### 步骤1：打开任务计划程序

1. 按 `Win + R`，输入 `taskschd.msc`，回车
2. 或搜索"任务计划程序"

#### 步骤2：创建基本任务

1. 右侧点击 **"创建基本任务"**
2. 名称：`Space Signals Daily Update`
3. 描述：`每日更新Space因子数据到数据库`

#### 步骤3：设置触发器

1. 触发器：选择 **"每天"**
2. 开始时间：建议 **凌晨1:00**（避开交易时段）
3. 重复间隔：每天

#### 步骤4：设置操作

1. 操作：**"启动程序"**
2. 程序或脚本：
   ```
   F:\AIQuantLab\run_daily_space_signals.bat
   ```
3. 添加参数：
   ```
   daily
   ```
4. 起始于（可选）：
   ```
   F:\AIQuantLab
   ```

#### 步骤5：高级设置

在"完成"前，勾选 **"在单击'完成'时，打开此任务属性的对话框"**

高级设置：
- ☑️ 允许按需运行任务
- ☑️ 如果过了计划开始时间，立即启动任务
- ☑️ 如果任务失败，重新启动间隔：**10分钟**
- ☑️ 尝试重新启动次数：**3次**

#### 步骤6：测试运行

在任务列表中找到刚创建的任务，右键选择 **"运行"**，测试是否正常执行。

---

### 方法2：通过PowerShell脚本配置（推荐）

创建一个自动配置脚本：

```powershell
# setup_daily_task.ps1
$TaskName = "Space Signals Daily Update"
$TaskDescription = "每日更新Space因子数据到数据库"
$ScriptPath = "F:\AIQuantLab\run_daily_space_signals.bat"
$WorkingDir = "F:\AIQuantLab"

# 检查任务是否已存在
$ExistingTask = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($ExistingTask) {
    Write-Host "任务已存在，正在删除旧任务..." -ForegroundColor Yellow
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
}

# 创建任务触发器（每天凌晨1点）
$Trigger = New-ScheduledTaskTrigger -Daily -At "01:00"

# 创建任务操作
$Action = New-ScheduledTaskAction `
    -Execute $ScriptPath `
    -Argument "daily" `
    -WorkingDirectory $WorkingDir

# 创建任务设置
$Settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 10)

# 注册任务（使用当前用户权限）
Register-ScheduledTask `
    -TaskName $TaskName `
    -Description $TaskDescription `
    -Trigger $Trigger `
    -Action $Action `
    -Settings $Settings `
    -User $env:USERNAME `
    -RunLevel Highest

Write-Host "✅ 任务计划创建成功！" -ForegroundColor Green
Write-Host "任务名称：$TaskName" -ForegroundColor Cyan
Write-Host "执行时间：每天凌晨1:00" -ForegroundColor Cyan
Write-Host "脚本路径：$ScriptPath" -ForegroundColor Cyan
```

保存为 `setup_daily_task.ps1`，然后以**管理员身份**运行PowerShell：

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\setup_daily_task.ps1
```

---

## 📊 监控和日志

### 日志文件位置

```
F:\AIQuantLab\logs\
├── daily_run.log              # 日常运行简要日志
├── init_run.log               # 初始化日志
├── space_pipeline_YYYYMMDD.log  # 详细执行日志（按日期）
└── missing_signals_YYYYMMDD.log # 未映射因子日志（如果有）
```

### 查看日志

```batch
# 查看最新的日常运行日志
type logs\daily_run.log

# 查看今天的详细日志
type logs\space_pipeline_20241024.log

# 查看未映射的因子
type logs\missing_signals_20241024.log
```

### 监控脚本（可选）

创建一个简单的监控脚本 `check_status.bat`：

```batch
@echo off
echo ============================================================
echo Space Signals Pipeline 状态检查
echo ============================================================
echo.

echo [日常运行日志]
echo ------------------------------------------------
type logs\daily_run.log | findstr /C:"成功" /C:"失败" | more

echo.
echo [最新执行日志（最后10行）]
echo ------------------------------------------------
for /f "delims=" %%i in ('dir /b /o-d logs\space_pipeline_*.log') do (
    type "logs\%%i" | more
    goto :found
)
:found

echo.
echo [未映射因子]
echo ------------------------------------------------
for /f "delims=" %%i in ('dir /b /o-d logs\missing_signals_*.log 2^>nul') do (
    type "logs\%%i"
    goto :end
)
echo 无未映射因子
:end

pause
```

---

## 🔧 故障排查

### 常见问题

#### 1. **虚拟环境激活失败**

**错误信息：**
```
[错误] 虚拟环境激活失败
```

**解决方法：**
```batch
# 检查虚拟环境是否存在
dir F:\AIQuantLab\.venv\Scripts\activate.bat

# 如果不存在，重新创建虚拟环境
cd F:\AIQuantLab
python -m venv .venv
.venv\Scripts\activate.bat
pip install -r requirements.txt
```

#### 2. **权限不足错误**

**错误信息：**
```
Access Denied / 拒绝访问
```

**解决方法：**
- 以管理员身份运行批处理脚本
- 检查数据库连接权限
- 检查 Space NAS 网络驱动器权限

#### 3. **任务计划不执行**

**排查步骤：**

1. **检查任务状态：**
   ```
   打开任务计划程序 → 找到任务 → 查看"上次运行结果"
   ```

2. **手动测试：**
   ```batch
   # 直接运行批处理文件
   F:\AIQuantLab\run_daily_space_signals.bat daily
   ```

3. **检查账户权限：**
   - 确保任务使用的账户有足够权限
   - 尝试使用 "不管用户是否登录都要运行"选项

4. **查看任务历史：**
   ```
   任务计划程序 → 查看 → 显示所有正在运行的任务
   ```

#### 4. **网络连接失败**

**错误信息：**
```
Signal path not accessible: \\space\signal
```

**解决方法：**
```batch
# 测试网络连接
net use \\space\signal

# 如果失败，重新映射网络驱动器
net use \\space\signal /user:space\bsshare PASSWORD
```

#### 5. **数据库连接失败**

**错误信息：**
```
数据库连接失败
```

**解决方法：**
- 检查 `configs/database.yaml` 配置
- 测试数据库连接：
  ```python
  from src.utils.db_connection import DBConnection
  conn = DBConnection()
  # 应该不报错
  ```
- 检查防火墙设置

---

## 📈 性能优化建议

### 硬件要求

- **CPU**: 4核以上（支持并行处理）
- **内存**: 8GB以上（推荐16GB）
- **硬盘**: SSD（加快数据读写）
- **网络**: 稳定的千兆网络（访问Space NAS）

### 优化配置

在 `configs/space_disk/space_config.yaml` 中调整：

```yaml
processing:
  use_parallel: true      # 启用并行处理
  max_workers: 4          # 并行线程数（根据CPU核心数调整）
  chunk_size: 1000        # 批处理大小
  max_memory_usage_gb: 8  # 最大内存使用（GB）
```

---

## 📞 联系支持

如有问题，请：
1. 查看日志文件 `logs/space_pipeline_YYYYMMDD.log`
2. 检查 `logs/missing_signals_YYYYMMDD.log` 是否有未映射因子
3. 参考本文档的故障排查部分

---

## 📝 更新记录

- **2024-10-24**: 初始版本
  - 支持初始化模式（从2006年）
  - 支持日常更新模式（最近5天）
  - 完整的Windows任务计划配置指南

