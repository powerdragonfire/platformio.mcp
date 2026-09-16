# Security Policy

platformio.mcp runs shell commands (`pio`, `addr2line`, serial port access) on behalf of an AI agent. Bugs that let a prompt escape the safety policy, execute arbitrary commands, or read files outside the project are security issues.

## Supported versions

Only the latest release on PyPI receives fixes.

## Reporting a vulnerability

Use GitHub's private reporting: **[Report a vulnerability](https://github.com/powerdragonfire/platformio.mcp/security/advisories/new)**.

If that does not work for you, email mihirgandecha.2002@gmail.com with "platformio.mcp security" in the subject.

Please include:

- The tool name and arguments that trigger the problem
- The `PLATFORMIO_MCP_POLICY` value in effect
- What the agent could do that it should not be able to

You will get an acknowledgement within 7 days. Fixes ship as a patch release with a GitHub Security Advisory crediting you, unless you ask otherwise.

## Scope notes

- Under `full` policy the server is meant to flash devices and run project builds, which execute `platformio.ini` extra scripts. Running an untrusted project is out of scope; use `build_only` or `read_only` for that.
- Serial output from a device is untrusted input. Bugs in how it is parsed are in scope.
