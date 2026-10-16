"""Safe lark-cli bridge used by the loopback dashboard.

The bridge never reads or persists access tokens.  Authentication remains
owned by lark-cli's OS keychain integration; Quotapool stores only the chosen
identity and stable recipient IDs used by the sharing UI.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Sequence

from .desktop import hidden_subprocess_kwargs


_USER_ID = re.compile(r"^ou_[A-Za-z0-9_-]+$")
_CHAT_ID = re.compile(r"^oc_[A-Za-z0-9_-]+$")
_REQUEST_ID = re.compile(r"^[A-Za-z0-9_-]{8,80}$")
_STATUS_LOCK = threading.Lock()
_STATUS_CACHE: tuple[float, Dict[str, Any]] = (0.0, {})


class LarkCLIError(RuntimeError):
    """A redacted lark-cli failure suitable for the local dashboard."""

    def __init__(self, message: str, *, details: Optional[Mapping[str, Any]] = None):
        super().__init__(message)
        self.details = dict(details or {})


def _cli_command() -> Optional[list[str]]:
    """Return an argv prefix without relying on a command shell."""
    if os.name == "nt":
        wrapper = shutil.which("lark-cli.cmd") or shutil.which("lark-cli")
        if not wrapper:
            return None
        root = Path(wrapper).resolve().parent
        script = root / "node_modules" / "@larksuite" / "cli" / "scripts" / "run.js"
        node = root / "node.exe"
        if script.is_file() and node.is_file():
            return [str(node), str(script)]
        return None
    executable = shutil.which("lark-cli")
    return [executable] if executable else None


def _parse_json_output(stdout: str, stderr: str) -> Dict[str, Any]:
    for value in (stdout.strip(), stderr.strip()):
        if not value:
            continue
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            # Some CLI builds print a short notice before the JSON envelope.
            start = value.find("{")
            if start < 0:
                continue
            try:
                parsed = json.loads(value[start:])
            except json.JSONDecodeError:
                continue
        if isinstance(parsed, dict):
            return parsed
    return {}


def _error_message(payload: Mapping[str, Any], fallback: str) -> str:
    error = payload.get("error")
    if isinstance(error, Mapping):
        return str(error.get("message") or error.get("hint") or fallback)
    return str(payload.get("message") or fallback)


def run_lark_cli(
    args: Sequence[str],
    *,
    timeout: float = 30,
    cwd: Optional[Path] = None,
) -> Dict[str, Any]:
    prefix = _cli_command()
    if not prefix:
        raise LarkCLIError("未检测到 lark-cli，请先安装并完成基础配置")
    env = os.environ.copy()
    env["LARKSUITE_CLI_NO_UPDATE_NOTIFIER"] = "1"
    env["LARKSUITE_CLI_NO_SKILLS_NOTIFIER"] = "1"
    try:
        result = subprocess.run(
            [*prefix, *map(str, args)],
            cwd=str(cwd) if cwd else None,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
            **hidden_subprocess_kwargs(),
        )
    except subprocess.TimeoutExpired as exc:
        raise LarkCLIError("飞书 CLI 响应超时，请稍后重试") from exc
    except OSError as exc:
        raise LarkCLIError("无法启动飞书 CLI") from exc
    payload = _parse_json_output(result.stdout, result.stderr)
    if result.returncode != 0 or payload.get("ok") is False:
        details: Dict[str, Any] = {}
        error = payload.get("error")
        if isinstance(error, Mapping):
            for key in ("type", "subtype", "missing_scopes", "console_url", "hint"):
                if error.get(key):
                    details[key] = error[key]
        raise LarkCLIError(_error_message(payload, "飞书 CLI 操作失败"), details=details)
    return payload


def _data(payload: Mapping[str, Any]) -> Dict[str, Any]:
    value = payload.get("data")
    return dict(value) if isinstance(value, Mapping) else dict(payload)


def settings_path(runtime: Mapping[str, Any]) -> Path:
    db_path = str(runtime.get("db_path") or "").strip()
    if db_path:
        return Path(db_path).expanduser().resolve().parent / "lark-share.json"
    root = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    return root / "Quotapool" / "state" / "lark-share.json"


def load_settings(runtime: Mapping[str, Any]) -> Dict[str, Any]:
    path = settings_path(runtime)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        raw = {}
    identity = str(raw.get("identity") or "user")
    if identity not in {"user", "bot"}:
        identity = "user"
    recipients = []
    for item in raw.get("recipients", []):
        try:
            recipients.append(_normalize_recipient(item))
        except (TypeError, ValueError):
            continue
    return {"version": 1, "identity": identity, "recipients": recipients}


def _normalize_recipient(value: Any) -> Dict[str, Any]:
    if not isinstance(value, Mapping):
        raise TypeError("recipient must be an object")
    kind = str(value.get("type") or "")
    recipient_id = str(value.get("id") or "")
    if kind == "user" and not _USER_ID.fullmatch(recipient_id):
        raise ValueError("无效的飞书用户 ID")
    if kind == "chat" and not _CHAT_ID.fullmatch(recipient_id):
        raise ValueError("无效的飞书群 ID")
    if kind not in {"user", "chat"}:
        raise ValueError("分享对象类型只能是用户或群")
    label = str(value.get("label") or recipient_id).strip()[:100]
    detail = str(value.get("detail") or "").strip()[:160]
    return {
        "key": f"{kind}:{recipient_id}",
        "type": kind,
        "id": recipient_id,
        "label": label or recipient_id,
        "detail": detail,
        "default": bool(value.get("default", True)),
    }


def save_settings(runtime: Mapping[str, Any], value: Mapping[str, Any]) -> Dict[str, Any]:
    identity = str(value.get("identity") or "user")
    if identity not in {"user", "bot"}:
        raise ValueError("发送身份只能是 user 或 bot")
    raw_recipients = value.get("recipients")
    if not isinstance(raw_recipients, list):
        raise ValueError("分享对象必须是列表")
    recipients = []
    seen = set()
    for raw in raw_recipients[:50]:
        item = _normalize_recipient(raw)
        if item["key"] in seen:
            continue
        seen.add(item["key"])
        recipients.append(item)
    saved = {"version": 1, "identity": identity, "recipients": recipients}
    path = settings_path(runtime)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(saved, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)
    return saved


def invalidate_status_cache() -> None:
    global _STATUS_CACHE
    with _STATUS_LOCK:
        _STATUS_CACHE = (0.0, {})


def public_status(runtime: Mapping[str, Any], *, fresh: bool = False) -> Dict[str, Any]:
    global _STATUS_CACHE
    settings = load_settings(runtime)
    prefix = _cli_command()
    if not prefix:
        return {"installed": False, "configured": False, "connected": False, "settings": settings}
    with _STATUS_LOCK:
        cached_at, cached = _STATUS_CACHE
        if not fresh and cached and time.monotonic() - cached_at < 15:
            return {**cached, "settings": settings}
    try:
        raw = _data(run_lark_cli(["auth", "status", "--json", "--verify"], timeout=12))
        identities = raw.get("identities") if isinstance(raw.get("identities"), Mapping) else {}
        user = identities.get("user") if isinstance(identities.get("user"), Mapping) else {}
        bot = identities.get("bot") if isinstance(identities.get("bot"), Mapping) else {}
        status = {
            "installed": True,
            "configured": bool(raw.get("appId")),
            "connected": bool(user.get("available") or bot.get("available")),
            "brand": str(raw.get("brand") or "feishu"),
            "appId": str(raw.get("appId") or ""),
            "activeIdentity": str(raw.get("identity") or "none"),
            "user": {
                "available": bool(user.get("available")),
                "status": str(user.get("status") or "unknown"),
                "name": str(user.get("userName") or ""),
                "openId": str(user.get("openId") or ""),
                "verified": bool(user.get("verified") or user.get("tokenStatus") == "valid"),
            },
            "bot": {
                "available": bool(bot.get("available")),
                "status": str(bot.get("status") or "unknown"),
            },
        }
    except LarkCLIError as exc:
        status = {
            "installed": True,
            "configured": False,
            "connected": False,
            "error": str(exc),
            "details": exc.details,
        }
    with _STATUS_LOCK:
        _STATUS_CACHE = (time.monotonic(), dict(status))
    return {**status, "settings": settings}


def start_auth() -> Dict[str, Any]:
    payload = _data(run_lark_cli([
        "auth", "login",
        "--scope", "im:message.send_as_user im:message im:chat:read contact:user.base:readonly",
        "--no-wait", "--json",
    ], timeout=20))
    verification_url = str(
        payload.get("verification_url")
        or payload.get("verification_uri_complete")
        or payload.get("verification_uri")
        or ""
    )
    device_code = str(payload.get("device_code") or "")
    if not verification_url or not device_code:
        raise LarkCLIError("飞书 CLI 未返回授权链接，请检查应用配置")
    qr_data_url = ""
    prefix = _cli_command()
    if prefix:
        with tempfile.TemporaryDirectory(prefix="quotapool-lark-qr-") as temp:
            output = Path(temp) / "authorization.png"
            env = os.environ.copy()
            env["LARKSUITE_CLI_NO_UPDATE_NOTIFIER"] = "1"
            env["LARKSUITE_CLI_NO_SKILLS_NOTIFIER"] = "1"
            result = subprocess.run(
                [*prefix, "auth", "qrcode", verification_url, "--output", output.name],
                cwd=temp,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=15,
                check=False,
                **hidden_subprocess_kwargs(),
            )
            if result.returncode == 0 and output.is_file():
                qr_data_url = "data:image/png;base64," + base64.b64encode(output.read_bytes()).decode("ascii")
    return {
        "verificationUrl": verification_url,
        "deviceCode": device_code,
        "expiresIn": int(payload.get("expires_in") or payload.get("expiresIn") or 600),
        "qrDataUrl": qr_data_url,
    }


def complete_auth(device_code: str) -> Dict[str, Any]:
    if not device_code or len(device_code) > 512:
        raise ValueError("无效的飞书授权会话")
    result = run_lark_cli(["auth", "login", "--device-code", device_code, "--json"], timeout=620)
    invalidate_status_cache()
    return result


def search_recipients(query: str, *, identity: str = "user") -> Dict[str, Any]:
    query = str(query or "").strip()
    if not query or len(query) > 50:
        raise ValueError("请输入 1–50 个字符的姓名、邮箱或群名")
    if identity not in {"user", "bot"}:
        raise ValueError("无效的飞书身份")
    recipients: list[Dict[str, Any]] = []
    errors: list[str] = []
    if identity == "user":
        try:
            users = _data(run_lark_cli([
                "contact", "+search-user", "--query", query,
                "--page-size", "20", "--as", "user", "--format", "json",
            ])).get("users", [])
            for row in users if isinstance(users, list) else []:
                if not isinstance(row, Mapping) or not _USER_ID.fullmatch(str(row.get("open_id") or "")):
                    continue
                recipients.append(_normalize_recipient({
                    "type": "user",
                    "id": row["open_id"],
                    "label": row.get("localized_name") or row["open_id"],
                    "detail": row.get("enterprise_email") or row.get("department") or row.get("email") or "飞书用户",
                    "default": True,
                }))
        except LarkCLIError as exc:
            errors.append(str(exc))
    try:
        chats = _data(run_lark_cli([
            "im", "+chat-search", "--query", query,
            "--page-size", "20", "--as", identity, "--format", "json",
        ])).get("chats", [])
        for row in chats if isinstance(chats, list) else []:
            if not isinstance(row, Mapping) or not _CHAT_ID.fullmatch(str(row.get("chat_id") or "")):
                continue
            recipients.append(_normalize_recipient({
                "type": "chat",
                "id": row["chat_id"],
                "label": row.get("name") or row["chat_id"],
                "detail": "外部群" if row.get("external") else "飞书群聊",
                "default": True,
            }))
    except LarkCLIError as exc:
        errors.append(str(exc))
    unique = {item["key"]: item for item in recipients}
    return {"recipients": list(unique.values()), "errors": errors}


def _send_target_args(recipient: Mapping[str, Any]) -> list[str]:
    return ["--user-id", str(recipient["id"])] if recipient["type"] == "user" else ["--chat-id", str(recipient["id"])]


def _idempotency_key(request_id: str, recipient_key: str, part: str) -> str:
    digest = hashlib.sha256(f"{request_id}:{recipient_key}:{part}".encode("utf-8")).hexdigest()[:32]
    return f"qp-{digest}"


def share(
    runtime: Mapping[str, Any],
    value: Mapping[str, Any],
) -> Dict[str, Any]:
    settings = load_settings(runtime)
    identity = str(value.get("identity") or settings["identity"])
    if identity not in {"user", "bot"}:
        raise ValueError("无效的发送身份")
    request_id = str(value.get("requestId") or "")
    if not _REQUEST_ID.fullmatch(request_id):
        raise ValueError("无效的分享请求 ID")
    selected = value.get("recipientKeys")
    selected_keys = set(map(str, selected)) if isinstance(selected, list) else set()
    recipients = [
        item for item in settings["recipients"]
        if item["key"] in selected_keys or (not selected_keys and item["default"])
    ]
    if not recipients:
        raise ValueError("请至少选择一个飞书分享对象")
    title = str(value.get("title") or "").strip()[:160]
    content = str(value.get("content") or "")
    if len(content) > 20000:
        raise ValueError("分享内容不能超过 20,000 个字符")
    message_format = str(value.get("format") or "markdown")
    if message_format not in {"markdown", "text"}:
        raise ValueError("内容格式只能是 markdown 或 text")
    file_path_text = str(value.get("filePath") or "").strip()
    file_path: Optional[Path] = None
    if file_path_text:
        file_path = Path(file_path_text).expanduser().resolve()
        if not file_path.is_file():
            raise ValueError("要分享的本地文件不存在")
    if not content.strip() and not file_path:
        raise ValueError("请输入分享内容或选择一个本地文件")
    message = content
    if title:
        message = f"## {title}\n\n{content}" if message_format == "markdown" else f"{title}\n\n{content}"
    results = []
    for recipient in recipients:
        item = {"key": recipient["key"], "label": recipient["label"], "ok": True, "messages": []}
        try:
            if message.strip():
                result = _data(run_lark_cli([
                    "im", "+messages-send", *_send_target_args(recipient),
                    f"--{message_format}", message,
                    "--idempotency-key", _idempotency_key(request_id, recipient["key"], "message"),
                    "--as", identity, "--format", "json",
                ], timeout=45))
                item["messages"].append({"type": "message", "messageId": str(result.get("message_id") or "")})
            if file_path:
                result = _data(run_lark_cli([
                    "im", "+messages-send", *_send_target_args(recipient),
                    "--file", f"./{file_path.name}",
                    "--idempotency-key", _idempotency_key(request_id, recipient["key"], "file"),
                    "--as", identity, "--format", "json",
                ], timeout=120, cwd=file_path.parent))
                item["messages"].append({"type": "file", "messageId": str(result.get("message_id") or "")})
        except LarkCLIError as exc:
            item["ok"] = False
            item["error"] = str(exc)
            item["details"] = exc.details
        results.append(item)
    return {
        "ok": all(item["ok"] for item in results),
        "identity": identity,
        "sent": sum(1 for item in results if item["ok"]),
        "failed": sum(1 for item in results if not item["ok"]),
        "results": results,
    }
