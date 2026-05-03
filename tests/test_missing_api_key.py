import subprocess
import os
import sys


def test_missing_anthropic_api_key():
    env = os.environ.copy()

    # Ensure API key is NOT set
    env.pop("ANTHROPIC_API_KEY", None)

    # Disable demo mode so validation triggers
    env["DEMO_MODE"] = "false"

    result = subprocess.run(
        [sys.executable, "scripts/watch_and_fix.py", "--once"],
        capture_output=True,
        text=True,
        env=env,
    )

    # Check exit code
    assert result.returncode != 0

    # Check error message
    assert "ANTHROPIC_API_KEY" in result.stderr
    assert "Quickstart" in result.stderr