# Contributing to platformio.mcp

Thanks for helping make AI agents better at embedded work. This guide covers the whole path from clone to merged PR.

## Ways to help

- **Report a bug** with the [bug report form](https://github.com/powerdragonfire/platformio.mcp/issues/new?template=bug_report.yml). Boards, toolchains, and serial drivers vary a lot, so real-world reports are the most valuable thing you can send.
- **Request a tool or feature** with the [feature request form](https://github.com/powerdragonfire/platformio.mcp/issues/new?template=feature_request.yml).
- **Add a crash decoder** for a platform we do not parse yet (see `src/platformio_mcp/parsers.py`).
- **Improve the skill** in `skills/platformio/SKILL.md` so agents run the build-flash-verify loop better.
- **Ask questions** in [Discussions](https://github.com/powerdragonfire/platformio.mcp/discussions).

Issues labelled [`good first issue`](https://github.com/powerdragonfire/platformio.mcp/labels/good%20first%20issue) are scoped for newcomers. Comment on one to claim it.

## Development setup

You need Python 3.12+ and [uv](https://docs.astral.sh/uv/). PlatformIO is only needed for integration tests.

```bash
git clone https://github.com/powerdragonfire/platformio.mcp && cd platformio.mcp
uv sync --group dev
uv run pytest                        # unit tests: no hardware, no network, about 5 seconds
uv run pytest -m integration         # builds the native fixture with your PlatformIO
uv run platformio-mcp doctor         # what pio_system_info reports on your machine
npx @modelcontextprotocol/inspector uv run platformio-mcp   # call tools interactively
```

To point Claude Code at your checkout instead of the PyPI release:

```bash
claude mcp add platformio -- uv run --directory /path/to/platformio.mcp platformio-mcp
```

## Project layout

| Path | What lives there |
| --- | --- |
| `src/platformio_mcp/server.py` | Creates the MCP server and the instructions the agent reads |
| `src/platformio_mcp/tools/` | One module per tool family: system, project, build, devices, packages, quality, analysis. Each has a `register(mcp)` |
| `src/platformio_mcp/pio.py` | Runs the `pio` CLI, captures logs, applies the safety policy |
| `src/platformio_mcp/parsers.py` | Turns build output and crash dumps into structured data |
| `src/platformio_mcp/toolchain.py` | Finds `addr2line`, `size`, `nm` for the active toolchain |
| `src/platformio_mcp/monitor.py` | Background serial monitor sessions |
| `src/platformio_mcp/cli.py` | `install`, `doctor`, and other subcommands |
| `tests/` | Unit tests plus `tests/projects/` fixtures for integration |
| `skills/platformio/SKILL.md` | The agent-facing skill shipped with the plugin |

## Making a change

`main` is protected. Every change lands through a pull request that passes CI on Linux, macOS, and Windows.

1. Create a branch: `git checkout -b my-change`
2. Make the change and add or update a test in `tests/`
3. Run `uv run pytest` until green
4. Add a line under `## [Unreleased]` in `CHANGELOG.md`
5. Open a PR: `gh pr create --fill` or use the GitHub UI

The PR template asks how you tested. If hardware was involved, say which board.

### Adding a new tool

1. Write a plain function in the right module under `src/platformio_mcp/tools/`, decorated with `@guard` so regex and unexpected errors become structured `ok: false` results
2. Call `check_policy("build")` or `check_policy("flash")` before anything that compiles or writes to a device
3. Return `ok`, `summary`, structured fields, and `log_path` like the other tools
4. Add it to that module's `register(mcp)` with a description the agent can act on; that text is the agent's only manual
5. Add a unit test that mocks the `pio` call, and update the tool count in `README.md` and `INSTRUCTIONS` in `server.py` if the loop changes

### Style

- Type hints on public functions.
- Return structured errors (`ok: false` plus a reason) rather than raising, so the agent can recover.
- Keep tool docstrings short and action-oriented: they are the agent's only manual.
- Formatting uses [ruff](https://docs.astral.sh/ruff/). Run `uvx ruff format` and `uvx ruff check --fix` on files you touch.

## Reporting a security issue

Do not open a public issue. See [SECURITY.md](SECURITY.md).

## License

By contributing you agree that your contributions are licensed under the [MIT License](LICENSE).
