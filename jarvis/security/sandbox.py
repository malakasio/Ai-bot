"""Sandbox execution module - re-exports from core."""

import asyncio


async def execute_direct(cmd: list[str], timeout: int = 30) -> dict:
    """Execute a command directly with security validation."""
    from jarvis.security.zones import validate_command

    # Validate command first
    allowed, reason = validate_command(cmd)
    if not allowed:
        raise PermissionError(f"Command blocked: {reason}")

    # Execute with timeout
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )

        stdout, stderr = await asyncio.wait_for(
            proc.communicate(),
            timeout=timeout
        )

        return {
            'returncode': proc.returncode,
            'stdout': stdout.decode('utf-8', errors='replace'),
            'stderr': stderr.decode('utf-8', errors='replace'),
        }
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise TimeoutError(f"Command timed out after {timeout}s")


__all__ = ['execute_direct']
