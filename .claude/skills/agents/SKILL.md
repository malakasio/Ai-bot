Skill: Agent Orchestration

Purpose

Coordinate multi-agent workflows via the orchestrator sub-agent.

Sub-agent definitions in .claude/agents/:
- sentinel.yaml — system health monitoring
- architect.yaml — design, refactoring, deep analysis
- haiku.yaml — fast responses, notifications
- orchestrator.yaml — task dispatch & coordination

System hooks in .claude/hooks/:
- pre-tool-use.sh — security zone validation before every tool call
- post-tool-use.sh — episode logging, metrics, autoDream trigger
- stop.sh — clean shutdown (DB close, LiteLLM stop, state save)
- precompact.sh — working memory backup before context compaction
- notification.sh — Telegram alerts on critical events for complex, parallelizable tasks.

Topologies

1. Hierarchical (Sub-agents)

Orchestrator
├── Sub-agent A (isolated task)
├── Sub-agent B (isolated task)
└── Sub-agent C (isolated task)
Orchestrator assigns tasks, monitors progress, aggregates results
Sub-agents have no shared state
Orchestrator validates all outputs: "Do not rubber-stamp weak work"
2. Peer Team (Agent Teams)

Task Queue (PostgreSQL) ← shared
├── Agent 1 (pull & work)
├── Agent 2 (pull & work)
└── Agent 3 (pull & work)
Agents compete for tasks atomically (SELECT FOR UPDATE SKIP LOCKED)
No coordination needed between agents
Suitable for embarrassingly parallel workloads
3. Managed (Cloud)

Uses Anthropic's managed agent API
For tasks requiring >1h of compute
Evaluation Criteria (Coordinator Mode)

Score each sub-agent output on:

Correctness (0–40): Does it solve the actual problem?
Completeness (0–30): Are edge cases handled?
Efficiency (0–20): Is the approach optimal?
Safety (0–10): Does it follow security zone rules?
Score < 70: Reject and re-assign with specific feedback. Score 70–85: Accept with improvement notes. Score > 85: Accept.

Known Failure Modes

Sub-agent timeout: kill after 5 minutes, retry with smaller task scope
Circular dependency: detect with dependency graph, fail fast
Context overflow: split task into smaller chunks
