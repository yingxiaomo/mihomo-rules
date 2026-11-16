# Sage 第二期完整路线图规范

> 状态：**Draft for user review**。用户 review 通过后，本文件升级为内核仓与训练仓共同执行基准；在此之前不代表已授权 push、CI、部署或生产策略启用。
>
> **第二期暂停**：当前仅执行 Adaptive → Sage 纯改名；本文阶段算法、参数与验收条款保持不变。旧 Adaptive 数据不迁移、不伪装为 Sage 数据，旧 reader 不保留；Workflow 发布逻辑、Go 版本、校验/事务及其他行为调整推迟到 B 阶段另行确认。
>
> 适用仓库：
> - 运行时内核：`yingxiaomo/mmihos`，本地 `D:\Github\123\mmihos`
> - 数据与训练：`yingxiaomo/mihomo-rules`，本地 `D:\Github\mihomo-rules`
>
> 基线实现：已交付五项改进 `c3f4e809`，原生事件与四维先验链路 `461f5d96` / `67bd61d`，灰失效对账第一阶段 `c60a5049`。这些状态均为本地交付状态；push、远端 CI、Release 和生产部署是相互独立的后续门禁。

## 1. 目的与范围

第二期把 Sage 从“已有可观测、可学习的单节点选择器”演进为具备可靠性对账、跨客户端佐证、提供商级保护、受控恢复探测、成本平局决策和分维度重训闭环的系统。

本期不追求把所有信号直接变成控制权。统一采用：

```text
观测合同 → shadow 反事实 → 用户验收 → soft action → 用户验收 → hard action
```

本文件合并：

1. 已交付五项：diagnostics、churn shadow、受限探索、学习状态持久化、场景/维度先验；
2. 原生事件采集这一共同数据底座；
3. 灰失效对账；
4. 多设备佐证；
5. 提供商熔断 shadow，以及后续 soft/hard；
6. 按需主动探测；
7. 倍率平局；
8. 分维度模型重训触发条件；
9. 暂缓的晚高峰重评条件。

## 2. 全局不可破坏约束

### 2.1 因果边界

- chosen-action outcome 只能说明实际所选节点的结果，不能证明未选节点更优或更差。
- churn、health probe、多设备、ProviderName 都是证据，不单独等于物理根因。
- 多设备共用同一 LAN/WAN，`multi-client corroborated` 不是 `confirmed node failure`。
- `ProviderName` 是本期明确采用的聚合域，但不宣称它必然等于真实机场、入口或落地故障域。

### 2.2 执行边界

- Sage 只在新 TCP 连接或新 UDP association 上选择；不承诺迁移既有 TCP/TLS/HTTP2 流，也不做逐 HTTP 请求调度。
- 所有异步结果必须绑定稳定 member identity；展示名不能作为延迟写入、学习状态或 cooldown 的唯一权威键。
- TCP/UDP、scene、真实流量/主动探测 provenance 必须可区分，不得混池伪装成同质证据。
- 所有网络 I/O、provider 回调和任意外部回调必须在 selector/observer 锁外执行。

### 2.3 Shadow 定义

“shadow”不是“影响很小”，而是以下行为与关闭该功能时完全一致：

- 候选集合、排序、anchor、switch margin；
- cooldown、handoff、探索预算与学习状态；
- 拨号/association 次数、重试、返回错误；
- TCP/UDP 数据、Close/half-close、连接寿命；
- 模型输入、reward、failure strike 和持久化控制状态。

Shadow 仅允许增加有界观测状态、计数器、事件和只读端点。任何 shadow 开关开启后导致上述行为变化，都按功能缺陷处理。

### 2.4 Schema、资源与隐私边界

- API 和持久化 schema 只能 additive 演进并显式升版；旧版本迁移必须有真实旧数据回归。
- 状态必须有界、可确定性淘汰；淘汰 rolling detail 不得修改累计 counters。
- `src_ip` 按本期决定进入**本地原生事件 schema**，来源为 `metadata.SrcIP`；序列化为规范化 IP 文本，无有效值时为空/缺失。
- 聚合主键保持不变，`src_ip` 不参与学习 identity、节点 identity 或 cooldown key。
- 原始 `src_ip` 不进入模型 artifact、公共 diagnostics API 或长期控制状态；训练/统计侧只派生佐证广度后再按数据保留策略处理。

