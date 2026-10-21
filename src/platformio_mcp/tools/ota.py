"""Over-the-air firmware upload (ArduinoOTA / espota) for ESP32 and ESP8266."""

from __future__ import annotations

import glob
import os
import platform as host_platform
import re
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from ..core import check_policy, default_envs, env_setting, guard, project_config
from ..parsers import parse_build_result, parse_diagnostics, parse_memory
from ..pio import DEFAULT_TIMEOUTS, PioResult, clean_output, resolve_project_dir, run_pio, tail, write_log

DEFAULT_PORTS = {"espressif32": 3232, "espressif8266": 8266}
IP_OR_MDNS_RE = re.compile(r"^(?:(?:\d{1,3}\.){3}\d{1,3}|[^\\/\s]+\.local)$")
PROGRESS_RE = re.compile(r"Uploading: \[[= ]*\] (?P<pct>\d+)%")
PORT_FLAG_RE = re.compile(r"(?:^|\s)(?:-p|--port)[= ](?P<port>\d+)")

# (regex, error code, what to do about it), first match wins.
FAILURES: tuple[tuple[str, str, str], ...] = (
    (r"Host \S+ Not Found", "host_not_found", "the host name did not resolve; use the board's IP address (it prints it after WiFi.begin) or check mDNS."),
    (r"No response from the ESP", "no_response", "the device never answered the OTA invitation on the UDP port: confirm the firmware calls ArduinoOTA.begin() in setup() and ArduinoOTA.handle() in loop(), that the board and this machine share a network, and that `port` matches ArduinoOTA.setPort (3232 on ESP32, 8266 on ESP8266)."),
    (r"Authentication Failed|No Answer to our Authentication", "auth_failed", "the OTA password was rejected; pass `auth` matching ArduinoOTA.setPassword() (or drop it when the sketch sets none)."),
    (r"Bad Answer:", "bad_answer", "the device replied with something other than OK/AUTH; a different service may be listening on that port."),
    (r"No response from device", "no_callback", "the device accepted the invitation but never connected back to this machine's TCP port: a firewall or VPN is blocking inbound connections, or the wrong host interface was picked (espota --host_ip)."),
    (r"Error Uploading", "transfer_failed", "the transfer broke mid-way; the device reset or Wi-Fi dropped. Retry, and keep the board close to the access point."),
    (r"Error response from device|No Result!", "device_rejected", "the device received the image but refused to apply it: usually the partition table has no OTA slot (app0/app1) or the image is larger than the slot. Check board_build.partitions in platformio.ini and the firmware size from pio_size_report."),
    (r"Please specify IP address or host name", "no_upload_port", "PlatformIO did not receive an upload port; pass `host`."),
)


def parse_espota(text: str) -> dict[str, Any]:
    """Pure parser for espota.py output as captured through `pio run -t upload`."""
    success = bool(re.search(r"\[INFO\]: Success|Result: OK", text))
    pct = None
    for m in PROGRESS_RE.finditer(text):
        pct = int(m["pct"])
    error = hint = None
    if not success:
        for pattern, code, fix in FAILURES:
            if re.search(pattern, text):
                error, hint = code, fix
                break
    auto_switched = "`upload_protocol` is switched to `espota`" in text
    return {"success": success, "progress_percent": pct, "error": error, "hint": hint, "auto_switched": auto_switched}


def _platform_family(cfg: dict, env: str) -> str | None:
    raw = str(env_setting(cfg, env, "platform") or "")
    for family in DEFAULT_PORTS:
        if family in raw:
            return family
    return None


def _flags_to_list(raw: Any) -> list[str]:
    if not raw:
        return []
    if isinstance(raw, str):
        return [f for f in re.split(r"[\s,]+", raw) if f]
    return [str(f) for f in raw]


def resolve_host(host: str) -> str:
    """Return the IP for `host`, raising a clear error when it does not resolve."""
    try:
        return socket.getaddrinfo(host, None, socket.AF_INET)[0][4][0]
    except (socket.gaierror, IndexError, OSError) as exc:
        raise RuntimeError(f"host '{host}' does not resolve ({exc}); the board is probably offline or on another network. Use the IP it prints after WiFi.begin(), or check that mDNS (.local) works from this machine.") from exc


def ping(ip: str, timeout_s: float = 1.5) -> bool | None:
    """Best-effort ICMP reachability; None when no ping binary is available."""
    exe = shutil.which("ping")
    if not exe:
        return None
    if host_platform.system() == "Windows":
        cmd = [exe, "-n", "1", "-w", str(int(timeout_s * 1000)), ip]
    else:
        cmd = [exe, "-c", "1", "-W", str(max(int(timeout_s), 1)), ip]
    try:
        return subprocess.run(cmd, capture_output=True, timeout=timeout_s + 3).returncode == 0
    except (subprocess.TimeoutExpired, OSError):
        return False


