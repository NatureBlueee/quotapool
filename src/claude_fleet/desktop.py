from __future__ import annotations

import argparse
import contextlib
import ctypes
import hashlib
import json
import ntpath
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from ctypes import wintypes
from pathlib import Path
from typing import Any, Dict, Iterator, List, Mapping, Optional, Sequence, Tuple

from .db import FleetDB
from .launcher import (
    build_claude_env,
    check_services,
    coordinator_port,
    dataplane_endpoint,
    exec_wrapped,
    load_runtime,
)
from .platform_paths import runtime_path as default_runtime_path


DESKTOP_CODE_RELATIVE = Path("Claude") / "claude-code"
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
CLAUDE_DESKTOP_RELATIVE = Path("app") / "Claude.exe"
DESKTOP_CODE_REAL_NAME = "claude.quotapool-real.exe"
DESKTOP_CODE_MARKER_NAME = ".quotapool-code-shim.json"
RECOVERY_REQUIRED_OBSERVATIONS = 2
RECOVERY_COOLDOWN_SECONDS = 30.0


def hidden_subprocess_kwargs() -> Dict[str, Any]:
    """Flags for a console child that must never flash a Windows terminal."""
    if os.name != "nt":
        return {}
    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startupinfo.wShowWindow = getattr(subprocess, "SW_HIDE", 0)
    return {
        "creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0),
        "startupinfo": startupinfo,
    }


def _version_key(value: str) -> Tuple[Tuple[int, ...], int, str]:
    numbers = tuple(int(part) for part in re.findall(r"\d+", value))
    stable = 1 if re.fullmatch(r"\d+(?:\.\d+)*", value) else 0
    return numbers, stable, value.lower()


def desktop_code_roots(
    environ: Optional[Mapping[str, str]] = None,
) -> List[Path]:
    env = os.environ if environ is None else environ
    roots: List[Path] = []
    for variable, fallback in (
        ("APPDATA", Path.home() / "AppData" / "Roaming"),
        ("LOCALAPPDATA", Path.home() / "AppData" / "Local"),
    ):
        root = Path(env.get(variable) or fallback) / DESKTOP_CODE_RELATIVE
        if root not in roots:
            roots.append(root)
    return roots


def discover_desktop_claude(
    roots: Optional[Sequence[Path]] = None,
) -> List[Dict[str, str]]:
    """Find Desktop-managed Claude Code binaries, newest first."""
    found: List[Dict[str, str]] = []
    for root in roots or desktop_code_roots():
        try:
            versions = list(root.iterdir())
        except OSError:
            continue
        for directory in versions:
            if not directory.is_dir():
                continue
            binaries = [directory / "claude.exe"]
            # Recent Desktop releases add a build-hash directory below each
            # version. Keep the old layout and inspect only one bounded level.
            try:
                binaries.extend(child / "claude.exe" for child in directory.iterdir()
                                if child.is_dir())
            except OSError:
                pass
            for binary in binaries:
                if not binary.is_file():
                    continue
                found.append({
                    "version": directory.name,
                    "binary": str(binary),
                    "root": str(root),
                })
    found.sort(key=lambda item: _version_key(item["version"]), reverse=True)
    return found


def current_desktop_claude(
    roots: Optional[Sequence[Path]] = None,
) -> Optional[Dict[str, str]]:
    installs = discover_desktop_claude(roots)
    return installs[0] if installs else None


