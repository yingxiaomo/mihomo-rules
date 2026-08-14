import argparse
import glob
import logging
import os
import re
import sys
import time
import traceback
from pathlib import Path
from typing import List
import warnings
import requests

warnings.filterwarnings("ignore", message=".*X does not have valid feature names.*")
warnings.filterwarnings("ignore", category=UserWarning)

import lightgbm as lgb
import numpy as np
import pandas as pd
from sklearn.metrics import mean_absolute_error
from sklearn.model_selection import train_test_split
from sklearn.preprocessing import StandardScaler, RobustScaler

logger = logging.getLogger(__name__)


# ==============================================================================
# 配置选项 - 请根据需要修改以下参数
# ==============================================================================

# --------------------------- 路径配置 ---------------------------
SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
DEFAULT_DATA_DIR = SCRIPT_DIR / "data"           # 存放CSV的目录（默认指向脚本同目录的 data 文件夹）
DEFAULT_MODEL_PATH = PROJECT_ROOT / "Model.bin"    # 模型输出路径（默认指向项目根目录）

# transform.go 特征定义文件配置
GO_LOCAL_PATH = SCRIPT_DIR / "transform.go"       # 本地transform.go路径（默认指向脚本同目录）
# 官方源码URL = vernesong/mihomo 官方 Alpha 分支（30 特征），供官方内核用户使用。
# 若使用 yingxiaomo/mmihos 魔改内核（25 特征），请切换到 smart-25feat 分支或改此 URL。
GO_REMOTE_URL = "https://raw.githubusercontent.com/vernesong/mihomo/Alpha/component/smart/lightgbm/transform.go"

# --------------------------- 特征变换配置 ---------------------------
# StandardScaler 特征列表
# 注意：这些特征必须与 transform.go 中的定义完全匹配
STD_SCALER_FEATURES = [
    'connect_time', 'latency', 'upload_mb', 'history_upload_mb',
    'maxuploadrate_kb', 'history_maxuploadrate_kb', 'download_mb',
    'history_download_mb', 'maxdownloadrate_kb', 'history_maxdownloadrate_kb',
    'duration_minutes', 'history_duration_minutes', 'traffic_ratio', 'traffic_density'
]

# RobustScaler 特征列表
ROBUST_SCALER_FEATURES = ['success', 'failure']

# --------------------------- 模型训练配置 ---------------------------
LGBM_PARAMS = {
    'objective': 'regression',     # 目标类型
    'metric': 'rmse',              # 评价指标
    'n_estimators': 10000,         # 最大迭代树数
    'learning_rate': 0.01,         # 学习率
    'random_state': 42,            # 固定随机数种子
    'n_jobs': -1,                  # 并行线程数
    'verbosity': -1,               # 日志输出等级
    'num_leaves': 96,              # 叶子节点数
    'max_depth': 12,               # 最大深度
    'min_data_in_leaf': 60,        # 叶子最小样本数
    'feature_fraction': 0.85,      # 特征采样比例
    'bagging_fraction': 0.85,      # 数据采样比例
    'bagging_freq': 5              # bagging 频率
}
EARLY_STOPPING_ROUNDS = 150        # 早停轮数

# --------------------------- 自动寻优配置 ---------------------------
ENABLE_AUTO_TUNING = False         # 是否启用自动超参寻优
TUNING_TRIALS = 20                 # 寻优尝试次数

# --------------------------- 通知配置 ---------------------------
# 本地运行可不设置，部署到服务器或 Github Actions 时设置 TG_BOT_TOKEN 和 TG_CHAT_ID 即可自动接收训练日志
TG_BOT_TOKEN = os.environ.get("TG_BOT_TOKEN", "")  # Telegram Bot Token（从环境变量读取）
TG_CHAT_ID = os.environ.get("TG_CHAT_ID", "")      # Telegram 聊天ID（从环境变量读取）
LOG_FILENAME = SCRIPT_DIR / "training.log"                      # 训练日志文件名
MAX_TG_CHUNKS = 5                                  # Telegram 日志最大发送块数

# ==============================================================================
# 核心类和函数定义
# ==============================================================================

