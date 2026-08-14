# mihomo-rules

Mihomo (Clash Meta) 配置文件模板、规则集与智能节点选择训练脚本。

提供开箱即用的 **Fake-IP / Redir-Host** 两种 DNS 模式配置模板，支持 **Smart（集成 LightGBM 机器学习） / Url-Test / Select** 多种节点选择策略。内置智能训练脚本，可根据历史延迟和负载数据动态优化节点权重，实现节点智能优选。

---

## 功能特性

- **4 套配置模板** — 覆盖 Fake-IP / Redir-Host、Smart / 普通四种组合，满足不同需求
- **智能节点选择** — 集成 LightGBM 机器学习训练脚本，根据历史数据自动优化节点权重
- **规则集自动更新** — 基于 DustinWin 和 MetaCubeX 规则源，支持自动更新
- **完整规则列表** — 涵盖 AI 服务（ChatGPT、Claude、Gemini 等）、流媒体、游戏、微软/苹果/谷歌服务等分类

---

## 配置模板一览

配置文件位于 [`configs/`](./configs/) 目录，共 4 套模板，可根据需求直接选用：

| 模板 | DNS 模式 | 节点策略 | 适用场景 |
|------|----------|----------|----------|
| [Smart-fakeip.yaml](./configs/Smart-fakeip.yaml) | Fake-IP | Smart + Url-Test + Select | **推荐** — 智能节点优选 + Fake-IP 高性能 DNS |
| [fakeip.yaml](./configs/fakeip.yaml) | Fake-IP | Url-Test + Select + 散列负载 | 轻量 — 自动选择 + Fake-IP |
| [smart-redirhost.yaml](./configs/smart-redirhost.yaml) | Redir-Host | Smart + Url-Test + Select | Smart 智能优选 + Redir-Host 兼容模式 |
| [redirhost.yaml](./configs/redirhost.yaml) | Redir-Host | Url-Test + Select + 散列负载 | 轻量 — 自动选择 + Redir-Host 兼容模式 |

> **快速选择**：想体验智能节点选择 → 选 Smart 系列；用 Fake-IP 还是 Redir-Host 取决于你的 Mihomo 版本和设备兼容性（Fake-IP 性能更好，Redir-Host 兼容性更广）。

### 示例配置

[`configs/example/`](./configs/example/) 目录下还提供了节点筛选和代理集合的示例文件，可作为自定义参考。

---

## 规则来源

- [DustinWin/ruleset_geodata](https://github.com/DustinWin/ruleset_geodata)
- [MetaCubeX/meta-rules-dat](https://github.com/MetaCubeX/meta-rules-dat/tree/meta)

---

## 目录结构

```
mihomo-rules/
├── archives/                   # 旧规则存档
├── configs/                    # Mihomo 配置文件
│   ├── Smart-fakeip.yaml      # Smart + Fake-IP 配置模板
│   ├── fakeip.yaml            # Fake-IP 配置模板
│   ├── smart-redirhost.yaml   # Smart + Redir-Host 配置模板
│   ├── redirhost.yaml         # Redir-Host 配置模板
│   └── example/               # 示例配置文件
├── icons/                      # 图标资源
├── rules/                      # 规则集（仅自用）
├── smart_trainer/              # 智能节点权重优化训练脚本
│   ├── train.py               # 训练脚本，用于生成智能模型
│   ├── requirements.txt       # Python 依赖
│   ├── data/                  # 存放 CSV 数据文件（本地训练时使用）
│   └── transform.go           # 可选的本地特征定义文件
├── Model.bin                   # 训练生成的智能模型
└── README.md
```

## 训练脚本版本兼容

- **`smart_trainer/train.py`**：对齐官方 `vernesong/mihomo` Alpha（30 特征 schema），**所有用户使用本脚本**（`GO_REMOTE_URL` 指向官方 transform.go）。包含节点分布健康检查（`check_data_balance`），训练前检测反馈循环/数据偏斜。

> 脚本从 `GO_REMOTE_URL` 下载对应 transform.go 训练。内核版本必须与脚本的特征 schema 匹配，否则模型不兼容会自动回退传统权重。

## 训练工作流（GitHub Actions）

仓库提供两个训练工作流（均在 `main` 分支）：

| 工作流 | 训练脚本 | 模型 tag | 触发方式 | 说明 |
|---|---|---|---|---|
| `Train and Deploy PLD Smart Model` | `train_pld.py` | `smart-pld-model` | **定时自动**（每周日 22:30 UTC）+ 脚本变更触发 + 手动兜底 | **PLD 先验模型**（30 特征，reward 标签），适配魔改内核 `usepld` 链路 |
| `Train and Deploy Smart Model` | `train.py` | `smart-model` | **手动触发**（改自动的方法见文件注释） | 官方 30 特征权重模型，适配官方版内核 `uselightgbm` 链路 |

**PLD 工作流运行条件**：需在仓库 Secrets 配置 `RCLONE_CONFIG_B64`（rclone.conf 的 base64，含 gdrive 远程）、`REMOTE`（如 `gdrive:smart_data`）；`TG_BOT_TOKEN` / `TG_CHAT_ID` 用于训练结果通知（可选）。手动触发：Actions 页面点 `Run workflow`。

> ⚠️ **重要**：训练脚本与内核的特征 schema 必须严格一致——魔改内核（`dev-smart` 分支，PLD 5 特征）请用 `train_pld.py` 并发布到 `smart-pld-model`；官方版内核请用 `train.py` 发布 `smart-model`。混用会导致模型特征错位、选路异常。

## 相关文档

- [智能权重模型训练教程](./TUTORIAL.md) — 详细介绍了如何使用训练脚本、配置自动化 GitHub Actions 以及 Rclone 同步。