def discover_claude_desktop_app() -> Optional[Path]:
    """Resolve the current Store Claude executable without flashing a shell.

    WindowsApps package directories are versioned and are not normally
    enumerable by a standard user.  Get-AppxPackage is the supported way to
    discover the active per-user package after an automatic Store update.
    """
    if os.name != "nt":
        return None
    powershell = Path(os.environ.get("WINDIR", r"C:\Windows")) / (
        "System32/WindowsPowerShell/v1.0/powershell.exe"
    )
    if not powershell.is_file():
        return None
    command = (
        "$p=Get-AppxPackage -Name Claude -ErrorAction SilentlyContinue | "
        "Sort-Object Version -Descending | Select-Object -First 1; "
        "if($p){$p.InstallLocation}"
    )
    try:
        result = subprocess.run(
            [
                str(powershell), "-NoProfile", "-NonInteractive",
                "-Command", command,
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=8,
            check=False,
            **hidden_subprocess_kwargs(),
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0 or not result.stdout.strip():
        return None
    binary = Path(result.stdout.strip()) / CLAUDE_DESKTOP_RELATIVE
    return binary if binary.is_file() else None


def desktop_launch_env(
    runtime: Mapping[str, Any],
    base: Optional[Mapping[str, str]] = None,
) -> Dict[str, str]:
    """Build the inherited environment used by Desktop's local sessions.

    Desktop launches the managed Claude Code binary itself, so Claude Code's
    ``processWrapper`` setting cannot wrap that first process.  Injecting the
    loopback CONNECT proxy into Desktop's own environment makes every local
    session inherit the QuotaPool route.  The Desktop application keeps
    responsibility for its own OAuth credential; the proxy replaces that
    credential only for Anthropic inference traffic.
    """
    env = build_claude_env(
        dict(os.environ if base is None else base),
        dict(runtime),
        workload_class="desktop",
        new_launch=True,
    )
    env.pop("CLAUDE_CODE_OAUTH_TOKEN", None)
    wrapper = str(runtime.get("desktop_wrapper") or "").strip()
    if wrapper:
        env["CLAUDE_CODE_PROCESS_WRAPPER"] = wrapper
    return env


def desktop_native_env(base: Optional[Mapping[str, str]] = None) -> Dict[str, str]:
    """Keep Desktop on its OS network path; the Code shim owns Fleet routing."""
    env = dict(os.environ if base is None else base)
    for key in (
        "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy",
        "all_proxy", "FLEET_HOLDER", "FLEET_SESSION", "FLEET_WORKLOAD_CLASS",
    ):
        env.pop(key, None)
    return env


def launch_claude_desktop(
    runtime_path: Optional[str] = None,
    *,
    executable: Optional[str] = None,
) -> Dict[str, Any]:
    """Start Store Claude with its normal Desktop network configuration."""
    if os.name != "nt":
        raise RuntimeError("Claude Desktop launch is Windows-only")
    ensure_services(runtime_path)
    runtime = load_runtime(runtime_path)
    binary = Path(executable).expanduser() if executable else discover_claude_desktop_app()
    if binary is None or not binary.is_file():
        raise RuntimeError("Microsoft Store Claude Desktop executable was not found")
    flags = (
        getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        | getattr(subprocess, "DETACHED_PROCESS", 0)
        | getattr(subprocess, "CREATE_NO_WINDOW", 0)
    )
    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    process = subprocess.Popen(
        [str(binary)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=desktop_native_env(),
        close_fds=True,
        creationflags=flags,
        startupinfo=startupinfo,
    )
    return {
        "started": True,
        "pid": process.pid,
        "executable": str(binary),
        "proxy_port": int(runtime.get("proxy_port", 13457)),
        "wrapper": str(runtime.get("desktop_wrapper") or ""),
    }


def _read_json(path: Path) -> Dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _update_runtime(runtime_file: Path, changes: Mapping[str, Any]) -> Dict[str, Any]:
    runtime = _read_json(runtime_file)
    if not runtime:
        raise RuntimeError(f"runtime config is missing or invalid: {runtime_file}")
    runtime.update(dict(changes))
    _private_json(runtime_file, runtime)
    return runtime


def install_desktop_code_shim(
    *,
    managed_binary: Optional[str] = None,
    shim_source: Optional[str] = None,
    runtime_path: Optional[str] = None,
) -> Dict[str, Any]:
    """Put a reversible QuotaPool trampoline at Desktop's managed CLI path."""
    if os.name != "nt":
        raise RuntimeError("Claude Desktop code shimming is Windows-only")
    runtime_file = (
        Path(runtime_path).expanduser() if runtime_path else default_runtime_path()
    )
    runtime = load_runtime(str(runtime_file))
    current = current_desktop_claude()
    binary = Path(
        managed_binary or (current or {}).get("binary") or ""
    ).expanduser()
    source = Path(
        shim_source
        or runtime.get("desktop_code_shim_source")
        or Path(sys.executable).with_name("quotapool-desktop-code.exe")
    ).expanduser()
    if not binary.is_file():
        raise RuntimeError("Desktop-managed claude.exe was not found")
    if not source.is_file():
        raise RuntimeError(f"QuotaPool Desktop code shim was not found: {source}")
    real = binary.with_name(DESKTOP_CODE_REAL_NAME)
    marker = binary.with_name(DESKTOP_CODE_MARKER_NAME)
    metadata = _read_json(marker)
    if metadata:
        recorded_real = Path(str(metadata.get("real_binary") or ""))
        if recorded_real != real or not real.is_file():
            raise RuntimeError("existing QuotaPool shim backup is incomplete")
    else:
        if real.exists():
            raise RuntimeError(
                f"refusing to overwrite untracked Claude backup: {real}"
            )
        try:
            os.replace(binary, real)
            temporary = binary.with_name(f".{binary.name}.quotapool-{os.getpid()}")
            shutil.copy2(source, temporary)
            os.replace(temporary, binary)
        except OSError as exc:
            with contextlib.suppress(OSError):
                if not binary.exists() and real.exists():
                    os.replace(real, binary)
            raise RuntimeError(f"could not install Desktop code shim: {exc}") from exc
        metadata = {
            "version": 1,
            "managed_binary": str(binary),
            "real_binary": str(real),
            "real_sha256": _sha256(real),
            "installed_at": time.time(),
        }
    source_hash = _sha256(source)
    if _sha256(binary) != source_hash:
        temporary = binary.with_name(f".{binary.name}.quotapool-{os.getpid()}")
        try:
            shutil.copy2(source, temporary)
            os.replace(temporary, binary)
        except OSError as exc:
            with contextlib.suppress(OSError):
                temporary.unlink()
            raise RuntimeError(f"could not refresh Desktop code shim: {exc}") from exc
    metadata.update({
        "shim_source": str(source),
        "shim_sha256": source_hash,
    })
    _private_json(marker, metadata)
    _update_runtime(runtime_file, {
        "claude_binary": str(real),
        "desktop_managed_claude_binary": str(binary),
        "desktop_code_real_binary": str(real),
        "desktop_code_shim_source": str(source),
        "desktop_code_shim_marker": str(marker),
    })
    return {
        "installed": True,
        "managed_binary": str(binary),
        "real_binary": str(real),
        "marker": str(marker),
        "shim_sha256": source_hash,
    }


def uninstall_desktop_code_shim(
    *,
    runtime_path: Optional[str] = None,
) -> Dict[str, Any]:
    """Restore the exact Claude binary saved by install_desktop_code_shim."""
    runtime_file = (
        Path(runtime_path).expanduser() if runtime_path else default_runtime_path()
    )
    runtime = load_runtime(str(runtime_file))
    marker = Path(str(runtime.get("desktop_code_shim_marker") or ""))
    metadata = _read_json(marker) if str(marker) else {}
    if not metadata:
        return {"restored": False, "reason": "shim marker not found"}
    binary = Path(str(metadata.get("managed_binary") or ""))
    real = Path(str(metadata.get("real_binary") or ""))
    expected_shim = str(metadata.get("shim_sha256") or "")
    if not binary.is_file() or not real.is_file():
        raise RuntimeError("cannot restore Claude: shim or backup is missing")
    if expected_shim and _sha256(binary) != expected_shim:
        raise RuntimeError("managed Claude binary changed; refusing unsafe restore")
    os.replace(real, binary)
    marker.unlink()
    _update_runtime(runtime_file, {
        "claude_binary": str(binary),
        "desktop_managed_claude_binary": str(binary),
        "desktop_code_real_binary": "",
        "desktop_code_shim_source": "",
        "desktop_code_shim_marker": "",
    })
    return {"restored": True, "managed_binary": str(binary)}


def _desktop_session_record(
    arguments: Sequence[str],
    environ: Mapping[str, str],
) -> Dict[str, Any]:
    """Read the Desktop-owned record for an explicitly resumed CLI session."""
    resume: Optional[str] = None
    plugin_dir: Optional[str] = None
    for index, value in enumerate(arguments):
        if value == "--resume" and index + 1 < len(arguments):
            resume = arguments[index + 1]
        elif value.startswith("--resume="):
            resume = value.split("=", 1)[1]
        elif value == "--plugin-dir" and index + 1 < len(arguments):
            plugin_dir = arguments[index + 1]
        elif value.startswith("--plugin-dir="):
            plugin_dir = value.split("=", 1)[1]
    if not resume or not plugin_dir:
        return {}

    parts = Path(plugin_dir).parts
    marker = next(
        (index for index, part in enumerate(parts)
         if part.casefold() == "skills-plugin"),
        None,
    )
    if marker is None or marker + 2 >= len(parts):
        return {}
    # Plugin path: skills-plugin/<org>/<account>. Desktop records invert the
    # identity order: claude-code-sessions/<account>/<org>/<local-id>.json.
    org_uuid, account_uuid = parts[marker + 1:marker + 3]
    appdata = environ.get("APPDATA")
    if not appdata:
        return {}
    directory = (
        Path(appdata) / "Claude" / "claude-code-sessions"
        / account_uuid / org_uuid
    )
    try:
        candidates = directory.glob("*.json")
        for path in candidates:
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError, json.JSONDecodeError):
                continue
            if isinstance(value, dict) and value.get("cliSessionId") == resume:
                return value
    except OSError:
        pass
    return {}


def normalize_desktop_model_settings(
    arguments: Sequence[str],
    environ: Optional[Mapping[str, str]] = None,
) -> List[str]:
    """Keep an explicit Opus launch from silently becoming Fable.

    Claude Desktop can retain ``{"ultracode": true}`` in a session after its
    visible model has been changed back to Opus. Claude Code then starts with
    ``--model claude-opus-*`` but sends ``model: claude-fable-*`` on the wire.
    That defeats model-scoped quota routing: the UI says Opus while the request
    really consumes Fable 7D and correctly waits when that pool is exhausted.

    For resumed Desktop sessions, its saved session record is the user's latest
    model choice and can be newer than the already-running command line. Reconcile
    that model first, then clear only the stale Ultracode flag for Opus. Preserve
    every other setting and every current Fable launch unchanged.
    """
    result = list(arguments)
    record = _desktop_session_record(result, environ or os.environ)
    model: Optional[str] = None
    model_index: Optional[int] = None
    model_prefix = ""
    settings_index: Optional[int] = None
    settings_prefix = ""

    for index, value in enumerate(result):
        if value == "--model" and index + 1 < len(result):
            model = result[index + 1]
            model_index = index + 1
            model_prefix = ""
        elif value.startswith("--model="):
            model = value.split("=", 1)[1]
            model_index = index
            model_prefix = "--model="
        elif value == "--settings" and index + 1 < len(result):
            settings_index = index + 1
            settings_prefix = ""
        elif value.startswith("--settings="):
            settings_index = index
            settings_prefix = "--settings="

    saved_model = record.get("model")
    if (
        isinstance(saved_model, str)
        and saved_model.strip()
        and model_index is not None
        and saved_model != model
    ):
        model = saved_model
        result[model_index] = f"{model_prefix}{saved_model}"

    if not model or "opus" not in model.casefold() or settings_index is None:
        return result

    raw = result[settings_index]
    if settings_prefix:
        raw = raw[len(settings_prefix):]
    try:
        settings = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError):
        return result
    if not isinstance(settings, dict) or settings.get("ultracode") is not True:
        return result

    settings["ultracode"] = False
    encoded = json.dumps(settings, ensure_ascii=False, separators=(",", ":"))
    result[settings_index] = f"{settings_prefix}{encoded}"
    return result


def desktop_code_main(argv: Optional[Sequence[str]] = None) -> int:
    """Entrypoint installed at Desktop's managed claude.exe location."""
    arguments = normalize_desktop_model_settings(
        sys.argv[1:] if argv is None else argv
    )
    try:
        ensure_services()
        runtime = load_runtime()
        real = str(runtime.get("desktop_code_real_binary") or "").strip()
        if not real:
            real = str(Path(sys.argv[0]).with_name(DESKTOP_CODE_REAL_NAME))
        if not Path(real).is_file():
            raise RuntimeError(f"original Desktop Claude binary is missing: {real}")
        exec_wrapped([real, *arguments])
    except RuntimeError as exc:
        print(f"quotapool-desktop-code: {exc}", file=sys.stderr)
        return 78
    return 0


def inspect_desktop(
    *,
    runtime_path: Optional[str] = None,
    settings_path: Optional[str] = None,
    roots: Optional[Sequence[Path]] = None,
) -> Dict[str, Any]:
    """Return a read-only compatibility report. Never starts a process."""
    runtime_file = (
        Path(runtime_path).expanduser()
        if runtime_path else default_runtime_path()
    )
    settings_file = Path(
        settings_path or Path.home() / ".claude" / "settings.json"
    ).expanduser()
    runtime = _read_json(runtime_file)
    settings = _read_json(settings_file)
    installs = discover_desktop_claude(roots)
    expected_wrapper = str(runtime.get("desktop_wrapper") or "")
    actual_wrapper = str(settings.get("processWrapper") or "")
    return {
        "platform": sys.platform,
        "supported_platform": os.name == "nt",
        "desktop_claude": installs[0] if installs else None,
        "desktop_versions": installs,
        "runtime_path": str(runtime_file),
        "runtime_configured": bool(runtime),
        "settings_path": str(settings_file),
        "process_wrapper_configured": bool(actual_wrapper),
        "process_wrapper_is_quotapool": bool(
            expected_wrapper and actual_wrapper == expected_wrapper
        ),
        "services": service_status(runtime) if runtime else {
            "coordinator": False,
            "dataplane": False,
        },
    }


def service_commands(
    runtime: Mapping[str, Any],
    *,
    python_executable: Optional[str] = None,
) -> Dict[str, List[str]]:
    python = python_executable or sys.executable
    node = str(runtime.get("node_binary") or "")
    prefix = str(runtime.get("prefix") or "")
    db_path = str(runtime.get("db_path") or "")
    if not node or not prefix or not db_path:
        raise RuntimeError(
            "runtime.json must define node_binary, prefix, and db_path"
        )
    node_args = [node]
    try:
        result = subprocess.run(
            [node, "--help"], capture_output=True, text=True,
            timeout=5, check=False,
            **hidden_subprocess_kwargs(),
        )
        if "--use-env-proxy" in result.stdout:
            node_args.append("--use-env-proxy")
    except (OSError, subprocess.SubprocessError):
        pass
    windows_command = os.name == "nt" or bool(
        re.match(r"^[A-Za-z]:[\\/]", node)
    )
    dataplane_script = (
        ntpath.join(prefix, "vendor", "teamclaude", "src", "index.js")
        if windows_command else
        str(Path(prefix) / "vendor" / "teamclaude" / "src" / "index.js")
    )
    return {
        "coordinator": [
            python, "-m", "claude_fleet.cli", "--db", db_path,
            "coordinator", "--host", "127.0.0.1",
            "--port", str(coordinator_port(dict(runtime))),
        ],
        "dataplane": node_args + [dataplane_script, "server", "--headless"],
    }


def _url_ready(url: str, timeout: float = 0.6) -> bool:
    # Never let the user's system/terminal proxy capture a loopback health
    # request. ProxyHandler({}) is the urllib equivalent of NO_PROXY=localhost.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(url, timeout=timeout) as response:
            return 200 <= int(response.status) < 300
    except (OSError, urllib.error.URLError):
        return False


def local_port_ready(host: str, port: int, timeout: float = 0.4) -> bool:
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return True
    except OSError:
        return False


def service_status(runtime: Mapping[str, Any]) -> Dict[str, Any]:
    if not runtime:
        return {"coordinator": False, "dataplane": False}
    coordinator_url = (
        f"http://127.0.0.1:{coordinator_port(dict(runtime))}/health"
    )
    dataplane_url = f"{dataplane_endpoint(dict(runtime))}/teamclaude/ready"
    return {
        "coordinator": _url_ready(coordinator_url),
        "dataplane": _url_ready(dataplane_url),
        "coordinator_url": coordinator_url,
        "dataplane_url": dataplane_url,
        "pid_file": str(_pid_file(runtime)),
        "recovery": _read_json(_recovery_file(runtime)),
    }


def _pid_file(runtime: Mapping[str, Any]) -> Path:
    configured = str(runtime.get("desktop_pid_file") or "").strip()
    if configured:
        return Path(configured).expanduser()
    db_path = str(runtime.get("db_path") or "").strip()
    if db_path:
        return Path(db_path).parent / "desktop-services.json"
    state_dir = str(runtime.get("state_dir") or "").strip()
    if state_dir:
        return Path(state_dir) / "desktop-services.json"
    return Path.cwd() / "desktop-services.json"


def _recovery_file(runtime: Mapping[str, Any]) -> Path:
    return _pid_file(runtime).with_name("service-recovery.json")


def _private_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


@contextlib.contextmanager
def _startup_lock(runtime: Mapping[str, Any]) -> Iterator[None]:
    path = _pid_file(runtime).with_suffix(".start.lock")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(path), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        FleetDB._lock_file(fd)
        yield
    finally:
        FleetDB._unlock_file(fd)
        os.close(fd)


def _service_env(
    runtime: Mapping[str, Any],
    *,
    coordinator: bool,
    runtime_file: Optional[str] = None,
) -> Dict[str, str]:
    env = os.environ.copy()
    for key in (
        "HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY",
        "https_proxy", "http_proxy", "all_proxy",
    ):
        env.pop(key, None)
    env["FLEET_RUNTIME_CONFIG"] = str(
        Path(runtime_file).expanduser() if runtime_file else default_runtime_path()
    )
    env["PYTHONUTF8"] = "1"
    env["NO_PROXY"] = env["no_proxy"] = str(
        runtime.get("no_proxy") or "localhost,127.0.0.1,::1"
    )
    if coordinator:
        team = _read_json(Path(str(runtime["teamclaude_config"])))
        token = (
            team.get("fleetCoordinator", {}).get("token")
            if isinstance(team.get("fleetCoordinator"), dict) else None
        )
        if not token:
            raise RuntimeError("teamclaude.json has no coordinator token")
        env["FLEET_COORDINATOR_TOKEN"] = str(token)
    else:
        env["TEAMCLAUDE_CONFIG"] = str(runtime["teamclaude_config"])
        env["TEAMCLAUDE_DISABLE_AUTOUPDATE"] = "1"
        outbound = str(runtime.get("outbound_proxy") or "").strip()
        if outbound:
            for key in ("HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY"):
                env[key] = outbound
    return env


def _spawn_hidden(
    name: str,
    command: Sequence[str],
    env: Mapping[str, str],
    log_dir: Path,
) -> Dict[str, Any]:
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"{name}.log"
    creationflags = 0
    startupinfo = None
    if os.name == "nt":
        creationflags = (
            subprocess.CREATE_NEW_PROCESS_GROUP
            | subprocess.DETACHED_PROCESS
            | subprocess.CREATE_NO_WINDOW
        )
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    with log_path.open("ab", buffering=0) as log:
        process = subprocess.Popen(
            list(command),
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            env=dict(env),
            close_fds=True,
            start_new_session=os.name != "nt",
            creationflags=creationflags,
            startupinfo=startupinfo,
        )
    created_at = _process_created_at(process.pid) or time.time()
    actual_image = _process_image(process.pid)
    return {
        "pid": process.pid,
        "created_at": created_at,
        # Windows venv and NVM launchers may immediately resolve to an
        # underlying interpreter path. Record what the kernel owns so later PID
        # validation does not reject our own healthy service.
        "executable": actual_image or str(Path(command[0]).resolve()),
        "log": str(log_path),
    }


def _wait_ready(runtime: Mapping[str, Any], name: str, timeout: float = 20.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if service_status(runtime).get(name):
            return
        time.sleep(0.15)
    raise RuntimeError(f"{name} did not become ready within {timeout:g}s")


def _service_listener_present(url: str) -> bool:
    """Check the bound socket too: a saturated accept queue can reject connects."""
    endpoint = urllib.parse.urlsplit(url)
    host, port = endpoint.hostname or "127.0.0.1", endpoint.port or 80
    return local_port_ready(host, port) or (os.name == "nt" and _listener_pid(port) is not None)


def _ensure_service_port_available(url: str) -> None:
    """An unhealthy listener must not be replaced by a competing process."""
    endpoint = urllib.parse.urlsplit(url)
    if _service_listener_present(url):
        raise RuntimeError(
            f"Service port {endpoint.port or 80} is occupied but health check failed; "
            "preserving the existing listener, no duplicate process started"
        )


def _existing_service_ready(url: str) -> bool:
    """Retry a busy existing service without spawning or replacing its listener.

    The quick status probe is for UI polling, not a startup failure verdict.
    Bound recovery to three 3-second probes plus 1/2-second backoff.
    """
    if not _service_listener_present(url):
        return False
    for attempt in range(3):
        if attempt:
            time.sleep(attempt)
        if _url_ready(url, timeout=3.0):
            return True
    # Recheck immediately before permitting a new process; never bypass a
    # listener that remains unhealthy, including a non-QuotaPool process.
    _ensure_service_port_available(url)
    return False


def _powershell_output(script: str) -> Optional[str]:
    executable = shutil.which("powershell.exe") or shutil.which("powershell")
    if not executable:
        return None
    try:
        result = subprocess.run(
            [executable, "-NoLogo", "-NoProfile", "-NonInteractive",
             "-Command", script],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=8,
            check=False,
            **hidden_subprocess_kwargs(),
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    value = result.stdout.strip()
    return value or None


def _listener_pid(port: int) -> Optional[int]:
    value = _powershell_output(
        "Get-NetTCPConnection -State Listen -LocalPort %d "
        "-ErrorAction SilentlyContinue | Select-Object -First 1 "
        "-ExpandProperty OwningProcess" % int(port)
    )
    try:
        return int(value or "")
    except ValueError:
        return None


def _process_command_line(pid: int) -> Optional[str]:
    return _powershell_output(
        "$p=Get-CimInstance Win32_Process -Filter 'ProcessId = %d' "
        "-ErrorAction SilentlyContinue; if ($p) { $p.CommandLine }" % int(pid)
    )


def _owned_listener_record(
    name: str, url: str, command: Sequence[str]
) -> Optional[Dict[str, Any]]:
    """Identify a listener as this exact QuotaPool service, or fail closed."""
    endpoint = urllib.parse.urlsplit(url)
    port = int(endpoint.port or 80)
    pid = _listener_pid(port)
    if not pid:
        return None
    image = _process_image(pid)
    command_line = _process_command_line(pid)
    created_at = _process_created_at(pid)
    if not image or not command_line or created_at is None or not command:
        return None
    expected_image = os.path.normcase(os.path.abspath(str(command[0])))
    actual_image = os.path.normcase(os.path.abspath(image))
    same_file = False
    try:
        same_file = os.path.samefile(expected_image, actual_image)
    except OSError:
        pass
    # A Python venv launcher is not a symlink/hardlink to its base interpreter,
    # so samefile is false even though the kernel reports the base image. In
    # that case accept the python/pythonw family, but still require every exact
    # service argument below. Non-Python launchers also require the exact launch
    # path in Win32_Process.CommandLine.
    expected_name = Path(expected_image).name.lower()
    actual_name = Path(actual_image).name.lower()
    python_family = {"python.exe", "pythonw.exe"}
    launch_image_in_command = (
        expected_name == actual_name
        and expected_image.replace("/", "\\").lower()
        in command_line.replace("/", "\\").lower()
    ) or (expected_name in python_family and actual_name in python_family)
    if expected_image != actual_image and not same_file and not launch_image_in_command:
        return None
    normalized = command_line.replace("/", "\\").lower()
    # Every service argument is checked, including the script/module, mode,
    # database and port. A random node/python process on the configured port is
    # therefore preserved rather than mistaken for QuotaPool.
    for argument in command[1:]:
        token = str(argument).replace("/", "\\").lower().strip()
        if token and token not in normalized:
            return None
    return {
        "pid": pid,
        "created_at": created_at,
        "executable": image,
        "service": name,
        "port": port,
    }


def _clear_recovery_observation(
    runtime: Mapping[str, Any], name: str
) -> None:
    path = _recovery_file(runtime)
    state = _read_json(path)
    if name not in state:
        return
    state.pop(name, None)
    if state:
        _private_json(path, state)
    elif path.exists():
        path.unlink()


def _recover_owned_unhealthy_listener(
    runtime: Mapping[str, Any],
    name: str,
    url: str,
    command: Sequence[str],
    *,
    now: Optional[float] = None,
) -> Dict[str, Any]:
    """Restart only a repeatedly unhealthy listener proven to be ours."""
    moment = time.time() if now is None else float(now)
    record = _owned_listener_record(name, url, command)
    if record is None:
        raise RuntimeError(
            f"{name} port is occupied but ownership could not be proven; "
            "preserving the listener"
        )
    path = _recovery_file(runtime)
    state = _read_json(path)
    previous = state.get(name) if isinstance(state.get(name), dict) else {}
    same_process = int(previous.get("pid") or -1) == int(record["pid"])
    first_seen = float(previous.get("first_seen") or moment) if same_process else moment
    observations = int(previous.get("observations") or 0) + 1 if same_process else 1
    state[name] = {
        "pid": record["pid"],
        "first_seen": first_seen,
        "last_seen": moment,
        "observations": observations,
        "state": "confirming",
    }
    _private_json(path, state)
    elapsed = moment - first_seen
    if (observations < RECOVERY_REQUIRED_OBSERVATIONS
            or elapsed < RECOVERY_COOLDOWN_SECONDS):
        raise RuntimeError(
            f"{name} is unhealthy but owned by QuotaPool; recovery confirmation "
            f"{observations}/{RECOVERY_REQUIRED_OBSERVATIONS}, "
            f"cooldown {max(0, RECOVERY_COOLDOWN_SECONDS - elapsed):.0f}s"
        )

    os.kill(int(record["pid"]), signal.SIGTERM)
    endpoint = urllib.parse.urlsplit(url)
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        if not local_port_ready(endpoint.hostname or "127.0.0.1",
                                endpoint.port or 80):
            _clear_recovery_observation(runtime, name)
            return {"restarted": True, **record}
        time.sleep(0.2)
    state = _read_json(path)
    if isinstance(state.get(name), dict):
        state[name]["state"] = "termination-timeout"
        _private_json(path, state)
    raise RuntimeError(f"{name} did not release its port after safe termination")


def start_services(runtime_path: Optional[str] = None) -> Dict[str, Any]:
    if os.name != "nt":
        raise RuntimeError("quotapool-desktop service startup is Windows-only")
    runtime = load_runtime(runtime_path)
    with _startup_lock(runtime):
        status = service_status(runtime)
        records = _read_json(_pid_file(runtime))
        commands = service_commands(runtime)
        log_dir = Path(str(runtime["db_path"])).parent / "logs"
        records_changed = False
        for healthy_name in ("coordinator", "dataplane"):
            if status.get(healthy_name):
                _clear_recovery_observation(runtime, healthy_name)
                command = commands.get(healthy_name)
                url = status.get(f"{healthy_name}_url")
                if command and url:
                    owned = _owned_listener_record(
                        healthy_name, str(url), command,
                    )
                    if owned:
                        old = records.get(healthy_name)
                        if isinstance(old, dict) and old.get("log"):
                            owned["log"] = old["log"]
                        if old != owned:
                            records[healthy_name] = owned
                            records_changed = True
        if records_changed:
            _private_json(_pid_file(runtime), records)
        coordinator_ready = bool(status["coordinator"])
        if not coordinator_ready:
            try:
                coordinator_ready = _existing_service_ready(status["coordinator_url"])
            except RuntimeError:
                _recover_owned_unhealthy_listener(
                    runtime, "coordinator", status["coordinator_url"],
                    commands["coordinator"],
                )
                coordinator_ready = False
        if not coordinator_ready:
            _ensure_service_port_available(status["coordinator_url"])
            spawned = _spawn_hidden(
                "coordinator", commands["coordinator"],
                _service_env(
                    runtime, coordinator=True, runtime_file=runtime_path
                ), log_dir,
            )
            records["coordinator"] = spawned
            _private_json(_pid_file(runtime), records)
            _wait_ready(runtime, "coordinator")
            owned = _owned_listener_record(
                "coordinator", status["coordinator_url"], commands["coordinator"]
            )
            if owned:
                owned["log"] = spawned["log"]
                records["coordinator"] = owned
                _private_json(_pid_file(runtime), records)
        dataplane_ready = bool(status["dataplane"])
        if not dataplane_ready:
            try:
                dataplane_ready = _existing_service_ready(status["dataplane_url"])
            except RuntimeError:
                _recover_owned_unhealthy_listener(
                    runtime, "dataplane", status["dataplane_url"],
                    commands["dataplane"],
                )
                dataplane_ready = False
        if not dataplane_ready:
            _ensure_service_port_available(status["dataplane_url"])
            spawned = _spawn_hidden(
                "dataplane", commands["dataplane"],
                _service_env(
                    runtime, coordinator=False, runtime_file=runtime_path
                ), log_dir,
            )
            records["dataplane"] = spawned
            _private_json(_pid_file(runtime), records)
            _wait_ready(runtime, "dataplane")
            owned = _owned_listener_record(
                "dataplane", status["dataplane_url"], commands["dataplane"]
            )
            if owned:
                owned["log"] = spawned["log"]
                records["dataplane"] = owned
                _private_json(_pid_file(runtime), records)
        status = service_status(runtime)
    # Claude Desktop downloads its CLI into a versioned directory.  A newly
    # downloaded version bypasses the old directory, so the one-minute service
    # watchdog also applies the reversible shim to the newest idle binary.
    source = str(runtime.get("desktop_code_shim_source") or "").strip()
    if source:
        try:
            status["desktop_code_shim"] = install_desktop_code_shim(
                shim_source=source,
                runtime_path=runtime_path,
            )
        except RuntimeError as exc:
            # Service recovery must not fail just because Claude is currently
            # holding the executable open during an update.  The next watchdog
            # pass retries, while the current session keeps running.
            status["desktop_code_shim"] = {
                "installed": False,
                "retryable_error": str(exc),
            }
    return status


def ensure_services(runtime_path: Optional[str] = None) -> None:
    runtime = load_runtime(runtime_path)
    status = service_status(runtime)
    if not status["coordinator"] or not status["dataplane"]:
        start_services(runtime_path)
    check_services(runtime)


def _process_created_at(pid: int) -> Optional[float]:
    if os.name != "nt":
        return None
    kernel32 = _kernel32()
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return None
    try:
        creation = wintypes.FILETIME()
        exit_time = wintypes.FILETIME()
        kernel = wintypes.FILETIME()
        user = wintypes.FILETIME()
        if not kernel32.GetProcessTimes(
            handle, ctypes.byref(creation), ctypes.byref(exit_time),
            ctypes.byref(kernel), ctypes.byref(user),
        ):
            return None
        ticks = (creation.dwHighDateTime << 32) | creation.dwLowDateTime
        return ticks / 10_000_000 - 11_644_473_600
    finally:
        kernel32.CloseHandle(handle)


def _process_image(pid: int) -> Optional[str]:
    if os.name != "nt":
        return None
    kernel32 = _kernel32()
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return None
    try:
        size = wintypes.DWORD(32768)
        buffer = ctypes.create_unicode_buffer(size.value)
        if not kernel32.QueryFullProcessImageNameW(
            handle, 0, buffer, ctypes.byref(size)
        ):
            return None
        return buffer.value
    finally:
        kernel32.CloseHandle(handle)


def _kernel32() -> Any:
    """Load the small set of typed Win32 process APIs used for PID safety."""
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    kernel32.GetProcessTimes.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.FILETIME), ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME), ctypes.POINTER(wintypes.FILETIME),
    ]
    kernel32.GetProcessTimes.restype = wintypes.BOOL
    kernel32.QueryFullProcessImageNameW.argtypes = [
        wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
        ctypes.POINTER(wintypes.DWORD),
    ]
    kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
    return kernel32


