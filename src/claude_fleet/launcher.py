from __future__ import annotations

import base64
import json
import os
import socket
import sys
import urllib.error
import time
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Dict, List, MutableMapping, Optional, Sequence


FLEET_TRANSPORT_USER_PREFIX = "fleet-holder-v1."
FLEET_HOLDER_SAFE = frozenset(
    "abcdefghijklmnopqrstuvwxyz"
    "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    "0123456789._:-"
)


def encode_transport_holder(holder: str) -> str:
    """Encode a non-secret launch holder for loopback CONNECT userinfo."""
    if (
        not holder
        or len(holder) > 256
        or any(char not in FLEET_HOLDER_SAFE for char in holder)
    ):
        raise RuntimeError("Fleet launch id is not transport-safe")
    encoded = base64.urlsafe_b64encode(holder.encode("utf-8")).decode("ascii")
    return FLEET_TRANSPORT_USER_PREFIX + encoded.rstrip("=")


def default_runtime_path() -> Path:
    return Path(
        os.environ.get(
            "FLEET_RUNTIME_CONFIG",
            str(Path.home() / ".config" / "claude-fleet" / "runtime.json"),
        )
    ).expanduser()


def load_runtime(path: Optional[str] = None) -> Dict[str, Any]:
    runtime_path = Path(path).expanduser() if path else default_runtime_path()
    try:
        value = json.loads(runtime_path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise RuntimeError(f"Fleet runtime config unavailable: {runtime_path}") from exc
    if not isinstance(value, dict):
        raise RuntimeError(f"Fleet runtime config is not a JSON object: {runtime_path}")
    return value


# Every port literal in this package used to be its own opinion, and they
# disagreed: `proxy_port` fell back to 3456 in three places and 13456 in a
# fourth, while doctor's readiness probe ignored the config altogether and
# hardcoded 13456. That last shape is the dangerous one — bring a second
# instance up on a different port to verify a change, and doctor reports on
# production instead, silently, while you read its output as being about the
# thing you were testing.
#
# 3456 was also the wrong number to fall back to. README: "13456 是刻意选择的
# 隔离端口；本机 3456 已被其他浏览器辅助服务占用." A default that probes a
# stranger's service is worse than no default.
DEFAULT_COORDINATOR_PORT = 17891
DEFAULT_PROXY_PORT = 13456


def coordinator_port(runtime: Dict[str, Any]) -> int:
    return int(runtime.get("coordinator_port") or DEFAULT_COORDINATOR_PORT)


def proxy_port(runtime: Dict[str, Any]) -> int:
    return int(runtime.get("proxy_port") or DEFAULT_PROXY_PORT)


def coordinator_url(runtime: Dict[str, Any], path: str = "") -> str:
    return "http://127.0.0.1:%d%s" % (coordinator_port(runtime), path)


def dataplane_url(runtime: Dict[str, Any], path: str = "") -> str:
    return "http://127.0.0.1:%d%s" % (proxy_port(runtime), path)


def runtime_portability_problems(runtime: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Absolute paths in runtime.json that do not exist on THIS host.

    The config is written by the macOS bootstrap and carries that machine's
    absolute paths. Copied to the Harness Linux server each one fails
    differently, and none of them fails in a way that names itself:

    - `outbound_proxy` is the dangerous one. On the laptop it points at a local
      proxy on 127.0.0.1:1082. On a server nothing listens there, so EVERY
      upstream request fails — and the resulting connection errors look like
      Anthropic being unreachable, not like a config problem.
    - `node_binary` is a Homebrew path; the data plane simply will not start.
    - `claude_binary` pins an exact version directory, so it breaks on the next
      Claude Code upgrade even on the machine that wrote it.

    Reported rather than repaired: guessing a substitute for an egress proxy
    would be worse than saying plainly that it is wrong.
    """
    problems: List[Dict[str, Any]] = []

    node_binary = str(runtime.get("node_binary") or "")
    if node_binary and not os.access(node_binary, os.X_OK):
        problems.append({
            "field": "node_binary",
            "value": node_binary,
            "problem": "not executable on this host",
            "impact": "the data plane cannot start",
        })

    claude_binary = str(runtime.get("claude_binary") or "")
    if claude_binary and not Path(claude_binary).exists():
        problems.append({
            "field": "claude_binary",
            "value": claude_binary,
            "problem": "does not exist on this host",
            "impact": "launches fail; note this path pins an exact version and "
                      "so also breaks on upgrade",
        })

    outbound = str(runtime.get("outbound_proxy") or "")
    if outbound:
        host, port = _proxy_endpoint(outbound)
        if host in {"127.0.0.1", "localhost", "::1"} and port and not _port_open(port):
            problems.append({
                "field": "outbound_proxy",
                "value": outbound,
                "problem": f"nothing is listening on {host}:{port}",
                "impact": "EVERY upstream request fails, and the errors look "
                          "like Anthropic being unreachable rather than a "
                          "misconfiguration",
            })
    return problems


def _proxy_endpoint(url: str) -> tuple:
    from urllib.parse import urlparse

    try:
        parsed = urlparse(url)
        return parsed.hostname or "", parsed.port
    except ValueError:
        return "", None


def _port_open(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.5):
            return True
    except OSError:
        return False


def _replace_header(existing: str, name: str, value: Optional[str]) -> str:
    wanted = name.lower()
    lines = [
        line for line in existing.splitlines()
        if ":" not in line or line.split(":", 1)[0].strip().lower() != wanted
    ]
    if value:
        lines.append(f"{name}: {value}")
    return "\n".join(line for line in lines if line.strip())


def _prepend_fleet_shim(path: str, runtime: Dict[str, Any]) -> str:
    prefix = str(runtime.get("prefix") or "").strip()
    if not prefix:
        return path
    shim = str(Path(prefix).expanduser() / "shims")
    entries = [
        entry for entry in path.split(os.pathsep)
        if entry and entry != shim
    ]
    return os.pathsep.join([shim] + entries)


def build_claude_env(
    base: MutableMapping[str, str],
    runtime: Dict[str, Any],
    *,
    workload_class: Optional[str] = None,
    workload_id: Optional[str] = None,
    execution_id: Optional[str] = None,
    parent_execution_id: Optional[str] = None,
    session_id: Optional[str] = None,
    launch_id: Optional[str] = None,
    new_launch: bool = False,
) -> Dict[str, str]:
    env = dict(base)
    # Bash tools can start another `claude -p`. processWrapper does not wrap
    # arbitrary shell commands, so PATH itself must resolve nested Claude
    # invocations back through Fleet instead of the native Keychain client.
    env["PATH"] = _prepend_fleet_shim(
        env.get("PATH", os.defpath),
        runtime,
    )
    proxy_port_value = proxy_port(runtime)
    env["NO_PROXY"] = env["no_proxy"] = "localhost,127.0.0.1,::1"
    ca_path = runtime.get("ca_path")
    if ca_path:
        env["NODE_EXTRA_CA_CERTS"] = str(ca_path)
    hold_seconds = int(runtime.get("hold_seconds", 21600))
    env["API_TIMEOUT_MS"] = str(max(
        int(env.get("API_TIMEOUT_MS", "0") or 0),
        hold_seconds * 1000 + 60_000,
        600_000,
    ))

    # A base URL or API key can silently bypass subscriptions and enter
    # usage-based billing. Fleet only permits the subscription proxy path.
    for key in (
        "ANTHROPIC_BASE_URL",
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "CLAUDE_CODE_OAUTH_REFRESH_TOKEN",
        "CLAUDE_CODE_OAUTH_SCOPES",
        "TC_ACCT",
    ):
        env.pop(key, None)
    # A headless client with no local login refuses to construct an inference
    # request before it reaches the proxy. Supply a deliberately invalid,
    # non-secret OAuth sentinel so Claude enters the same inference-only mode as
    # a setup-token session. The loopback data plane replaces it with the chosen
    # account credential. If the proxy is unavailable or bypassed, this value can
    # only fail authentication; it cannot authorize usage or billing.
    env["CLAUDE_CODE_OAUTH_TOKEN"] = (
        "sk-ant-oat01-claude-fleet-local-sentinel-not-a-credential"
    )

    resolved_launch_id = launch_id or (None if new_launch else env.get("FLEET_LAUNCH_ID"))
    if not resolved_launch_id:
        resolved_launch_id = f"{socket.gethostname()}:{uuid.uuid4().hex}"
    # Claude Agent View workers can strip every ANTHROPIC_CUSTOM_HEADERS value.
    # CONNECT proxy userinfo survives that boundary. It carries only the
    # non-secret launch holder, never an account pin or credential, and the data
    # plane accepts this reserved form only from a loopback socket.
    transport_user = encode_transport_holder(resolved_launch_id)
    # A fixed non-secret password keeps proxy clients from optimizing away an
    # empty-userinfo Basic header. The data plane authenticates loopback by
    # socket address and reads only the reserved username.
    proxy_url = f"http://{transport_user}:local@127.0.0.1:{proxy_port_value}"
    env["HTTPS_PROXY"] = env["HTTP_PROXY"] = proxy_url
    env["https_proxy"] = env["http_proxy"] = proxy_url
    parent_process_id = None if new_launch else env.get("FLEET_PROCESS_ID")
    process_id = f"{socket.gethostname()}:{uuid.uuid4().hex}"
    workload = workload_class or env.get("FLEET_WORKLOAD_CLASS") or "interactive"
    workload_identity = workload_id or env.get("FLEET_WORKLOAD_ID")
    execution = (
        execution_id
        or (None if new_launch else env.get("FLEET_EXECUTION_ID"))
        or f"exec-{uuid.uuid4().hex[:16]}"
    )
    parent = (
        parent_execution_id
        or (None if new_launch else env.get("FLEET_PARENT_EXECUTION_ID"))
    )
    resolved_session_id = (
        session_id
        or (None if new_launch else env.get("FLEET_SESSION_ID"))
    )
    env["FLEET_LAUNCH_ID"] = resolved_launch_id
    env["FLEET_PROCESS_ID"] = process_id
    if parent_process_id:
        env["FLEET_PARENT_PROCESS_ID"] = parent_process_id
    else:
        env.pop("FLEET_PARENT_PROCESS_ID", None)
    env["FLEET_WORKLOAD_CLASS"] = workload
    if workload_identity:
        env["FLEET_WORKLOAD_ID"] = workload_identity
    else:
        env.pop("FLEET_WORKLOAD_ID", None)
    env["FLEET_EXECUTION_ID"] = execution
    if parent:
        env["FLEET_PARENT_EXECUTION_ID"] = parent
    else:
        env.pop("FLEET_PARENT_EXECUTION_ID", None)
    if resolved_session_id:
        env["FLEET_SESSION_ID"] = resolved_session_id
    else:
        env.pop("FLEET_SESSION_ID", None)

    headers = env.get("ANTHROPIC_CUSTOM_HEADERS", "")
    for name, value in (
        ("X-Fleet-Process-Id", process_id),
        ("X-Fleet-Launch-Id", resolved_launch_id),
        ("X-Fleet-Parent-Process-Id", parent_process_id),
        ("X-Fleet-Execution-Id", execution),
        ("X-Fleet-Parent-Execution-Id", parent),
        ("X-Fleet-Session-Id", resolved_session_id),
        ("X-Fleet-Workload-Class", workload),
        ("X-Fleet-Workload-Id", workload_identity),
    ):
        headers = _replace_header(headers, name, value)
    env["ANTHROPIC_CUSTOM_HEADERS"] = headers
    return env


def coordinator_endpoint(runtime: Dict[str, Any]) -> str:
    """The one place that decides which coordinator this host talks to.

    Resolution order, most specific first:
      1. FLEET_COORDINATOR_URL   — explicit override, used by canaries
      2. runtime.json coordinator_url
      3. http://<coordinator_host or 127.0.0.1>:<coordinator_port>

    This exists because the address used to be hardcoded to 127.0.0.1 here while
    the data plane read `fleetCoordinator.url` from teamclaude.json. Setting the
    coordinator to bind a non-loopback address therefore satisfied the data
    plane and broke `fleet run` — every session enters through it, so the whole
    host stopped working with:

        coordinator is unavailable on 127.0.0.1:17891; refusing direct Claude access

    Two components must never be able to resolve different coordinators; that is
    a split control plane, and it is silent until something fails.
    """
    override = os.environ.get("FLEET_COORDINATOR_URL")
    if override:
        return override.rstrip("/")
    configured = str(runtime.get("coordinator_url") or "").strip()
    if configured:
        return configured.rstrip("/")
    host = str(runtime.get("coordinator_host") or "127.0.0.1").strip()
    port = coordinator_port(runtime)
    return f"http://{host}:{port}"


def dataplane_endpoint(runtime: Dict[str, Any]) -> str:
    """The data plane is always local: it holds this host's credentials and
    talks to Anthropic directly. Nothing should ever point it elsewhere."""
    return dataplane_url(runtime)


# Enough to cover a service restart (~4s), short enough that a genuinely dead
# coordinator is reported promptly. At launch there is no session to protect
# yet, so holding longer buys nothing.
SERVICE_CHECK_ATTEMPTS = 4
SERVICE_CHECK_BACKOFF_SECONDS = 2.0


def check_services(runtime: Dict[str, Any]) -> None:
    # Both probes ask one question: is the service answering? Neither asks it
    # to produce a report.
    #
    # This used to probe `/teamclaude/status`, which serialises every account,
    # route and waiting session. Measured on a data plane that had been up five
    # hours: 11.3s, 12.5s, 20.1s, 2.1s — while the fleet itself was fine, the
    # coordinator answered in 1ms, and no request was waiting. `fleet run`
    # refused to start Claude, and the auth probe reported the credential chain
    # broken, both because a display endpoint could not be rendered inside two
    # seconds. The gate was measuring the cost of the report.
    #
    # `/teamclaude/ready` answers from constants. Its latency measures the one
    # thing this gate cares about: whether the event loop is turning.
    checks = [
        ("coordinator", f"{coordinator_endpoint(runtime)}/health"),
        ("TeamClaude", f"{dataplane_endpoint(runtime)}/teamclaude/ready"),
    ]
    # Retry briefly before refusing. Failing closed is right — starting Claude
    # unfenced is worse than not starting it — but a service restart takes about
    # four seconds, and refusing on the FIRST failed probe turns a routine
    # restart into "the user re-runs the command".
    #
    # Found by accident: the live outage test, meant to confirm that in-flight
    # requests WAIT when the coordinator goes away, never reached the data plane
    # because `fleet run` refused first. Two paths share one symptom and only
    # the in-session one had been fixed.
    #
    # Bounded on purpose. At launch there is no session to preserve yet, so
    # holding longer than a restart window buys nothing and hides a real outage.
    for name, url in checks:
        last = None
        for attempt in range(SERVICE_CHECK_ATTEMPTS):
            try:
                with urllib.request.urlopen(url, timeout=2) as response:
                    if response.status != 200:
                        raise RuntimeError(
                            f"{name} at {url} returned HTTP {response.status}"
                        )
                last = None
                break
            except urllib.error.HTTPError:
                # ANY status line is an answer, and answering is the whole
                # question. This gate asks whether the process is reachable and
                # turning, not whether it implements a particular route.
                #
                # Learned the hard way, twice in one change. I first allowed
                # only 404, reasoning that an older data plane would not know
                # `/ready`. It returns 400 for an unknown path, so the gate
                # refused every launch the moment the control plane deployed —
                # the exact failure the compatibility branch existed to prevent,
                # introduced by writing the branch too narrowly.
                #
                # A data plane that is up but broken is not this gate's job. It
                # was never caught by the old status probe either, and refusing
                # to launch over it is worse than launching.
                last = None
                break
            except (OSError, urllib.error.URLError) as exc:
                last = exc
                if attempt + 1 < SERVICE_CHECK_ATTEMPTS:
                    time.sleep(SERVICE_CHECK_BACKOFF_SECONDS)
        if last is not None:
            # Fail closed, and name the actual target. There is deliberately no
            # fallback to a local coordinator: silently serving from a second
            # control plane is how a split brain starts, and it would be
            # invisible until two hosts disagreed about an account.
            raise RuntimeError(
                f"{name} is unavailable at {url} after "
                f"{SERVICE_CHECK_ATTEMPTS} attempts; refusing direct Claude access"
            ) from last


def exec_wrapped(
    command: Sequence[str],
    *,
    runtime_path: Optional[str] = None,
    workload_class: Optional[str] = None,
    workload_id: Optional[str] = None,
    execution_id: Optional[str] = None,
    parent_execution_id: Optional[str] = None,
    session_id: Optional[str] = None,
    launch_id: Optional[str] = None,
    new_launch: bool = False,
) -> None:
    if not command:
        raise RuntimeError("processWrapper received no target command")
    runtime = load_runtime(runtime_path)
    check_services(runtime)
    command = list(command)
    if command[0] == "claude" and runtime.get("claude_binary"):
        command[0] = str(runtime["claude_binary"])
    env = build_claude_env(
        os.environ,
        runtime,
        workload_class=workload_class,
        workload_id=workload_id,
        execution_id=execution_id,
        parent_execution_id=parent_execution_id,
        session_id=session_id,
        launch_id=launch_id,
        new_launch=new_launch,
    )
    # Claude requires the wrapper to replace itself, not leave an intermediate
    # supervisor process.
    os.execvpe(command[0], command, env)


def wrapper_main(argv: Optional[Sequence[str]] = None) -> int:
    args = list(argv if argv is not None else sys.argv[1:])
    try:
        exec_wrapped(args)
    except RuntimeError as exc:
        print(f"claude-fleet: {exc}", file=sys.stderr)
        return 78
    return 0
