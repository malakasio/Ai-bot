"""
Base agent class with:
- Full agentic tool-use loop (v6 fix)
- Action evaluation/scoring
- Task state checkpointing
- Failure handling matrix (from v3 blueprint)
- Self-improvement proposal generation
"""
from __future__ import annotations

import asyncio
import json
import time
import uuid
from dataclasses import dataclass, field, asdict
from typing import Any, Callable, Optional

from jarvis.config import get_config
from jarvis.llm.client import run_agent, simple_completion
from jarvis.llm.router import route, TASK_TOOL_SETS

_CONVERSATIONAL_WORDS = frozenset([
    "γεια", "hello", "hi", "hey", "sup", "ok", "okay", "ωραία", "ωραιο",
    "καλημέρα", "καλησπέρα", "καληνύχτα", "good", "morning", "night",
    "ευχαριστώ", "ευχαριστω", "thanks", "thank", "παρακαλώ",
    "bye", "αντιο", "αντίο", "ναι", "όχι", "yes", "no", "sure",
    "σωστό", "σωστα", "τέλεια", "εντάξει", "οκ",
    # Social inquiry words (how are you, what time, what do you do)
    "κάνεις", "είσαι", "ώρα", "πώς", "howdy",
])

# Words that mean the user wants an ACTION — always needs tools
_ACTION_WORDS = frozenset([
    "ψάξε", "βρες", "search", "find", "τρέξε", "run", "execute",
    "δες", "look", "check", "πες", "tell", "εξήγησε", "explain",
    "γράψε", "write", "φτιάξε", "make", "create", "δημιούργησε",
    "υπολόγισε", "calculate", "compute", "ανάλυσε", "αναλύσε", "analyze",
    "μεταφρασε", "translate", "σύνδεσε", "connect", "άνοιξε", "open",
    "κλείσε", "close", "κατέβασε", "download", "ανέβασε", "upload",
    "εγκατέστησε", "install", "ρώτα", "ask", "στείλε", "send",
    "δείξε", "show", "πρόβλεψε", "predict", "σύγκρινε", "compare",
])


def _is_conversational(text: str) -> bool:
    """True only for pure social messages (greetings, acks) that need no tools."""
    t = text.lower().strip()
    words = set(t.split())
    # Never conversational if action words present
    if words & _ACTION_WORDS:
        return False
    # Must be a known social word OR very short (≤8 chars, no digits)
    if len(t) <= 8 and not any(c.isdigit() for c in t):
        return True
    return bool(words & _CONVERSATIONAL_WORDS)


_DEFAULT_SYSTEM_PROMPT = """\
You are JARVIS — an autonomous AI assistant with persistent memory, tools, and 24/7 background processes.

You are the user's personal digital executive. You don't just answer questions — you think, decide, and act.

Core directives:
- Be direct and concise. Never more words than necessary.
- Use tools proactively when they improve the answer. Don't ask — just do.
- Search memory before answering factual questions about the user or past conversations.
- Run code to verify calculations, process data, or test hypotheses.
- Browse the web for current information when your knowledge may be outdated.
- Always respond in the same language as the user.
- Save important information, decisions, and user preferences to memory automatically.
- NEVER output XML, tool-call syntax, or JSON in your visible response text.

You have full access to: web search, web browsing, Python execution, file operations, and memory.
The user's conversation history is injected above when available.\
"""
from jarvis.memory.database import db_write, db_fetch_one
from jarvis.memory.store import save_memory, load_procedural_memory, propose_skill_update
from jarvis.observability.logger import get_logger, get_audit
from jarvis.observability.metrics import get_metrics
from jarvis.security.rollback import create_rollback_point

log = get_logger("agent")


@dataclass
class TaskState:
    """v6 fix: custom TaskState (asyncio.Task has no .checkpoint())"""
    task_id: str
    step: int = 0
    data: dict = field(default_factory=dict)
    status: str = "running"

    async def save(self):
        from jarvis.memory.database import db_write
        await db_write(
            "INSERT OR REPLACE INTO checkpoints (task_id, state_json) VALUES (?,?)",
            (self.task_id, json.dumps(asdict(self))),
        )

    @classmethod
    async def load(cls, task_id: str) -> Optional["TaskState"]:
        from jarvis.memory.database import db_fetch_one
        row = await db_fetch_one(
            "SELECT state_json FROM checkpoints WHERE task_id=?",
            (task_id,),
        )
        if row:
            return cls(**json.loads(row["state_json"]))
        return None