### 2.5 发布与验收边界

- 每个阶段均由用户验收；前一阶段验收不自动授权后一阶段。
- 继承第一阶段已接受的发布依赖：push 顺序固定为内核分支 → 读回远端精确 ref → 训练仓 `main` → 精确提交 CI/目标构建；本文只记录顺序，不构成任何一次 push/CI 授权。
- 部署生产路由器必须另获用户明确授权；文档、实现、测试、push 或 CI 成功均不等于部署授权。
- breaker/probe 与倍率 action 不在同一生产批次同时改变策略，避免无法归因。
- hard 排除永久保留 fail-open；任何状态机都不得排空候选集。

## 3. 状态与执行顺序

| 顺序 | 工作项 | 当前状态 | 进入下一阶段的门禁 |
|---|---|---|---|
| 0 | 五项已交付能力 + 原生事件底座 | 本地完成，未统一完成发布/部署门禁 | 两仓 push、精确 CI/构建、用户授权部署并读回运行哈希 |
| 1 | 灰失效对账 | 本地完成，纯观察 | 用户验收端点、分母、等级、排除枚举及 shadow 不变量；生产开始积累 |
| 2 | 多设备佐证 | 待实现 | `src_ip` schema/迁移/广度计数/隐私边界验收，且不升级硬标签 |
| 3 | ProviderName 熔断 shadow | 待实现 | 反事实状态、影响面、误报与 fail-open 模拟验收；仍不改选路 |
| 4 | 按需主动探测 | 待实现 | 异步、预算、provenance、half-open/全灭恢复和锁外 I/O 验收 |
| 5 | ProviderName soft breaker | 待实现 | 降权效果、恢复、误伤、全灭 fail-open 验收 |
| 6 | ProviderName hard breaker | 待实现 | 排除、half-open、恢复、TTL 与永久 fail-open 验收 |
| C1 | 倍率平局 shadow | 可与 2–4 并行开发，但独立发布 | 反事实选择与成本/质量影响验收 |
| C2 | 倍率平局 action | 待 C1 验收 | 独立开关发布，不与 breaker/probe action 同批 |
| M1 | 分维度重训 | 挂起等待数据 | 原生事件 ≥ 4 周，且每维度有效 `(node,scene)` 平均样本 ≥ 50 |
| T1 | 晚高峰重评 | 挂起 | 原生事件 ≥ 4 周并满足第 14 节严格可比性合同 |

生产观察时钟从包含原生事件和灰失效观测的新内核获授权部署、相应开关启用且运行哈希读回成功时开始；旧内核时期不能补造为新 schema 的生产数据。

---

## 4. 已交付项一：Diagnostics 可观测面

**目标**

为 Sage 提供稳定、只读、版本化的状态入口，使选择、学习、探索、churn、持久化和后续可靠性模块都能被独立验收，而不是依赖日志猜测。

**验收标准**

- `GET /group/{name}/sage` 对正确组返回固定结构；错误组和未来 schema 明确拒绝。
- counters、maps、events、配置快照和版本字段语义固定；零值字段不因“当前没事件”而消失。
- GET 不清理、不刷新、不推进 cooldown/churn，也不改变下一次选择。
- schema 迁移保留已声明兼容的旧 counters/maps/events，并有真实旧版本 fixture。
- 并发读写通过 race 测试，输出集合有界且排序确定。

**影子先行方式**

Diagnostics 本身永久只读，不拥有策略控制权。新模块先把反事实、状态和原因写入该端点，用户验收后才能申请独立 action 开关。

**与现有组件的边界**

- 不替代日志、原生事件文件或训练数据库；它是运行时摘要，不是完整训练源。
- 不通过读取动作维护内部状态。
- 不把展示名当稳定 identity，不泄漏原始 `src_ip`。

## 5. 已交付项二：Churn Shadow

**目标**

观察“连接快速结束/低数据结束”等服务损失迹象，为灰失效提供辅助证据，同时保持因果诚实，不把模糊 lifecycle 当作节点硬失败。

**验收标准**