class GoTransformParser:
    def __init__(self, go_file_path: str):
        with open(go_file_path, 'r', encoding='utf-8') as f:
            self.content = f.read()
        self.feature_order = self._parse_feature_order()

    def _parse_feature_order(self) -> List[str]:
        feature_dict = {}
        pattern = re.compile(r'(\d+)\s*:\s*"([^"]+)"')
        for match in pattern.finditer(self.content):
            idx, name = match.groups()
            feature_dict[int(idx)] = name

        if not feature_dict:
            raw_pattern = re.compile(r'(\d+)\s*:\s*`([^`]+)`')
            for match in raw_pattern.finditer(self.content):
                idx, name = match.groups()
                feature_dict[int(idx)] = name

        if not feature_dict:
            raise ValueError("未找到特征定义，请检查 transform.go 是否包含 0:" + "... = \"特征名\" 的格式")

        return [feature_dict[i] for i in sorted(feature_dict.keys())]

    def get_feature_order(self) -> List[str]:
        return self.feature_order


def send_telegram_msg(text: str) -> None:
    if not TG_BOT_TOKEN or not TG_CHAT_ID: return
    url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage"
    data = {
        "chat_id": TG_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True
    }
    try:
        requests.post(url, json=data, timeout=10)
    except Exception as e:
        print(f"⚠️ TG 发送失败: {e}")


def send_telegram_logs(header_msg: str) -> None:
    if not TG_BOT_TOKEN or not TG_CHAT_ID:
        print("⚠️ Telegram 未配置，已跳过消息推送")
        return

    send_telegram_msg(header_msg)

    try:
        with open(LOG_FILENAME, "r", encoding='utf-8') as f:
            content = f.read()
        print(f"📋 日志文件大小: {len(content)} 字符")
    except Exception as e:
        content = f"无法读取日志文件: {e}"
        print(f"⚠️ 读取日志文件失败: {e}")

    chunk_size = 3500
    total_len = len(content)

    if total_len == 0:
        send_telegram_msg("<i>(日志为空)</i>")
        return

    chunks = []
    for i in range(0, total_len, chunk_size):
        chunks.append(content[i : i + chunk_size])
    
    if len(chunks) > MAX_TG_CHUNKS:
        chunks = chunks[:MAX_TG_CHUNKS]
        chunks[-1] = chunks[-1][:-50] + "... (日志过长，已截断)"

    for i, chunk in enumerate(chunks):
        formatted_msg = f"<pre>{chunk}</pre>"
        send_telegram_msg(formatted_msg)
        if i < len(chunks) - 1:
            time.sleep(0.5)

    print(f"📨 Telegram 日志已推送 ({len(chunks)}/{min(len(chunks), MAX_TG_CHUNKS)} 块)")


def setup_logging(filename: str = LOG_FILENAME) -> None:
    log_path = Path(filename)
    log_path.parent.mkdir(parents=True, exist_ok=True)

    # 获取 Root 全局日志
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)

    # 清空可能冲突的旧处理器
    if root_logger.hasHandlers():
        root_logger.handlers.clear()

#   formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%Y-%m-%d %H:%M:%S")

    formatter = logging.Formatter("%(message)s")

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(formatter)
    root_logger.addHandler(console_handler)

    file_handler = logging.FileHandler(filename, encoding='utf-8')
    file_handler.setLevel(logging.INFO)
    file_handler.setFormatter(formatter)
    root_logger.addHandler(file_handler)
    
    lgb_logger = logging.getLogger('LightGBM')
    lgb_logger.setLevel(logging.INFO)
    lgb_logger.propagate = True

    try:
        lgb.register_logger(root_logger)
    except AttributeError:
        pass  

    logger.info(f"--- 训练开始: {time.strftime('%Y-%m-%d %H:%M:%S')} ---")


def print_separator(title: str = None) -> None:
    logger.info("=" * 60)
    if title:
        logger.info(f"{title}")
        logger.info("=" * 60)


def get_feature_order() -> list:
    if GO_LOCAL_PATH.exists():
        logger.info(f"📂 [本地模式] 检测到源码: {GO_LOCAL_PATH}")
        try:
            parser = GoTransformParser(str(GO_LOCAL_PATH))
            feature_order = parser.get_feature_order()
            logger.info(f"✨ 成功解析出 {len(feature_order)} 个特征")
            return feature_order
        except Exception as e:
            logger.info(f"⚠️ 本地读取失败 ({e})，切换至在线模式...")
    else:
        logger.info("ℹ️ 未检测到本地 transform.go，切换至在线模式...")

    logger.info(f"☁️ [在线模式] 正在下载: {GO_REMOTE_URL}")
    try:
        resp = requests.get(GO_REMOTE_URL, timeout=15)
        resp.raise_for_status()
        logger.info("✅ 下载成功")
        
        GO_LOCAL_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(str(GO_LOCAL_PATH), 'w', encoding='utf-8') as f:
            f.write(resp.text)
        
        parser = GoTransformParser(str(GO_LOCAL_PATH))
        feature_order = parser.get_feature_order()
        logger.info(f"✨ 成功解析出 {len(feature_order)} 个特征")
        return feature_order
    except Exception as e:
        logger.info(f"❌ [错误] 下载失败: {e}")
        raise RuntimeError("无法获取特征定义")


