#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path


def read_object(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def write_private(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config-dir", required=True)
    parser.add_argument("--state-dir", required=True)
    parser.add_argument("--prefix", required=True)
    parser.add_argument("--claude-binary", required=True)
    parser.add_argument("--token", required=True)
    parser.add_argument("--node-binary", required=True)
    parser.add_argument("--outbound-proxy", default="")
    parser.add_argument("--no-proxy", default="")
    args = parser.parse_args()

    config_dir = Path(args.config_dir).expanduser()
    state_dir = Path(args.state_dir).expanduser()
    team_path = config_dir / "teamclaude.json"
    runtime_path = config_dir / "runtime.json"
    team = read_object(team_path)
    existing_runtime = read_object(runtime_path)
    accounts = team.get("accounts") if isinstance(team.get("accounts"), list) else []
    proxy = team.get("proxy") if isinstance(team.get("proxy"), dict) else {}
    existing_coordinator = (
        team.get("fleetCoordinator")
        if isinstance(team.get("fleetCoordinator"), dict)
        else {}
    )
    coordinator_token = existing_coordinator.get("token") or args.token
    proxy.update({"port": 13456, "host": "127.0.0.1"})
    team.update({
        "proxy": proxy,
        "upstream": "https://api.anthropic.com",
        "switchThreshold": 0.98,
        "holdSeconds": 21600,
        "distributeSessions": False,
        "eventLogging": "hide",
        "autoUpdate": False,
        "quotaProbeSeconds": 300,
        "warmupSeconds": 0,
        "accounts": accounts,
        "sx": {"mode": "off"},
        "stormRamp": {
            "enabled": False,
            "startConc": 1,
            "stepConc": 1,
            "stepMs": 250,
            "windowMs": 30000,
            "maxConc": 0,
            "admissionTimeoutMs": 0,
        },
        "fleetCoordinator": {
            "enabled": True,
            "url": "http://127.0.0.1:17891",
            # Reinstalling binaries must not silently split the running data
            # plane and coordinator onto different tokens. Rotation is a
            # separate, explicit operation with an ordered service restart.
            "token": coordinator_token,
            "timeoutMs": 15000,
        },
    })
    requested_claude = str(Path(args.claude_binary).expanduser())
    fleet_shim = str(Path(args.prefix).expanduser() / "shims" / "claude")
    if Path(requested_claude) == Path(fleet_shim):
        previous_claude = existing_runtime.get("claude_binary")
        if not previous_claude or Path(str(previous_claude)) == Path(fleet_shim):
            raise SystemExit(
                "refusing to store the Claude Fleet shim as claude_binary; "
                "this would create a recursive launcher"
            )
        requested_claude = str(previous_claude)
    runtime = {
        "version": 1,
        "prefix": str(Path(args.prefix).expanduser()),
        "claude_binary": requested_claude,
        "node_binary": args.node_binary,
        "proxy_port": 13456,
        "coordinator_port": 17891,
        "ca_path": str(config_dir / "teamclaude-ca.pem"),
        "hold_seconds": 21600,
        "db_path": str(state_dir / "fleet.sqlite3"),
        "teamclaude_config": str(team_path),
    }
    outbound_proxy = (
        args.outbound_proxy.strip()
        or str(existing_runtime.get("outbound_proxy") or "").strip()
    )
    if outbound_proxy:
        runtime["outbound_proxy"] = outbound_proxy
    no_proxy = (
        args.no_proxy.strip()
        or str(existing_runtime.get("no_proxy") or "").strip()
        or "localhost,127.0.0.1,::1"
    )
    runtime["no_proxy"] = no_proxy
    write_private(team_path, team)
    write_private(runtime_path, runtime)
    print(json.dumps({
        "runtime": str(runtime_path),
        "teamclaude": str(team_path),
        "accounts_preserved": len(accounts),
    }))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