- transport、target、candidate identity、provenance 与 exclusion 原因可查。
- 成功数据阈值能重置精确 key；neutral/local cancellation、内部流量、无请求、管理收敛关闭不计入。
- TCP/UDP 隔离；同名节点 replacement 不继承旧证据。
- Relay 迟到字节、自然 Close/策略 Close 竞争和 exactly-once settlement 有永久回归。
- churn 不增加官方 failure strike，不直接改变 ranking/cooldown。

**影子先行方式**

始终以辅助观察项运行；只输出 suspicion 与排除原因。后续 breaker 最多把它作为弱证据，不能让 churn 单独触发 soft/hard。

**与现有组件的边界**

- 与灰失效 attempt/close 对账相关，但不等同于 explicit setup/outcome failure。
- 与 media handoff 生命周期共享真实连接路径，但不能把 handoff policy close 记成网络失败。
- 不作为训练标签中的 `failure=1`。

## 6. 已交付项三：受限探索

**目标**

在不破坏稳定选路的前提下，为缺证据候选补齐 chosen-action 观测，避免长期只有当前最优节点获得样本。

**验收标准**

- 探索仅在严格 eligible opportunity 上计数，当前节奏保持每 transport × scene 每 32 次机会最多一次。
- 候选按 evidence coverage、last-explored ordinal、稳定 identity 选择；不使用纯 `hash(target)` 替代时间探索。
- 不越过协议/health/cooldown/handoff 等强约束，不修改 anchor 的正常语义。
- 探索 provenance 可查，结果仍只解释实际 chosen action。
- 候选更新、同名 replacement、并发和持久化恢复具有确定行为。

**影子先行方式**

探索 action 已是已交付能力；其任何新算法先输出 `wouldExplore/wouldChoose/reason`，不得直接替换现有 cadence。若以后采用 rendezvous hash，只允许作为证据相同候选之间的最终稳定 tie-break。

**与现有组件的边界**

- 与按需主动探测共享总观测预算，但目的不同；rescue probe 活跃时暂停普通探索。
- 不把主动探测结果计为探索 chosen-action 用户样本。
- 不为 breaker open 的 ProviderName 执行普通探索；其恢复走 half-open probe。

## 7. 已交付项四：学习状态持久化

**目标**

在重启/配置重载后恢复长期学习证据，同时避免把短期事故、过期对象或旧 generation 控制权永久继承。

**验收标准**

- 文件 ownership、版本、group、member identity、TTL 和上限均明确。
- 同 spec reload 复用正确 owner；changed/removed group 排空旧 writer，且不阻塞新路由发布。
- v1/v2 等受支持迁移有真实 fixture；未来版本、错误 group、损坏文件 fail closed 到安全默认。
- 终止 fence 后 footer/counters 不再被迟到 callback 修改；incomplete attempt 明确记录。
- 短窗口 rolling series、breaker 控制状态、probe 调度债务默认不跨 generation 恢复。

**影子先行方式**

新持久化字段先只恢复到 diagnostics/shadow 状态；确认迁移、TTL 和 identity 无污染后，才能恢复为控制输入。

**与现有组件的边界**

- 长期 metrics/prior evidence 可持久化；短期 cooldown、half-open ownership、探测退避不是长期模型。
- 原生事件文件是 append-only 训练事实，不是可直接恢复的 selector 内存快照。
- 不允许同名 replacement 继承旧 member 的 delayed write。

## 8. 已交付项五：场景/维度先验

**目标**

在实时证据稀疏时，以 scene-aware 的维度先验补充 failure/connect/response/throughput 估计；实时观测随置信度增长覆盖先验，而不是由模型永久统治选择。

**验收标准**

- feature order、model schema、transform metadata 与内核严格匹配；不匹配时安全回退。
- 每个维度独立估计和置信度融合，不把复合 reward 伪装成真实 connect/throughput/failure 标签。
- 训练使用全局时间因果重放和未来时间切片；不将本行 outcome 泄漏进前态。
- 已见/未见组合分开报告；模型加载、缺失模型和坏模型行为可测。
- 在线真实观测最终压过低置信先验。

**影子先行方式**

新模型/新 head 先并行推理，输出 `baselineScore`、`candidateScore` 和 would-select 差异；模型结果不进入 ranking，直到离线时间切片和生产 shadow 都通过用户验收。

