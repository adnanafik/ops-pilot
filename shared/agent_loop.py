"""Generic agentic execution engine for ops-pilot.

Replaces the single-call LLM pattern with an iterative tool-use loop where
the model drives the investigation. The loop runs until the model calls
end_turn (COMPLETED), max_turns is reached (TURN_LIMIT), or tool failures
block progress (TOOL_FAILURE).

After the loop exits — regardless of exit condition — a second extraction call
converts the full conversation history into a structured Pydantic model. This
uniform post-loop extraction is the key design insight: you should never try to
parse the last assistant message as JSON directly, because the loop can exit at
any point (TURN_LIMIT, TOOL_FAILURE, mid-sentence end_turn). The extraction call
handles all of them the same way.

Phase 1 of the ops-pilot evolution. See CLAUDE.md for the full roadmap.
"""

from __future__ import annotations

import asyncio
import json
import logging
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Any, Generic, TypeVar

from pydantic import BaseModel

from shared.context_budget import ContextBudget
from shared.exceptions import RateLimitExceeded
from shared.tenant_context import TenantContext

if TYPE_CHECKING:
    from providers.base import CIProvider
    from shared.models import Failure
    from shared.trust_context import TrustContext

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)


# ── Content block types ────────────────────────────────────────────────────────
# The Anthropic API response.content is a list of typed blocks. A single
# assistant response can contain both text blocks and tool_use blocks — they
# are not mutually exclusive. We define our own types rather than importing
# from the Anthropic SDK so this module is testable without real API calls.


@dataclass
class TextBlock:
    """A text content block from the model's response."""
    text: str


@dataclass
class ToolUseBlock:
    """A tool call requested by the model.

    The id field is critical: it's the correlation key that links this
    tool_use block to its tool_result block in the next user message.
    A tool_use with no matching tool_result in the following message is
    an API error — never omit a result even if the tool errored.
    """
    id: str     # API-generated, used to match tool_result back to this call
    name: str   # must match a tool name in the registry
    input: dict # validated against the tool's input_schema by the model


# ── Tool infrastructure ────────────────────────────────────────────────────────


class Permission(Enum):
    """Blast-radius classification for every tool.

    This is declared on the tool definition, not at call time, so engineers
    reviewing tool registrations can see the risk level before deploying.
    Phase 7 will use this to drive confirmation prompts and audit logging.
    The point of declaring it now: you cannot retrofit blast-radius thinking
    after a wrong action has already hit production.
    """
    READ_ONLY             = "read_only"
    WRITE                 = "write"
    DANGEROUS             = "dangerous"
    REQUIRES_CONFIRMATION = "requires_confirmation"


@dataclass
class ToolResult:
    """Output of a single tool execution."""
    content: str
    is_error: bool = False


@dataclass
class ToolContext:
    """Runtime dependencies injected into every tool at execution time.

    Tools are stateless definition objects — they never store a provider
    reference or failure state. Everything they need at runtime comes through
    here. This is the standard dependency injection pattern: tools are
    easy to test in isolation (mock the context), and adding a new runtime
    dependency means adding a field here, not modifying every tool class.
    """
    provider: CIProvider | None
    failure: Failure
    tenant_id: str = ""


