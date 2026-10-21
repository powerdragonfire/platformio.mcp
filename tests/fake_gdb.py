"""A stand-in for `pio debug --interface=gdb -- --interpreter=mi2 -x .pioinit`: scripted GDB/MI on stdout.

Modes (argv[1]): ok (default), probe_missing, never_prompt, init_error, no_stop.
"""

import sys
import threading
import time

MODE = sys.argv[1] if len(sys.argv) > 1 else "ok"
PROJECT = sys.argv[2] if len(sys.argv) > 2 else None
out = sys.stdout


def emit(*lines: str) -> None:
    for line in lines:
        out.write(line + "\n")
    out.flush()


def prompt() -> None:
    out.write("(gdb) \n")
    out.flush()


STOP_MAIN = '*stopped,reason="breakpoint-hit",disp="del",bkptno="1",frame={addr="0x400d1234",func="main",args=[],file="src/main.cpp",fullname="/p/src/main.cpp",line="12"},thread-id="1",stopped-threads="all"'
STOP_LOOP = '*stopped,reason="breakpoint-hit",disp="keep",bkptno="2",frame={addr="0x400d1300",func="loop",args=[],file="src/main.cpp",fullname="/p/src/main.cpp",line="42"},thread-id="1",stopped-threads="all"'
STOP_SIGINT = '*stopped,reason="signal-received",signal-name="SIGINT",signal-meaning="Interrupt",frame={addr="0x400d2000",func="delay",args=[{name="ms",value="1000"}],file="cores/esp32/esp32-hal-misc.c",fullname="/fw/cores/esp32/esp32-hal-misc.c",line="185"},thread-id="1",stopped-threads="all"'


if PROJECT:
    import os

    d = os.path.join(PROJECT, ".pio", ".piodebug-fake")
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, ".pioinit"), "w", encoding="utf-8") as fp:
        fp.write("target extended-remote :3333\nmonitor init\nload\ntbreak main\n")

if MODE == "probe_missing":
    emit('@"Open On-Chip Debugger v0.11.0-esp32\\n"', "Error: unable to open ftdi device with vid 0403, pid 6010, description '*', serial '*' at bus location '*'", "Error: no device found")
    sys.exit(1)
if MODE == "init_error":
    emit('=thread-group-added,id="i1"', '~"PlatformIO: debug_tool = esp-prog\\n"', '&"Error in sourced command file:\\n"', '&"Remote communication error.  Target disconnected.: Connection reset by peer.\\n"')
    sys.exit(1)
if MODE == "never_prompt":
    emit('~"PlatformIO: Initializing remote target...\\n"')
    time.sleep(30)
    sys.exit(0)

emit(
    '=thread-group-added,id="i1"',
    '~"GNU gdb (crosstool-NG esp-2021r2-patch5) 9.2.90.20200913-git\\n"',
    '~"PlatformIO Unified Debugger -> https://bit.ly/pio-debug\\n"',
    '~"PlatformIO: debug_tool = esp-prog\\n"',
    '~"PlatformIO: Initializing remote target...\\n"',
    '@"Info : accepting \'gdb\' connection on tcp/3333\\n"',
    '~"Loading section .iram0.vectors, size 0x400 lma 0x40080000\\n"',
    '~"PlatformIO: Initialization completed\\n"',
)
prompt()
if MODE != "no_stop":
    # PlatformIO auto-resumes to the init break after the script finishes.
    emit('~"PlatformIO: Resume the execution to `debug_init_break = tbreak main`\\n"', "0^running")
    prompt()
    time.sleep(0.05)
    emit(STOP_MAIN)
    prompt()

hang_lock = threading.Lock()
hanging = {"on": False}

for raw in sys.stdin:
    cmd = raw.strip()
    token = ""
    while cmd and cmd[0].isdigit():
        token, cmd = token + cmd[0], cmd[1:]
    if not cmd:
        continue
    if cmd in ("-gdb-exit", "q", "quit"):
        emit(token + "^exit")
        break
    if cmd == "bt":
        emit('&"bt\\n"', '~"#0  main () at src/main.cpp:12\\n"', '~"#1  0x400d5f00 in app_main () at /fw/main.c:33\\n"', token + "^done")
        prompt()
    elif cmd == "p 1+1":
        emit('&"p 1+1\\n"', '~"$1 = 2\\n"', token + "^done")
        prompt()
    elif cmd == "echo unicode":
        emit('~"h\\303\\251\\n"', token + "^done")
        prompt()
    elif cmd == "p nosuch":
        emit('&"p nosuch\\n"', '&"No symbol \\"nosuch\\" in current context.\\n"', token + '^error,msg="No symbol \\"nosuch\\" in current context."')
        prompt()
    elif cmd == "-stack-list-frames":
        emit(token + '^done,stack=[frame={level="0",addr="0x400d1234",func="main",file="src/main.cpp",fullname="/p/src/main.cpp",line="12"},frame={level="1",addr="0x400d5f00",func="app_main",file="main.c",line="33"}]')
        prompt()
    elif cmd.startswith("break "):
        emit(token + '^done,bkpt={number="2",type="breakpoint",disp="keep",enabled="y",addr="0x400d1300",func="loop",file="src/main.cpp",fullname="/p/src/main.cpp",line="42",times="0"}')
        prompt()
    elif cmd in ("continue", "-exec-continue", "c"):
        emit(token + "^running", '*running,thread-id="all"')
        prompt()
        time.sleep(0.15)
        emit(STOP_LOOP)
        prompt()
    elif cmd == "next":
        emit(token + "^running", '*running,thread-id="all"')
        prompt()
        time.sleep(0.05)
        emit('*stopped,reason="end-stepping-range",frame={addr="0x400d1240",func="main",args=[],file="src/main.cpp",fullname="/p/src/main.cpp",line="13"},thread-id="1",stopped-threads="all"')
        prompt()
    elif cmd == "hang":
        emit(token + "^running", '*running,thread-id="all"')
        prompt()
        hanging["on"] = True
    elif cmd == "-exec-interrupt":
        if hanging["on"]:
            hanging["on"] = False
            emit(token + "^done")
            prompt()
            time.sleep(0.05)
            emit(STOP_SIGINT)
            prompt()
        else:
            emit(token + '^error,msg="The program is not being run."')
            prompt()
    elif cmd == "crash-now":
        sys.exit(3)
    else:
        emit(f'&"{cmd}\\n"', token + f'^error,msg="Undefined command: \\"{cmd.split()[0]}\\".  Try \\"help\\"."')
        prompt()
