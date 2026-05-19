"""JARVIS v7.0 observability stack.

* ``observability.tracing``    — structured trace emitter, OpenTelemetry
                                  instrumentation, evaluation scoring,
                                  metrics persistence.
* ``observability.dashboard``  — FastAPI real-time log + metrics
                                  streaming endpoint.

Both modules import lazily; ``import observability`` is safe even when
opentelemetry / fastapi / asyncpg are missing.
"""

__all__ = ["tracing", "dashboard"]
