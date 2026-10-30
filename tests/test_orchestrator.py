"""Unit tests for orchestrator.py's queueing/concurrency/stop logic and the
dashboard's Flask routes.

Deliberately uses fake run_fn/decompose_fn — no Docker, no Ollama — so this
runs fast and deterministically in CI. The real Docker-control-plane path is
covered separately by tests/test_sandbox.py; the real LLM path is only
exercised manually (see README.md), same as the single-agent CLI.
"""

import json
import threading
import time

import pytest

import mini_devin as md
from orchestrator import Orchestrator, Plan, parse_plan


def wait_until(predicate, timeout=5, interval=0.02):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    raise AssertionError("condition not met within timeout")


def agent_status(orch, agent_id):
    for a in orch.snapshot()["agents"]:
        if a["id"] == agent_id:
            return a["status"]
    return None


def make_counting_run_fn():
    lock = threading.Lock()
    state = {"active": 0, "peak": 0}
    gate = threading.Event()

    def run_fn(orch, rec, log):
        with lock:
            state["active"] += 1
            state["peak"] = max(state["peak"], state["active"])
        gate.wait(timeout=5)
        with lock:
            state["active"] -= 1
        return "ok"

    return run_fn, gate, state


def noop_remove(container_name):
    pass


def test_caps_at_three_and_queues_rest():
    run_fn, gate, state = make_counting_run_fn()
    decompose_fn = lambda task: Plan("parallel", "r", [f"sub{i}" for i in range(5)])
    orch = Orchestrator(run_fn=run_fn, decompose_fn=decompose_fn, remove_container_fn=noop_remove)

    orch.submit("do 5 independent things")
    wait_until(lambda: len(orch.snapshot()["agents"]) == 5)
    wait_until(lambda: orch.snapshot()["running"] == 3)

    snap = orch.snapshot()
    assert snap["running"] == 3
    assert snap["queued"] == 2

    gate.set()
    wait_until(lambda: all(a["status"] == "done" for a in orch.snapshot()["agents"]))
    assert state["peak"] == 3


def test_serial_plan_runs_one_agent():
    captured = []

    def run_fn(orch, rec, log):
        captured.append(rec)
        return "done"

    decompose_fn = lambda task: Plan("serial", "r", ["step1", "step2", "step3"])
    orch = Orchestrator(run_fn=run_fn, decompose_fn=decompose_fn, remove_container_fn=noop_remove)

    orch.submit("original task")
    wait_until(lambda: len(orch.snapshot()["agents"]) == 1)
    wait_until(lambda: orch.snapshot()["agents"][0]["status"] == "done")

    assert len(captured) == 1
    rec = captured[0]
    assert "original task" in rec.task
    assert "step1" in rec.task and "step2" in rec.task and "step3" in rec.task
    assert rec.max_steps > md.MAX_STEPS


def test_stop_queued_and_running():
    release = threading.Event()

    def run_fn(orch, rec, log):
        while not rec.stop_event.is_set():
            if release.is_set():
                return "ok"
            time.sleep(0.02)
        raise md.AgentStopped("stopped")

    decompose_fn = lambda task: Plan("parallel", "r", [f"sub{i}" for i in range(5)])
    orch = Orchestrator(run_fn=run_fn, decompose_fn=decompose_fn, remove_container_fn=noop_remove)

    orch.submit("5 things")
    wait_until(lambda: len(orch.snapshot()["agents"]) == 5)
    wait_until(lambda: orch.snapshot()["running"] == 3)

    snap = orch.snapshot()
    running_ids = [a["id"] for a in snap["agents"] if a["status"] == "running"]
    queued_ids = [a["id"] for a in snap["agents"] if a["status"] == "queued"]
    assert len(running_ids) == 3
    assert len(queued_ids) == 2

    orch.stop(queued_ids[0])
    wait_until(lambda: agent_status(orch, queued_ids[0]) == "stopped")

    orch.stop(running_ids[0])
    wait_until(lambda: agent_status(orch, running_ids[0]) == "stopped")

    release.set()
    gate_pred = lambda: all(a["status"] in ("done", "stopped") for a in orch.snapshot()["agents"])
    wait_until(gate_pred)


def test_run_fn_exception_marks_error():
    def run_fn(orch, rec, log):
        raise RuntimeError("boom")

    decompose_fn = lambda task: Plan("serial", "r", [task])
    orch = Orchestrator(run_fn=run_fn, decompose_fn=decompose_fn, remove_container_fn=noop_remove)

    orch.submit("task")
    wait_until(lambda: len(orch.snapshot()["agents"]) == 1)
    wait_until(lambda: orch.snapshot()["agents"][0]["status"] == "error")
    assert "boom" in orch.snapshot()["agents"][0]["result"]


@pytest.mark.parametrize(
    "raw,expected_mode,expected_subtasks",
    [
        ('{"mode":"parallel","reason":"x","subtasks":["a","b"]}', "parallel", ["a", "b"]),
        ('{"mode":"serial","reason":"x","subtasks":["a","b","c"]}', "serial", ["a", "b", "c"]),
        ("not json at all", "serial", None),
        ('{"mode":"bogus","subtasks":["a","b"]}', "serial", None),
        ('{"mode":"parallel","subtasks":[]}', "serial", None),
        ('{"mode":"parallel","subtasks":["only one"]}', "serial", ["only one"]),
        ('{"mode":"parallel","subtasks":[1,2,"a","b"]}', "parallel", ["a", "b"]),
    ],
)
def test_parse_plan(raw, expected_mode, expected_subtasks):
    plan = parse_plan(raw, task="ORIGINAL")
    assert plan.mode == expected_mode
    assert plan.subtasks == (expected_subtasks if expected_subtasks is not None else ["ORIGINAL"])


def test_parse_plan_clamps_subtask_count():
    raw = json.dumps({"mode": "parallel", "reason": "x", "subtasks": [f"s{i}" for i in range(10)]})
    plan = parse_plan(raw, task="ORIGINAL")
    assert len(plan.subtasks) == 6


class FakeOrch:
    def __init__(self):
        self.submitted = []

    def submit(self, task):
        self.submitted.append(task)
        return "job123"

    def stop(self, agent_id):
        return agent_id == "known"

    def kill(self, agent_id):
        return agent_id == "known"

    def snapshot(self):
        return {"max_concurrent": 3, "running": 0, "queued": 0, "jobs": [], "agents": []}


def test_dashboard_routes():
    from dashboard import create_app

    app = create_app(FakeOrch())
    client = app.test_client()

    res = client.get("/api/state")
    assert res.status_code == 200
    assert res.get_json()["max_concurrent"] == 3

    res = client.post("/api/tasks", json={"task": "do something"})
    assert res.status_code == 202
    assert res.get_json()["job_id"] == "job123"

    res = client.post("/api/tasks", json={"task": "   "})
    assert res.status_code == 400

    res = client.post("/api/tasks", data="not json", content_type="text/plain")
    assert res.status_code == 400

    assert client.post("/api/agents/unknown/stop").status_code == 404
    assert client.post("/api/agents/known/stop").status_code == 200
    assert client.post("/api/agents/unknown/kill").status_code == 404
    assert client.post("/api/agents/known/kill").status_code == 200