**与现有组件的边界**

- 先验只补缺失/低置信度维度，不绕过 health、cooldown、handoff 或 breaker。
- 现有 response 维度继续保留；第 13 节本轮新重训 gate 只冻结用户指定的 failure/connect/throughput 真实标签，response 不在该次新 head 范围内，除非后续单独批准。
- 模型 artifact 不包含 `src_ip` 或设备身份。

## 9. 共同数据底座：原生事件采集

**目标**

以 attempt/outcome 原生事件替代对累计快照做不可靠反推，为灰失效、多设备、分维度训练和晚高峰重评提供可审计事实。

**验收标准**

- attempt 有唯一 ID，并满足 `selected = success + failure + neutral/incomplete` 的生命周期守恒。
- 事件明确记录 event time、node stable identity/display name、ProviderName、scene、transport、target 范围、action provenance 与 exclusion/terminal reason。
- 真实维度保持原单位：connect 毫秒、throughput bps、failure 0/1；缺失值保持缺失，不用 0 冒充。
- 增加 `src_ip` 后 schema 升版，旧 reader 明确拒绝或迁移，训练端有新旧 fixture。
- collector durable readiness、失败后非训练后缀、shutdown fence、reload ownership 与跨仓消费均通过回归。

**影子先行方式**

采集只旁路写事件；写入失败必须告警并停止把文件当训练源，但不能阻断用户连接或改变选择。

**与现有组件的边界**

- 数据底座不自行评分、熔断或选择。
- diagnostics 是摘要，原生事件是事实流，两者可对账但互不伪造。
- 用户授权部署前没有生产原生事件窗口。

## 10. 灰失效对账

**目标**

对账 health probe 与真实 attempt/close 结果，识别“probe 看似存活但真实流量持续异常”等分歧；输出 suspicion，不直接断言根因。

**验收标准**

- 沿用 `GET /group/{name}/sage`，additive 提供固定 `grayFailure` 结构及开关状态。
- key 为稳定 member identity × transport；30 秒 bucket，独立 5 分钟/15 分钟半开窗口。
- attempt 分母严格为 `success + failure`；TCP close 分母严格为 `progress + tiny`，阈值为最终下行 `<1 KiB` 与 `>=1 KiB`。
- 固定 attempt/close exclusion 枚举即使为 0 也存在；等级为 `insufficient/normal/watch/suspected`。
- rolling series 上限 512，确定性淘汰不影响累计 counters。
- diagnostics schema v2 能迁移 v1 counters/maps/events；rolling series 不跨 generation 恢复。
- 开关启用时完整满足第 2.3 节 shadow 不变量。

**影子先行方式**

第一阶段永久只观察，不写 ranking/cooldown/failure strike。后续多设备与 ProviderName breaker 读取的是分歧证据，不回写或篡改灰失效分母。

**与现有组件的边界**

- health 与真实目标不同，只能形成灰失效疑似。
- churn 是弱辅助证据；不能替代 explicit attempt outcome。
- 生产数据依赖新内核先部署启用，旧运行期不能补造。

## 11. 多设备佐证

**目标**

在不改变现有聚合 key 的前提下，量化同一灰失效现象覆盖的客户端来源广度，减少单客户端局部异常造成的误判。

**验收标准**

- 原生事件 schema 增加 `src_ip`，值来自 `metadata.SrcIP`；IPv4-mapped 地址规范化，缺失/无效显式表示 unavailable。
- 灰失效/churn 的既有主聚合 key 完全不变；设备广度是独立字段/计数器。
- 输出至少区分 `deviceEvidenceUnavailable`、`distinctSrcIP`、`single-client suspected`、`multi-client corroborated`。
- 同一 attempt 重复事件不能增加 distinct breadth；窗口过期、上限淘汰和地址重用行为确定。
- 原始 IP 不进入 diagnostics、模型、identity、cooldown 或 ProviderName breaker key。
- 多设备只能提升佐证等级，绝不直接生成 hard failure、strike、soft/hard breaker transition。
- 测试覆盖：同 IP 重复、两个 IP、共享 WAN 语义、无 SrcIP、IPv4-mapped、窗口边界、同名 replacement、schema 兼容及 shadow 不变量。

