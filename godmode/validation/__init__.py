"""God Mode Validation Module.

Provides validation pipeline for agent outputs:
- Syntax checking (ruff, tsc, shellcheck)
- Unit and integration test execution
- Security scanning (bandit, git secrets)
- Log analysis
- Quality scoring (0-100 scale)
- Self-healing with feedback loop
"""

from .pipeline import ValidationPipeline, ValidationResult

__all__ = ["ValidationPipeline", "ValidationResult"]
