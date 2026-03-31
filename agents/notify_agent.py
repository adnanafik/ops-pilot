"""NotifyAgent — sends Slack (or console) alerts when a fix is ready.

Generates a concise, actionable Slack message and posts it to the
configured webhook URL. Falls back to console output in demo mode or
when no webhook is configured.

Slack message format:
  :circle: *CI failure — <service>* | branch `...` | commit `...`
  Root cause: ...
  Fix: Draft PR #N opened → <link>
  Severity: HIGH | Confidence: HIGH | _ops-pilot_
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from typing import Optional

import httpx

from agents.base_agent import BaseAgent
from shared.models import AgentStatus, Alert, Failure, Fix, Severity, Triage

logger = logging.getLogger(__name__)

SEVERITY_EMOJI = {
    Severity.LOW: ":white_circle:",
    Severity.MEDIUM: ":yellow_circle:",
    Severity.HIGH: ":red_circle:",
    Severity.CRITICAL: ":rotating_light:",
}

SYSTEM_PROMPT = """You are writing a Slack notification for an engineering team about a CI failure that has been triaged and fixed automatically.

Rules:
- Be concise — engineers are busy
- Lead with the severity emoji and service name
- Include: root cause (1 sentence), fix action, PR link, severity, confidence
- Use Slack mrkdwn formatting: *bold*, `code`, <url|text>
- End with _ops-pilot_
- No markdown headers, no bullet lists longer than 3 items

Output a single Slack mrkdwn message string. No JSON wrapper, just the message text."""


class NotifyAgent(BaseAgent[Alert]):
    """Sends Slack notifications when a CI fix is ready.

    Args:
        slack_webhook_url: Incoming webhook URL. Falls back to
                           ``SLACK_WEBHOOK_URL`` environment variable.
        channel:           Channel name for logging (not used for posting —
                           that's baked into the webhook URL).
        demo_mode:         If True, prints to console instead of posting.
    """

    def __init__(
        self,
        slack_webhook_url: Optional[str] = None,
        slack_bot_token: Optional[str] = None,
        channel: str = "#platform-alerts",
        demo_mode: bool = True,
        **kwargs,
    ) -> None:
        """Initialize the notifier with optional Slack credentials.

        Priority:
          1. slack_bot_token (xoxb-...) — posts to any channel via chat.postMessage
          2. slack_webhook_url          — posts to one fixed channel
          3. console output             — demo/dev fallback
        """
        super().__init__(**kwargs)
        self.slack_bot_token = slack_bot_token or os.environ.get("SLACK_BOT_TOKEN", "")
        self.slack_webhook_url = slack_webhook_url or os.environ.get("SLACK_WEBHOOK_URL", "")
        self.channel = channel
        self.demo_mode = demo_mode

    def describe(self) -> str:
        """Return a one-line description of this agent's responsibility."""
        return "Sends Slack alerts with triage summary and PR link when a fix is ready"

    def run(self, failure: Failure, triage: Triage, fix: Fix) -> Alert:
        """Generate and send a Slack notification.

        Args:
            failure: Original CI failure.
            triage:  Triage results from TriageAgent.
            fix:     Fix details from FixAgent.

        Returns:
            Alert model with the message that was sent.
        """
        self._status = AgentStatus.RUNNING
        logger.info("NotifyAgent: preparing alert for %s", failure.id)

        try:
            slack_message = self._generate_message(failure, triage, fix)

            if self.demo_mode or (not self.slack_bot_token and not self.slack_webhook_url):
                self._console_output(slack_message)
                delivery = "logged to console (demo mode)"
            elif self.slack_bot_token:
                self._post_via_bot(slack_message)
                delivery = f"sent to Slack {self.channel}"
            else:
                self._post_to_webhook(slack_message)
                delivery = f"sent to Slack {self.channel}"

            alert = Alert(
                failure_id=failure.id,
                output=f"Team notified — {delivery}.",
                slack_message=slack_message,
                channel=self.channel,
                timestamp=datetime.utcnow(),
            )

            self._status = AgentStatus.COMPLETE
            logger.info("NotifyAgent: alert %s", delivery)
            return alert

        except Exception as exc:
            self._status = AgentStatus.FAILED
            logger.error("NotifyAgent: failed — %s", exc)
            raise

    def _generate_message(self, failure: Failure, triage: Triage, fix: Fix) -> str:
        """Use the LLM to generate a concise Slack message."""
        emoji = SEVERITY_EMOJI.get(triage.severity, ":white_circle:")

        user_message = f"""Generate a Slack notification for this CI incident:

Repository: {failure.pipeline.repo}
Branch: {failure.pipeline.branch}
Commit: {failure.pipeline.commit}
Job: {failure.failure.job}

Root cause: {triage.output}
Severity: {triage.severity.value.upper()}
Affected service: {triage.affected_service}
Production impact: {triage.production_impact or 'none'}
Fix confidence: {triage.fix_confidence}

Fix: {fix.output}
PR title: {fix.pr_title}
PR URL: {fix.pr_url}
PR number: {fix.pr_number}

Use this severity emoji: {emoji}
Channel context: {self.channel}"""

        return self._call_llm(
            system=SYSTEM_PROMPT,
            user=user_message,
            max_tokens=300,
        ).strip()

    def _post_via_bot(self, message: str) -> None:
        """Post to any channel via Slack bot token (chat.postMessage API)."""
        with httpx.Client(timeout=10) as http:
            resp = http.post(
                "https://slack.com/api/chat.postMessage",
                headers={"Authorization": f"Bearer {self.slack_bot_token}"},
                json={"channel": self.channel, "text": message, "mrkdwn": True},
            )
            resp.raise_for_status()
            data = resp.json()
            if not data.get("ok"):
                raise RuntimeError(f"Slack API error: {data.get('error')}")

    def _post_to_webhook(self, message: str) -> None:
        """POST the message to the Slack incoming webhook (single channel)."""
        payload = {"text": message}
        with httpx.Client(timeout=10) as http:
            resp = http.post(self.slack_webhook_url, json=payload)
            resp.raise_for_status()

    def _console_output(self, message: str) -> None:
        """Print the Slack message to stdout for demo/dev usage."""
        separator = "─" * 60
        print(f"\n{separator}")
        print("SLACK NOTIFICATION (demo mode):")
        print(separator)
        print(message)
        print(separator + "\n")
