# 飞书 CLI 分享

Quotapool 通过本机 `lark-cli` 提供飞书分享能力。授权凭据仍由飞书 CLI 和系统凭据存储管理；控制台只保存分享对象的类型、ID、显示名称及默认勾选状态，不保存 access token、refresh token、App Secret 或登录口令。

## 产品流程

1. 在“飞书分享”设置页检测并绑定本机 `lark-cli`。
2. 使用姓名、邮箱或群名搜索分享对象，可保存多个常用用户和群。
3. 为每个对象配置是否默认勾选，并选择默认发送身份：当前用户或应用机器人。
4. 在控制台点击“发到飞书”，预览标题、Markdown/纯文本、可选本地文件和接收对象。
5. 用户明确点击“确认发送”后才调用本地后端；每个接收对象分别返回成功或失败结果，允许部分成功后有针对性地重试。

未完成授权或未配置接收对象时，“发到飞书”只会引导回设置页，不会打开可发送弹窗，也不会产生外发请求。

## 生成器接入

控制台暴露统一入口：

```js
window.openLarkShare({
  title: "巡检报告",
  content: "# 巡检结果\n\n所有服务正常。",
  format: "markdown",
  filePath: "reports/latest.md"
});
```

字段说明：

- `title`：分享标题。
- `content`：Markdown 或纯文本正文。
- `format`：`markdown` 或 `text`。
- `filePath`：可选的本机文件路径；相对路径以 CLI 工作目录为准。

调用只负责打开并预填预览弹窗，不会自动发送。接收对象和身份仍由用户在弹窗中确认。

## 本地接口

- `POST /api/lark/auth/start`：启动设备授权，返回授权链接和二维码。
- `POST /api/lark/auth/complete`：确认设备授权结果。
- `POST /api/lark/status/refresh`：重新检测 CLI、应用和身份状态。
- `POST /api/lark/recipients/search`：合并搜索用户与群。
- `POST /api/lark/settings`：保存默认身份和多个常用对象。
- `POST /api/lark/share`：按对象逐一发送并返回结果台账。

分享接口还要求请求头 `X-Quotapool-Confirm: share-lark`。它是本地 UI 的显式确认门槛，并不替代飞书授权或权限校验。

## 可靠性与安全边界

- CLI 通过 Node 入口直接启动，不拼接 shell 命令。
- 接收对象 ID、内容长度和文件路径在后端校验。
- 每个对象使用稳定的幂等键，降低重试导致重复发送的概率。
- CLI 错误在返回浏览器前会脱敏。
- 授权 `deviceCode` 只保存在服务进程内存中，不下发到浏览器。
- 所有控制台接口仍只监听 `127.0.0.1`。
