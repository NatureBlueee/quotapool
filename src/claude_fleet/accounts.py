"""One command that actually turns an account off, and on again.

There were two switches and `fleet` exposed the wrong one.

The switch that decides anything is `disabled` in `teamclaude.json`: the data
plane reads it and skips the account. The control plane has a separate
`accounts.state` column, and that is what `fleet account enable/disable` wrote.
`fleet account list` read the first one.

So turning an account OFF worked by accident — the control plane stopped
choosing it, which is enough. Turning one back ON did not: the control plane
marked it healthy, the data plane went on reporting `disabled: true` on every
routing request, and `coordinator.py` put it straight back into the exclusion
set. No error anywhere. The only thing that could really re-enable an account
was `teamclaude enable`, and `teamclaude` is not on PATH.

That is what "手动调好像也调不了" was, literally: it could be switched off but
not on, and the failure was silent.

## Three modes, because `disabled` was doing three jobs

    active    serve requests
    paused    do not serve, but STAY ALIVE — keep the token fresh, keep reading
              quota. Turning it back on turns on something that can be described.
    retired   do not serve and do not spend anything on it. For a credential
              that is genuinely gone.

The distinction is not cosmetic. Measured on this machine: four accounts were
switched off, and 100 hours later their access tokens were still the ones from
the moment they were switched off and their quota readings were 64 hours stale —
because `disabled` also meant "stop refreshing" and "stop probing". The fleet had
four accounts it could not say anything about.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, Optional

from .db import FleetDB
from .launcher import dataplane_url, load_runtime

MODES = ("active", "paused", "retired")

# Mirrors config.js:withConfigLock and claude-direct. O_EXCL rather than flock
# because it is the only mechanism a dependency-free Node process and CPython
# can both take.
LOCK_WAIT_SECONDS = 3.0
LOCK_STALE_SECONDS = 30.0


def _take_lock(path: Path) -> Optional[int]:
    deadline = time.time() + LOCK_WAIT_SECONDS
    lock = Path(str(path) + ".lock")
    while time.time() < deadline:
        try:
            return os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            try:
                if time.time() - os.stat(lock).st_mtime > LOCK_STALE_SECONDS:
                    os.unlink(lock)
                    continue
            except FileNotFoundError:
                continue
            time.sleep(0.025)
    return None


def _write_credentials(path: Path, account_id: str, mode: str) -> Dict[str, Any]:
    """Set the switch the data plane actually reads, atomically."""
    lock_fd = _take_lock(path)
    if lock_fd is None:
        raise RuntimeError(
            "账号配置的锁被占了 %.0f 秒还没释放。数据面可能正在刷新令牌; 稍后再试。"
            % LOCK_WAIT_SECONDS)
    try:
        config = json.loads(path.read_text(encoding="utf-8"))
        matched = None
        for entry in config.get("accounts", []):
            if not isinstance(entry, dict):
                continue
            if account_id in (entry.get("accountUuid"), entry.get("name")):
                matched = entry
                break
        if matched is None:
            raise RuntimeError("凭据里没有账号 %r。`fleet account list` 看有哪些。"
                               % account_id)

        matched["disabled"] = mode != "active"
        matched["retired"] = mode == "retired"
        # Why, and when. Neither field existed, which is why "四个账号是谁关的、
        # 什么时候关的" had no answer anywhere.
        if mode == "active":
            matched.pop("disabledAt", None)
            matched.pop("disabledReason", None)
        else:
            matched["disabledAt"] = time.time()
            matched["disabledReason"] = mode

        temp = path.with_suffix(path.suffix + ".fleet-account.tmp")
        # The cleanup has to span the rename too, not just the write. Caught by
        # the test: a failing `os.replace` left a half-named copy of every
        # account's credentials sitting beside the real file, which reads as a
        # backup to whoever finds it next.
        try:
            fd = os.open(str(temp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(config, handle, indent=2, ensure_ascii=False)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(str(temp), str(path))
        except BaseException:
            temp.unlink(missing_ok=True)
            raise
        dir_fd = os.open(str(path.parent), os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
        return matched
    finally:
        os.close(lock_fd)
        try:
            os.unlink(str(path) + ".lock")
        except FileNotFoundError:
            pass


def _reload(runtime: Dict[str, Any]) -> str:
    """Make it take effect now rather than at the next restart.

    `POST /teamclaude/reload` already existed and nothing used it from here, so
    an operator change waited for a service restart — which is the one thing
    somebody reaching for this command does not want to do.
    """
    request = urllib.request.Request(
        dataplane_url(runtime, "/teamclaude/reload"), data=b"{}", method="POST",
        headers={"Content-Type": "application/json"})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=10):
            return "已生效"
    except urllib.error.HTTPError as exc:
        return "数据面拒绝了重载 (HTTP %d) —— 改动已落盘, 下次重启生效" % exc.code
    except Exception:                                         # noqa: BLE001
        return "数据面没有应答 —— 改动已落盘, 它启动时会读到"


def set_mode(db: FleetDB, account_id: str, mode: str,
             runtime_path: Optional[str] = None) -> Dict[str, Any]:
    """Both switches and the reload, in one call. The only supported way."""
    if mode not in MODES:
        raise RuntimeError("mode 必须是 %s 之一" % ", ".join(MODES))

    runtime = load_runtime(runtime_path)
    credentials = Path(str(runtime["teamclaude_config"])).expanduser()
    entry = _write_credentials(credentials, account_id, mode)

    # The control plane keys on uuid where it has one, and the credential file
    # may have been addressed by name. Resolve to the same identity the data
    # plane will report, or the two switches disagree again.
    resolved = entry.get("accountUuid") or entry.get("name") or account_id
    state = {"active": "healthy", "paused": "disabled", "retired": "disabled"}[mode]

    from .control import FleetControl

    control = FleetControl(db)
    # Create the row if the data plane has never served this account, rather
    # than skipping. Swallowing the miss was the first attempt and it put the
    # two switches back out of step — the credential file said paused and the
    # control plane had no opinion at all, which is the exact shape this command
    # exists to end.
    control.upsert_account(str(resolved))
    control.set_account_state(str(resolved), state)

    with db.transaction() as conn:
        db.event(conn, "account.mode_set", str(resolved), {
            "mode": mode,
            "credentials": str(credentials),
            "control_state": state,
        })

    return {
        "account_id": resolved,
        "name": entry.get("name"),
        "mode": mode,
        "disabled": entry["disabled"],
        "retired": entry["retired"],
        "dataplane": _reload(runtime),
        "means": {
            "active": "接收请求",
            "paused": "不接请求, 但继续刷新令牌与探测额度 —— 随时可以开回来",
            "retired": "不接请求, 也不再为它花任何东西",
        }[mode],
    }
