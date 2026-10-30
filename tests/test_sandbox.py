"""Smoke test for the Docker sandbox mechanics in mini_devin.py.

Deliberately does not touch Ollama/the LLM: it calls start_sandbox,
write_file, and run_command directly to prove the container control plane
works end to end (write a page, serve it, fetch it over the mapped port).
"""

import time
import urllib.request

import pytest

import mini_devin as md


TEST_CONTAINERS = [md.CONTAINER_NAME, "mini-devin-test-a", "mini-devin-test-b"]


@pytest.fixture(autouse=True)
def cleanup_container():
    yield
    for name in TEST_CONTAINERS:
        try:
            md.get_docker().containers.get(name).remove(force=True)
        except md.docker.errors.NotFound:
            pass


def _fetch(port, timeout_s=10):
    body = None
    for _ in range(timeout_s):
        time.sleep(1)
        try:
            body = urllib.request.urlopen(f"http://localhost:{port}", timeout=2).read().decode()
            break
        except Exception:
            continue
    return body


def test_sandbox_serves_a_written_file():
    md.start_sandbox()

    marker = "mini-devin CI smoke test"
    md.write_file("/app/index.html", f"<h1>{marker}</h1>")
    md.run_command("cd /app && nohup python3 -m http.server 8000 > /tmp/server.log 2>&1 &")

    body = _fetch(md.HOST_PORT)
    assert body is not None, "sandbox HTTP server never came up"
    assert marker in body


def test_two_agents_are_isolated():
    """Two Agent instances with distinct containers/ports must not see each
    other's files — this is the property the multi-agent orchestrator relies on."""
    agent_a = md.Agent("mini-devin-test-a", host_port=None)
    agent_b = md.Agent("mini-devin-test-b", host_port=None)

    agent_a.start_sandbox()
    agent_b.start_sandbox()
    assert agent_a.host_port != agent_b.host_port

    agent_a.write_file("/app/index.html", "<h1>agent-a</h1>")
    agent_a.run_command("cd /app && nohup python3 -m http.server 8000 > /tmp/server.log 2>&1 &")

    agent_b.write_file("/app/index.html", "<h1>agent-b</h1>")
    agent_b.run_command("cd /app && nohup python3 -m http.server 8000 > /tmp/server.log 2>&1 &")

    body_a = _fetch(agent_a.host_port)
    body_b = _fetch(agent_b.host_port)

    assert body_a is not None and "agent-a" in body_a
    assert body_b is not None and "agent-b" in body_b
