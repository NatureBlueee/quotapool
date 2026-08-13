# 调度器：目标函数与数学模型

README 第一行一直引用这个文件，但它从不存在（`git log --all -- OPTIMIZER.md` 为空）。
模型本身一直在 `src/claude_fleet/optimizer.py` 里实现着，只是没写下来。这份文档补上，
并记录每个权重为什么是那个值——因为权重是可以被无意改坏的，而改坏的方式不会报错，
只会安静地浪费额度。

---

## 1. 经济目标

两条，同时要：

1. **周窗口重置前把额度花完。** 没花完的部分在重置那一刻**直接消失，不结转**。
   一个账号剩 70% 却在一小时后重置，那 70% 就是七十个即将蒸发的单位。
2. **服务不中断。** Agent 在跑的时候不能因为"这个账号没了、下一个还没准备好"而停。

这两条互相拉扯：把所有请求压到快过期的账号上能最大化利用，但那个账号一耗尽，
所有会话同时断。反过来，均匀分散能保证连续，但会让快过期的额度白白蒸发。

**关键在于：这两条由模型的不同部分负责，不是靠一个权重去调和。**

---

## 2. 模型：时间展开的整数最小费用最大流

一个 quantum = 一个周窗口的 1%（`QUANTA_PER_WEEKLY_WINDOW = 100`）。
时间切成 30 分钟的 slot（`SLOT_SECONDS`），时域最长 7 天（`HORIZON_SECONDS`）。

```
                    每个账号的周额度余量 = 一条源边的容量
   source ──────────▶ weekly_node ──────────▶ cycle_node ──────────▶ slot_k ──────▶ sink
          成本:                    成本:                    成本:            成本:
          priority                deadline                migration       slot_index
          + preference            (5h 优先, 周次之)        (仅 slot 0)
          + 负载
```

- **源边容量** = 该账号还剩多少周额度（扣除保留线后）；
- **cycle 边** = 嵌套的 5 小时窗口容量，当前周期用实测余量，未来周期用 `1 - five_floor`；
- **slot→sink 边容量** = 该 30 分钟预测需要多少 quanta，成本 `slot_index`
  （所以近期需求永远不会为了远期需求而被牺牲）；
- 需求由 `estimate_forecast` 从近 6 小时的 `account.runtime_synced` 事件里回归出
  燃烧速率，摊到每个 slot。

时域**止于最晚的那个周重置**。重置之后真实额度会被重新观测、模型重建——
不去发明未来的容量。

### 求解结果怎么用

```python
first = flow.tagged_flow(0)               # 只取 slot 0 的流量
chosen = max(first, key=lambda a: first[a])   # 流量最大的账号
```

**未来所有 slot 的存在，只是为了把成本正确地压到 slot 0 上。** 模型每个请求边界重算，
所以它是滚动时域控制（MPC），不是一次性排产计划。

---

## 3. 两条目标分别由什么保证

这是整份文档最重要的一节。

### 目标一（花完）由 deadline 成本保证

```python
cycle_cost = five_deadline_index * DEADLINE_LEXICOGRAPHIC_SCALE + weekly_deadline_index
```

字典序：先比 5 小时窗口还有几个 slot 到期，再比周窗口。越快到期成本越低，
所以流量优先走它——即"先花快过期的"。

### 目标二（不中断）由容量约束保证，**不是**由任何惩罚项保证

这一点极易误解，而误解会导致把权重调错。

每个未来 slot 都有一条 `slot_k → sink` 的需求边。如果把所有流量压到一个账号，
它的源边容量会先耗尽，后面的 slot 就**无法满足**——最大流会因此自动把需求铺到
多个账号上。**分散不是因为分散被奖励，而是因为不分散根本满足不了需求。**

`tests/test_optimizer_economics.py` 里两个测试钉住了这点：

- `test_demand_spreads_across_accounts_when_one_cannot_cover_it`
- `test_spreading_happens_with_the_load_penalty_removed_entirely`
  ——把所有账号的 `active_families` 归零（等于让负载惩罚完全失效），
  流量仍然铺开。

这就是为什么把 `ACTIVE_FAMILY_LOAD_COST` 从 50,000 降到 500 是安全的：
如果连续性依赖那个惩罚，降它就是拿目标二换目标一。它不依赖。

---

## 4. 权重表，以及为什么是这些数

| 常量 | 值 | 作用 | 为什么是这个量级 |
|---|---|---|---|
| `priority_cost` | `(priority - min) × 1,000,000` | 操作员分层 | 要压过一切自动目标；同时 `decide_route` 在求解前还有一道 `minimum_priority` 硬预筛 |
| `preference_cost` | `500,000` | 软偏好 | 低于 priority 一个量级：能被"账号不可用"绕过，不能被普通紧迫性绕过 |
| `DEADLINE_LEXICOGRAPHIC_SCALE` | `1,000` | 一个 30 分钟 slot 的紧迫性 | 基准单位。1 小时差 = 2,000 |
| `ACTIVE_FAMILY_LOAD_COST` | **`500`** | 负载平局打破 | **必须 < 1,000**。见下 |
| `MIGRATION_COST` | `200` | 换号丢 prompt cache | 只在 slot 0 且候选 ≠ 当前账号时收取。低于半个 slot 的紧迫性——换号是有代价的，但不该拦住真正紧迫的迁移 |

