# Sage 运行资产与生产边界

Sage P1 ABI 已在训练、workflow 和运行脚本侧切换到独立 v2 命名空间；本仓改动不代表已部署、已采集真实 v2 数据或已发布模型：

- `upsage.sh` → `/root/smart/upsage.sh`（v2 事件上传脚本；部署需另行授权）
- `update_sage_prior.sh` → `/root/smart/update_sage_prior.sh`（v2 模型更新脚本；部署需另行授权）
- `sage-profile.example.yaml`：合并至已有 profile 的组选项，不能当完整配置启动。
- `sage-cron.example`：只供逐项替换原两条任务；其他 cron 保持不变。

Push workflow 只运行完整 Python 单测和 production Go writer → trainer → real Go loader/prediction parity 的 synthetic v2 gate；不会读取 remote 或发布 Release。只有 scheduled/manual workflow 可读取真实 v2 remote，并发布 `sage-prior-v2-model-*` / `SagePrior-v2.zip`。Go consumer 固定为 Sage P1 提交 `cbe478cdd5fd78587755c35a5603a8bd32cf4f7a`。

## 固定边界

旧 `train-pld.yml`、`train_pld.py`、PLD 数据/模型/Release 全部保留。本线只上传和训练 `sage-native-v2`；旧事件、旧 bucket 和旧隐私 key 不迁移、不删除。P1 采集目录为 `/etc/nikki/run/sage-events-v2`，隐私 key 文件名为 `/etc/nikki/run/sage-native-v2.key`，上传 remote 为 `gdrive:smart_data/sage-native-v2`。

训练 ABI 固定为 `sage-native-events-v2` / `sage-prior-native-features-v2`，事件文件前缀 `sage-native-v2`，prior schema `3`。安装目录固定为 `/etc/nikki/run/sage-prior-v2`。旧 `sage-native`、`sage-events`、`sage-prior` 和 `SagePrior-v1.zip` 不作为 P1 输入，也不被脚本迁移或覆盖。

生产继续保持 `exploration: true`、`collect-events: true` 和当前 googlevideo.com 媒体参数；不配置 `prior-model`，不启用灰失效或第二期策略。首次生产模型加载、自动安装/重启仍需单独授权和受控验收。

## 凭据与通知

仓库文件不含 Telegram 凭据。运行前由既有受保护环境提供 `TG_TOKEN` 与 `TG_CHAT_ID`；cron 不继承交互 shell 环境，部署时必须显式验证注入及通知，不能声称复制文件后通知即已恢复。未提供凭据时通知跳过；原 PLD 脚本不改。rclone 继续读取 `/root/.config/rclone/rclone.conf`，该文件与隐私 key 不上传、不提交。

## 尚未授权的运维增强

本次脚本来自现网版本，只改产品名称/路径并将硬编码通知凭据外置。上传仍是大小校验和原有轮换逻辑；更新仍是原有下载/校验/换装/重启逻辑。没有新增互斥、原子目录切换、完整失败回滚或下载/安装权限分离，不能称作这些功能已经实现。

因此不要把模板自动安装到生产，也不要在初次采集阶段激活未经审核的模型更新重启任务。模型安装/重启与加强校验需要下一步单独确认。脚本测试只证明名称路径、闭合文件上传、失败保留、无发布/同版跳过等原有流程，不证明生产模型已经加载或外部服务已经成功。

A 本地验证不等于 B 远端 CI/Release 成功，更不等于 C 生产授权。当前已完成：内核发布构建 → 无模型 Sage 部署与采集 → 首次真实上传/训练/发布/Go 消费。尚未执行：生产模型加载与自动更新；不得因 Release 成功自动跨越这条授权边界。
