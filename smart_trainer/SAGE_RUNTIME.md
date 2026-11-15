# Sage 运行资产与生产边界

截至 2026-11-15，Sage 无模型内核和原生事件上传已投入生产；首个真实数据模型已发布，但尚未授权生产加载：

- `upsage.sh` → `/root/smart/upsage.sh`（已安装并完成首次真实上传）
- `update_sage_prior.sh` → `/root/smart/update_sage_prior.sh`（未安装/未激活自动更新任务）
- `sage-profile.example.yaml`：合并至已有 profile 的组选项，不能当完整配置启动。
- `sage-cron.example`：只供逐项替换原两条任务；其他 cron 保持不变。

首个真实闭合文件已通过完整性和四维最低样本门槛，训练 workflow、真实 Go consumer gate、Release 发布与资产下载校验均已完成。Release 为 `sage-prior-model-3`；这只证明训练/发布/消费闭环可运行，不代表模型质量足以启用。当前 connect / throughput / failure 置信度为 0，response 为 0.139509，因此生产继续保持无 `prior-model` 的纯在线模式。

## 固定边界

旧 `train-pld.yml`、`train_pld.py`、PLD 数据/模型/Release 全部保留。本线只上传和训练 `sage-native`；旧测试事件、旧 bucket 和隐私 key 不迁移、不删除。新内核默认采集目录为 `/etc/nikki/run/sage-events`，新的隐私 key 为 `/etc/nikki/run/sage-native.key`。

生产继续保持 `exploration: true`、`collect-events: true` 和当前 googlevideo.com 媒体参数；不配置 `prior-model`，不启用灰失效或第二期策略。首次生产模型加载、自动安装/重启仍需单独授权和受控验收。

## 凭据与通知

仓库文件不含 Telegram 凭据。运行前由既有受保护环境提供 `TG_TOKEN` 与 `TG_CHAT_ID`；cron 不继承交互 shell 环境，部署时必须显式验证注入及通知，不能声称复制文件后通知即已恢复。未提供凭据时通知跳过；原 PLD 脚本不改。rclone 继续读取 `/root/.config/rclone/rclone.conf`，该文件与隐私 key 不上传、不提交。

## 尚未授权的运维增强

本次脚本来自现网版本，只改产品名称/路径并将硬编码通知凭据外置。上传仍是大小校验和原有轮换逻辑；更新仍是原有下载/校验/换装/重启逻辑。没有新增互斥、原子目录切换、完整失败回滚或下载/安装权限分离，不能称作这些功能已经实现。

因此不要把模板自动安装到生产，也不要在初次采集阶段激活未经审核的模型更新重启任务。模型安装/重启与加强校验需要下一步单独确认。脚本测试只证明名称路径、闭合文件上传、失败保留、无发布/同版跳过等原有流程，不证明生产模型已经加载或外部服务已经成功。

A 本地验证不等于 B 远端 CI/Release 成功，更不等于 C 生产授权。当前已完成：内核发布构建 → 无模型 Sage 部署与采集 → 首次真实上传/训练/发布/Go 消费。尚未执行：生产模型加载与自动更新；不得因 Release 成功自动跨越这条授权边界。