**影子先行方式**

先在事件和 diagnostics 中仅暴露 breadth 与反事实置信度；比较“无设备证据/单设备/多设备”对后续 shadow breaker 的影响，不改变选择。

**与现有组件的边界**

- `src_ip` 是客户端来源佐证，不是硬件设备 ID；NAT、DHCP、VPN 和共享 WAN 会限制独立性。
- 不取代 target breadth、distinct node 或 explicit setup/outcome 证据。
- 不将多设备命名为 confirmed failure。

## 12. ProviderName 熔断与按需主动探测

### 12.1 ProviderName 熔断

**目标**

当同一 `ProviderName` 下多个节点出现跨目标、跨客户端且有 explicit outcome 支撑的异常时，提供统一保护，降低整组故障期间的重复失败。

**验收标准**

- 聚合域按用户决定使用节点所属 `ProviderName`；空/未知 ProviderName 单独归类且不得意外合并所有未知节点。
- 对账 key 固定为 `ProviderName × transport`；scene 仅作为 diagnostics 分解维度，不改变主分母。
- 与灰失效对账共用 30 秒 bucket、5 分钟短窗和 15 分钟长窗；状态转换必须同时披露短/长窗，不能用累计历史代替当前窗口。
- 真实用户 attempt 的基础分母固定为 `attemptDenom = success + failure`；分子为 `attemptFailures = failure`。neutral、incomplete、local/admin cancellation、内部流量、active probe 和 schema-invalid 事件不进入分母，分别进入固定 exclusion counters。
- 灰分歧使用独立分母：`grayDenom = health-at-selection 已知且 alive 的 success + failure`，`grayFailures = 其中的 failure`；health 缺失/过期不得当作 alive 或 failure。
- breadth 只在同一窗口内对 `attemptFailures` 去重统计 stable node identity、target scope 和可用 `src_ip`；breadth 是状态转换的佐证门槛，不进入 attempt/gray 比率分母。churn 另列为弱证据，不能进入 failure 分子。
- 状态机固定为：

  ```text
  Closed → Suspected → Soft Open → Open → Half Open → Closed
  ```

- Shadow 阶段输出当前状态、证据分母、distinct nodes/targets/src-ip breadth、若启用将影响的候选和 would-select 差异。
- local/neutral cancellation 排除；churn 不能单独触发；“所有 ProviderName 同时异常”必须作为本地/WAN 共同原因保护条件。
- Open 时普通探索不进入；Half Open 只由专门探测恢复。
- 控制状态有 TTL，不作为长期模型持久化；重启不得无限继承 Open。
- Soft 阶段只降权，不排除；Hard 阶段才允许普通选路排除。
- **永久 fail-open：**任何 soft/hard 计算都不得排空候选；全部受限时选择 least-bad / 最早可恢复候选并记录原因。
- `attemptDenom/attemptFailures`、`grayDenom/grayFailures` 与 breadth/exclusion 的语义在本文件中冻结；各状态的数值率阈值、breadth 门槛和 TTL 在该阶段实施规范中基于已验收生产分布预先冻结，shadow、soft、hard 必须复用同一口径，不能上线后临时漂移。

**影子先行方式**

先完整运行状态机但不把状态接入 candidate filter/score；验收误报、状态持续时间、would-exclude 数和 fail-open 模拟后，才能依次申请 Soft、Hard 两个独立 action 开关。

**与现有组件的边界**

- `ProviderName` 是接受的控制聚合域，不被描述为物理故障域。
- 灰失效提供节点级分歧；多设备只增加佐证；breaker 自己拥有短期状态机。
- 不修改原始 metric/reward，不向模型写“提供商失败”伪标签。

### 12.2 按需主动探测

**目标**

为 breaker Half Open、全部候选低于 scene SLO 或全部候选冷却/不可用提供异步恢复路径，使后续连接能发现恢复节点。

**验收标准**

