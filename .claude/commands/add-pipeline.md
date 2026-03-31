Add a new pipeline to ops-pilot.yml for the repository or service: $ARGUMENTS

Steps:
1. Read `ops-pilot.yml` to see the current pipelines and config structure.
2. Read `shared/config.py` to understand all available PipelineConfig fields and validation rules.
3. Read `ops-pilot.example.yml` to see the full example with all providers.
4. Determine the correct provider based on the input:
   - GitHub Actions → `provider: github_actions`
   - GitLab CI → `provider: gitlab_ci` (ask for gitlab_url if self-hosted)
   - Jenkins → `provider: jenkins` (ask for jenkins_url, jenkins_job, code_host)
5. Ask the user for any missing required information:
   - Slack channel (default: #platform-alerts)
   - Severity threshold (default: medium)
   - Base branch (default: main)
6. Add the new pipeline entry to `ops-pilot.yml` using the Edit tool.
7. Validate the config by running:
   `source venv/bin/activate && python3 -c "from shared.config import load_config; cfg = load_config(); print(f'OK — {len(cfg.pipelines)} pipelines')" `
8. Show the user what was added and confirm it loaded successfully.

If no repo/service is provided, explain usage:
  /add-pipeline owner/repo
  /add-pipeline mygroup/project --provider gitlab_ci
  /add-pipeline myorg/backend --provider jenkins --jenkins-url https://ci.example.com