class Tool(ABC):
    """Abstract base class for all agent tools.

    A Tool is a stateless definition object. It declares its name, description,
    JSON schema, permission level, and how to execute a call. It does not store
    runtime state — ToolContext provides that at execution time.

    Writing good descriptions is as important as writing correct implementations.
    The model decides which tool to call based on descriptions alone. A vague
    description leads to wrong tool selection regardless of implementation quality.
    Write descriptions for the model, not for engineers.
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """Tool name as it appears in tool_use blocks from the model.

        Must be unique across all tools registered with a given loop instance.
        """
        ...

    @property
    @abstractmethod
    def description(self) -> str:
        """What this tool does, when to use it, and what it returns.

        Written for the model. Include: what the output looks like, when this
        tool is more useful than alternatives, any important limitations.
        """
        ...

    @property
    @abstractmethod
    def input_schema(self) -> dict:
        """JSON Schema for the tool's input parameters.

        Must have 'type': 'object', 'properties', and 'required' keys.
        The model constructs its tool_use inputs from this schema — be precise
        about types and which fields are required vs. optional.
        """
        ...

    @property
    def permission(self) -> Permission:
        """Blast-radius classification. Override for non-read-only tools."""
        return Permission.READ_ONLY

    @abstractmethod
    async def execute(self, input: dict, ctx: ToolContext) -> ToolResult:
        """Execute the tool and return a result.

        Raise freely — the loop catches all exceptions and converts them
        to error ToolResults that are fed back to the model. This means
        tools should not swallow exceptions internally.

        Args:
            input: Dict constructed by the model matching input_schema.
            ctx:   Runtime context: provider, failure metadata, tenant id.
        """
        ...

    def to_api_dict(self) -> dict:
        """Serialize to the Anthropic API tool definition format."""
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
        }


# ── Loop outcome and result ────────────────────────────────────────────────────


class LoopOutcome(Enum):
    """How the agent loop terminated.

    These are distinct failure modes that warrant different escalation messages.
    Do not collapse them into a single "needs human review" bucket. An on-call
    engineer needs to know whether to: think harder (COMPLETED+LOW), retry with
    a higher turn limit (TURN_LIMIT), or fix a broken integration (TOOL_FAILURE).
    """
    COMPLETED    = "completed"     # model called end_turn voluntarily — happy path
    TURN_LIMIT   = "turn_limit"    # hit max_turns — investigation cut short
    TOOL_FAILURE = "tool_failure"  # all distinct tools errored — integration broken


@dataclass
class LoopResult(Generic[T]):
    """Complete result from one agent loop run.

    last_assistant_text is always populated even on non-COMPLETED exits.
    The model's investigative notes have diagnostic value for escalation
    even when the loop ended prematurely — never discard this.
    """
    outcome: LoopOutcome
    model_confidence: str      # self-reported: "HIGH" | "MEDIUM" | "LOW"
    extracted: T | None        # structured output from post-loop extraction call
    turns_used: int
    failed_tools: list[str]    # cumulative list of tool names that errored
    last_assistant_text: str   # partial findings — attach to escalation alerts


# ── Loop footer ────────────────────────────────────────────────────────────────
# Injected by AgentLoop, not written by domain agents (TriageAgent, FixAgent).
# Domain agents own the "what you are and what you're investigating" part.
# This footer owns the "how the loop works" part. Keeping them separate means
# domain agents don't need to know about loop internals.

def _loop_footer(schema_json: str) -> str:
    return f"""

## Investigation loop instructions

You are inside a tool-use loop. Use the tools to gather evidence before answering.
When you have sufficient evidence to answer confidently:
  - Stop calling tools
  - Write a concise summary of your findings and reasoning
  - The system will extract your findings into this JSON schema:

{schema_json}