def check_data_balance(df: pd.DataFrame, top_ratio_threshold: float = 0.5, min_nodes: int = 5) -> bool:
    """
    节点分布健康检查：识别反馈循环/选择偏差风险。

    当内核开 `uselightgbm: true` 时，模型只让被它偏好的节点产生大量数据，
    未选中的节点几乎没有新样本 -> 每天用该数据训练会让模型自我强化当前偏好
    （"训练节点永远选不到"的统计版）。健康数据应覆盖足够多的节点、且没有
    单一节点独占。此检查在训练前跑，异常时告警但不阻断。

    返回 True=健康可训练，False=存在反馈循环/多样性风险。
    """
    if df is None or len(df) == 0:
        logger.info("ℹ️ 无数据，跳过节点分布检查")
        return False

    if 'node_name' not in df.columns:
        logger.info("ℹ️ 数据缺少 node_name 列，跳过节点分布检查")
        return True

    counts = df['node_name'].value_counts()
    total = len(df)
    coverage = len(counts)
    top_node = counts.index[0]
    top_ratio = counts.iloc[0] / total
    top5_ratio = counts.head(5).sum() / total

    logger.info("📊 [节点分布检查]")
    logger.info(f"  覆盖节点数: {coverage} | 总样本: {total}")
    logger.info(f"  单节点最大占比: {top_ratio:.1%} ({top_node})")
    logger.info(f"  前5节点合计占比: {top5_ratio:.1%}")

    warnings = []
    if coverage < min_nodes:
        warnings.append(f"覆盖节点过少 ({coverage}<{min_nodes})，样本代表性不足")
    if top_ratio > top_ratio_threshold:
        warnings.append(
            f"单节点占比 {top_ratio:.1%} > {top_ratio_threshold:.0%}，疑似反馈循环："
            "被模型偏好的节点自我强化，其他节点几乎无样本。建议提高 explore-rate、"
            "多攒几天数据，或先做节点均衡采样再训"
        )
    if top5_ratio > 0.9:
        warnings.append("前5节点合计占比 >90%，节点多样性不足")

    if warnings:
        logger.warning("⚠️ 节点分布健康告警（反馈循环风险）:")
        for w in warnings:
            logger.warning(f"  - {w}")
        logger.info("💡 若本批次数据来自 uselightgbm:true 运行，请斟酌是否用此数据训练")
        return False
    logger.info("✅ 节点分布健康，可直接训练")
    return True


def load_data(data_dir: Path) -> pd.DataFrame:
    logger.info("[步骤1] 加载原始数据")

    if not data_dir.exists() or not data_dir.is_dir():
        raise FileNotFoundError(f"数据目录不存在: {data_dir}")

    all_files = sorted(glob.glob(str(data_dir / "*.csv")))
    if not all_files:
        raise FileNotFoundError(f"数据目录中没有找到任何 CSV 数据文件: {data_dir}")

    logger.info(f"📥 选中了 {len(all_files)} 个数据文件")

    dfs = []
    for f in all_files:
        try:
            df = pd.read_csv(f, encoding='utf-8', on_bad_lines='skip')
            dfs.append(df)
        except Exception as e:
            logger.info(f"⚠️ 跳过损坏的文件 {f}: {e}")
            continue

    if not dfs:
        raise ValueError("没有加载到任何可用数据")

    merged_df = pd.concat(dfs, ignore_index=True)
    logger.info(f"📊 数据加载完成，共 {len(merged_df)} 条记录")
    return merged_df


