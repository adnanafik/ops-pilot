"""FastAPI demo server for ops-pilot.

Serves pre-recorded scenarios with simulated streaming so the demo works
at zero cost with no live API calls. Set DEMO_MODE=false and provide
ANTHROPIC_API_KEY to run real agents instead.

Routes:
    GET  /                     → redirect to /static/index.html
    GET  /scenarios            → list available scenario IDs and labels
    GET  /scenarios/{id}       → full scenario JSON
    POST /run/{id}             → stream agent steps with SSE
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
from typing import AsyncIterator

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

logger = logging.getLogger(__name__)

SCENARIOS_DIR = Path(__file__).parent / "scenarios"
STATIC_DIR = Path(__file__).parent / "static"
DEMO_MODE = os.environ.get("DEMO_MODE", "true").lower() != "false"

app = FastAPI(
    title="ops-pilot demo",
    description="Agentic CI/CD incident responder — interactive demo",
    version="0.1.0",
)

app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


def _load_scenarios() -> dict[str, dict]:
    """Load all scenario JSON files from the scenarios directory."""
    scenarios: dict[str, dict] = {}
    for path in sorted(SCENARIOS_DIR.glob("*.json")):
        try:
            data = json.loads(path.read_text())
            scenarios[data["id"]] = data
        except Exception as exc:
            logger.warning("Skipping %s: %s", path.name, exc)
    return scenarios


@app.get("/")
async def root() -> FileResponse:
    """Serve the demo UI."""
    return FileResponse(str(STATIC_DIR / "index.html"))


@app.get("/scenarios")
async def list_scenarios() -> list[dict]:
    """Return a list of available scenarios with their IDs and labels."""
    scenarios = _load_scenarios()
    return [
        {"id": sid, "label": s["label"]}
        for sid, s in scenarios.items()
    ]


@app.get("/scenarios/{scenario_id}")
async def get_scenario(scenario_id: str) -> dict:
    """Return the full JSON for a specific scenario."""
    scenarios = _load_scenarios()
    if scenario_id not in scenarios:
        raise HTTPException(status_code=404, detail=f"Scenario '{scenario_id}' not found")
    return scenarios[scenario_id]


@app.post("/run/{scenario_id}")
async def run_scenario(scenario_id: str) -> StreamingResponse:
    """Stream agent steps for the given scenario using Server-Sent Events.

    In DEMO_MODE, replays pre-recorded steps with realistic delays.
    In live mode, runs real agents against the Anthropic API.
    """
    scenarios = _load_scenarios()
    if scenario_id not in scenarios:
        raise HTTPException(status_code=404, detail=f"Scenario '{scenario_id}' not found")

    scenario = scenarios[scenario_id]

    if DEMO_MODE:
        generator = _stream_demo(scenario)
    else:
        generator = _stream_live(scenario)

    return StreamingResponse(
        generator,
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


async def _stream_demo(scenario: dict) -> AsyncIterator[str]:
    """Replay pre-recorded agent steps with typewriter-friendly delays."""
    yield _sse_event("start", {"id": scenario["id"], "label": scenario["label"]})

    agent_steps = scenario.get("agents", [])
    delays = {"monitor": 1.0, "triage": 2.5, "fix": 2.0, "notify": 1.0}

    for step in agent_steps:
        agent_name = step["agent"]
        delay = delays.get(agent_name, 1.5)

        # Signal that this agent is starting
        yield _sse_event("agent_start", {"agent": agent_name})
        await asyncio.sleep(0.3)

        # Stream the output character-by-character for typewriter effect
        output = step.get("output", "")
        chunk_size = 4
        for i in range(0, len(output), chunk_size):
            chunk = output[i: i + chunk_size]
            yield _sse_event("chunk", {"agent": agent_name, "text": chunk})
            await asyncio.sleep(0.02)

        # Send the full structured step data
        await asyncio.sleep(delay)
        yield _sse_event("agent_complete", step)

    yield _sse_event("done", {"id": scenario["id"]})


async def _stream_live(scenario: dict) -> AsyncIterator[str]:
    """Run real agents and stream their outputs.

    Requires ANTHROPIC_API_KEY to be set in the environment.
    """
    import sys

    sys.path.insert(0, str(Path(__file__).parent.parent))

    from shared.models import (
        DiffSummary,
        Failure,
        FailureDetail,
        PipelineInfo,
    )
    from agents.triage_agent import TriageAgent
    from agents.fix_agent import FixAgent
    from agents.notify_agent import NotifyAgent

    yield _sse_event("start", {"id": scenario["id"], "label": scenario["label"]})

    try:
        failure = Failure(
            id=scenario["id"],
            pipeline=PipelineInfo(**scenario["pipeline"]),
            failure=FailureDetail(**scenario["failure"]),
            diff_summary=DiffSummary(**scenario["diff_summary"]),
        )

        # Monitor step (already "detected" — just emit)
        monitor_step = next(
            (s for s in scenario["agents"] if s["agent"] == "monitor"), None
        )
        if monitor_step:
            yield _sse_event("agent_start", {"agent": "monitor"})
            await asyncio.sleep(0.5)
            yield _sse_event("agent_complete", monitor_step)

        # Triage
        yield _sse_event("agent_start", {"agent": "triage"})
        triage_agent = TriageAgent()
        triage = await asyncio.to_thread(triage_agent.run, failure)
        triage_step = {
            "agent": "triage",
            "status": "complete",
            "timestamp": triage.timestamp.isoformat(),
            "output": triage.output,
            "severity": triage.severity.value,
            "affected_service": triage.affected_service,
            "regression_introduced_in": triage.regression_introduced_in,
        }
        yield _sse_event("agent_complete", triage_step)

        # Fix
        yield _sse_event("agent_start", {"agent": "fix"})
        fix_agent = FixAgent(demo_mode=True)
        fix = await asyncio.to_thread(fix_agent.run, failure, triage)
        fix_step = {
            "agent": "fix",
            "status": "complete",
            "timestamp": fix.timestamp.isoformat(),
            "output": fix.output,
            "pr_title": fix.pr_title,
            "pr_body": fix.pr_body,
            "pr_url": fix.pr_url,
            "pr_number": fix.pr_number,
        }
        yield _sse_event("agent_complete", fix_step)

        # Notify
        yield _sse_event("agent_start", {"agent": "notify"})
        notify_agent = NotifyAgent(demo_mode=True)
        alert = await asyncio.to_thread(notify_agent.run, failure, triage, fix)
        notify_step = {
            "agent": "notify",
            "status": "complete",
            "timestamp": alert.timestamp.isoformat(),
            "output": alert.output,
            "slack_message": alert.slack_message,
        }
        yield _sse_event("agent_complete", notify_step)

        yield _sse_event("done", {"id": scenario["id"]})

    except Exception as exc:
        logger.exception("Live run failed for %s", scenario["id"])
        yield _sse_event("error", {"message": str(exc)})


def _sse_event(event: str, data: dict) -> str:
    """Format a Server-Sent Event string."""
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"
