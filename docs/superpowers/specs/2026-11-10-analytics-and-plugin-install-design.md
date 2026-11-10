# Analytics metadata and plugin installation

## Scope

Keep the existing local debugger and PostHog missing-capability changes intact. Add reliable release metadata, best-effort client model and reasoning-effort metadata, optional user-authorized prompt capture, and portable plugin packaging. No publishing or marketplace submissions are included.

## Analytics behavior

- Preserve `$mcp_server_version` on both event paths and verify it matches the running package's `__version__`. Add `platformio_mcp_version` as an explicit application property. Test both delivery paths.
- Keep the SDK's native model detection and `llm_model` schema injection enabled. Capture only supplied model information, with its source; never infer a model from the client name.
- Support explicitly configured model and effort environment values for clients that cannot send metadata. Prefer per-request, recognized metadata over deployment configuration. Omit unknown values instead of claiming detection. Validate effort against documented values; ignore unrecognized structures.
- Keep SDK agent context as intent, clearly distinguished from the original user prompt. MCP tool calls do not generally contain conversation history or original prompts.
- Approved default: original prompt capture is off. `PLATFORMIO_MCP_CAPTURE_PROMPTS=1` allows the first 2,000 characters of `_meta["platformio.mcp"]["prompt"]`. Do not scrape chat history or local client files. Existing analytics-off, local-only, DO_NOT_TRACK, and CI controls take precedence. Document what can leave the machine and the limits of redaction.
- Add tests for actual SDK event properties, opt-out precedence, missing metadata, invalid effort, and prompt capture disabled/enabled. Mock outbound delivery.

## Plugin packaging

- Preserve the existing Claude marketplace and skill layout. Document `/plugin marketplace add` and `/plugin install`; use the client's actual command names.
- Add a Codex marketplace at `.agents/plugins/marketplace.json` pointing to the repository root, with explicit installation and authentication policies.
- Add portable root `mcp.json` and manifest schema metadata. Keep the legacy `.mcp.json` for clients that require it. Use `uvx` and the PlatformIO extra so plugin users need only uv installed.
- Add Codex compatibility metadata only where needed by supported installed clients. Keep release synchronization aware of any newly introduced version-bearing manifests.
- Document Codex marketplace registration and plugin selection through its supported plugin UI/CLI. Other clients retain generated MCP configuration; do not promise a universal `/plugins` command.
- Validate Claude manifests with its CLI, exercise Codex parsing using an isolated temporary home if supported, and check JSON, release consistency, and packaging references.

## Limits

Reasoning effort and model cannot be guaranteed when clients do not expose them. Original prompt capture needs the user's local opt-in plus host-provided text. Packaging makes the repository installable after publication; listing in public directories is a separate action.

## Sources checked

- https://developers.openai.com/plugins/build/plugins
- https://code.claude.com/docs/en/plugins-reference
- https://posthog.com/docs/mcp-analytics
- Installed PostHog Python SDK and local Codex/Claude CLI help.

## Approval and concrete metadata contract

User approved: “Proceed with prompt capture opt in”. Model and effort come from `x-codex-turn-metadata` or `platformio.mcp` per-request metadata. Explicit environment fallbacks are `PLATFORMIO_MCP_MODEL` and `PLATFORMIO_MCP_REASONING_EFFORT`. Properties use `$mcp_llm_model`, `$mcp_llm_model_source`, `reasoning_effort`, and `reasoning_effort_source`. Configuration sources are distinguished from request metadata. The SDK retains its native self-reported model argument. No new prompt hooks are installed into clients; hosts must explicitly supply prompt metadata. Portable and legacy MCP files share one identical named server; actual Codex activation remains subject to client validation.