def _find_espota(family: str) -> str | None:
    base = os.environ.get("PLATFORMIO_PACKAGES_DIR") or str(Path(os.environ.get("PLATFORMIO_CORE_DIR", Path.home() / ".platformio")) / "packages")
    hits = sorted(glob.glob(str(Path(base) / f"framework-arduino{family}*" / "tools" / "espota.py")))
    return hits[0] if hits else None


def _run_espota_direct(espota: str, host: str, port: int, auth: str | None, image: Path, filesystem: bool, timeout: float) -> PioResult:
    cmd = [sys.executable, espota, "--debug", "--progress", "-i", host, "-p", str(port), "-f", str(image)]
    if auth:
        cmd += ["-a", auth]
    if filesystem:
        cmd.append("--spiffs")
    start = time.monotonic()
    timed_out = False
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, errors="replace", timeout=timeout)
        raw, rc = proc.stdout + ("\n" + proc.stderr if proc.stderr.strip() else ""), proc.returncode
    except subprocess.TimeoutExpired as exc:
        timed_out, rc = True, -1
        raw = (exc.stdout or "") + f"\n[platformio-mcp] timed out after {timeout:.0f}s"
    output = clean_output(raw)
    logged_cmd = ["***" if cmd[i - 1] == "-a" else c for i, c in enumerate(cmd)]
    log_path = write_log("upload-ota", f"$ {' '.join(logged_cmd)}\n\n{output}\n")
    return PioResult(args=cmd[1:], returncode=rc, output=output, duration_s=round(time.monotonic() - start, 2), log_path=log_path, timed_out=timed_out, raw_output=raw)


