# Publication reconciliation / 发布回执核对

The sole runtime receipt source is api-data's `pipeline-state` branch. Resolve
its current commit once, then read publication.json and notified-fingerprint
from that immutable commit. A later publisher cannot mix two state snapshots.
The source commit, complete database digest and acknowledged fingerprint must
still pass the existing publication contract before reconciliation skips.

唯一运行回执来源是 api-data 的 `pipeline-state` 分支。每次核对先冻结该分支的
完整提交，再从同一提交读取 publication.json 和 notified-fingerprint；并发发布
不会混读两份状态。配置源提交、完整数据库摘要与通知确认指纹仍须符合原契约，
才能跳过通知。

There is no fallback to stale main receipts. A missing state branch stops with
an explicit seed requirement; missing publication or acknowledgement inside an
existing state snapshot requests generation or notification. Server errors,
invalid state references and malformed publication data stop reconciliation.

不回退读取 main 上的旧回执。状态分支缺失时明确失败并要求先准备已验证种子；
已有状态快照中缺发布记录或确认时，要求构建或通知。服务错误、无效状态引用及
损坏发布数据阻止核对，不产生成功结果。

Activation requires the verified api-data state seed and state-isolated API
workflow together. This local compatibility candidate does not publish the seed,
trigger a workflow or change production. Configuration JSON, source versions,
Somua exports and the API/physical/weekly contracts are unchanged.

启用时须配套已验证的 api-data 状态种子及状态隔离工作流。本地配套候选不发布
种子、不触发工作流、不修改生产。配置 JSON、来源版本、苏摩导出以及 API／物理／
周更契约保持不变。

The publication CI pattern runs both original notification tests and the real
local-Git snapshot transport regressions. External GitHub requests are simulated
in those tests; no successful live deployment is asserted.

发布核对 CI 同时运行原通知测试与真实本地 Git 快照传输回归；外部 GitHub 响应
由测试适配，不能据此宣称线上部署成功。
