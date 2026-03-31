Analyze the following CI failure and produce a structured triage report, exactly as TriageAgent would.

Failure input: $ARGUMENTS

Steps:
1. Read `shared/models.py` to understand the Triage model fields.
2. Read `agents/triage_agent.py` to understand the prompt and output format.
3. Analyze the failure input (log lines, error message, or description provided).
4. Produce a triage report with these fields:
   - **Root cause** (1-2 sentences, precise)
   - **Severity**: LOW / MEDIUM / HIGH / CRITICAL with reasoning
   - **Affected service**: which component/service is broken
   - **Regression introduced in**: commit or change that caused it (if determinable)
   - **Production impact**: what breaks in production if unaddressed
   - **Fix confidence**: HIGH / MEDIUM / LOW with reasoning
   - **Suggested fix**: concrete code change needed (file + what to change)

If no failure input is provided, explain usage:
  /triage <paste log lines or error description here>

Keep the output concise and actionable — this is what an on-call engineer reads first.