### `ACTIVE_FAMILY_LOAD_COST` 为什么必须小于 1,000

原值 50,000 意味着**一个活跃会话 = 50 个 slot = 25 小时的紧迫性**。

后果是可测的：一个账号持有 67% 额度、43 小时后重置，只要它上面有一个会话，
新请求就会被推给一个 83% 额度、158 小时后才重置的闲置账号——前者那 67% 到期蒸发。
这正是目标一的反面。

现在 500 < 1,000 的含义是：负载只能在**到期时间相同**的账号之间打破平局，
压不过哪怕 30 分钟的真实紧迫性。

`tests/test_optimizer_economics.py::test_the_weight_cannot_outvote_half_an_hour_of_urgency`
把这条不等式直接断言成算术，而不是留在注释里。

### 曾经还有一道预筛，已删除

`control.py` 里原本在调用求解器**之前**有一步
`optimization_accounts = [a for a in ... if a.active_families == minimum_load]`。

它把"有 1 个会话但额度快过期"的账号整个踢出候选集。因为发生在目标函数之前，
deadline 项永远没有机会纠正它。已删（commit `9cc5fcf`），
**只保留 `minimum_priority` 那一道**——那是操作员策略，不是自动目标。

---

## 5. priority 的两层语义（容易搞混）

| 层 | 语义 | 位置 |
|---|---|---|
| `decide_route` | **硬预筛**：可用的高优先级账号存在时，低优先级账号根本不进候选集 | `control.py`，求解器之前 |
| 求解器内部 | **软成本**：`(priority - min) × 1,000,000` 的边成本，可以被压过 | `optimizer.py` |

求解器内部之所以是软的，是有意的：如果一个低优先级账号的 5 小时窗口一小时后就关闭，
把它那点额度花掉、同时把高优先级账号的长窗口留给后面的 slot，是**更便宜也更正确**的
计划。硬性拒绝会造成浪费。

两个测试分别钉住这两层：

- `test_quota_utilization.py::test_priority_still_outranks_urgency`（真实决策路径，硬）
- `test_optimizer_economics.py::test_inside_the_solver_priority_is_a_cost_not_a_veto`（求解器，软）

如果有人把求解器里的 priority "修"成硬否决，第二个测试会失败并说明为什么那不是 bug。

---

## 6. 最优性范围（不要过度宣称）

`OptimizationResult.optimality_scope` 字段的原话：

> exact integral optimum for the current finite-horizon forecast; reused only
> for identical planning snapshots within five seconds

逐条拆开：

- **exact integral**：最小费用流在整数容量上求的是精确整数最优，不是近似或启发式；
- **for the current forecast**：最优性是相对于**当前那份预测**而言的。
  预测错了，解就只是"对错误输入的精确最优"；
- **finite-horizon**：时域止于最晚周重置，之后的世界不在模型里；
- **five seconds**：相同规划快照的解会被复用 5 秒，避免重连风暴把几十个相同求解
  串行压进 SQLite 写事务。

### 模型不知道的事

诚实列出来，因为不写下来就会被当成它知道：

- **未来需求**。`estimate_forecast` 从近 6 小时事件回归燃烧速率，是**平坦外推**——
  没有昼夜模型，不知道你明天要跑一个大任务。数据不足时退化为
  `0.005 × active_families`/小时，并把 `used_default_forecast` 标出来。
- **token 数**。所有量都是窗口百分比，模型不知道一次请求会烧多少 token。
- **Fleet 无法制造需求**。它只能路由已经到来的请求。所以"把额度用完"实际含义是
  **"有请求时优先花快过期的"**，不是"主动去消耗"。如果一周里根本没有那么多请求，
  额度还是会剩——这是模型能力之外的事。
- **模型专属桶**。当前只有一个 `weekly_used` 列，`_effective_weekly` 取
  unscoped 与 model-scoped 的 max（保守方向）。真正的每模型账本尚未实现。

---

## 7. 多机（服务器 + 本地）

同一批账号被两台机器共用时，模型只有在**看到同一份额度真相**时才成立。
两个 coordinator 各自独立求解同一批账号，等于两份互不相识的计划分别以为自己
拥有全部容量——`ARCHITECTURE.md` 禁止这种 split-brain，而实测中它确实发生了。

统一之后，第 3 节的两条保证第一次能在全局生效：服务器的 daemon 会主动去消耗
本地那些即将过期的额度，而不是各自守着自己看得见的那几个账号。

见 `UNIFIED-QUOTA-PLAN.md`。
