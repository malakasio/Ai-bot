"""Telegram Reporting Bot for God Mode.

Sends automated notifications about task progress, validation results,
and completion status to the user via Telegram.

Notification Types:
- Task started
- Progress updates (every 5 min or phase completion)
- Validation results
- Task completion (success/failure)
- Manual intervention required
"""
from __future__ import annotations

import os
from datetime import datetime
from typing import Optional
from uuid import UUID


class TelegramReporter:
    """Sends God Mode notifications via Telegram."""

    def __init__(self, bot_token: Optional[str] = None, user_id: Optional[str] = None):
        self.bot_token = bot_token or os.getenv("TELEGRAM_BOT_TOKEN")
        self.user_id = user_id or os.getenv("TELEGRAM_USER_ID")
        self.enabled = bool(self.bot_token and self.user_id)

    async def send_message(self, text: str, parse_mode: str = "Markdown") -> bool:
        """Send a message to the user via Telegram."""
        if not self.enabled:
            print(f"[telegram] Notifications disabled (no token/user_id)")
            return False

        try:
            import httpx

            url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
            data = {
                "chat_id": self.user_id,
                "text": text,
                "parse_mode": parse_mode,
                "disable_web_page_preview": True
            }

            async with httpx.AsyncClient() as client:
                response = await client.post(url, json=data, timeout=10.0)

            if response.status_code == 200:
                return True
            else:
                print(f"[telegram] Failed to send: {response.status_code} {response.text}")
                return False

        except Exception as e:
            print(f"[telegram] Error sending message: {e}")
            return False

    async def notify_task_started(
        self,
        task_id: UUID,
        title: str,
        agent_id: str,
        branch: str,
        plan: dict
    ) -> bool:
        """Notify that a God Mode task has started."""
        phases = plan.get("phases", [])
        phase_list = "\n".join(
            f" {i+1}. {phase['name']}"
            for i, phase in enumerate(phases[:5])  # Max 5 phases in notification
        )

        if len(phases) > 5:
            phase_list += f"\n ... and {len(phases) - 5} more phases"

        estimated_min = sum(p.get("estimated_duration_ms", 0) for p in phases) // 60000

        text = f"""🚀 *God Mode Task Started*

*Task:* {title}
*ID:* `{task_id}`
*Agent:* `{agent_id}`
*Branch:* `{branch}`

*Plan ({len(phases)} phases):*
{phase_list}

*Estimated:* {estimated_min}-{estimated_min + 5} minutes

_Next update in 5 minutes..._
"""
        return await self.send_message(text)

    async def notify_progress(
        self,
        task_id: UUID,
        title: str,
        current_phase: int,
        total_phases: int,
        progress_pct: int,
        tests_passed: Optional[int] = None,
        tests_total: Optional[int] = None,
        score: Optional[int] = None
    ) -> bool:
        """Notify about task progress."""
        # Phase status
        phase_status = []
        for i in range(total_phases):
            if i < current_phase:
                phase_status.append(f"Phase {i+1}/{total_phases}: ✅ Complete")
            elif i == current_phase:
                phase_status.append(f"Phase {i+1}/{total_phases}: 🔄 In progress ({progress_pct}%)")
            else:
                phase_status.append(f"Phase {i+1}/{total_phases}: ⏸️ Pending")

        phase_text = "\n".join(phase_status[:3])  # Show max 3 phases
        if total_phases > 3:
            phase_text += f"\n... and {total_phases - 3} more"

        # Test status
        test_text = ""
        if tests_passed is not None and tests_total is not None:
            test_text = f"\n*Tests:* {tests_passed}/{tests_total} passing"

        # Score
        score_text = ""
        if score is not None:
            score_text = f"\n*Score:* {score}/100"

        text = f"""⏳ *Task Progress*

*Task:* {title}
*ID:* `{task_id}`

{phase_text}{test_text}{score_text}

_Next update in 5 minutes..._
"""
        return await self.send_message(text)

    async def notify_validation_results(
        self,
        task_id: UUID,
        title: str,
        score: int,
        validation_results: dict,
        accepted: bool
    ) -> bool:
        """Notify about validation results."""
        results = validation_results or {}

        # Build status lines
        status_lines = []

        if "syntax" in results:
            status = "✅ PASS" if results["syntax"].get("passed") else "❌ FAIL"
            status_lines.append(f"{status} Syntax check")

        if "tests" in results:
            tests = results["tests"]
            passed = tests.get("passed", 0)
            total = tests.get("total", 0)
            status = "✅ PASS" if passed == total else f"⚠️  {passed}/{total} PASS"
            status_lines.append(f"{status} Unit tests")

            # Show failed tests
            if passed < total and "failed" in tests:
                for test in tests["failed"][:3]:  # Max 3 failed tests
                    status_lines.append(f"   - {test}")

        if "security" in results:
            status = "✅ PASS" if results["security"].get("passed") else "❌ FAIL"
            status_lines.append(f"{status} Security scan")

        if "secrets" in results:
            status = "✅ PASS" if results["secrets"].get("passed") else "❌ FAIL"
            status_lines.append(f"{status} No secrets leaked")

        status_text = "\n".join(status_lines)

        # Acceptance status
        if accepted:
            acceptance = f"*Score:* {score}/100 ✅ *ACCEPTED*"
        else:
            acceptance = f"*Score:* {score}/100 ❌ *REJECTED*\n\n_Attempting auto-fix..._"

        text = f"""🧪 *Validation Results*

*Task:* {title}
*ID:* `{task_id}`

{status_text}

{acceptance}
"""
        return await self.send_message(text)

    async def notify_task_complete(
        self,
        task_id: UUID,
        title: str,
        score: int,
        duration_ms: int,
        tests_passed: int,
        tests_total: int,
        commits: list[str],
        files_changed: list[str],
        merge_commit: Optional[str] = None
    ) -> bool:
        """Notify that a task completed successfully."""
        duration_min = duration_ms // 60000
        duration_sec = (duration_ms % 60000) // 1000

        commit_text = f"{len(commits)} commit{'s' if len(commits) != 1 else ''}"
        files_text = f"{len(files_changed)} file{'s' if len(files_changed) != 1 else ''}"

        merge_text = ""
        if merge_commit:
            merge_text = f"\n\n*Changes merged to main* ✅\n*Commit:* `{merge_commit[:8]}`"

        text = f"""✅ *Task Complete!*

*Task:* {title}
*ID:* `{task_id}`

*Final Score:* {score}/100
*Duration:* {duration_min}m {duration_sec}s
*Tests:* {tests_passed}/{tests_total} passing
*Commits:* {commit_text}
*Files changed:* {files_text}{merge_text}

*Worktree cleaned up* ✅
"""
        return await self.send_message(text)

    async def notify_task_failed(
        self,
        task_id: UUID,
        title: str,
        score: int,
        attempts: int,
        max_attempts: int,
        error: str,
        validation_results: Optional[dict] = None
    ) -> bool:
        """Notify that a task failed after max retries."""
        # Extract key issues
        issues = []

        if validation_results:
            if "tests" in validation_results:
                failed = validation_results["tests"].get("failed", [])
                if failed:
                    issues.append(f"- {len(failed)} tests failing")

            if "security" in validation_results:
                if not validation_results["security"].get("passed"):
                    issues.append("- Security issues detected")

            if "performance" in validation_results:
                perf = validation_results["performance"]
                if perf.get("slower_than_baseline"):
                    issues.append(f"- Performance: {perf.get('slowdown', 'N/A')}x slower")

        issues_text = "\n".join(issues) if issues else f"- {error[:100]}"

        text = f"""❌ *Task Failed*

*Task:* {title}
*ID:* `{task_id}`

*Score:* {score}/100 (below threshold)
*Attempts:* {attempts}/{max_attempts}

*Issues:*
{issues_text}

*Manual intervention required.*

_Agent logs saved to:_
`/root/Ai-bot/logs/agent-{task_id}.log`
"""
        return await self.send_message(text)

    async def notify_manual_intervention(
        self,
        task_id: UUID,
        title: str,
        reason: str,
        action_required: str
    ) -> bool:
        """Notify that manual intervention is required."""
        text = f"""⚠️ *Manual Intervention Required*

*Task:* {title}
*ID:* `{task_id}`

*Reason:* {reason}

*Action Required:*
{action_required}

_Task paused until resolved._
"""
        return await self.send_message(text)


# Singleton instance
_reporter: Optional[TelegramReporter] = None


def get_reporter() -> TelegramReporter:
    """Get the global TelegramReporter instance."""
    global _reporter
    if _reporter is None:
        _reporter = TelegramReporter()
    return _reporter