@dataclass
class ActionResult:
    success: bool
    output: str
    score: float = 0.0
    error: str | None = None
    tokens_used: int = 0
    duration_ms: float = 0.0


class BaseAgent:
    """
    Base agent with tool execution, evaluation, and self-improvement.
    """

    def __init__(
        self,
        agent_id: str | None = None,
        name: str = "jarvis",
        system_prompt: str | None = None,
    ):
        self.agent_id = agent_id or str(uuid.uuid4())[:8]
        self.name = name
        self._system_prompt = system_prompt
        self._tools: list[dict] = []
        self._tool_handlers: dict[str, Callable] = {}
        self._skill_cache: dict[str, str] = {}
        self._initialized = False
        self.prior_messages: list[dict] = []  # injected session history

    async def initialize(self):
        """Load system prompt and skills."""
        if self._initialized:
            return

        if self._system_prompt is None:
            from pathlib import Path
            from jarvis.config import JARVIS_HOME
            import os

            # Priority: env var > JARVIS_HOME/system.md > JARVIS_HOME/CLAUDE.md > default
            # When Claude API is active, use CLAUDE.md as the actual system prompt
            # (blueprint: "CLAUDE.md = DNA of the agent — loaded once at startup")
            if os.environ.get("JARVIS_SYSTEM_PROMPT"):
                self._system_prompt = os.environ["JARVIS_SYSTEM_PROMPT"]
            elif (JARVIS_HOME / "system.md").exists():
                self._system_prompt = (JARVIS_HOME / "system.md").read_text().strip()
            elif Path("CLAUDE.md").exists() and get_config().llm.has_anthropic:
                # With Claude Opus/Sonnet, CLAUDE.md fits in context — use it fully
                self._system_prompt = Path("CLAUDE.md").read_text().strip()
                log.info("System prompt loaded from CLAUDE.md (Claude API active)")
            else:
                self._system_prompt = _DEFAULT_SYSTEM_PROMPT

        # Load top-3 most relevant SKILL.md rules (keep short to save tokens)
        self._skill_cache = load_procedural_memory()
        if self._skill_cache:
            skill_text = "\n".join(
                f"[{name}]: {content.split(chr(10))[0][:120]}"
                for name, content in list(self._skill_cache.items())[:3]
            )
            self._system_prompt += f"\n\nActive skills: {skill_text}"

        self._initialized = True
        log.info(f"Agent {self.agent_id} initialized ({len(self._skill_cache)} skills loaded)")

    def register_tool(self, name: str, description: str, input_schema: dict, handler: Callable):
        """Register a tool for this agent."""
        self._tools.append({
            "name": name,
            "description": description,
            "input_schema": input_schema,
        })
        self._tool_handlers[name] = handler

    async def _execute_tool(self, tool_name: str, args: dict) -> Any:
        """Execute a registered tool."""
        handler = self._tool_handlers.get(tool_name)
        if not handler:
            return f"[Unknown tool: {tool_name}]"
        try:
            result = await handler(**args) if asyncio.iscoroutinefunction(handler) else handler(**args)
            return result
        except Exception as e:
            log.error(f"Tool {tool_name} failed: {e}")
            return f"[Tool error: {e}]"

    async def run_task(
        self,
        task: str,
        task_id: str | None = None,
        context: dict | None = None,
        max_retries: int = 3,
    ) -> ActionResult:
        """
        Run a task with full agentic loop, evaluation, and retry.
        """
        if not self._initialized:
            await self.initialize()

        task_id = task_id or str(uuid.uuid4())
        metrics = get_metrics()
        start_ts = time.time()

        # Determine routing
        decision = route(task)
        tool_set = TASK_TOOL_SETS.get(decision.task_type, [])

        # Don't pass tools for conversational messages — model uses them blindly
        # even for greetings, causing XML hallucinations and unnecessary latency
        if tool_set and not _is_conversational(task):
            active_tools = [t for t in self._tools if t["name"] in tool_set]
        else:
            active_tools = []  # pure conversation: no tools needed

        # Run memory search + task DB write in PARALLEL — not sequential
        from jarvis.memory.store import search_memories, inject_time_context

        # Claude has 1M context — use more memories for richer context
        memory_top_k = 20 if get_config().llm.has_anthropic else 5
        memories_task = asyncio.create_task(search_memories(task, top_k=memory_top_k))
        db_task = asyncio.create_task(db_write(
            """INSERT INTO tasks (id, task_type, payload, status, agent_id)
               VALUES (?,?,?,?,?)
               ON CONFLICT(id) DO UPDATE SET
                 status='running', agent_id=excluded.agent_id, started_at=unixepoch('now')""",
            (task_id, decision.task_type, task[:1000], "running", self.agent_id),
        ))

        memories, _ = await asyncio.gather(memories_task, db_task)
        memory_context = ""
        if memories:
            # Claude: inject up to 20 memories with full content; others: 3 truncated
            limit = len(memories) if get_config().llm.has_anthropic else 3
            trunc = 500 if get_config().llm.has_anthropic else 200
            memory_items = "\n".join(
                f"- [{m['time_human']}] {m['content'][:trunc]}" for m in memories[:limit]
            )
            memory_context = f"\n\nRelevant memories ({len(memories[:limit])} found):\n{memory_items}"

        enriched_task = inject_time_context(task) + memory_context

        # Resume from checkpoint if available
        task_state = await TaskState.load(task_id) or TaskState(task_id=task_id, data={"task": task})
        await task_state.save()

        last_error = None
        for attempt in range(max_retries):
            try:
                # Create rollback point before mutating actions
                if decision.task_type in ("code_generation", "system_mgmt", "architecture"):
                    await create_rollback_point(f"pre-task-{task_id[:8]}")

                from jarvis.memory.store import trim_context
                raw_messages = self.prior_messages + [{"role": "user", "content": enriched_task}]
                max_tok = get_config().memory.max_working_memory_tokens
                messages = trim_context(raw_messages, self._system_prompt, max_tok)

                text, usage = await run_agent(
                    messages=messages,
                    tools=active_tools,
                    system=self._system_prompt,
                    decision=decision,
                    tool_executor=self._execute_tool,
                    max_iterations=get_config().llm.max_iterations,
                )

                duration_ms = (time.time() - start_ts) * 1000

                # Never return blank — provide a fallback response
                if not text or not text.strip():
                    text = "Κατάλαβα. Πώς μπορώ να βοηθήσω;"

                # Evaluate output quality
                score = await self._evaluate_output(task, text, decision.task_type)

                # Fire-and-forget post-task writes — don't block the response
                async def _post_task():
                    try:
                        await db_write(
                            "UPDATE tasks SET status='completed', result=?, score=?, finished_at=? WHERE id=?",
                            (text[:5000], score, time.time(), task_id),
                        )
                        await save_memory(
                            content=f"Task: {task[:200]}\nResult: {text[:300]}\nScore: {score}",
                            memory_type="episodic",
                            importance=min(1.0, score / 100),
                            tags=[decision.task_type, self.agent_id],
                        )
                        if score < 70:
                            await self._propose_improvement(task, text, score, decision.task_type)
                    except Exception as e:
                        log.warning(f"Post-task write failed: {e}")

                asyncio.create_task(_post_task())

                metrics.tasks_total.inc()
                metrics.tasks_success.inc()
                metrics.task_score.observe(score)

                result = ActionResult(
                    success=True,
                    output=text,
                    score=score,
                    tokens_used=usage.get("input_tokens", 0) + usage.get("output_tokens", 0),
                    duration_ms=duration_ms,
                )

                get_audit().log_action(
                    tool=f"agent:{decision.task_type}",
                    input_data=task[:200],
                    output=text[:500],
                    success=True,
                    duration_ms=duration_ms,
                    score=score,
                    model_used=decision.model,
                    tokens_used=result.tokens_used,
                )

                return result

            except Exception as e:
                last_error = str(e)
                log.warning(f"Task attempt {attempt + 1} failed: {e}")
                task_state.step = attempt + 1
                task_state.data["last_error"] = last_error
                await task_state.save()

                if attempt < max_retries - 1:
                    await asyncio.sleep(2 ** attempt)  # exponential backoff

        # All retries failed
        await db_write(
            "UPDATE tasks SET status='failed', error=?, finished_at=? WHERE id=?",
            (last_error, time.time(), task_id),
        )
        metrics.tasks_failed.inc()
        metrics.record_error()

        return ActionResult(
            success=False,
            output="",
            error=last_error,
            duration_ms=(time.time() - start_ts) * 1000,
        )

    # Failure modes tracked by the self-improvement loop
    FAILURE_MODES = {
        "hallucination": ["I think", "I believe", "probably", "might be", "not sure"],
        "tool_failure": ["[TOOL ERROR", "[ERROR:", "[BLOCKED:", "[TIMEOUT:"],
        "incomplete": ["...", "to be continued", "in progress", "TODO"],
        "off_topic": [],  # detected by LLM evaluation
    }

    async def _evaluate_output(self, task: str, output: str, task_type: str) -> float:
        """
        Evaluate output quality (0-100). Heuristic first, LLM validation for borderline cases.
        Identifies failure modes: hallucination, tool_failure, incomplete, off_topic.
        """
        if not output.strip():
            return 0.0
        if len(output) < 10:
            return 20.0

        score = 50.0
        detected_modes: list[str] = []

        # Tool failures are hard failures
        if any(kw in output for kw in self.FAILURE_MODES["tool_failure"]):
            score -= 20
            detected_modes.append("tool_failure")

        # Length appropriateness
        if 30 < len(output) < 12000:
            score += 15

        # No error keywords  
        if not any(kw.lower() in output.lower() for kw in ["failed to", "exception", "traceback"]):
            score += 10

        # Completeness
        if output.strip()[-1] in ".!?»\n":
            score += 10
        else:
            detected_modes.append("incomplete")

        # Uncertainty language → possible hallucination
        lower = output.lower()
        if sum(1 for w in self.FAILURE_MODES["hallucination"] if w in lower) >= 3:
            score -= 10
            detected_modes.append("hallucination")

        # Task-specific bonuses
        if task_type in ("code_generation", "code_review") and ("```" in output or "def " in output):
            score += 10
        if task_type == "analysis" and len(output) > 200:
            score += 5

        score = min(100.0, max(0.0, score))

        # LLM micro-evaluation — disabled by default to avoid double API call latency.
        # Enable with JARVIS_EVAL_LLM=true (adds ~300ms per message).
        import os
        if (os.environ.get("JARVIS_EVAL_LLM") == "true"
                and 35 < score < 75
                and task_type not in ("simple_qa", "voice", "notification")):
            try:
                from jarvis.llm.client import simple_completion
                eval_prompt = (
                    f"Task: {task[:150]}\n"
                    f"Response: {output[:300]}\n"
                    "Rate this response 0-100. Reply ONLY with a number."
                )
                rating_str = await simple_completion(eval_prompt, task_type="simple_qa")
                import re
                m = re.search(r"\b(\d{1,3})\b", rating_str)
                if m:
                    llm_score = float(m.group(1))
                    score = score * 0.6 + llm_score * 0.4
            except Exception:
                pass

        self._last_failure_modes = detected_modes
        return min(100.0, max(0.0, score))

    _last_failure_modes: list[str] = []

    async def _propose_improvement(self, task: str, output: str, score: float, task_type: str):
        """
        Self-improvement Level 1 (blueprint):
        - L1: SKILL.md append-only, automatic, no human approval needed
        - L2: Proposals to DB for human review (more complex changes)
        """
        modes = ", ".join(self._last_failure_modes) if self._last_failure_modes else "quality below threshold"

        # L1: Auto-apply to SKILL.md — only safe, append-only rule additions
        if score < 50 or "tool_failure" in self._last_failure_modes:
            await self._auto_update_skill(task_type, task, modes, score)

        # L2: Store in DB for human review (complex improvements)
        proposal = (
            f"Score: {score:.0f}/100 | Failure modes: {modes}\n"
            f"Task: {task[:200]}\n"
            f"Output (truncated): {output[:300]}\n\n"
            f"Suggested: Add guidance for {task_type} tasks to avoid: {modes}."
        )
        await propose_skill_update(task_type, proposal)
        log.info(f"Improvement L1+L2 for {task_type}: score={score:.0f}, modes={modes}")

    async def _auto_update_skill(self, task_type: str, task: str, modes: str, score: float):
        """L1 self-improvement: append a lesson learned to the SKILL.md file directly."""
        from pathlib import Path
        import time as _time
        skill_file = Path(f".claude/skills/{task_type}/SKILL.md")
        try:
            skill_file.parent.mkdir(parents=True, exist_ok=True)
            timestamp = _time.strftime("%Y-%m-%d %H:%M")
            lesson = (
                f"\n\n## Auto-learned {timestamp} (score={score:.0f})\n"
                f"- Task pattern: {task[:100]}\n"
                f"- Issue: {modes}\n"
                f"- Rule: Be more careful with {task_type} tasks involving these patterns.\n"
            )
            with open(skill_file, "a") as f:
                f.write(lesson)
            log.info(f"L1: Auto-updated {skill_file}")
        except Exception as e:
            log.warning(f"L1 skill update failed: {e}")