def preprocess_data(df: pd.DataFrame, feature_order: list):
    logger.info("[步骤2] 特征工程与目标构建")
    logger.info("🧹 正在执行数据清洗...")

    # 初步清洗：去掉没有 weight 的空数据
    df.dropna(subset=['weight'], inplace=True)
    
    # 确保 weight 列本身没有被污染成字符串
    df['weight'] = pd.to_numeric(df['weight'], errors='coerce')
    df.dropna(subset=['weight'], inplace=True)
    df = df[df['weight'] > 0].copy()

    logger.info("--> 正在从预处理数据中提取特征和目标...")
    
    original_cols = set(df.columns)
    df = df.reindex(columns=feature_order + ['weight'], fill_value=0)
    
    missing_features = [f for f in feature_order if f not in original_cols]
    if missing_features:
        features_list = "🔹 ".join(missing_features)
        logger.info(f"⚠️ 检测到 {len(missing_features)} 个特征在数据中不存在（可能是源码新增），已用 0 填充")
        logger.info(f"⚠️ 缺少以下特征:🔹 {features_list}，请及时更新内核")

    for col in feature_order:
        df[col] = pd.to_numeric(df[col], errors='coerce')
    
    # 统计有多少行被污染成了 NaN
    bad_rows_count = df.isna().any(axis=1).sum()
    
    # 剔除所有包含 NaN 的行
    if bad_rows_count > 0:
        df.dropna(inplace=True)
        logger.info(f"🛡️ 成功拦截并剔除了 {bad_rows_count} 条包含畸形字符串的脏记录！")

    X = df[feature_order]
    y = df['weight']
    
    logger.info(f"🧹 清洗后剩余 {len(df)} 条极其干净的有效记录。")
    logger.info(f"✅ 成功提取特征和目标，共 {len(feature_order)} 个特征。")

    return X, y


def get_scaler_columns(X: pd.DataFrame):
    std_cols = [c for c in STD_SCALER_FEATURES if c in X.columns]
    rob_cols = [c for c in ROBUST_SCALER_FEATURES if c in X.columns]
    return std_cols, rob_cols


def apply_scaler_transform(X: pd.DataFrame, std_scaler, robust_scaler, std_cols, rob_cols):
    X_scaled = X.copy()
    
    if std_cols and std_scaler:
        X_scaled[std_cols] = std_scaler.transform(X_scaled[std_cols])
    
    if rob_cols and robust_scaler:
        X_scaled[rob_cols] = robust_scaler.transform(X_scaled[rob_cols])
    
    return X_scaled


def auto_tune_params(X_train: pd.DataFrame, y_train: pd.Series, X_test: pd.DataFrame, y_test: pd.Series):
    logger.info("--> 正在进行自动超参寻优...")
    logger.info(f"🎯 寻优次数: {TUNING_TRIALS}")
    
    try:
        import optuna
    except ImportError as e:
        raise RuntimeError(
            "自动超参寻优需要安装 optuna。请运行：pip install optuna"
        ) from e
    
    def objective(trial):
        params = {
            'objective': 'regression',
            'metric': 'rmse',
            'n_estimators': LGBM_PARAMS['n_estimators'],
            'learning_rate': trial.suggest_float('learning_rate', 0.008, 0.02, log=True),
            'num_leaves': trial.suggest_int('num_leaves', 31, 127),
            'max_depth': trial.suggest_int('max_depth', 6, 12),
            'min_data_in_leaf': trial.suggest_int('min_data_in_leaf', 50, 200),
            'feature_fraction': trial.suggest_float('feature_fraction', 0.7, 0.9),
            'bagging_fraction': trial.suggest_float('bagging_fraction', 0.7, 0.9),
            'bagging_freq': trial.suggest_int('bagging_freq', 3, 10),
            'random_state': 42,
            'n_jobs': -1,
            'verbosity': -1
        }
        
        model = lgb.train(
            params,
            lgb.Dataset(X_train, label=y_train),
            valid_sets=[lgb.Dataset(X_test, label=y_test)],
            callbacks=[lgb.early_stopping(EARLY_STOPPING_ROUNDS, verbose=False)]
        )
        
        return model.best_score.get('valid_0', {}).get('rmse', float('inf'))
    
    study = optuna.create_study(direction='minimize', pruner=optuna.pruners.MedianPruner())
    study.optimize(objective, n_trials=TUNING_TRIALS, show_progress_bar=False)
    
    logger.info(f"✅ 寻优完成")
    logger.info(f"🏆 最佳 RMSE: {study.best_value:.6f}")
    logger.info(f"📋 最佳参数: {study.best_params}")
    
    best_params = LGBM_PARAMS.copy()
    best_params.update(study.best_params)
    return best_params


