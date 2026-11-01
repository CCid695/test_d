"""Smoke tests for k8s_backend.K8sSandbox against a real cluster.

Skipped by default unless a cluster matching MINI_DEVIN_KUBE_CONTEXT is
reachable; set MINI_DEVIN_REQUIRE_K8S=1 to turn a skip into a hard failure
(used by the CI job that spins up a throwaway KinD cluster, so this path
can't silently stop being covered).
"""

import os
import time

import pytest

import k8s_backend as kb
import mini_devin as md

REQUIRE_K8S = os.environ.get("MINI_DEVIN_REQUIRE_K8S") == "1"

if not kb.cluster_reachable():
    if REQUIRE_K8S:
        pytest.fail(f"MINI_DEVIN_REQUIRE_K8S=1 but no cluster reachable at context {kb.KUBE_CONTEXT}")
    pytest.skip(f"no Kubernetes cluster reachable at context {kb.KUBE_CONTEXT}", allow_module_level=True)

TEST_NAMES = ["mini-devin-test-a", "mini-devin-test-b"]


@pytest.fixture(autouse=True)
def cleanup():
    yield
    for name in TEST_NAMES:
        kb.remove_agent_resources(name)


def _exec_text(sandbox, command):
    code, out = sandbox.exec(command)
    return code, out


def test_sandbox_lifecycle_write_run_and_remove():
    sandbox = kb.K8sSandbox("mini-devin-test-a", "testa")
    url = sandbox.start(print, lambda: False)
    assert url == kb.agent_url("testa")

    marker = "k8s sandbox smoke test"
    sandbox.put_archive(md._build_tar("/app/index.html", f"<h1>{marker}</h1>"))

    code, out = _exec_text(sandbox, "cat /app/index.html")
    assert code == 0
    assert marker in out

    sandbox.remove()

    # Deployment should be gone (and with it, via owner references, its
    # Service and Ingress).
    deadline = time.time() + 30
    while time.time() < deadline:
        pods = kb._core.list_namespaced_pod(
            kb.NAMESPACE, label_selector="mini-devin.agent-id=testa"
        ).items
        if not pods:
            break
        time.sleep(1)
    else:
        pytest.fail("pod for testa was not removed within 30s")


def test_two_sandboxes_are_isolated_and_service_dns_resolves():
    """Two K8sSandboxes must not see each other's files, and agent-to-agent
    DNS (via the Service) should resolve — proving the Service selector is
    wired correctly, independent of the Ingress/port-forward path."""
    sandbox_a = kb.K8sSandbox("mini-devin-test-a", "testa")
    sandbox_b = kb.K8sSandbox("mini-devin-test-b", "testb")

    sandbox_a.start(print, lambda: False)
    sandbox_b.start(print, lambda: False)

    sandbox_a.put_archive(md._build_tar("/app/index.html", "<h1>agent-a</h1>"))
    sandbox_a.exec("cd /app && nohup python3 -m http.server 8000 > /tmp/server.log 2>&1 &")

    sandbox_b.put_archive(md._build_tar("/app/index.html", "<h1>agent-b</h1>"))
    sandbox_b.exec("cd /app && nohup python3 -m http.server 8000 > /tmp/server.log 2>&1 &")

    time.sleep(2)

    fetch = (
        "python3 -c \"import urllib.request as u; "
        "print(u.urlopen('http://{}:8000/', timeout=5).read().decode())\""
    )
    code, out = sandbox_a.exec(fetch.format("mini-devin-test-b"))
    assert code == 0
    assert "agent-b" in out

    code, out = sandbox_b.exec(fetch.format("mini-devin-test-a"))
    assert code == 0
    assert "agent-a" in out
