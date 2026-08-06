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

## 相关文档

- [智能权重模型训练教程](./TUTORIAL.md) — 详细介绍了如何使用训练脚本、配置自动化 GitHub Actions 以及 Rclone 同步。
