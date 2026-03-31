Run the ops-pilot watcher with the specified mode: $ARGUMENTS

Read `scripts/watch_and_fix.py` to understand available flags, then run the appropriate command:

**Modes:**
- `once` → process current failures and exit (safe for testing)
- `dry-run` → triage only, no PRs or Slack notifications
- `once dry-run` → single pass, triage only (safest for debugging)
- `watch` → continuous polling loop (default, runs until Ctrl+C)
- `--config <path>` → use a specific config file

**Steps:**
1. Check that `ops-pilot.yml` exists: read it and show the configured pipelines.
2. Check that `.env` exists and `ANTHROPIC_API_KEY` is set (required for live mode).
3. Run the watcher using the venv Python:

For `once` or `dry-run`:
```
source venv/bin/activate && python3 scripts/watch_and_fix.py --once --dry-run
```

For continuous watch:
```
source venv/bin/activate && python3 scripts/watch_and_fix.py
```

4. Show the output and explain what each line means.
5. If errors occur, diagnose and explain the fix.

If no mode is provided, default to `once --dry-run` (safest option) and explain what it's doing.