def _record_matches_process(record: Mapping[str, Any]) -> bool:
    try:
        pid = int(record["pid"])
        expected_created = float(record["created_at"])
        expected_image = os.path.normcase(os.path.abspath(str(record["executable"])))
    except (KeyError, TypeError, ValueError):
        return False
    created = _process_created_at(pid)
    image = _process_image(pid)
    if created is None or image is None:
        return False
    actual_image = os.path.normcase(os.path.abspath(image))
    executable_matches = actual_image == expected_image
    if not executable_matches:
        try:
            executable_matches = os.path.samefile(actual_image, expected_image)
        except OSError:
            executable_matches = False
    if not executable_matches:
        command_line = _process_command_line(pid) or ""
        executable_matches = (
            Path(actual_image).name.lower() == Path(expected_image).name.lower()
            and expected_image.replace("/", "\\").lower()
            in command_line.replace("/", "\\").lower()
        )
    return abs(created - expected_created) < 2.0 and executable_matches


def stop_services(runtime_path: Optional[str] = None) -> Dict[str, Any]:
    if os.name != "nt":
        raise RuntimeError("quotapool-desktop service stop is Windows-only")
    runtime = load_runtime(runtime_path)
    with _startup_lock(runtime):
        path = _pid_file(runtime)
        records = _read_json(path)
        stopped: List[str] = []
        skipped: List[str] = []
        for name in ("dataplane", "coordinator"):
            record = records.get(name)
            if not isinstance(record, dict) or not _record_matches_process(record):
                if record:
                    skipped.append(name)
                continue
            try:
                os.kill(int(record["pid"]), signal.SIGTERM)
                stopped.append(name)
                records.pop(name, None)
            except OSError:
                skipped.append(name)
        if records:
            _private_json(path, records)
        elif path.exists():
            path.unlink()
        return {"stopped": stopped, "skipped": skipped}


