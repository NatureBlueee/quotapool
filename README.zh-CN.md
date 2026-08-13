# Quotapool

[English](README.md) · **简体中文**

[![CI](https://github.com/NatureBlueee/quotapool/actions/workflows/ci.yml/badge.svg)](https://github.com/NatureBlueee/quotapool/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-informational.svg)](LICENSE)
[![Zero dependencies](https://img.shields.io/badge/运行时依赖-0-brightgreen.svg)](#零依赖)

把你自己手上的几份 Claude 订阅，变成一个不会中断的池子。

额度用完的时候 Claude Code 照常干活——不用你挑账号，不用重启任何东西，
留给 Claude Desktop 的那一份也还是你的。

整套东西跑在你自己机器上。凭据不离开本机，它只把**你自己的**请求路由到
**你自己持有的**订阅上。

```
claude          # 同一条命令，现在在你所有账号之间调度
```

---

## 谁会需要它

手上不止一份 Claude 订阅、而且会连着几个小时用 Claude Code 的人——尤其是
**订阅不只用来写代码**的人。如果其中一部分额度是留给 Claude Desktop 或
CoWork 的，那正是大多数工具做错的地方。

只有一份订阅、而且只是交互式地用，你不需要这个。

## 它做什么

**它不会停。** 一个额度窗口烧完时，请求会**等**，等额度回来再继续——而不是报错。
一个长任务能跨过窗口边界活下来。

**你不用管它。** 换账号发生在会话中途，不需要重启 Claude Code。你永远不必知道
自己此刻在哪个账号上。

**它不拿不属于它的。** 给任何账号划一条预留线，Claude Code 再忙也花不过去。
那条线就是你下午四点桌面端还能用的原因。

---

## 安装

需要 Node 20+、Python 3.9+，以及已经装好的 Claude Code。

```sh
git clone https://github.com/NatureBlueee/quotapool
cd quotapool
./install.sh --dry-run     # 它会做的每一处改动，全列出来
./install.sh
```

然后把账号加进来，每个都是一次普通的浏览器登录：

```sh
quotapool login                # 每份订阅跑一次
claude
```

`./install.sh --uninstall` 会把机器恢复原状，并且**不删你的凭据**。

> **装到你在意的机器上之前**，先读 [`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md)：
> 它逐项列出这个程序会接管你机器上的什么、假设了哪些前提，以及**我们自己那套
> 部署到底是怎么装的**——两台机器各跑哪些服务和定时器、彼此怎么连、那条链路
> 真出过什么故障。它是写来给你映射到自己环境的，不是让你照抄的。

### 万一坏了

`claude` 是走 Quotapool 的，所以一个装坏的 Quotapool 本来会把 `claude` 一起
带走。它不会：

```sh
claude-direct              # 挑一个账号直连，完全绕开调度器
fleet-rollback now         # 回到上一个被证明可用的版本
quotapool doctor           # 到底哪里不对
```

`claude-direct` 刻意不 import 这套东西的任何其他部分——它只读一个 JSON 文件，
然后 exec 真正的 Claude Code。**在别的都不行的时候它还行**，包括 `claude`
本身已经被装坏的时候。

---

## 零依赖

两边都没有。`pyproject.toml` 没有任何运行时依赖，vendored 数据面的
`package.json` 也没有——只有 Python 标准库和 Node 标准库。

这是**安全属性**，不是审美偏好。这个程序把你所有 OAuth token 放在一个文件里；
每多一个依赖，就多一个能读到那个文件的包。`pip install -e '.[dev]'` 只多装
一样东西：`pytest`，而且只在跑测试时需要。

---

## 两台机器

一台笔记本和一台服务器可以共用同一组订阅。各自跑自己的调度器，一台挂了另一台
照常干活；额度读数在两边之间传播，所以不会出现"某个账号明明还有额度，另一台
却以为它烧完了"。

但**两台不是对等的**：只有一台是控制主机（跑看门狗、有 git、会向另一台部署），
另一台从不反向探测。这意味着**控制主机睡着时，另一台没有人在看**。
[`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md) 把这些不对称全写明了，包括我们自己
测到的空白时长。

---

## 它不会做的事

这些是刻意的，采用之前值得对照一下你的需求：

- **不用 API key，不用 usage credit。** 这里没有任何东西能在你已经付费的订阅
  之外产生花费。
- **不接第三方上游。** 上游 TeamClaude 支持回落到兼容 API，这条被移除了。
- **不做 IP 规避。** 上游能开住宅代理绕过基于 IP 的限流，这条在启动时和每一次
  配置热加载时都会被拒绝。
- **不替别人路由。** 这是一个单用户的本地工具，并且会一直是。

---

## 安全

对手模型是**跟你同一个用户身份的本机进程**——一个被投毒的依赖、一个能访问
localhost 的浏览器扩展、一个构建脚本。绑定在 127.0.0.1 挡住的是网络，不是它们。

[`THREAT-MODEL.md`](THREAT-MODEL.md) 写明了谁是对手、什么最值得偷、
以及**已知薄弱的地方**——包括一次外部扫描的九条发现里，修了哪两条、
剩下七条为什么记录而不修。[`SECURITY.md`](SECURITY.md) 里也没有粉饰
`FLEET_AUTH_MODE=enforce`：它挡得住误闯的调用方，挡不住读过源码的人。

---

## 其余文档

| | |
|---|---|
| [`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md) | 它会接管什么、我们自己怎么装的 |
| [`ARCHITECTURE.md`](ARCHITECTURE.md) | 两个平面，以及为什么切在那里 |
| [`THREAT-MODEL.md`](THREAT-MODEL.md) | 对手是谁、已知薄弱在哪 |
| [`SECURITY.md`](SECURITY.md) | 怎么报安全问题 |
| [`OPTIMIZER.md`](OPTIMIZER.md) | 账号是怎么被选中的 |
| [`TEST-MATRIX.md`](TEST-MATRIX.md) | 哪条测试守哪条要求 |
| [`CONTRIBUTING.md`](CONTRIBUTING.md) | 怎么跑那 604 条测试，以及唯一不可商量的那条规矩 |
| [`CHANGELOG.md`](CHANGELOG.md) | 改了什么，以及是哪次故障逼出来的 |

---

## 基于 TeamClaude

数据面是 [TeamClaude](https://github.com/KarpelesLab/teamclaude)（MIT，
KarpelesLab）的一个 fork，OAuth 刷新、MITM 代理、SSE 转发、额度拒绝重试
仍然是它的。控制面——调度、策略、持久化、运维工具——是原创。

[`NOTICE`](NOTICE) 记录了衍生关系、上游包的哈希，以及改了哪些地方。

不是 Anthropic 的产品，与 Anthropic 无关联。npm 上那个叫 `claude-fleet` 的包
是别人的项目，跟这个没有任何关系。

## 许可

MIT，见 [`LICENSE`](LICENSE)；fork 的上游许可见
[`vendor/teamclaude/LICENSE`](vendor/teamclaude/LICENSE)。
