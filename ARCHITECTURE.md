# Claude Fleet 架构与安全边界

```text
Top-level Claude ── fleet run / PATH shim ─┐
Agent View / respawn / teammate ─ wrapper ─┼─ HTTPS proxy ─┐
daemon / workflow ─ stable workload id ────┘               │
                                                            ▼
                                             controlled TeamClaude data plane
                                             OAuth · MITM · SSE · safe replay
                                                            │
                                    route decision before upstream bytes
                                                            ▼
                                               Fleet coordinator + SQLite
                                      execution graph · fencing · policy · MPC
                                                            │
                                                Max subscriptions only
```

## 唯一权威与分层

- Claude Code 持有自己的 session/context，不通过换配置目录迁移账号；
- TeamClaude 持有 OAuth token 并串行 refresh，是凭证单写者；
- Fleet SQLite 是 route、writer、共享保留线、daemon 政策和 execution 谱系的权威；
- `CLAUDE_CONFIG_DIR` 只作为 workload namespace，不作为账号身份；
- 客户端没有 API fallback，协调器或数据面不可用时 fail closed。

## 一次请求的裁决顺序

1. wrapper 为启动族注入稳定 `X-Fleet-Process-Id`、execution/workload 标识；
2. Claude Code 提供原生 `x-claude-code-session-id`；
3. 数据面缓冲请求体，但尚不向上游发送；
4. coordinator 同步匿名额度状态、验证 writer lease、读取持久 route/policy；
5. 先应用 hard pin、共享 floor、workload allow-list、并发与运行时健康硬约束；
6. 保持当前账号 sticky；需要切换时应用 next/chain/preference/reset urgency；
7. 数据面找到所选 OAuth account，刷新 token、重写 account UUID 后发送；
8. `X-Fleet-*` 在本地数据面被剥离，绝不传给 Anthropic；
9. quota 429 且尚未输出时，将失败账号加入本请求 exclude 后再次裁决；
10. 已开始 SSE、普通 RPM 429、超时或网络断开均不跨账号盲目重放。

## 单写者 fencing

writer holder 是独立顶层启动生成的 launch ID。由同一顶层进程产生的 respawn/worker
继承该 ID，因此恢复不会误判；另一个终端单独 resume 同一 session 时 holder 不同，
会得到 `lease_conflict`。显式 takeover 递增 generation，旧 route generation 无法续写。

## Execution graph

网络 session 存入 `executions`，hook 生命周期存入 `execution_nodes`：

```text
session
├─ subagent
├─ team member
└─ forked session
```

只记录允许字段。fork 不自动合并 transcript；已有多个进度版本由 reconciler 标记后，
人工选择 canonical，再通过 Git diff/worktree 合并文件状态。

## 调度函数

硬约束通过后，候选分数主要由：

```text
manual preference
+ 可消费周额度 / 距周 reset 时间
+ 可消费 5h 额度 / 距 5h reset 时间
- 当前活跃 session
- account priority
```

组成。每次请求边界持续重算；sticky 避免无谓 prompt-cache 冷启动。共享账号 floor 和
daemon allowed-accounts 不参加软评分，属于不可穿透约束。

## 运行状态机

```text
HEALTHY  ── drain ──> DRAINING
   │                    │
quota rejected          │ existing sessions finish
   ▼                    ▼
EXHAUSTED ── reset ──> HEALTHY

no eligible subscription account
   └─> execution PARKED / request held
```

没有任何从 PARKED 指向 API billing 的边。

## macOS 与 Linux

macOS 使用两个 LaunchAgent：

- `com.claude-fleet.coordinator`
- `com.claude-fleet.dataplane`

普通 Linux 使用等价 systemd user services。Harness 权威服务器使用 root 管理、以
`nature` 身份运行的 system services，但仍保持 loopback 旁路端口，不接管既有 Harness
unit。第一版保持单活 SQLite coordinator；多机部署时
应把 coordinator 放到 Linux 单活节点，并让 sidecar 通过受认证内网连接，之后再将
route lease 升级到 PostgreSQL。不要在没有分布式 fencing 的情况下直接启动两个
active coordinator。

## 端口与凭证

- 所有服务绑定 `127.0.0.1`；
- 代理 CA 不进入系统 Keychain/系统根信任；
- 配置与 token 文件为 `0600`；
- prompt/response 正文日志默认关闭；
- token 不进入 Fleet 数据库、hook ledger、CLI 状态或测试 fixture；
- 禁用自动更新、keep-warm、sx.org、API key 和自定义 upstream。
