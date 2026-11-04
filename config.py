"""Shared configuration constants for mini-devin.

This module exists to break circular imports between mini_devin.py and
k8s_backend.py by providing a single source of truth for shared constants.
"""

# Tag every agent-created sandbox with this label so a dashboard/orchestrator
# can find and clean up ones left over from a previous run.
AGENT_LABEL = {"mini-devin.role": "agent"}

# The Docker image used for all sandbox pods
IMAGE = "python:3.11-slim"

# The port a sandbox's own HTTP server listens on
SANDBOX_PORT = 8000