def wrapper_main(argv: Optional[Sequence[str]] = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    try:
        ensure_services()
        exec_wrapped(arguments)
    except RuntimeError as exc:
        print(f"quotapool-desktop-wrapper: {exc}", file=sys.stderr)
        return 78
    return 0


def _parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="quotapool-desktop")
    root.add_argument("--runtime")
    commands = root.add_subparsers(dest="command", required=True)
    inspect_cmd = commands.add_parser("inspect")
    inspect_cmd.add_argument("--settings")
    commands.add_parser("status")
    commands.add_parser("start")
    commands.add_parser("stop")
    launch_cmd = commands.add_parser("launch")
    launch_cmd.add_argument("--executable")
    install_shim = commands.add_parser("install-code-shim")
    install_shim.add_argument("--managed")
    install_shim.add_argument("--source")
    commands.add_parser("uninstall-code-shim")
    dashboard_cmd = commands.add_parser("dashboard")
    dashboard_cmd.add_argument("--host", default="127.0.0.1")
    dashboard_cmd.add_argument("--port", type=int, default=17892)
    dashboard_cmd.add_argument("--open", action="store_true")
    dashboard_cmd.add_argument("--background", action="store_true")
    return root


def _start_dashboard_background(
    runtime_path: Optional[str], host: str, port: int, open_browser: bool
) -> Dict[str, Any]:
    runtime = load_runtime(runtime_path)
    url = f"http://{host}:{port}/"
    if host not in ("127.0.0.1", "localhost", "::1"):
        raise RuntimeError("dashboard is loopback-only")
    command = [
        sys.executable, "-m", "claude_fleet.dashboard",
        "--host", host, "--port", str(port),
    ]
    dashboard_entry = runtime.get('dashboard_entry')
    if dashboard_entry:
        entry = Path(str(dashboard_entry))
        if not entry.is_absolute() or not entry.is_file():
            raise RuntimeError('configured dashboard entry is unavailable')
        command = [sys.executable, str(entry), '--host', host, '--port', str(port)]
    if runtime_path:
        command.extend(["--runtime", runtime_path])
    with _startup_lock(runtime):
        # Snapshot collection intentionally samples disks, processes and
        # subscriptions, so it can take several seconds under host pressure.
        # Service liveness uses the cheap shell document instead; otherwise a
        # healthy dashboard is misclassified and background start appears to
        # hang while repeatedly executing the most expensive endpoint.
        if _url_ready(url, timeout=2.0):
            already_running = True
        else:
            # A slow snapshot is not evidence that the server is gone. Starting
            # a replacement while its port is still listening used to create a
            # growing set of Windows dashboard processes because SO_REUSEADDR
            # permits multiple listeners there.
            occupied_deadline = time.monotonic() + 10.0
            while local_port_ready(host, port) and time.monotonic() < occupied_deadline:
                if _url_ready(url, timeout=2.0):
                    already_running = True
                    break
                time.sleep(0.2)
            else:
                if local_port_ready(host, port):
                    raise RuntimeError(
                        "dashboard port is listening but the snapshot API is not ready"
                    )
                already_running = False

        if not already_running:
            log_dir = Path(str(runtime["db_path"])).parent / "logs"
            log_dir.mkdir(parents=True, exist_ok=True)
            record = _spawn_hidden(
                "dashboard", command,
                _service_env(runtime, coordinator=False, runtime_file=runtime_path),
                log_dir,
            )
            deadline = time.monotonic() + 12.0
            while time.monotonic() < deadline:
                if _url_ready(url, timeout=2.0):
                    records = _read_json(_pid_file(runtime))
                    records["dashboard"] = record
                    _private_json(_pid_file(runtime), records)
                    break
                time.sleep(0.15)
            else:
                raise RuntimeError("dashboard did not become ready within 12s")

        owned = _owned_listener_record("dashboard", f"{url}api/snapshot", command)
        if owned:
            records = _read_json(_pid_file(runtime))
            old = records.get("dashboard")
            if isinstance(old, dict) and old.get("log"):
                owned["log"] = old["log"]
            records["dashboard"] = owned
            _private_json(_pid_file(runtime), records)

    if open_browser:
        import webbrowser
        webbrowser.open(url)
    return {"running": True, "already_running": already_running, "url": url}


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "dashboard":
            if args.background:
                result = _start_dashboard_background(
                    args.runtime, args.host, args.port, args.open
                )
                print(json.dumps(result, ensure_ascii=False, indent=2))
                return 0
            from .dashboard import serve_dashboard

            return serve_dashboard(
                args.runtime,
                host=args.host,
                port=args.port,
                open_browser=args.open,
            )
        if args.command == "inspect":
            result = inspect_desktop(
                runtime_path=args.runtime,
                settings_path=args.settings,
            )
        elif args.command == "status":
            result = service_status(load_runtime(args.runtime))
        elif args.command == "start":
            result = start_services(args.runtime)
        elif args.command == "launch":
            result = launch_claude_desktop(
                args.runtime, executable=args.executable
            )
        elif args.command == "install-code-shim":
            result = install_desktop_code_shim(
                managed_binary=args.managed,
                shim_source=args.source,
                runtime_path=args.runtime,
            )
        elif args.command == "uninstall-code-shim":
            result = uninstall_desktop_code_shim(runtime_path=args.runtime)
        else:
            result = stop_services(args.runtime)
    except RuntimeError as exc:
        print(f"quotapool-desktop: {exc}", file=sys.stderr)
        return 78
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
