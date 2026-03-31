Scaffold a new CIProvider implementation for: $ARGUMENTS

Steps:
1. Read `providers/base.py` to understand the full CIProvider interface (all 7 abstract methods).
2. Read `providers/github.py` as the reference implementation to follow the same patterns.
3. Read `providers/factory.py` to understand how providers are wired in.
4. Read `shared/config.py` to understand PipelineConfig fields.

Then scaffold the new provider:

**File to create:** `providers/<name>.py`
- Class name: `<Name>Provider(CIProvider)`
- Implement all 7 abstract methods with real API calls (not stubs)
- Follow the same docstring, error handling, and logging patterns as GitHubProvider
- Add a `_headers()` helper for auth
- Handle idempotency in `create_branch` (silence "already exists" errors)
- Handle existing PR/MR in `open_draft_pr` (return existing on conflict)

**Update `providers/factory.py`:**
- Add the new provider name to the if/elif chain in `make_provider()`

**Update `providers/__init__.py`:**
- Export the new provider class

**Update `shared/config.py`:**
- Add the new provider name to `_KNOWN_PROVIDERS`
- Add any provider-specific config fields to `PipelineConfig`

**Update `ops-pilot.example.yml`:**
- Add an example pipeline entry for the new provider

After creating the files, verify imports work:
  `source venv/bin/activate && python3 -c "from providers import make_provider; print('OK')"`

If no provider name is provided, explain usage:
  /new-provider CircleCI
  /new-provider Bitbucket Pipelines
  /new-provider Azure DevOps
