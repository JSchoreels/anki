# Repository Instructions

## General

- If unsure about a feature, double-check with the user before implementing.
- Do not implement extra logic that was not requested; consider maintenance cost before adding logic.
- Cover implemented behavior with tests. If no relevant tests exist, ask the user how to proceed.
- Keep diagrams aligned with the codebase when adding or updating them.
- When manipulating data (create, read, update, delete), add documentation in `docs/*.MD`; consult those docs for data inconsistencies before fixing behavior.
- Consider logging when adding logic, using maintainable log levels and operationally useful messages.
- After fixes or new features, ask the user whether to retrigger a release. Do not run a release unless the user explicitly confirms. If confirmed for an internal draft build, use `just release::draft --skip-ci-check true`.

## Code Style

- Prefer fewer conditions.
- Do not add fallbacks or new profiles unless explicitly requested.
- Avoid putting too much code in one file or class; keep code decoupled and clean.
- Avoid fallbacks for behavior that is being built now.

## Shell Commands

- Prefix shell commands with `rtk` when practical.
- Use `rtk proxy <cmd>` when a raw command needs to run without filtering.
