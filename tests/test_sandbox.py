"""Smoke test for the Docker sandbox mechanics in mini_devin.py.

Deliberately does not touch Ollama/the LLM: it calls start_sandbox,
write_file, and run_command directly to prove the container control plane
works end to end (write a page, serve it, fetch it over the mapped port).
"""

import time
import urllib.request

import pytest

import mini_devin as md


@pytest.fixture(autouse=True)
def cleanup_container():
    yield
    try:
        md.get_container().remove(force=True)
    except md.docker.errors.NotFound:
        pass


def test_sandbox_serves_a_written_file():
    md.start_sandbox()

    marker = "mini-devin CI smoke test"
    md.write_file("/app/index.html", f"<h1>{marker}</h1>")
    md.run_command("cd /app && nohup python3 -m http.server 8000 > /tmp/server.log 2>&1 &")

    body = None
    for _ in range(10):
        time.sleep(1)
        try:
            body = urllib.request.urlopen(f"http://localhost:{md.HOST_PORT}", timeout=2).read().decode()
            break
        except Exception:
            continue

    assert body is not None, "sandbox HTTP server never came up"
    assert marker in body
