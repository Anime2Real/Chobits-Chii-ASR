# docs/contracts —— 跨服务契约（机器可读单一事实源）

本目录是 Chobits-Chii 家族 **LLM 垫片 ↔ ASR 门面** 跨服务契约的机器可读定义，
为两服务契约的**单一事实源（single source of truth）**（2026-10-06 起）。
契约的人类可读说明分散在：

- `Chobits-Chii-LLM/README.md`「服务接口」节（X-6 错误码表）
- `Chobits-Chii-ASR/README.md`「为切换 Qwen3-ASR 做的准备」节（ASR WS 错误帧表）
- `Chobits-Chii-ServerDeploy` `origin/cloud` 分支 `docs/compatibility.md`
  （最完整的契约文档，含兼容矩阵与上线顺序约束）

内容冲突时以本目录 schema 为准；上述文档中的契约表格是本目录的人类可读镜像。

## 文件

| 文件 | 内容 |
|---|---|
| `errors.schema.json` | X-6 服务端错误码枚举：LLM HTTP 错误体 `code`（11 值）与 ASR WS 错误帧 `code`（7 值，`internal_error` 共享），顶层枚举为两者并集（17 值） |
| `asr-ticket.schema.json` | ASR 流式 WS 票据格式：旧版 3 段 `<exp>.<jti>.<sig>` 与新版 4 段 `<exp>.<jti>.<idb64>.<sig>`，含身份取值与兼容矩阵 |
| `VERSION` | 契约版本号（语义化版本，见下） |

## 修改规则

契约总原则：**错误响应只加不改**；修改任一协议时先上兼容新旧的一侧，
再上只产生新格式的一侧（详见 ServerDeploy `docs/compatibility.md`）。

1. **先改本目录 schema 与 VERSION，再改实现。** 实现侧落点：
   - 错误码：`Chobits-Chii-LLM/tools/provision/errors.py`、
     `Chobits-Chii-ASR/tools/backend_funasr.py`（`ERROR_CODES`）、
     Mascot `packages/common/errors/server-error.ts` 白名单（含三语文案）。
     新增取值必须三端同步，单侧添加视为协议破坏。
   - ASR 票据：签发方 `Chobits-Chii-LLM/tools/provision/state.py`（`issueAsrTicket`）、
     验签方 `Chobits-Chii-ASR/tools/server.py`（`_ticket_verify` / `_ticket_mark_used`）。
2. **只增不改不删**：已有枚举值的语义不得改变；废弃取值保留在枚举中并在
   `x-code-meanings` 标注废弃，不得直接移除（旧客户端仍可能收到）。
3. 修改后同步更新各 README 中的人类可读镜像表格。

## 版本号规则（VERSION 文件）

语义化版本 `MAJOR.MINOR.PATCH`：

- **MAJOR**：破坏性变更（删除/改义已有取值、票据格式不兼容变更）——需要跨端发版协调；
- **MINOR**：向后兼容的新增（新增错误码取值、新增可选字段）；
- **PATCH**：纯文档/描述修订，不改变协议语义。

当前版本：**1.0.0**（2026-10-06 首次落地，内容为既有线上契约的固化，无协议变更）。
