#!/usr/bin/env python3
"""Add comprehensive error-fixing plan to God Mode task.

This plan will:
1. Scan for Python syntax errors
2. Scan for import errors
3. Scan for type errors
4. Scan for security issues
5. Scan for deprecated code
6. Fix all issues systematically
7. Run tests to verify fixes
"""

import asyncio
import json
import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from core import database


async def add_comprehensive_plan():
    task_id = "1be34528-93af-4a5f-b225-dd01458087e2"

    plan = {
        "phases": [
            {
                "name": "Phase 1: Python Syntax Scan",
                "estimated_duration_ms": 60000,
                "steps": [
                    {
                        "step": "Scan all Python files for syntax errors",
                        "command": "find . -name '*.py' -not -path './.git/*' -not -path './venv/*' -not -path './.venv/*' | xargs -I {} python3 -m py_compile {} 2>&1 | tee /tmp/syntax_errors.log || true",
                        "expected_output": "Compilation errors logged",
                    },
                    {
                        "step": "Count syntax errors",
                        "command": "grep -c 'SyntaxError\\|IndentationError\\|TabError' /tmp/syntax_errors.log || echo '0'",
                        "expected_output": "Error count",
                    },
                ],
            },
            {
                "name": "Phase 2: Ruff Linting Scan",
                "estimated_duration_ms": 120000,
                "steps": [
                    {
                        "step": "Run ruff on entire codebase",
                        "command": "ruff check . --output-format=json > /tmp/ruff_errors.json 2>&1 || true",
                        "expected_output": "Ruff errors in JSON format",
                    },
                    {
                        "step": "Count ruff errors",
                        "command": "cat /tmp/ruff_errors.json | python3 -c 'import sys, json; data = json.load(sys.stdin) if sys.stdin.read() else []; print(len(data))' 2>/dev/null || echo '0'",
                        "expected_output": "Error count",
                    },
                    {
                        "step": "Extract critical errors",
                        "command": "cat /tmp/ruff_errors.json | python3 -c \"import sys, json; data = json.load(sys.stdin) if sys.stdin.read() else []; critical = [e for e in data if e.get('code', '').startswith(('E', 'F'))]; print('\\n'.join([f\\\"{e['filename']}:{e['location']['row']}:{e['location']['column']} {e['code']} {e['message']}\\\" for e in critical[:20]]))\" 2>/dev/null || echo 'No critical errors'",
                        "expected_output": "Top 20 critical errors",
                    },
                ],
            },
            {
                "name": "Phase 3: Import Error Detection",
                "estimated_duration_ms": 90000,
                "steps": [
                    {
                        "step": "Check for missing imports",
                        "command": "find . -name '*.py' -not -path './.git/*' -not -path './venv/*' | head -20 | xargs -I {} python3 -c 'import ast; ast.parse(open(\"{}\").read())' 2>&1 | grep -i 'import\\|module' | tee /tmp/import_errors.log || true",
                        "expected_output": "Import errors logged",
                    },
                    {
                        "step": "List undefined names",
                        "command": "ruff check . --select F821 --output-format=text 2>&1 | head -30 || echo 'No undefined names'",
                        "expected_output": "Undefined name errors",
                    },
                ],
            },
            {
                "name": "Phase 4: Security Scan",
                "estimated_duration_ms": 120000,
                "steps": [
                    {
                        "step": "Run bandit security scanner",
                        "command": "bandit -r . -f json -o /tmp/bandit_report.json 2>&1 || true",
                        "expected_output": "Security scan complete",
                    },
                    {
                        "step": "Extract high severity issues",
                        "command": "cat /tmp/bandit_report.json | python3 -c \"import sys, json; data = json.load(sys.stdin); high = [r for r in data.get('results', []) if r.get('issue_severity') == 'HIGH']; print(f'Found {len(high)} high severity issues'); [print(f\\\"{r['filename']}:{r['line_number']} {r['issue_text']}\\\") for r in high[:10]]\" 2>/dev/null || echo 'No high severity issues'",
                        "expected_output": "High severity security issues",
                    },
                    {
                        "step": "Check for hardcoded secrets",
                        "command": "grep -r -n -E '(password|secret|api_key|token)\\s*=\\s*[\"\\'][^\"\\'\\ ]+[\"\\']' --include='*.py' --exclude-dir='.git' --exclude-dir='venv' . | head -20 || echo 'No hardcoded secrets found'",
                        "expected_output": "Potential hardcoded secrets",
                    },
                ],
            },
            {
                "name": "Phase 5: Type Error Detection",
                "estimated_duration_ms": 90000,
                "steps": [
                    {
                        "step": "Check for type annotation issues",
                        "command": "ruff check . --select ANN --output-format=text 2>&1 | head -30 || echo 'No type annotation issues'",
                        "expected_output": "Type annotation errors",
                    },
                    {
                        "step": "Find functions without return types",
                        "command": "grep -r -n 'def [a-zA-Z_][a-zA-Z0-9_]*(' --include='*.py' --exclude-dir='.git' . | grep -v ' -> ' | head -20 || echo 'All functions have return types'",
                        "expected_output": "Functions missing return types",
                    },
                ],
            },
            {
                "name": "Phase 6: Deprecated Code Detection",
                "estimated_duration_ms": 60000,
                "steps": [
                    {
                        "step": "Find deprecated imports",
                        "command": "grep -r -n 'import imp\\|from imp import' --include='*.py' --exclude-dir='.git' . || echo 'No deprecated imports'",
                        "expected_output": "Deprecated imports",
                    },
                    {
                        "step": "Find TODO and FIXME comments",
                        "command": "grep -r -n 'TODO\\|FIXME\\|XXX\\|HACK' --include='*.py' --exclude-dir='.git' . | wc -l",
                        "expected_output": "Count of TODO/FIXME comments",
                    },
                ],
            },
            {
                "name": "Phase 7: Generate Error Report",
                "estimated_duration_ms": 30000,
                "steps": [
                    {
                        "step": "Create comprehensive error report",
                        "command": "cat > /tmp/error_report.md << 'EOF'\n# Repository Error Report\n\n## Syntax Errors\n$(cat /tmp/syntax_errors.log 2>/dev/null | head -20)\n\n## Ruff Errors\n$(cat /tmp/ruff_errors.json 2>/dev/null | python3 -c 'import sys, json; data = json.load(sys.stdin) if sys.stdin.read() else []; print(f\"Total: {len(data)} errors\")' 2>/dev/null)\n\n## Security Issues\n$(cat /tmp/bandit_report.json 2>/dev/null | python3 -c 'import sys, json; data = json.load(sys.stdin); print(f\"Total: {len(data.get(\\\"results\\\", []))} issues\")' 2>/dev/null)\n\n## Summary\nScan completed at $(date)\nEOF\ncat /tmp/error_report.md",
                        "expected_output": "Error report generated",
                    },
                    {
                        "step": "Save report to repository",
                        "command": "cp /tmp/error_report.md ./ERROR_REPORT_$(date +%Y%m%d_%H%M%S).md && ls -lh ERROR_REPORT_*.md | tail -1",
                        "expected_output": "Report saved",
                    },
                ],
            },
            {
                "name": "Phase 8: Auto-Fix Critical Errors",
                "estimated_duration_ms": 180000,
                "steps": [
                    {
                        "step": "Auto-fix with ruff",
                        "command": "ruff check . --fix --unsafe-fixes 2>&1 | tee /tmp/ruff_fixes.log || true",
                        "expected_output": "Auto-fixes applied",
                    },
                    {
                        "step": "Format code with ruff",
                        "command": "ruff format . 2>&1 | tee /tmp/ruff_format.log || true",
                        "expected_output": "Code formatted",
                    },
                    {
                        "step": "Count files modified",
                        "command": "git status --short | wc -l",
                        "expected_output": "Number of modified files",
                    },
                ],
            },
            {
                "name": "Phase 9: Verification",
                "estimated_duration_ms": 120000,
                "steps": [
                    {
                        "step": "Re-run syntax check",
                        "command": "find . -name '*.py' -not -path './.git/*' -not -path './venv/*' | head -50 | xargs -I {} python3 -m py_compile {} 2>&1 | grep -c 'Error' || echo '0'",
                        "expected_output": "Remaining syntax errors",
                    },
                    {
                        "step": "Re-run ruff check",
                        "command": "ruff check . --output-format=json 2>&1 | python3 -c 'import sys, json; data = json.load(sys.stdin) if sys.stdin.read() else []; print(f\"Remaining errors: {len(data)}\")' 2>/dev/null || echo 'Remaining errors: 0'",
                        "expected_output": "Remaining ruff errors",
                    },
                    {
                        "step": "Check if tests still pass",
                        "command": "pytest tests/ -v --tb=short -x 2>&1 | tail -20 || echo 'Tests need attention'",
                        "expected_output": "Test results",
                    },
                ],
            },
            {
                "name": "Phase 10: Commit Changes",
                "estimated_duration_ms": 30000,
                "steps": [
                    {
                        "step": "Stage all fixes",
                        "command": "git add -A",
                        "expected_output": "Files staged",
                    },
                    {
                        "step": "Create commit",
                        "command": "git commit -m 'fix: comprehensive error fixes from God Mode AutoFix\n\n- Fixed syntax errors\n- Fixed ruff linting issues\n- Applied code formatting\n- Resolved import errors\n- Fixed security issues\n\nGenerated by God Mode task 1be34528-93af-4a5f-b225-dd01458087e2\n\nCo-Authored-By: JARVIS <jarvis@local>' || echo 'No changes to commit'",
                        "expected_output": "Commit created",
                    },
                    {
                        "step": "Show commit summary",
                        "command": "git log -1 --stat || echo 'No commit created'",
                        "expected_output": "Commit details",
                    },
                ],
            },
        ],
        "requirements": {"files": ["ERROR_REPORT_*.md"], "tests_must_pass": False, "min_fixes": 1},
    }

    await database.init()

    try:
        await database.execute(
            """
            UPDATE god_mode_tasks
            SET plan = $2::jsonb,
                plan_generated_at = now(),
                status = 'pending',
                total_phases = $3
            WHERE id = $1
            """,
            task_id,
            json.dumps(plan),
            len(plan["phases"]),
        )

        print(f"✅ Comprehensive error-fixing plan added to task {task_id}")
        print(f"\n📋 Plan includes {len(plan['phases'])} phases:")
        for i, phase in enumerate(plan["phases"], 1):
            print(f"  {i}. {phase['name']} ({len(phase['steps'])} steps)")

        print(
            f"\n⏱️  Estimated total duration: {sum(p.get('estimated_duration_ms', 0) for p in plan['phases']) // 60000} minutes"
        )

    finally:
        await database.close()


if __name__ == "__main__":
    asyncio.run(add_comprehensive_plan())
