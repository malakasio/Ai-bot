"""God Mode Docker Isolation Module.

Provides Docker container sandboxing for agent execution:
- Isolated filesystem (bind mount worktree)
- Resource limits (CPU, memory)
- Network isolation (optional)
- Security constraints (no privileged, read-only root)

Architecture:
- Each agent runs in ephemeral Docker container
- Worktree mounted at /workspace
- Container auto-removed on exit
- Logs captured for debugging

Container image: python:3.11-slim with JARVIS dependencies
Resource limits: 2 CPU cores, 4GB RAM, 10GB disk
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Optional
from uuid import UUID


@dataclass
class Container:
    """Represents a Docker container for agent isolation."""

    container_id: str
    task_id: UUID
    image: str
    worktree_path: Path
    created_at: float


class DockerManager:
    """Manages Docker containers for agent isolation."""

    def __init__(self, default_image: str = "jarvis-godmode:latest"):
        self.default_image = default_image
        self.containers: dict[UUID, Container] = {}

    async def create_container(
        self,
        task_id: UUID,
        worktree_path: Path,
        image: Optional[str] = None,
        cpu_limit: float = 2.0,
        memory_limit: str = "4g",
        network_mode: str = "bridge",
    ) -> Container:
        """
        Create isolated Docker container for agent execution.

        Args:
            task_id: Task UUID
            worktree_path: Path to git worktree to mount
            image: Docker image (default: jarvis-godmode:latest)
            cpu_limit: CPU cores limit (default: 2.0)
            memory_limit: Memory limit (default: 4g)
            network_mode: Network mode (bridge, none, host)

        Returns:
            Container object with ID and metadata

        Raises:
            RuntimeError: If container creation fails
        """
        import time

        image = image or self.default_image

        # Ensure image exists
        await self._ensure_image(image)

        # Create container with resource limits
        container_name = f"godmode-{task_id}"

        create_args = [
            "docker",
            "run",
            "-d",  # Detached
            "--name",
            container_name,
            "--rm",  # Auto-remove on exit
            f"--cpus={cpu_limit}",
            f"--memory={memory_limit}",
            f"--network={network_mode}",
            "--security-opt",
            "no-new-privileges",
            "--read-only",  # Read-only root filesystem
            "--tmpfs",
            "/tmp:rw,noexec,nosuid,size=1g",
            "-v",
            f"{worktree_path}:/workspace:rw",
            "-w",
            "/workspace",
            "-e",
            f"TASK_ID={task_id}",
            "-e",
            "JARVIS_ZONE=green",
            "-e",
            "PYTHONUNBUFFERED=1",
            image,
            "sleep",
            "infinity",  # Keep container running
        ]

        proc = await asyncio.create_subprocess_exec(
            *create_args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await proc.communicate()

        if proc.returncode != 0:
            raise RuntimeError(f"Failed to create container: {stderr.decode()}")

        container_id = stdout.decode().strip()

        print(f"[docker] Created container {container_id[:12]} for task {task_id}")

        container = Container(
            container_id=container_id,
            task_id=task_id,
            image=image,
            worktree_path=worktree_path,
            created_at=time.time(),
        )

        self.containers[task_id] = container
        return container

    async def exec_command(
        self, container: Container, command: str, timeout: Optional[int] = None
    ) -> dict:
        """
        Execute command inside container.

        Args:
            container: Container to execute in
            command: Shell command to run
            timeout: Optional timeout in seconds

        Returns:
            Dict with: exit_code, stdout, stderr, duration_ms
        """
        import time

        start_ms = int(time.time() * 1000)

        exec_args = ["docker", "exec", container.container_id, "bash", "-c", command]

        try:
            proc = await asyncio.create_subprocess_exec(
                *exec_args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
            )

            if timeout:
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            else:
                stdout, stderr = await proc.communicate()

            duration_ms = int(time.time() * 1000) - start_ms

            return {
                "exit_code": proc.returncode,
                "stdout": stdout.decode(),
                "stderr": stderr.decode(),
                "duration_ms": duration_ms,
            }

        except asyncio.TimeoutError:
            # Kill container on timeout
            await self.stop_container(container, force=True)
            raise RuntimeError(f"Command timed out after {timeout}s")

    async def get_logs(self, container: Container, tail: int = 100) -> str:
        """Get container logs."""
        proc = await asyncio.create_subprocess_exec(
            "docker",
            "logs",
            "--tail",
            str(tail),
            container.container_id,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()

        return stdout.decode() + stderr.decode()

    async def get_stats(self, container: Container) -> dict:
        """Get container resource usage stats."""
        proc = await asyncio.create_subprocess_exec(
            "docker",
            "stats",
            "--no-stream",
            "--format",
            "{{json .}}",
            container.container_id,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()

        if proc.returncode != 0:
            return {"error": stderr.decode()}

        try:
            stats = json.loads(stdout.decode())
            return {
                "cpu_percent": stats.get("CPUPerc", "0%").rstrip("%"),
                "memory_usage": stats.get("MemUsage", "0B / 0B"),
                "memory_percent": stats.get("MemPerc", "0%").rstrip("%"),
                "net_io": stats.get("NetIO", "0B / 0B"),
                "block_io": stats.get("BlockIO", "0B / 0B"),
            }
        except json.JSONDecodeError:
            return {"error": "Failed to parse stats"}

    async def stop_container(
        self, container: Container, force: bool = False, timeout: int = 10
    ) -> bool:
        """
        Stop and remove container.

        Args:
            container: Container to stop
            force: Force kill (SIGKILL) instead of graceful stop
            timeout: Seconds to wait before force kill

        Returns:
            True if successful, False otherwise
        """
        if force:
            stop_args = ["docker", "kill", container.container_id]
        else:
            stop_args = ["docker", "stop", "-t", str(timeout), container.container_id]

        proc = await asyncio.create_subprocess_exec(
            *stop_args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await proc.communicate()

        if proc.returncode != 0:
            print(f"[docker] Failed to stop container: {stderr.decode()}")
            return False

        # Remove from tracking
        if container.task_id in self.containers:
            del self.containers[container.task_id]

        print(f"[docker] Stopped container {container.container_id[:12]}")
        return True

    async def cleanup_all(self):
        """Stop and remove all tracked containers."""
        for container in list(self.containers.values()):
            await self.stop_container(container, force=True)

    async def _ensure_image(self, image: str):
        """Ensure Docker image exists, pull if needed."""
        # Check if image exists
        proc = await asyncio.create_subprocess_exec(
            "docker",
            "image",
            "inspect",
            image,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await proc.communicate()

        if proc.returncode == 0:
            return  # Image exists

        # Image doesn't exist, try to pull
        print(f"[docker] Pulling image {image}...")

        proc = await asyncio.create_subprocess_exec(
            "docker", "pull", image, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await proc.communicate()

        if proc.returncode != 0:
            raise RuntimeError(f"Failed to pull image {image}: {stderr.decode()}")

        print(f"[docker] Image {image} pulled successfully")

    async def build_godmode_image(self, repo_root: Path) -> str:
        """
        Build jarvis-godmode Docker image from Dockerfile.

        Returns:
            Image name (jarvis-godmode:latest)
        """
        dockerfile_path = repo_root / "godmode" / "Dockerfile"

        if not dockerfile_path.exists():
            # Create Dockerfile
            dockerfile_content = """FROM python:3.11-slim

# Install system dependencies
RUN apt-get update && apt-get install -y \\
    git \\
    curl \\
    build-essential \\
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies
COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

# Install additional tools
RUN pip install --no-cache-dir \\
    ruff \\
    bandit \\
    pytest \\
    pytest-asyncio

# Create workspace
RUN mkdir -p /workspace
WORKDIR /workspace

# Non-root user
RUN useradd -m -u 1000 jarvis
USER jarvis

CMD ["bash"]
"""
            dockerfile_path.write_text(dockerfile_content)
            print(f"[docker] Created Dockerfile at {dockerfile_path}")

        # Build image
        print("[docker] Building jarvis-godmode image...")

        proc = await asyncio.create_subprocess_exec(
            "docker",
            "build",
            "-t",
            "jarvis-godmode:latest",
            "-f",
            str(dockerfile_path),
            str(repo_root),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()

        if proc.returncode != 0:
            raise RuntimeError(f"Failed to build image: {stderr.decode()}")

        print("[docker] Image jarvis-godmode:latest built successfully")
        return "jarvis-godmode:latest"
