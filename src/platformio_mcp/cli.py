"""`platformio.mcp` command line: serve (default), install, doctor."""

from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path

from . import __version__

CLIENTS = ("claude-code", "claude-desktop", "cursor", "codex", "windsurf", "print")


def server_command(with_platformio: bool, policy: str | None) -> dict:
    pkg = 'platformio.mcp[platformio]' if with_platformio else "platformio.mcp"
    entry = {"command": "uvx", "args": [pkg]}
    if policy:
        entry["env"] = {"PLATFORMIO_MCP_POLICY": policy}
    return entry


def _merge_json(path: Path, key_path: list[str], entry: dict) -> None:
    data = {}
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8") or "{}")
        except json.JSONDecodeError as exc:
            sys.exit(f"{path} is not valid JSON ({exc}); fix it or add the entry by hand:\n{json.dumps(entry, indent=2)}")
    node = data
    for key in key_path[:-1]:
        node = node.setdefault(key, {})
    node[key_path[-1]] = entry
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def claude_desktop_config() -> Path:
    system = platform.system()
    if system == "Darwin":
        return Path.home() / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json"
    if system == "Windows":
        return Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming")) / "Claude" / "claude_desktop_config.json"
    return Path.home() / ".config" / "Claude" / "claude_desktop_config.json"


def install(client: str, with_platformio: bool, policy: str | None, scope: str) -> None:
    entry = server_command(with_platformio, policy)
    if client == "print":
        print(json.dumps({"mcpServers": {"platformio": entry}}, indent=2))
        return
    if client == "claude-code":
        claude = shutil.which("claude")
        if not claude:
            sys.exit("`claude` CLI not found on PATH. Install Claude Code, or run: uvx platformio.mcp install --print")
        cmd = [claude, "mcp", "add", "--scope", scope, "--transport", "stdio"]
        for k, v in entry.get("env", {}).items():
            cmd += ["--env", f"{k}={v}"]
        cmd += ["platformio", "--", entry["command"], *entry["args"]]
        print("$ " + " ".join(cmd))
        subprocess.run(cmd, check=False)
        return
    if client == "claude-desktop":
        path = claude_desktop_config()
        _merge_json(path, ["mcpServers", "platformio"], entry)
        print(f"Added 'platformio' to {path}. Restart Claude Desktop.")
        return
    if client == "cursor":
        path = Path.home() / ".cursor" / "mcp.json" if scope == "user" else Path.cwd() / ".cursor" / "mcp.json"
        _merge_json(path, ["mcpServers", "platformio"], entry)
        print(f"Added 'platformio' to {path}. Reload Cursor's MCP settings.")
        return
    if client == "windsurf":
        path = Path.home() / ".codeium" / "windsurf" / "mcp_config.json"
        _merge_json(path, ["mcpServers", "platformio"], entry)
        print(f"Added 'platformio' to {path}. Restart Windsurf.")
        return
    if client == "codex":
        path = Path.home() / ".codex" / "config.toml"
        block = "\n[mcp_servers.platformio]\ncommand = \"uvx\"\nargs = [" + ", ".join(json.dumps(a) for a in entry["args"]) + "]\n"
        if entry.get("env"):
            block += "[mcp_servers.platformio.env]\n" + "".join(f'{k} = "{v}"\n' for k, v in entry["env"].items())
        existing = path.read_text(encoding="utf-8") if path.exists() else ""
        if "[mcp_servers.platformio]" in existing:
            print(f"{path} already has [mcp_servers.platformio]; edit it by hand if needed:\n{block}")
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(existing.rstrip("\n") + "\n" + block, encoding="utf-8")
        print(f"Appended [mcp_servers.platformio] to {path}. Restart Codex.")
        return
    sys.exit(f"unknown client {client}; choose from {', '.join(CLIENTS)}")


def doctor() -> None:
    from .tools.system import pio_system_info

    print(json.dumps(pio_system_info(), indent=2))


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="platformio.mcp", description="MCP server for PlatformIO. With no subcommand it serves over stdio.")
    parser.add_argument("--version", action="version", version=f"platformio.mcp {__version__}")
    sub = parser.add_subparsers(dest="cmd")
    sub.add_parser("serve", help="run the MCP server on stdio (default)")
    p_install = sub.add_parser("install", help="register this server with an MCP client")
    p_install.add_argument("--client", choices=CLIENTS, help="which client to configure")
    for c in CLIENTS:
        p_install.add_argument(f"--{c}", dest="client", action="store_const", const=c, help=f"shortcut for --client {c}")
    p_install.add_argument("--with-platformio", action="store_true", help="bundle PlatformIO Core via the [platformio] extra so nothing else needs installing")
    p_install.add_argument("--policy", choices=("full", "build_only", "read_only"), help="set PLATFORMIO_MCP_POLICY for the server")
    p_install.add_argument("--scope", choices=("user", "project"), default="user", help="where the client stores the config (claude-code, cursor)")
    sub.add_parser("doctor", help="print pio_system_info as JSON")

    args = parser.parse_args(argv)
    if args.cmd == "install":
        if not args.client:
            p_install.error("choose a client, e.g. --claude-code, --cursor, --claude-desktop, --codex, --windsurf, or --print")
        install(args.client, args.with_platformio, args.policy, args.scope)
    elif args.cmd == "doctor":
        doctor()
    else:
        from .server import serve

        serve()


if __name__ == "__main__":
    main()
