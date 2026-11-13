# Mihomo Smart 训练脚本使用教程

## 快速导航

- **[本地训练](#本地训练)** — 如果你只想在本地训练模型，看这个就够了
- **[自动化训练 (GitHub Actions)](#自动化训练-github-actions)** — 配置手动/定时训练与模型发布

---

## 本地训练

### 准备工作

1. **安装 Python 依赖**：
   ```bash
   python -m pip install -r smart_trainer/requirements.txt
   ```

2. **准备训练数据**：
   - 官方 Smart 权重模型：将 `smart_weight_data*.csv` 放入 `smart_trainer/data/`，运行 `train.py`。
   - PLD 先验模型：将 `smart_samples*.db` / `smart_samples*.csv` 放入该目录，运行 `train_pld.py`。

3. **官方链路可选：准备 transform.go**：
   仅 `train.py` 使用它。可将 [Mihomo 源码](https://github.com/vernesong/mihomo/blob/Alpha/component/smart/lightgbm/transform.go#L13)中的 `transform.go` 复制到 `smart_trainer/`，脚本会优先使用本地文件。`train_pld.py` 使用内置的固定 PLD 特征顺序，不读取该文件。

### 运行训练

```bash
python smart_trainer/train.py
# PLD 内核则运行：python smart_trainer/train_pld.py
```

`train.py`（官方链路）会：
- 加载 CSV；读取本地 `transform.go`，不存在时从 GitHub 下载；
- 按动态特征顺序构造数据，以输入顺序前 80% 训练、后 20% 验证（`shuffle=False`）；
- 在训练集拟合并应用 StandardScaler / RobustScaler；
- 训练并保存 `Model.bin`（含特征顺序和 Scaler 参数）。

`train_pld.py`（PLD 链路）会：
- 加载 SQLite / CSV 并按全局事件时间稳定排序；
- 按固定 30 特征构造因果前态，以过去 80% 训练、未来 20% 验证；
- 只对 `node_sample_count` 拟合并应用 RobustScaler；
- 训练并保存 `PriorModel.bin`（含变换元数据），再按已见/未见组合报告验证结果。

### 训练配置

官方链路可在 `smart_trainer/train.py` 顶部修改以下配置；PLD 参数位于 `train_pld.py`，两者不要混用：

```python
# 模型训练配置
LGBM_PARAMS = {
    'n_estimators': 10000,       # 最大迭代轮数
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
python smart_trainer/train.py --data_dir smart_trainer/data
python smart_trainer/train_pld.py --data_dir smart_trainer/data
```

| 参数 | 说明 |
| :--- | :--- |
| `train.py --data_dir` | 官方链路 CSV 数据目录（默认：`smart_trainer/data/`） |
| `train_pld.py --data_dir` | PLD 链路 SQLite / CSV 数据目录（默认：`smart_trainer/data/`） |

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
# ImmortalWrt 25.12 / apk
apk update && apk add rclone sqlite3-cli coreutils-base64

# 旧版 OpenWrt / opkg
opkg update && opkg install rclone sqlite3-cli coreutils-base64
```

> `upcsv.db.sh` 对正在采集的 `smart_samples.db` 使用 SQLite 在线备份 API，
> 因此必须安装提供 `sqlite3` 命令的包；缺失时脚本会返回失败并保留本地原库，
> 不会退回到直接复制活跃数据库。

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

第一步，安装 base64 工具（如未在前一步安装）：
```bash
apk add coreutils-base64        # ImmortalWrt 25.12
# opkg install coreutils-base64 # 旧版 OpenWrt
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
1. **拉取数据** — 使用 rclone 从云端下载对应训练数据
2. **训练模型** — 官方工作流运行 `train.py`；PLD 工作流运行 `train_pld.py`
3. **发布 Release** — 分别更新 `smart-model` / `smart-pld-model` 标签
4. **发送通知** — 通过 Telegram 发送训练结果和日志

> `.github/workflows/train.yml`（官方模型）默认仅手动触发；`.github/workflows/train-pld.yml` 默认每周日 22:30 UTC 定时运行，也支持手动触发，并在 `train_pld.py` 变更时触发。
