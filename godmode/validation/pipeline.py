"""Validation Pipeline for God Mode Agent Outputs.

Validates agent work across multiple dimensions:
- Syntax: ruff (Python), tsc (TypeScript), shellcheck (Bash)
- Tests: pytest, jest, or project-specific test runner
- Security: bandit (Python), git-secrets, custom patterns
- Logs: error/warning analysis
- Quality: composite score 0-100

Scoring breakdown:
- Correctness (0-40): Tests pass, no syntax errors
- Completeness (0-30): All requirements met, edge cases handled
- Efficiency (0-20): Performance acceptable, no obvious waste
- Safety (0-10): No security issues, follows best practices

Self-healing:
- Score < 70: Reject with specific feedback
- Score 70-85: Accept with improvement notes
- Score > 85: Accept
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional
from uuid import UUID


@dataclass
class ValidationResult:
    """Result of validation pipeline."""

    score: int  # 0-100
    passed: bool  # score >= 70

    # Breakdown
    correctness_score: int  # 0-40
    completeness_score: int  # 0-30
    efficiency_score: int  # 0-20
    safety_score: int  # 0-10

    # Detailed results
    syntax: dict  # {passed, errors: [{file, line, message}]}
    tests: dict  # {passed, total, failed: [test_names], output}
    security: dict  # {passed, issues: [{severity, file, line, message}]}
    logs: dict  # {errors, warnings, patterns: [{level, message, count}]}

    # Feedback for agent
    feedback: list[str] = field(default_factory=list)

    # Metadata
    duration_ms: int = 0
    timestamp: Optional[str] = None


class ValidationPipeline:
    """Validates agent outputs with self-healing feedback."""

    def __init__(self, worktree_path: Path):
        self.worktree_path = worktree_path
        self.project_root = worktree_path

    async def validate(
        self,
        task_id: UUID,
        files_changed: list[str],
        commits: list[str],
        requirements: Optional[dict] = None,
    ) -> ValidationResult:
        """
        Run full validation pipeline.

        Args:
            task_id: Task UUID
            files_changed: List of modified file paths
            commits: List of commit SHAs
            requirements: Optional dict with expected outcomes

        Returns:
            ValidationResult with score and detailed breakdown
        """
        import time

        start_ms = int(time.time() * 1000)

        # Run all checks in parallel
        syntax_task = asyncio.create_task(self._check_syntax(files_changed))
        tests_task = asyncio.create_task(self._run_tests())
        security_task = asyncio.create_task(self._check_security(files_changed))
        logs_task = asyncio.create_task(self._analyze_logs())

        syntax_result = await syntax_task
        tests_result = await tests_task
        security_result = await security_task
        logs_result = await logs_task

        # Calculate scores
        correctness_score = self._score_correctness(syntax_result, tests_result)
        completeness_score = self._score_completeness(tests_result, files_changed, requirements)
        efficiency_score = self._score_efficiency(logs_result, tests_result)
        safety_score = self._score_safety(security_result, logs_result)

        total_score = correctness_score + completeness_score + efficiency_score + safety_score

        # Generate feedback
        feedback = self._generate_feedback(
            syntax_result,
            tests_result,
            security_result,
            logs_result,
            correctness_score,
            completeness_score,
            efficiency_score,
            safety_score,
        )

        duration_ms = int(time.time() * 1000) - start_ms

        return ValidationResult(
            score=total_score,
            passed=total_score >= 70,
            correctness_score=correctness_score,
            completeness_score=completeness_score,
            efficiency_score=efficiency_score,
            safety_score=safety_score,
            syntax=syntax_result,
            tests=tests_result,
            security=security_result,
            logs=logs_result,
            feedback=feedback,
            duration_ms=duration_ms,
            timestamp=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        )

    async def _check_syntax(self, files_changed: list[str]) -> dict:
        """Check syntax for all changed files."""
        errors = []

        # Group files by type
        py_files = [f for f in files_changed if f.endswith(".py")]
        ts_files = [f for f in files_changed if f.endswith((".ts", ".tsx"))]
        sh_files = [f for f in files_changed if f.endswith(".sh")]

        # Check Python with ruff
        if py_files:
            for py_file in py_files:
                file_path = self.worktree_path / py_file
                if not file_path.exists():
                    continue

                proc = await asyncio.create_subprocess_exec(
                    "ruff",
                    "check",
                    str(file_path),
                    cwd=self.worktree_path,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                stdout, stderr = await proc.communicate()

                if proc.returncode != 0:
                    # Parse ruff output
                    for line in stdout.decode().split("\n"):
                        if ":" in line and py_file in line:
                            errors.append(
                                {
                                    "file": py_file,
                                    "line": self._extract_line_number(line),
                                    "message": line.strip(),
                                }
                            )

        # Check TypeScript with tsc
        if ts_files:
            tsconfig = self.worktree_path / "tsconfig.json"
            if tsconfig.exists():
                proc = await asyncio.create_subprocess_exec(
                    "tsc",
                    "--noEmit",
                    cwd=self.worktree_path,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                stdout, stderr = await proc.communicate()

                if proc.returncode != 0:
                    for line in stdout.decode().split("\n"):
                        for ts_file in ts_files:
                            if ts_file in line:
                                errors.append(
                                    {
                                        "file": ts_file,
                                        "line": self._extract_line_number(line),
                                        "message": line.strip(),
                                    }
                                )
                                break

        # Check shell scripts with shellcheck
        if sh_files:
            for sh_file in sh_files:
                file_path = self.worktree_path / sh_file
                if not file_path.exists():
                    continue

                proc = await asyncio.create_subprocess_exec(
                    "shellcheck",
                    "-f",
                    "json",
                    str(file_path),
                    cwd=self.worktree_path,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                stdout, stderr = await proc.communicate()

                if proc.returncode != 0 and stdout:
                    try:
                        issues = json.loads(stdout.decode())
                        for issue in issues:
                            errors.append(
                                {
                                    "file": sh_file,
                                    "line": issue.get("line", 0),
                                    "message": issue.get("message", ""),
                                }
                            )
                    except json.JSONDecodeError:
                        pass

        return {
            "passed": len(errors) == 0,
            "errors": errors,
            "files_checked": len(py_files) + len(ts_files) + len(sh_files),
        }

    async def _run_tests(self) -> dict:
        """Run project test suite."""
        # Detect test framework
        if (self.worktree_path / "pytest.ini").exists() or (
            self.worktree_path / "pyproject.toml"
        ).exists():
            return await self._run_pytest()
        elif (self.worktree_path / "package.json").exists():
            return await self._run_jest()
        else:
            # No tests configured
            return {
                "passed": 0,
                "total": 0,
                "failed": [],
                "output": "No test framework detected",
                "skipped": True,
            }

    async def _run_pytest(self) -> dict:
        """Run pytest and parse results."""
        proc = await asyncio.create_subprocess_exec(
            "pytest",
            "-v",
            "--tb=short",
            "--color=no",
            cwd=self.worktree_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        )
        stdout, stderr = await proc.communicate()

        output = stdout.decode() + stderr.decode()

        # Parse pytest output
        passed = 0
        failed = []
        total = 0

        for line in output.split("\n"):
            if " PASSED" in line:
                passed += 1
                total += 1
            elif " FAILED" in line:
                total += 1
                # Extract test name
                test_name = line.split(" FAILED")[0].strip()
                failed.append(test_name)

        # Also check summary line
        summary_match = re.search(r"(\d+) passed", output)
        if summary_match:
            passed = int(summary_match.group(1))

        failed_match = re.search(r"(\d+) failed", output)
        if failed_match:
            failed_count = int(failed_match.group(1))
            total = passed + failed_count

        return {
            "passed": passed,
            "total": total,
            "failed": failed,
            "output": output[-2000:],  # Last 2000 chars
            "skipped": False,
        }

    async def _run_jest(self) -> dict:
        """Run jest and parse results."""
        proc = await asyncio.create_subprocess_exec(
            "npm",
            "test",
            "--",
            "--ci",
            "--json",
            cwd=self.worktree_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()

        try:
            result = json.loads(stdout.decode())
            passed = result.get("numPassedTests", 0)
            total = result.get("numTotalTests", 0)
            failed = []

            for test_result in result.get("testResults", []):
                for test in test_result.get("assertionResults", []):
                    if test.get("status") == "failed":
                        failed.append(test.get("fullName", "unknown"))

            return {
                "passed": passed,
                "total": total,
                "failed": failed,
                "output": json.dumps(result, indent=2)[-2000:],
                "skipped": False,
            }
        except json.JSONDecodeError:
            return {
                "passed": 0,
                "total": 0,
                "failed": [],
                "output": stdout.decode()[-2000:],
                "skipped": False,
            }

    async def _check_security(self, files_changed: list[str]) -> dict:
        """Run security checks."""
        issues = []

        py_files = [f for f in files_changed if f.endswith(".py")]

        # Run bandit on Python files
        if py_files:
            for py_file in py_files:
                file_path = self.worktree_path / py_file
                if not file_path.exists():
                    continue

                proc = await asyncio.create_subprocess_exec(
                    "bandit",
                    "-f",
                    "json",
                    str(file_path),
                    cwd=self.worktree_path,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                stdout, stderr = await proc.communicate()

                try:
                    result = json.loads(stdout.decode())
                    for issue in result.get("results", []):
                        issues.append(
                            {
                                "severity": issue.get("issue_severity", "UNKNOWN"),
                                "file": py_file,
                                "line": issue.get("line_number", 0),
                                "message": issue.get("issue_text", ""),
                            }
                        )
                except json.JSONDecodeError:
                    pass

        # Check for common secret patterns
        secret_patterns = [
            (r'(?i)(api[_-]?key|apikey)\s*[:=]\s*["\']([^"\']+)["\']', "API key"),
            (r'(?i)(secret[_-]?key|secretkey)\s*[:=]\s*["\']([^"\']+)["\']', "Secret key"),
            (r'(?i)(password|passwd|pwd)\s*[:=]\s*["\']([^"\']+)["\']', "Password"),
            (r'(?i)(token)\s*[:=]\s*["\']([^"\']+)["\']', "Token"),
            (r"-----BEGIN (RSA |DSA )?PRIVATE KEY-----", "Private key"),
        ]

        for file_path_str in files_changed:
            file_path = self.worktree_path / file_path_str
            if not file_path.exists() or file_path.suffix in [".pyc", ".so", ".o"]:
                continue

            try:
                content = file_path.read_text()
                for pattern, secret_type in secret_patterns:
                    matches = re.finditer(pattern, content)
                    for match in matches:
                        # Skip if in .env.example or comments
                        if ".example" in file_path_str or file_path_str.startswith("#"):
                            continue

                        line_num = content[: match.start()].count("\n") + 1
                        issues.append(
                            {
                                "severity": "HIGH",
                                "file": file_path_str,
                                "line": line_num,
                                "message": f"Possible {secret_type} in plaintext",
                            }
                        )
            except (UnicodeDecodeError, PermissionError):
                pass

        # Filter out LOW severity issues
        high_medium_issues = [i for i in issues if i["severity"] in ["HIGH", "MEDIUM"]]

        return {
            "passed": len(high_medium_issues) == 0,
            "issues": high_medium_issues,
            "total_scanned": len(files_changed),
        }

    async def _analyze_logs(self) -> dict:
        """Analyze git commit messages and any log files."""
        patterns = []

        # Check recent git log for error indicators
        proc = await asyncio.create_subprocess_exec(
            "git",
            "log",
            "-10",
            "--oneline",
            cwd=self.worktree_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()

        log_content = stdout.decode()

        # Count error/warning indicators in commit messages
        error_count = len(re.findall(r"(?i)(error|fail|broke|broken)", log_content))
        warning_count = len(re.findall(r"(?i)(warning|warn|todo|fixme)", log_content))

        if error_count > 0:
            patterns.append(
                {
                    "level": "error",
                    "message": "Error-related terms in commit messages",
                    "count": error_count,
                }
            )

        if warning_count > 0:
            patterns.append(
                {
                    "level": "warning",
                    "message": "Warning-related terms in commit messages",
                    "count": warning_count,
                }
            )

        return {"errors": error_count, "warnings": warning_count, "patterns": patterns}

    def _score_correctness(self, syntax: dict, tests: dict) -> int:
        """Score correctness (0-40)."""
        score = 40

        # Syntax errors: -20 points
        if not syntax["passed"]:
            penalty = min(20, len(syntax["errors"]) * 5)
            score -= penalty

        # Test failures: -20 points
        if not tests.get("skipped", False):
            total = tests.get("total", 0)
            passed = tests.get("passed", 0)

            if total > 0:
                pass_rate = passed / total
                score -= int((1.0 - pass_rate) * 20)

        return max(0, score)

    def _score_completeness(
        self, tests: dict, files_changed: list[str], requirements: Optional[dict]
    ) -> int:
        """Score completeness (0-30)."""
        score = 30

        # No tests written: -15 points
        if tests.get("skipped", False) or tests.get("total", 0) == 0:
            if any(f.endswith(".py") for f in files_changed):
                score -= 15

        # Check if requirements met (if provided)
        if requirements:
            expected_files = requirements.get("files", [])
            missing_files = [f for f in expected_files if f not in files_changed]
            if missing_files:
                score -= min(15, len(missing_files) * 5)

        return max(0, score)

    def _score_efficiency(self, logs: dict, tests: dict) -> int:
        """Score efficiency (0-20)."""
        score = 20

        # Warning patterns: -5 points
        if logs["warnings"] > 3:
            score -= 5

        # Too many test failures suggests inefficient approach: -10 points
        if not tests.get("skipped", False):
            failed_count = len(tests.get("failed", []))
            if failed_count > 5:
                score -= 10

        return max(0, score)

    def _score_safety(self, security: dict, logs: dict) -> int:
        """Score safety (0-10)."""
        score = 10

        # Security issues: -10 points
        if not security["passed"]:
            high_issues = [i for i in security["issues"] if i["severity"] == "HIGH"]
            if high_issues:
                score = 0
            else:
                score -= min(10, len(security["issues"]) * 3)

        # Error patterns in logs: -2 points
        if logs["errors"] > 2:
            score -= 2

        return max(0, score)

    def _generate_feedback(
        self,
        syntax: dict,
        tests: dict,
        security: dict,
        logs: dict,
        correctness: int,
        completeness: int,
        efficiency: int,
        safety: int,
    ) -> list[str]:
        """Generate actionable feedback for agent."""
        feedback = []

        # Correctness feedback
        if correctness < 30:
            if not syntax["passed"]:
                feedback.append(
                    f"Fix {len(syntax['errors'])} syntax error(s): "
                    + ", ".join(f"{e['file']}:{e['line']}" for e in syntax["errors"][:3])
                )

            if not tests.get("skipped", False):
                failed = tests.get("failed", [])
                if failed:
                    feedback.append(f"Fix {len(failed)} failing test(s): " + ", ".join(failed[:3]))

        # Completeness feedback
        if completeness < 20:
            if tests.get("skipped", False) or tests.get("total", 0) == 0:
                feedback.append("Add unit tests to verify functionality")

        # Efficiency feedback
        if efficiency < 15:
            if logs["warnings"] > 3:
                feedback.append("Address warnings in code/commits")

        # Safety feedback
        if safety < 7:
            if not security["passed"]:
                high_issues = [i for i in security["issues"] if i["severity"] == "HIGH"]
                if high_issues:
                    feedback.append(
                        f"CRITICAL: Fix {len(high_issues)} high-severity security issue(s): "
                        + ", ".join(f"{i['file']}:{i['line']}" for i in high_issues[:2])
                    )
                else:
                    feedback.append("Address security issues detected by scanner")

        if not feedback:
            feedback.append("All checks passed. Good work!")

        return feedback

    def _extract_line_number(self, line: str) -> int:
        """Extract line number from error message."""
        match = re.search(r":(\d+):", line)
        if match:
            return int(match.group(1))
        return 0
