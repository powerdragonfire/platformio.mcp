"""GDB remote stub for one RISC-V panic block, run by GDB as `target remote | python -m platformio_mcp.panic_stub <file>`.

The stub is esp-idf-panic-decoder's GdbServer with two exits added. Version 1.5 of that server reads stdin
in a loop that never ends on EOF, so a GDB killed on timeout would leave it spinning at full CPU. This stub
exits when stdin closes, and exits after `--deadline` seconds in case the pipe stays open.
"""

from __future__ import annotations

import argparse
import os
import threading

from esp_idf_panic_decoder.gdb_panic_server import GdbServer, parse_idf_riscv_panic_output


class _ExitOnEof:
    def __init__(self, stream):
        self.stream = stream

    def read(self, n: int = -1) -> str:
        data = self.stream.read(n)
        if not data:
            raise SystemExit(0)
        return data


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("panic_file")
    parser.add_argument("--deadline", type=float, default=120, help="seconds before the stub exits on its own; 0 disables")
    args = parser.parse_args(argv)
    if args.deadline > 0:
        timer = threading.Timer(args.deadline, os._exit, args=(2,))
        timer.daemon = True
        timer.start()
    with open(args.panic_file) as f:
        info = parse_idf_riscv_panic_output(f.read())
    server = GdbServer(info)
    server.in_stream = _ExitOnEof(server.in_stream)
    try:
        server.run()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
