"""Best-effort Docker/GoNetEm project cleanup."""

from __future__ import annotations

import logging

FORCE_CLOSE_TIMEOUT_SECONDS = 10.0


class GoNetEmCleanupMixin:
    """Docker container cleanup and force-close fallback."""

    @staticmethod
    def _parse_project_containers(stdout: str, project_id: str) -> list[str]:
        """Return container IDs belonging to *project_id*.

        GoNetEm names containers ``ntm{project_id}.{node}``. Docker's
        ``name`` filter is a substring match, so filtering on ``ntm`` alone
        also matches every other project on the host. Match the exact project
        prefix instead.

        Args:
            stdout: Output of ``docker ps --format '{{.ID}}\t{{.Names}}'``.
            project_id: GoNetEm project identifier.

        Returns:
            IDs of containers scoped to the project.
        """
        prefix = f"ntm{project_id}"
        container_ids: list[str] = []
        for line in stdout.splitlines():
            parts = line.strip().split("\t", 1)
            if len(parts) != 2:
                continue
            container_id, name = parts
            if name == prefix or name.startswith(prefix + "."):
                container_ids.append(container_id)
        return container_ids

    def _run_docker_command(self, command: list[str]) -> bool:
        """Run a Docker cleanup command and report whether it succeeded."""
        try:
            result = self._command_runner.run(command, capture_output=True, text=True)
        except OSError as exc:
            logging.error(
                "GoNetEmLauncher: failed to run %s: %s", " ".join(command), exc
            )
            return False
        if result.returncode != 0:
            logging.error(
                "GoNetEmLauncher: %s failed (exit %s): %s",
                " ".join(command),
                result.returncode,
                (result.stderr or "").strip(),
            )
            return False
        return True

    def _list_project_containers(self, project_id: str) -> list[str] | None:
        """List Docker containers scoped to *project_id*.

        Returns ``None`` when Docker could not be queried, so callers can
        distinguish a failed cleanup from an empty project.
        """
        try:
            result = self._command_runner.run(
                ["docker", "ps", "-a", "--format", "{{.ID}}\t{{.Names}}"],
                capture_output=True,
                text=True,
            )
        except OSError as exc:
            logging.error("GoNetEmLauncher: could not list Docker containers: %s", exc)
            return None
        if result.returncode != 0:
            logging.error(
                "GoNetEmLauncher: docker ps failed (exit %s): %s",
                result.returncode,
                (result.stderr or "").strip(),
            )
            return None
        return self._parse_project_containers(result.stdout, project_id)

    def force_close_project(self) -> bool:
        """Forcefully close the project without waiting for progress messages.

        Docker cleanup only needs the project request, so it still runs after
        a failed ``close_project()`` has already closed the gRPC transport and
        cleared ``_client``. ``ProjectClose`` is sent only when a client is
        still available.

        Returns:
            True when cleanup completed (or there was nothing to clean),
            False when the project id is missing, a Docker command failed, or
            ``ProjectClose`` raised. The caller can use this to decide whether
            to retain project handles for a retry.
        """
        succeeded = True
        try:
            request = self._request
            if request is None:
                return True

            project_id = str(getattr(request, "id", "") or "")
            if not project_id:
                logging.warning(
                    "GoNetEmLauncher: project id is empty; skipping Docker cleanup"
                )
                return False

            container_ids = self._list_project_containers(project_id)
            if container_ids is None:
                succeeded = False
                container_ids = []
            for cid in container_ids:
                if not self._run_docker_command(["docker", "stop", "-t", "0", cid]):
                    succeeded = False
            for cid in container_ids:
                if not self._run_docker_command(["docker", "rm", cid]):
                    succeeded = False

            if self._client is not None:
                response = self._client.ProjectClose(
                    request, timeout=FORCE_CLOSE_TIMEOUT_SECONDS
                )
                # ProjectClose is server-streaming: consume the stream so the
                # server can process the request before the transport closes.
                for _ in response:
                    pass
            return succeeded
        except Exception as exc:
            logging.error("GoNetEmLauncher: force_close_project failed: %s", exc)
            return False
        finally:
            self._close_transport()
