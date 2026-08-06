# Mihomo Smart 训练脚本使用教程

## 快速导航

- **[本地训练](#本地训练)** — 如果你只想在本地训练模型，看这个就够了
- **[自动化训练 (GitHub Actions)](#自动化训练-github-actions)** — 如果你想每天自动训练并发布模型，看这个

---

## 本地训练

### 准备工作

1. **安装 Python 依赖**：
   ```bash
   cd smart_trainer
   pip install -r requirements.txt
   ```

2. **准备 CSV 数据**：
   启用 Mihomo Smart 模式，导出 CSV 文件到 `smart_trainer/data/` 目录。

3. **可选：准备 transform.go**：
   将 [Mihomo 源码](https://github.com/vernesong/mihomo/blob/Alpha/component/smart/lightgbm/transform.go#L13)中的 `transform.go` 文件复制到 `smart_trainer/` 目录，脚本会优先使用本地文件，避免网络下载。

### 运行训练

```bash
python smart_trainer/train.py
```

脚本会自动执行：
- 检查本地 `transform.go`，如不存在则从 GitHub 下载
- 加载 CSV 数据文件
- 特征工程：确保特征顺序与 transform.go 一致
- 数据分割：80% 训练集，20% 验证集
- 特征变换：在训练集上拟合 StandardScaler 和 RobustScaler
- 训练 LightGBM 模型（支持可选的自动超参寻优）
- 保存模型文件（含特征顺序和 Scaler 参数）

### 训练配置

可在 `smart_trainer/train.py` 顶部修改以下配置：

```python
# 模型训练配置
LGBM_PARAMS = {
    'n_estimators': 5000,        # 最大迭代轮数
    'learning_rate': 0.01,       # 学习率
    'num_leaves': 96,            # 叶子节点数
    'max_depth': 12,             # 最大深度
    'min_data_in_leaf': 60,      # 叶子最小数据量
    'feature_fraction': 0.85,     # 特征采样比例
    'bagging_fraction': 0.85,    # 数据采样比例
    'bagging_freq': 5,           # bagging频率
}
EARLY_STOPPING_ROUNDS = 150      # 早停轮数

# 自动寻优配置
ENABLE_AUTO_TUNING = False       # 是否启用自动超参寻优
TUNING_TRIALS = 20               # 寻优尝试次数

# 特征变换配置
STD_SCALER_FEATURES = [...]      # StandardScaler 特征列表
ROBUST_SCALER_FEATURES = [...]  # RobustScaler 特征列表
```

### 命令行参数

```bash
python smart_trainer/train.py --data_dir ./data
```

| 参数 | 说明 |
| :--- | :--- |
| `--data_dir` | CSV 数据目录 (默认: `smart_trainer/data/`) |

---

## 自动化训练 (GitHub Actions)

> 以下内容仅在需要自动化训练时需要配置。如果只是本地训练，可以忽略。

### 前置要求：配置 Rclone

为了让 GitHub Actions 自动从云端拉取数据，需要配置 Rclone。

#### 1. 安装 Rclone

**Linux/macOS**
```bash
curl https://rclone.org/install.sh | sudo bash
```

**Windows**

前往 [Rclone Downloads](https://rclone.org/downloads/) 下载安装。

**OpenWrt**
```bash
opkg update && opkg install rclone
```

#### 2. 配置云盘连接

```bash
rclone config
```

- 输入 `n` 新建配置
- 输入名称（如 `gdrive`），**记住这个名称**
- 选择存储服务商并完成授权

#### 3. 上传 CSV 数据到云盘

将 Mihomo 导出的 CSV 文件上传到网盘。

**方法一：使用上传脚本（推荐）**

下载上传脚本：
- Linux/macOS: [upload.sh](./smart_trainer/upload.sh)
- Windows: [upload.bat](./smart_trainer/upload.bat)

赋予执行权限（Linux/macOS）：
```bash
chmod +x upload.sh
```

修改脚本中的 `remote_name` 为你的 rclone 配置名称（如 `gdrive`）。

运行脚本：

Linux/macOS:
```bash
# 上传文件夹
./upload.sh ./data

# 上传单个文件
./upload.sh ./mihomo_data.csv
```

Windows:
```batch
upload.bat "C:\path\to\csv"
```

**方法二：手动命令**

第一步，创建存放数据的文件夹（如不存在）：
```bash
rclone mkdir gdrive:/mihomo-data
```

第二步，上传 CSV 文件：
```bash
rclone copy "/path/to/你的csv文件.csv" gdrive:/mihomo-data
```

#### 4. 获取配置 Base64 编码

将配置文件转换为 Base64（单行无换行）。

首先查看配置文件路径：
```bash
rclone config file
```

记下输出的路径。常见路径如下：

- **Linux/macOS**: `~/.config/rclone/rclone.conf`
- **Windows**: `%USERPROFILE%\.config\rclone\rclone.conf`

**Linux/macOS**

将路径替换为上面命令输出的实际路径：
```bash
cat ~/.config/rclone/rclone.conf | base64 -w 0
```

**Windows (PowerShell)**

将 `C:\Users\你的用户名\.config\rclone\rclone.conf` 替换为实际路径：
```powershell
[Convert]::ToBase64String([IO.File]::ReadAllBytes("C:\Users\你的用户名\.config\rclone\rclone.conf"))
```

**OpenWrt**

SSH 连接到路由器后执行：

第一步，安装 base64 工具（如未安装）：
```bash
opkg update && opkg install coreutils-base64
```

第二步，查看配置文件路径：
```bash
rclone config file
```

第三步，转换为 Base64（将路径替换为上面命令输出的实际路径）：
```bash
cat /root/.config/rclone/rclone.conf | base64 -w 0
```

复制输出的单行字符串（不要换行），这是 `RCLONE_CONFIG_B64` 的值。

### 配置 GitHub Secrets

在仓库 `Settings` → `Secrets and variables` → `Actions` 中添加：

| 变量名 | 描述 | 示例 |
| :--- | :--- | :--- |
| `RCLONE_CONFIG_B64` | Base64 编码的 rclone 配置文件 | (运行上面命令获取) |
| `REMOTE` | rclone 远程存储路径 | `gdrive:/mihomo-data` |
| `TG_BOT_TOKEN` | Telegram 机器人 Token | (从 @BotFather 获取) |
| `TG_CHAT_ID` | Telegram 接收通知的 ID | (从 @userinfobot 获取) |

### 工作流说明

自动化流程：
1. **拉取数据** — 使用 rclone 从云端下载 CSV 文件
2. **训练模型** — 运行 `smart_trainer/train.py --data_dir .`
3. **发布 Release** — 自动更新 `smart-model` 标签的 Release
4. **发送通知** — 通过 Telegram 发送训练结果和日志

> 无需手动触发，GitHub Actions 会每天自动运行一次。