def train_model(X_train: pd.DataFrame, y_train: pd.Series, X_test: pd.DataFrame, y_test: pd.Series):
    logger.info("--> 正在训练 LightGBM 模型...")
    
    params = LGBM_PARAMS.copy()
    
    if ENABLE_AUTO_TUNING:
        params = auto_tune_params(X_train, y_train, X_test, y_test)
    
    logger.info(f"📊 训练集: {len(X_train)} 条样本, {X_train.shape[1]} 个特征")
    logger.info(f"🧪 验证集: {len(X_test)} 条样本")
    logger.info(f"🔄 最大迭代轮数: {params['n_estimators']}")
    logger.info(f"⏹️ 早停轮数: {EARLY_STOPPING_ROUNDS}")
    logger.info(f"📈 学习率: {params['learning_rate']}")
    
    train_data = lgb.Dataset(X_train, label=y_train)
    test_data = lgb.Dataset(X_test, label=y_test, reference=train_data)

    model = lgb.train(
        params,
        train_data,
        valid_sets=[test_data],
        callbacks=[
            lgb.early_stopping(EARLY_STOPPING_ROUNDS, verbose=False),
            lgb.log_evaluation(500)           # 每 500 轮输出一次日志，避免过于频繁的日志干扰阅读
        ]
    )
    
    logger.info(f"✅ 训练完成")
    logger.info(f"🏆 最佳迭代轮数: {model.best_iteration}")

    y_pred = model.predict(X_test, num_iteration=model.best_iteration)
    mae_score = mean_absolute_error(y_test, y_pred)
    rmse_score = model.best_score.get('valid_0', {}).get('rmse', None)

    if rmse_score is not None:
        logger.info(f"📈 验证集 RMSE: {rmse_score:.6f}")
    else:
        logger.info(f"⚠️ 无法获取验证集 RMSE 分数")

    logger.info(f"📉 验证集 MAE: {mae_score:.6f}")
    
    return model


def verify_feature_integrity(feature_order: list) -> None:
    missing_std = [f for f in STD_SCALER_FEATURES if f not in feature_order]
    missing_rob = [f for f in ROBUST_SCALER_FEATURES if f not in feature_order]
    if missing_std or missing_rob:
        message_lines = [
            "🚨 致命错误：transform.go 中的特征顺序已发生变化，当前 scaler 配置无法安全应用。",
        ]
        if missing_std:
            message_lines.append(f"  - StandardScaler 预期特征缺失: {missing_std}")
        if missing_rob:
            message_lines.append(f"  - RobustScaler 预期特征缺失: {missing_rob}")
        message_lines.append("请确认 transform.go 是否已更新，并同步 STD_SCALER_FEATURES/ROBUST_SCALER_FEATURES。")
        raise ValueError("\n".join(message_lines))


def save_model_and_config(model: lgb.Booster, std_scaler, robust_scaler, feature_order: list, std_cols: list, rob_cols: list, output_path: Path):
    verify_feature_integrity(feature_order)
    logger.info("--> 正在保存模型及配置...")

    model.save_model(str(output_path), num_iteration=model.best_iteration)
    logger.info(f"📦 模型主体已保存到: {output_path}")

    order_block = "[order]\n" + "".join([f"{i}={name}\n" for i, name in enumerate(feature_order)]) + "[/order]\n"

    std_indices = [feature_order.index(f) for f in std_cols if f in feature_order]
    robust_indices = [feature_order.index(f) for f in rob_cols if f in feature_order]

    definitions_block = "[definitions]\n"
    if std_indices and std_scaler:
        definitions_block += f"std_type=StandardScaler\nstd_features={','.join(map(str, std_indices))}\nstd_mean={','.join(map(str, std_scaler.mean_))}\nstd_scale={','.join(map(str, std_scaler.scale_))}\n\n"
    if robust_indices and robust_scaler:
        definitions_block += f"robust_type=RobustScaler\nrobust_features={','.join(map(str, robust_indices))}\nrobust_center={','.join(map(str, robust_scaler.center_))}\nrobust_scale={','.join(map(str, robust_scaler.scale_))}\n"
    definitions_block += "[/definitions]\n"

    transformed_indices = set(std_indices + robust_indices)
    untransformed_list = [f"{i}:{name}" for i, name in enumerate(feature_order) if i not in transformed_indices]

    final_transforms_block = (
        "\n\nend of trees\n\n"
        f"[transforms]\n{order_block}{definitions_block}untransformed_features={','.join(untransformed_list)}\ntransform=true\n[/transforms]\n"
    )

    with open(output_path, 'a', encoding='utf-8') as f:
        f.write(final_transforms_block)
    logger.info("✅ 变换配置已成功附加到模型文件末尾。")