@guard
def pio_upload_ota(
    host: str,
    project_dir: str | None = None,
    env: str | None = None,
    port: int | None = None,
    auth: str | None = None,
    filesystem: bool = False,
    build: bool = True,
    timeout_s: float = 180,
    verify_reachable: bool = True,
) -> dict[str, Any]:
    check_policy("flash")
    if not host or not host.strip():
        raise ValueError("pass `host`: the board's IP address or mDNS name (e.g. 192.168.1.42 or esp32.local).")
    host = host.strip()
    path = resolve_project_dir(project_dir)
    cfg = project_config(str(path))
    env = env or (default_envs(cfg) or [None])[0]
    if not env:
        raise ValueError("no environment found in platformio.ini; pass env=.")
    family = _platform_family(cfg, env)
    declared_protocol = str(env_setting(cfg, env, "upload_protocol") or "").strip()
    existing_flags = _flags_to_list(env_setting(cfg, env, "upload_flags"))
    if port is None:
        m = PORT_FLAG_RE.search(" ".join(existing_flags))
        port = int(m["port"]) if m else DEFAULT_PORTS.get(family or "", 3232)

    target_host = host
    reachable: bool | None = None
    if verify_reachable:
        ip = resolve_host(host)
        reachable = ping(ip)
        if reachable is False:
            return {
                "ok": False,
                "error": "host_unreachable",
                "summary": f"{host} ({ip}) did not answer a ping, so the OTA invitation would time out too. Is the board powered and on the same network? Does the sketch call ArduinoOTA.begin() after WiFi connects, and did it print this IP? Pass verify_reachable=false to skip this check if ICMP is blocked.",
                "host": host,
                "ip": ip,
                "port": port,
                "env": env,
            }
        # The espressif builders only auto-switch to espota for an IP or *.local name, so hand them the IP for plain hostnames.
        if declared_protocol != "espota" and not IP_OR_MDNS_RE.match(host):
            target_host = ip

    t0 = time.monotonic()
    if build:
        flags = list(existing_flags)
        if auth and not any(f.startswith("--auth") or f == "-a" for f in flags):
            flags.append(f"--auth={auth}")
        if not PORT_FLAG_RE.search(" ".join(flags)):
            flags.append(f"--port={port}")
        target = "uploadfs" if filesystem else "upload"
        args = ["run", "-d", str(path), "-e", env, "-t", target, "--upload-port", target_host]
        res = run_pio(args, cwd=str(path), timeout=max(timeout_s, DEFAULT_TIMEOUTS["upload"]), tool="upload-ota", env_extra={"PLATFORMIO_UPLOAD_FLAGS": " ".join(flags)})
        build_result = parse_build_result(res.output)
        errors = [d.to_dict() for d in parse_diagnostics(res.output) if d.kind == "error"]
        memory = parse_memory(res.output)
        image = path / ".pio" / "build" / env / ("spiffs.bin" if filesystem else "firmware.bin")
        if filesystem and not image.exists():
            candidates = sorted((path / ".pio" / "build" / env).glob("*fs.bin")) + sorted((path / ".pio" / "build" / env).glob("littlefs.bin"))
            image = candidates[0] if candidates else image
    else:
        if not family:
            raise ValueError(f"env {env} is not an espressif32/espressif8266 environment; build=false needs the framework's espota.py.")
        espota = _find_espota(family)
        if not espota:
            raise FileNotFoundError(f"espota.py not found under the framework-arduino{family} package; run pio_build once so PlatformIO installs it, or use build=true.")
        build_dir = path / ".pio" / "build" / env
        if filesystem:
            found = sorted(build_dir.glob("spiffs.bin")) + sorted(build_dir.glob("littlefs.bin")) + sorted(build_dir.glob("*fs.bin"))
            if not found:
                raise FileNotFoundError(f"no filesystem image in {build_dir}; run pio_run_target('buildfs') first or use build=true.")
            image = found[0]
        else:
            image = build_dir / "firmware.bin"
            if not image.exists():
                raise FileNotFoundError(f"{image} does not exist; run pio_build first or use build=true.")
        res = _run_espota_direct(espota, target_host, port, auth, image, filesystem, timeout_s)
        build_result, errors, memory = {"status": None, "environments": [env]}, [], {}

    parsed = parse_espota(res.output)
    compile_errors = [e for e in errors if e["file"] not in ("upload", "uploadfs")]
    ok = res.ok and parsed["success"] and build_result.get("status") != "failed"
    duration = round(time.monotonic() - t0, 2)
    firmware_bytes = image.stat().st_size if image.exists() else None
    what = "filesystem image" if filesystem else "firmware"
    if ok:
        summary = f"OTA {what} upload to {host}:{port} succeeded in {duration}s" + (f" ({firmware_bytes:,} bytes)" if firmware_bytes else "") + ". The board reboots into the new image now; use pio_monitor_capture on its serial port, or wait a few seconds and ping it, to confirm it came back."
        error = None
    elif res.timed_out:
        error, summary = "timeout", f"OTA upload to {host}:{port} timed out after {timeout_s:.0f}s" + (f" at {parsed['progress_percent']}%" if parsed["progress_percent"] is not None else "") + ". The device stopped answering mid-transfer; check Wi-Fi signal and that nothing else blocks loop() while ArduinoOTA.handle() runs."
    elif parsed["error"]:
        error, summary = parsed["error"], f"OTA upload to {host}:{port} failed ({parsed['error']}): {parsed['hint']}"
    elif compile_errors:
        first = compile_errors[0]
        error, summary = "build_failed", f"Build failed before the OTA upload: {first['file']}:{first['line']}: {first['message']}. Fix it and retry."
    else:
        error, summary = "upload_failed", f"OTA upload to {host}:{port} failed (exit {res.returncode}) without a recognised espota message; see output_tail and the log."
    if parsed["auto_switched"] and not ok:
        summary += " Note: platformio.ini does not declare upload_protocol = espota, PlatformIO switched automatically because the port looks like an IP."
    result: dict[str, Any] = {
        "ok": ok,
        "summary": summary,
        "host": host,
        "target_host": target_host,
        "port": port,
        "env": env,
        "platform_family": family,
        "upload_path": "pio_run" if build else "espota_direct",
        "filesystem": filesystem,
        "firmware_path": str(image),
        "firmware_bytes": firmware_bytes,
        "progress_percent": parsed["progress_percent"],
        "reachable": reachable,
        "duration_s": duration,
        "memory": memory,
        "errors": errors[:20],
        "exit_code": res.returncode,
        "output_tail": tail(res.output, 40),
        "log_path": res.log_path,
    }
    if error:
        result["error"] = error
    return result


def register(mcp) -> None:
    mcp.tool(name="pio_upload_ota", description=(
        "Flash firmware over Wi-Fi to an ESP32/ESP8266 running ArduinoOTA (espota). Give `host` (IP or name.local); "
        "port defaults to 3232 (ESP32) / 8266 (ESP8266) and `auth` is the ArduinoOTA password. By default it resolves and pings "
        "the host first, then runs `pio run -t upload --upload-port <host>` so the image matches the current build; build=false "
        "sends the existing .pio/build/<env>/firmware.bin with espota.py directly. filesystem=true sends the SPIFFS/LittleFS image "
        "instead. Failures are mapped to what to fix: no_response (ArduinoOTA.handle not running), auth_failed, no_callback (firewall), "
        "device_rejected (partition table has no OTA slot). Blocked under build_only/read_only policy."
    ))(pio_upload_ota)
