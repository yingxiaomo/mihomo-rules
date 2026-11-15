# Sage 运行资产：纯改名阶段

这些文件只准备本地名称切换，尚未安装到路由器：

- `upsage.sh` → `/root/smart/upsage.sh`
- `update_sage_prior.sh` → `/root/smart/update_sage_prior.sh`
- `sage-profile.example.yaml`：合并至已有 profile 的组选项，不能当完整配置启动。
- `sage-cron.example`：只供逐项替换原两条任务；其他 cron 保持不变。

## 固定边界

旧 `train-pld.yml`、`train_pld.py`、PLD 数据/模型/Release 全部保留。本线只上传和训练 `sage-native`；旧测试事件、旧 bucket 和隐私 key 不迁移、不删除。新内核默认采集目录为 `/etc/nikki/run/sage-events`，新的隐私 key 为 `/etc/nikki/run/sage-native.key`。

首次生产切换必须保持 `exploration: true`、`collect-events: true` 和当前 googlevideo.com 媒体参数；不配置 `prior-model`，不启用灰失效或第二期策略。

## 凭据与通知

仓库文件不含 Telegram 凭据。运行前由既有受保护环境提供 `TG_TOKEN` 与 `TG_CHAT_ID`；cron 不继承交互 shell 环境，部署时必须显式验证注入及通知，不能声称复制文件后通知即已恢复。未提供凭据时通知跳过；原 PLD 脚本不改。rclone 继续读取 `/root/.config/rclone/rclone.conf`，该文件与隐私 key 不上传、不提交。

## 尚未授权的运维增强

本次脚本来自现网版本，只改产品名称/路径并将硬编码通知凭据外置。上传仍是大小校验和原有轮换逻辑；更新仍是原有下载/校验/换装/重启逻辑。没有新增互斥、原子目录切换、完整失败回滚或下载/安装权限分离，不能称作这些功能已经实现。

因此不要把模板自动安装到生产，也不要在初次采集阶段激活未经审核的模型更新重启任务。模型安装/重启与加强校验需要下一步单独确认。脚本测试只证明名称路径、闭合文件上传、失败保留、无发布/同版跳过等原有流程，不证明生产模型已经加载或外部服务已经成功。

A 本地验证不等于 B 远端 CI/Release 成功，更不等于 C 生产授权。首次真实顺序为：内核发布构建 → 获准部署无模型 Sage 并采集 → Sage 原生训练发布 → 单独授权加载模型。