- 当前连接立即选择 least-bad/fail-open 候选；主动探测不得阻塞现场连接做赛马。
- “全部不达标”只使用新鲜有效证据；missing/unknown 不得当作不达标。
- 探测使用受控 endpoint；setup/response 与吞吐 payload 的合同分别定义，不对用户真实目标任意发应用请求。
- 每条结果标记 `provenance=active-probe`，不进入 churn，不冒充用户 throughput 或普通 chosen-action outcome。
- 探测可低置信恢复 health/setup，并驱动 Half Open 决策；不能仅凭单一 probe 证明用户目标业务可用。
- 具备 global/ProviderName/scene 三层预算、singleflight 合并、指数退避和 jitter；rescue probe 活跃时暂停普通探索。
- 所有网络 I/O 在锁外；并发关闭、reload、超时、迟到结果和同名 replacement 安全。
- diagnostics 暴露 trigger、budget、in-flight、结果、provenance、退避和恢复原因。

**影子先行方式**

先运行严格 shadow 的 `dry-run`，只计算 would-probe，不新增拨号、association 或网络 I/O。通过 shadow 验收后，可进入**受控探测观察阶段**：允许在预算内发起真实 probe，但该阶段有网络副作用，明确不属于第 2.3 节 shadow；其结果仍不得改变 ranking/breaker。受控探测的安全性和数据隔离验收后，才允许接入 Half Open 恢复。

**与现有组件的边界**

- 与 exploration 共用观测预算但不是同一种样本；不得污染用户事件标签。
- 是 hard breaker 的必要恢复路径，因此在 hard 排除前完成。
- probe scheduler debt 与 in-flight ownership 不跨重启恢复。

## 13. 倍率平局与分维度重训

### 13.1 倍率平局

**目标**

仅当候选的场景质量没有实质差异时，优先较低流量倍率，降低成本而不牺牲可靠性或明显质量。

**验收标准**

- 倍率来自显式配置/元数据 `cost-multiplier`，不得从节点名称解析；它表示流量倍率，不宣称等于真实货币边际成本。
- 用于平局判断的是内核 `scoreSageMetricsWithPrior(scene, ...)` 输出的归一化 scene cost：`score = scoreMicros / 1_000_000`，范围 `[0,1]` 且越低越好。对候选 `a/b`，仅当 `abs(score_a - score_b) < 0.05`（即严格少于 50,000 scoreMicros、5 个百分点）才算平局；这不是相对百分比，等于 `0.05` 不算平局。
- 先应用协议、health、cooldown、breaker、handoff 等强约束；倍率只在剩余质量平局集合内参与。
- 平局内词典序固定为：较低倍率 → 较低原场景分数 → 稳定 member identity。
- 独立配置开关、独立 diagnostics；shadow 输出 baseline choice、cost-aware choice、分差、倍率差和 would-switch。
- 冷启动、新 anchor 和已有昂贵 anchor 的迁移有确定规则；action 版本必须保留最小驻留/滞回，不能制造成本振荡。
- 探索可以越过成本偏好补证据，但受昂贵节点预算约束；倍率不改 metric/reward/model label。

**影子先行方式**

先只计算成本反事实并统计：平局机会数、would-switch、预计倍率变化、质量分差分布。用户验收后再启用独立 action 开关，且不与 breaker/probe action 同批上线。

**与现有组件的边界**

- 不是新的质量维度，不写入现有四维 score。
- 不越过 failure avoidance、ProviderName breaker 或 media handoff。
- 不替换 switch-margin；它是在满足强约束后的独立平局层。

### 13.2 分维度模型重训触发条件

**目标**

使用原生事件中的真实单位分别训练 failure/connect/throughput 维度，停止从复合 reward 或累计快照反推标签。

**验收标准**

重训门禁必须同时满足：

1. 从已验收生产原生事件开始，覆盖连续时间跨度 `>= 28 天`；
2. 对每个待训练维度 `d`，先由同一套固定 exclusion 合同选出 production user selections，再统计其中该维度有真实有效标签的事件；
3. 令 `P_d` 为窗口内在该维度适用的 production user selection 中出现过的全部稳定 `(node identity, scene)` pair；pair universe 在检查标签是否缺失**之前**建立。某 pair 即使有效标签数为 0，也保留在 `P_d` 并对分子贡献 0，禁止通过删掉零覆盖 pair 抬高平均值。要求：

   ```text
   valid_samples_d / |P_d| >= 50
   ```