def run_training():
    setup_logging()
    print_separator("Mihomo 模型训练开始")
    parser = argparse.ArgumentParser()
    parser.add_argument("--data_dir", type=Path, default=DEFAULT_DATA_DIR)
    parser.add_argument("--output", type=Path, default=DEFAULT_MODEL_PATH)
    args = parser.parse_args()

    try:
        feature_order = get_feature_order()
    except Exception as e:
        logger.error(f"初始化失败: {e}")
        logger.info(f"初始化失败: {e}")
        return

    logger.info("[特征 Schema 校验]")
    logger.info(f"📋 transform.go 定义的特征数量: {len(feature_order)}")
    logger.info(f"🔍 StandardScaler 配置: {len(STD_SCALER_FEATURES)} 个特征")
    logger.info(f"🔍 RobustScaler 配置: {len(ROBUST_SCALER_FEATURES)} 个特征")
    
    scaler_features_set = set(STD_SCALER_FEATURES + ROBUST_SCALER_FEATURES)
    undefined_features = [f for f in feature_order if f not in scaler_features_set]
    if undefined_features:
        logger.info(f"⚠️ 以下特征未配置变换策略，将保持原始值: {undefined_features}")
    
    logger.info("✓ 特征 Schema 校验通过")

    verify_feature_integrity(feature_order)

    try:
        df = load_data(args.data_dir)
    except FileNotFoundError as e:
        logger.info(f"❌ 数据加载失败: {e}")
        logger.info("请确认数据目录中存在 CSV 文件，或使用 --data_dir 指定正确目录。")
        return

    # 节点分布健康检查：检测反馈循环/选择偏差，避免模型自我强化当前偏好
    data_healthy = check_data_balance(df)
    if not data_healthy:
        logger.info("⚠️ 数据分布不健康，本次训练仍在继续，但建议复核数据来源/探索率")

    try:
        result = preprocess_data(df, feature_order)
    except Exception as e:
        logger.info(f"❌ 数据预处理失败: {e}")
        return

    if result is None:
        return
    X, y = result

    X_train, X_test, y_train, y_test = train_test_split(
        X,
        y,
        test_size=0.2,
        random_state=42,
        shuffle=False  # 按时间顺序划分，模拟真实场景（用过去训练，预测未来）
    )
    logger.info(f"🧠 训练集: {len(X_train)} 条 | 🧪 验证集: {len(X_test)} 条")

    std_cols, rob_cols = get_scaler_columns(X_train)
    logger.info("[特征变换]")
    logger.info(f"🔄 StandardScaler 将处理 {len(std_cols)} 个特征")
    logger.info(f"🔄 RobustScaler 将处理 {len(rob_cols)} 个特征")

    std_scaler = StandardScaler() if std_cols else None
    robust_scaler = RobustScaler() if rob_cols else None

    if std_cols and std_scaler:
        std_scaler.fit(X_train[std_cols])
        logger.info(f"✓ StandardScaler 已在训练集上拟合")
    
    if rob_cols and robust_scaler:
        robust_scaler.fit(X_train[rob_cols])
        logger.info(f"✓ RobustScaler 已在训练集上拟合")

    X_train_scaled = apply_scaler_transform(X_train, std_scaler, robust_scaler, std_cols, rob_cols)
    X_test_scaled = apply_scaler_transform(X_test, std_scaler, robust_scaler, std_cols, rob_cols)
    logger.info(f"✓ 已完成训练集和测试集的特征变换")

    model = train_model(X_train_scaled, y_train, X_test_scaled, y_test)

    save_model_and_config(model, std_scaler, robust_scaler, feature_order, std_cols, rob_cols, args.output)

    print_separator("训练完成")
    logger.info(f"🎉 最终模型 '{args.output}' 已生成，随时可以部署！")
    
    header = (
        f"✅ <b>Mihomo 训练成功</b>\n"
        f"📊 数据量: <code>{len(df)}</code> 条\n"
        f"🔄 训练轮数: <code>{model.best_iteration}</code>\n"
        f"🎯 模型: <code>{args.output}</code>"
    )
    send_telegram_logs(header)


if __name__ == "__main__":
    try:
        run_training()
    except Exception as e:
        error_trace = traceback.format_exc()
        logger.error(f"严重错误: {error_trace}")
        logger.info(f"\n❌ 发生严重错误:\n{error_trace}")
        
        header = (
            f"❌ <b>Mihomo 训练失败</b>\n"
            f"⚠️ 错误原因: <code>{str(e)}</code>"
        )
        send_telegram_logs(header)
        sys.exit(1)
