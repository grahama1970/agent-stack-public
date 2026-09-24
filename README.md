# agent-stack (public export)

This repository is an **auto-generated, one-way export** of selected agent skills
from a private development repository. It is regenerated on every change upstream;
pull requests here cannot be merged back (open an issue instead).

## Layout

```
skills/<name>/SKILL.md   # skill description, triggers, usage
skills/<name>/...        # scripts, docs, tests for that skill
EXPORT_MANIFEST.json     # source commit, file list and sha256 hashes for this export
```

Test fixtures, run outputs, reports and session logs are intentionally not exported,
so some tests or examples may reference files that are absent.

## Using a skill

1. Read `skills/<name>/SKILL.md` for purpose, triggers and commands.
2. Copy or symlink the skill directory into your agent's skills directory
   (e.g. `~/.claude/skills/<name>` or `~/.pi/agent/skills/<name>`).
3. Run the skill's entry point as documented (most use `./run.sh` or `uv run`).
   Some skills expect external services or API keys; supply your own via environment variables.

## License

Unless a skill states otherwise in its own `SKILL.md`/`LICENSE`, the contents are
provided under the MIT License, as-is and without warranty.