4. gate 报告总量、pair 数、平均值、minimum、P10/P50/P90；用户冻结的是平均 `>=50`，minimum 和分位数用于披露覆盖不均，不私自升级为额外硬门禁；
5. 标签精确定义：
   - connect：事件中的真实连接耗时，单位毫秒；
   - throughput：事件中的真实吞吐，单位 bps，只取语义有效的用户流量观测；
   - failure：真实 terminal outcome 映射为 0/1，neutral/incomplete/excluded 不强行映射；
6. 三个 head 独立过 gate、独立训练、独立验收；某一维度不足不得由其他维度样本替代；
7. 按事件时间做未来切片，防止本行 outcome 泄漏，分别报告已见/未见 node-scene 组合；
8. 与当前模型比较 calibration、error、排序反事实和 coverage；没有通过离线与生产 shadow 双验收不得进入 ranking；
9. artifact 带 schema、feature order、label unit、训练时间范围和数据摘要；内核不匹配时安全回退。

**影子先行方式**

新分维度模型与当前先验并行加载、并行推理，只输出 baseline/new score 与 would-select 差异。先验融合和 ranking 接管必须另设 action 开关。

**与现有组件的边界**

- 本轮重训范围按用户决定是 failure/connect/throughput；response 维度沿用现状，未来若重训需单独定义真实 timer origin 和门禁。
- `src_ip` 只用于匹配/佐证或混杂控制，不作为可泛化模型特征或标签。
- active-probe throughput 不混入用户真实 throughput；probe provenance 可单独评估。

## 14. 晚高峰策略：明确挂起

**目标**

只在原生数据具备严格可比性后，重新判断是否存在可重复的晚高峰退化；当前不实现固定时段权重。

**验收标准**

- 原生生产事件跨度至少 28 天。
- 峰值/对照时段按相同 node × scene × transport × target class × client/source strata 匹配，并控制 weekday、节点版本、目标构成和 action provenance。
- 每个进入比较的 strata 在峰值和对照两侧各至少 50 个独立 attempt；同一连接的多条事件不能重复计数。
- failure、connect、response、throughput 分维度报告，不能用一个复合 reward 代替。
- 报告有效样本、被排除样本、覆盖天数、效应量、置信区间和日间稳定性；不能只比较聚合均值/中位数。
- 结论需在多个日期方向一致，并完成控制混杂后的敏感性分析；不满足则继续挂起。
- 即使证实，也先实现“时段先验 shadow + 实时证据覆盖”，不得让固定规则压过新鲜实时观测。

**影子先行方式**

第一次重评只做离线报告；若证据成立，另立时段先验规范并先并行推理，不直接修改在线 ranking。

**与现有组件的边界**

- 旧 chosen-action 约 74k 样本不满足可比性，不能作为本阶段上线依据。
- 晚高峰不是 ProviderName breaker 触发器，也不等于成本倍率。
- 不在本路线图中承诺一定实现时段策略；数据不支持就是验收结论。

## 15. 分阶段统一验收清单

每阶段交付至少包含：

1. 冻结的事件、窗口、分母、状态和 exclusion 合同；
2. 固定 schema/端点及版本迁移 fixture；
3. shadow 开关关闭/开启的行为等价回归；
4. TCP/UDP、scene、identity replacement、reload、并发与 exactly-once 回归；
5. 有界状态、淘汰、TTL、重启恢复边界；
6. 受影响包 race、全仓 test/vet/build；
7. 对核心选择路径的独立冻结树审查；
8. 内核/训练仓 commit、parent、tree、远端 ref 和 CI 精确对应关系；
9. 未部署/已部署、运行哈希、回退点分别记录；
10. 用户验收结论。未验收项不得在台账中写成完成。

## 16. 共同执行基准的生效与变更

- 用户 review 通过后，将文首状态改为 `Baseline v1.0`，并在两仓的入口文档中使用指针引用本文件；不复制第二份正文，避免漂移。
- 后续每项实施规范可细化阈值和测试，但不得静默改变本文件的因果边界、shadow 定义、用户验收权、push/部署边界或 hard fail-open。
- 若必须改变这些共同约束，先修改本文件并由用户 review，再实现代码。
- 当前草案只新增本文档；不代表已经授权提交、push、CI、部署或策略启用。