If a tool returns an error, adapt: try an alternative approach or proceed with
the evidence you have. Set fix_confidence to LOW when evidence is incomplete —
never fabricate certainty.
"""


# ── AgentLoop ─────────────────────────────────────────────────────────────────


class AgentLoop(Generic[T]):
    """Generic agentic execution engine.

    Runs a tool-use loop driven by the model until one of three exits:
      COMPLETED   — model decides it has enough signal (end_turn, no tool calls)
      TURN_LIMIT  — safety net: max_turns reached (should not be the norm)
      TOOL_FAILURE — all registered tools errored; investigation is blocked

    After exit, a second 'extraction call' converts the full conversation history
    into a structured T instance. This second call is the key pattern: it works
    uniformly for all three exit conditions, and it lets the model decide how to
    populate fix_confidence when evidence is incomplete.

    This class is generic (AgentLoop[T]) and knows nothing about CI failures,
    triage logic, or DevOps. Domain knowledge lives in the system prompt and
    the tool implementations provided by the caller.
    """

    def __init__(
        self,
        tools: list[Tool],
        backend: Any,              # duck-typed: needs complete() + complete_with_tools()
        domain_system_prompt: str,
        response_model: type[T],
        model: str,
        max_turns: int = 10,
        tool_timeout: float = 30.0,
        max_tokens: int = 4096,
        confirm: Callable[[Tool, dict], Awaitable[bool]] | None = None,
        context_budget: ContextBudget | None = None,
        tenant_context: TenantContext | None = None,
        trust_context: TrustContext | None = None,
        actor: str = "agent",
    ) -> None:
        self._tools: dict[str, Tool] = {t.name: t for t in tools}
        self._confirm = confirm
        self._backend = backend
        self._response_model = response_model
        self._model = model
        self._max_turns = max_turns
        self._tool_timeout = tool_timeout
        self._max_tokens = max_tokens
        self._context_budget = context_budget
        self._tenant_context = tenant_context
        self._trust_context = trust_context
        self._actor = actor

        # Full system prompt = domain instructions + loop mechanics footer.
        # The schema is embedded in the footer so the model knows the shape
        # that the extraction call will expect — it reasons toward that shape.
        schema_json = json.dumps(response_model.model_json_schema(), indent=2)
        self._system = domain_system_prompt + _loop_footer(schema_json)

    async def run(
        self,
        messages: list[dict],
        ctx: ToolContext,
    ) -> LoopResult[T]:
        """Run the agent loop.

        Args:
            messages: Initial message list. A list, not a raw string, so callers
                      can prime with examples or structured context later. Typically
                      a single user message with the failure details.
            ctx:      Runtime context injected into every tool execution.

        Returns:
            LoopResult containing outcome, extracted structured data, and
            diagnostic information for escalation messages.
        """
        history = list(messages)  # copy — never mutate the caller's list
        failed_tools: list[str] = []
        last_text = ""

        for turn in range(self._max_turns):
            logger.debug("AgentLoop turn %d/%d", turn + 1, self._max_turns)

            # ── Step 0: Compact history if budget threshold is reached ───────
            # Runs before the model call so the outgoing request stays within
            # the context limit. Compaction replaces processed tool_result bodies
            # with stubs — the model's interpretations in assistant turns are
            # preserved. The last user message (unprocessed tool results) is
            # always left intact.
            if self._context_budget is not None and self._context_budget.should_compact(history):
                before = self._context_budget._estimate_tokens(history)
                history = self._context_budget.compact(history)
                after = self._context_budget._estimate_tokens(history)
                logger.info(
                    "AgentLoop: compacted history at turn %d — %d→%d estimated tokens",
                    turn + 1,
                    before,
                    after,
                )

            # ── Step 1: Call the model ───────────────────────────────────────
            # Check rate limit before calling the model.
            if self._tenant_context is not None:
                estimated = ContextBudget._estimate_tokens(history)
                try:
                    self._tenant_context.rate_limiter.check_and_consume(estimated)
                except RateLimitExceeded as exc:
                    logger.warning(
                        "AgentLoop: rate limit reached for tenant '%s': %s",
                        self._tenant_context.tenant_id,
                        exc,
                    )
                    extracted = await self._extract_structured(history)
                    return LoopResult(
                        outcome=LoopOutcome.TURN_LIMIT,
                        model_confidence="LOW",
                        extracted=extracted,
                        turns_used=turn + 1,
                        failed_tools=failed_tools,
                        last_assistant_text=last_text + f" [rate limit reached: {exc}]",
                    )

            # Pass list(history) — a snapshot — not the mutable history reference.
            # If we passed history directly, the mock in tests would record the
            # reference and see future appends in call_args. More importantly,
            # it's semantically correct: the API receives the conversation as it
            # stood at this turn, not as it will look after we process the response.
            raw = self._backend.complete_with_tools(
                messages=list(history),
                tools=[t.to_api_dict() for t in self._tools.values()],
                system=self._system,
                model=self._model,
                max_tokens=self._max_tokens,
            )
            text_blocks, tool_uses = self._parse_response(raw)

            # Record usage after successful LLM call
            if self._tenant_context is not None:
                call_tokens = ContextBudget._estimate_tokens(
                    [{"content": [getattr(b, "text", "") for b in raw.content]}]
                )
                self._tenant_context.usage_tracker.record_tokens(call_tokens)
                self._tenant_context.usage_tracker.record_api_call()

            if text_blocks:
                last_text = " ".join(b.text for b in text_blocks)

            # ── Step 2: Append the full assistant message (atomic) ───────────
            # The entire response — both text blocks and tool_use blocks — goes
            # into one assistant message. Do not split them. The assistant turn
            # is atomic: splitting it or stripping the text blocks produces
            # malformed history that the API will reject on the next call.
            assistant_content: list[dict] = []
            for b in text_blocks:
                assistant_content.append({"type": "text", "text": b.text})
            for b in tool_uses:
                assistant_content.append({
                    "type": "tool_use",
                    "id": b.id,
                    "name": b.name,
                    "input": b.input,
                })
            history.append({"role": "assistant", "content": assistant_content})

            # ── Step 3: No tool calls → model is done ───────────────────────
            if not tool_uses:
                logger.debug("AgentLoop: end_turn after %d turns", turn + 1)
                extracted = await self._extract_structured(history)
                return LoopResult(
                    outcome=LoopOutcome.COMPLETED,
                    model_confidence=self._read_confidence(extracted),
                    extracted=extracted,
                    turns_used=turn + 1,
                    failed_tools=failed_tools,
                    last_assistant_text=last_text,
                )

            # ── Step 4: Execute all tool calls concurrently ──────────────────
            # Collect ALL results before appending anything to history. If you
            # appended results one at a time as they finish, you'd get malformed
            # interleaved history. Execute everything, then do one append.
            tool_results = await self._execute_tools_concurrent(
                tool_uses, ctx, failed_tools, last_text
            )

            # ── Step 5: Append one user message with ALL results ─────────────
            # The API requires every tool_use block to have a matching
            # tool_result in the immediately following user message, keyed by
            # tool_use_id. Missing a result is an API error. Results from
            # asyncio.gather() are in input order (matching tool_use order),
            # not completion order — this is what the API expects.
            history.append({"role": "user", "content": tool_results})

        # ── Turn limit reached ───────────────────────────────────────────────
        # This is the safety net, not the intended stopping point. If you're
        # regularly hitting this, something else is wrong: a tool is returning
        # noise, the prompt is chasing red herrings, or the limit is too low.
        logger.warning(
            "AgentLoop: turn limit (%d) reached — partial findings will be escalated",
            self._max_turns,
        )
        extracted = await self._extract_structured(history)

        # TOOL_FAILURE if every registered tool errored at least once.
        # TURN_LIMIT otherwise — the investigation was cut short, not blocked.
        outcome = (
            LoopOutcome.TOOL_FAILURE
            if set(failed_tools) >= set(self._tools.keys())
            else LoopOutcome.TURN_LIMIT
        )
        return LoopResult(
            outcome=outcome,
            model_confidence="LOW",
            extracted=extracted,
            turns_used=self._max_turns,
            failed_tools=failed_tools,
            last_assistant_text=last_text,
        )

    # ── Internal helpers ───────────────────────────────────────────────────────

    async def _execute_tools_concurrent(
        self,
        tool_uses: list[ToolUseBlock],
        ctx: ToolContext,
        failed_tools: list[str],  # mutated in-place to track cumulative failures
        last_text: str = "",
    ) -> list[dict]:
        """Execute all tool calls concurrently; return results in original order.

        asyncio.gather() preserves input order even when tasks complete out of
        order. Result[i] always corresponds to tool_uses[i]. This matters because
        the API matches tool_result blocks to tool_use blocks by id, and the order
        must match the tool_use order in the preceding assistant message.

        Args:
            tool_uses:    Tool call blocks from the current assistant turn.
            ctx:          Runtime context injected into tool.execute().
            failed_tools: Cumulative list of errored tool names — mutated in-place.
            last_text:    Last assistant text, used as context for explanation generation.
        """
        async def run_one(block: ToolUseBlock) -> dict:
            tool = self._tools.get(block.name)
            tenant_id = (
                self._tenant_context.tenant_id
                if self._tenant_context is not None
                else ""
            )

            if tool is None:
                failed_tools.append(block.name)
                result_content = (
                    f"Unknown tool '{block.name}'. "
                    f"Available tools: {sorted(self._tools.keys())}"
                )
                if self._trust_context is not None:
                    self._trust_context.audit_log.record(
                        tenant_id=tenant_id,
                        actor=self._actor,
                        tool_name=block.name,
                        tool_input=block.input,
                        tool_result=result_content,
                        is_error=True,
                        explanation=None,
                    )
                return {
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": result_content,
                    "is_error": True,
                }

            # ── Permission gate ──────────────────────────────────────────────
            # Block tools not in the deployment's allowlist before execution.
            if (
                self._tenant_context is not None
                and not self._tenant_context.permissions.is_allowed(block.name)
            ):
                failed_tools.append(block.name)
                result_content = (
                    f"Tool '{block.name}' is not permitted for this deployment. "
                    "Use an alternative tool or conclude without this data."
                )
                if self._trust_context is not None:
                    self._trust_context.audit_log.record(
                        tenant_id=tenant_id,
                        actor=self._actor,
                        tool_name=block.name,
                        tool_input=block.input,
                        tool_result=result_content,
                        is_error=True,
                        explanation=None,
                    )
                return {
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": result_content,
                    "is_error": True,
                }

            # ── Confirmation gate ────────────────────────────────────────────
            # Phase 7: if trust_context is set, generate explanation and auto-proceed.
            # Phase 8 will add a real approval gate here instead.
            # Without trust_context, fall through to the existing confirm hook.
            explanation: str | None = None
            if tool.permission == Permission.REQUIRES_CONFIRMATION:
                if self._trust_context is not None:
                    explanation = self._trust_context.explanation_generator.generate(
                        tool_name=block.name,
                        tool_input=block.input,
                        context_summary=last_text,
                    )
                    # auto-proceed — observability without blocking
                else:
                    approved = (
                        self._confirm is not None
                        and await self._confirm(tool, block.input)
                    )
                    if not approved:
                        failed_tools.append(block.name)
                        return {
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": (
                                f"Tool '{block.name}' requires explicit confirmation "
                                "before execution. No confirmation hook is configured "
                                "— action blocked. Summarise what you intended to do "
                                "and why, so a human can review and approve."
                            ),
                            "is_error": True,
                        }

            # ── Execute ──────────────────────────────────────────────────────
            is_error = False
            result_content = ""
            try:
                result = await asyncio.wait_for(
                    tool.execute(block.input, ctx),
                    timeout=self._tool_timeout,
                )
                is_error = result.is_error
                result_content = result.content
                if result.is_error:
                    failed_tools.append(block.name)
            except TimeoutError:
                failed_tools.append(block.name)
                is_error = True
                result_content = f"Tool timed out after {self._tool_timeout}s. Try requesting fewer lines."
                logger.warning("Tool '%s' timed out after %.1fs", block.name, self._tool_timeout)
            except Exception as exc:
                failed_tools.append(block.name)
                is_error = True
                result_content = f"Tool error: {exc}"
                logger.warning("Tool '%s' raised: %s", block.name, exc)

            # ── Audit log ────────────────────────────────────────────────────
            if self._trust_context is not None:
                self._trust_context.audit_log.record(
                    tenant_id=tenant_id,
                    actor=self._actor,
                    tool_name=block.name,
                    tool_input=block.input,
                    tool_result=result_content,
                    is_error=is_error,
                    explanation=explanation,
                )

            return {
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": result_content,
                **({"is_error": True} if is_error else {}),
            }

        return list(await asyncio.gather(*[run_one(b) for b in tool_uses]))

    async def _extract_structured(self, history: list[dict]) -> T | None:
        """Second LLM call: extract structured output from conversation history.

        This runs after the loop regardless of exit condition. Why a separate
        call instead of parsing the last assistant message?

        1. The loop can exit via TURN_LIMIT mid-investigation — last message
           may not be clean JSON.
        2. The loop can exit via TOOL_FAILURE — last message is an error result.
        3. Even on COMPLETED, the model may have added reasoning text around
           its JSON, or formatted it differently.

        A dedicated extraction call with "here's the full transcript, extract
        what you found" handles all three cases uniformly. The model populates
        fix_confidence: LOW itself when evidence is incomplete.
        """
        schema_json = json.dumps(self._response_model.model_json_schema(), indent=2)
        conversation = _summarize_history(history)
        try:
            raw = self._backend.complete(
                system=(
                    f"Extract the investigation findings into the JSON schema below. "
                    f"Return only valid JSON with no markdown fences or explanation.\n\n"
                    f"{schema_json}"
                ),
                user=(
                    f"Investigation transcript:\n\n{conversation}\n\n"
                    f"Extract the findings. Set fix_confidence to LOW if evidence "
                    f"is incomplete or the root cause is uncertain."
                ),
                model=self._model,
                max_tokens=1024,
            )
            text = raw.strip()
            if text.startswith("```"):
                text = "\n".join(
                    line for line in text.splitlines()
                    if not line.startswith("```")
                ).strip()
            return self._response_model.model_validate_json(text)
        except Exception as exc:
            logger.error("Structured extraction failed: %s", exc)
            return None

    @staticmethod
    def _parse_response(raw: Any) -> tuple[list[TextBlock], list[ToolUseBlock]]:
        """Parse an Anthropic SDK Message into our typed content blocks."""
        texts: list[TextBlock] = []
        tools: list[ToolUseBlock] = []
        for block in raw.content:
            if block.type == "text":
                texts.append(TextBlock(text=block.text))
            elif block.type == "tool_use":
                tools.append(ToolUseBlock(id=block.id, name=block.name, input=dict(block.input)))
        return texts, tools

    @staticmethod
    def _read_confidence(extracted: Any) -> str:
        if extracted is None:
            return "LOW"
        conf = getattr(extracted, "fix_confidence", None)
        if conf is None:
            return "LOW"
        return str(conf.value) if hasattr(conf, "value") else str(conf)


def _summarize_history(history: list[dict]) -> str:
    """Produce a readable transcript of the conversation for the extraction call."""
    lines: list[str] = []
    for msg in history:
        role = msg.get("role", "").upper()
        content = msg.get("content", "")
        if isinstance(content, str):
            lines.append(f"{role}: {content}")
        elif isinstance(content, list):
            for block in content:
                if not isinstance(block, dict):
                    continue
                btype = block.get("type", "")
                if btype == "text":
                    lines.append(f"{role}: {block['text']}")
                elif btype == "tool_use":
                    lines.append(
                        f"TOOL CALL [{block['name']}]: "
                        f"{json.dumps(block.get('input', {}))}"
                    )
                elif btype == "tool_result":
                    snippet = str(block.get("content", ""))[:500]
                    err = " [ERROR]" if block.get("is_error") else ""
                    lines.append(f"TOOL RESULT{err}: {snippet}")
    return "\n".join(lines)
