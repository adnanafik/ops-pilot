Create a new demo scenario JSON file for ops-pilot from the following failure description: $ARGUMENTS

Steps:
1. Read `demo/scenarios/null_pointer_auth.json` as the reference format to follow exactly.
2. Read `shared/models.py` to understand the Failure, Triage, Fix, and Alert model fields.
3. Read `agents/triage_agent.py` and `agents/fix_agent.py` to understand what realistic output looks like.

Then create a new scenario file:

**File to create:** `demo/scenarios/<slug>.json`
- `slug` = lowercase, underscores, derived from the failure description (e.g., "db_connection_timeout")
- Mirror the exact JSON structure of `null_pointer_auth.json`:
  - `scenario_id`, `title`, `description`
  - `failure`: realistic Failure model (repo, workflow, sha, logs, etc.)
  - `triage`: realistic Triage model (root_cause, severity, affected_service, suggested_fix, etc.)
  - `fix`: realistic Fix model (branch, pr_url, pr_number, patch_summary, files_changed)
  - `alert`: realistic Alert model (channel, message, severity, pr_url)
- Make the logs field realistic — include actual stack traces or error output that matches the failure type
- Set severity appropriately: flaky tests → LOW/MEDIUM, crashes → HIGH, security → CRITICAL
- The `suggested_fix` should reference real file paths and specific code changes
- The `patch_summary` should describe a concrete, believable patch

**Also update `docs/scenarios/`:**
- Copy the new file to `docs/scenarios/<slug>.json` so the GitHub Pages demo can serve it
- Update `docs/index.html` to add a new scenario button for this scenario:
  - Find the `scenarios` array in the JS section
  - Add an entry: `{ id: "<slug>", label: "<short label>", description: "<one line>" }`

After creating the files, verify the JSON is valid:
  `python3 -c "import json; json.load(open('demo/scenarios/<slug>.json')); print('JSON valid')"`

If no failure description is provided, explain usage:
  /scenario "Redis connection timeout in payment service"
  /scenario "OOM killed in Kubernetes pod during image processing"
  /scenario "Import cycle causes module load failure in FastAPI app"
