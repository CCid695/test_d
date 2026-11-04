"""Unit tests for orchestrator.py's queueing/concurrency/stop logic and the
dashboard's Flask routes.

Deliberately uses fake run_fn/decompose_fn — no Kubernetes, no Ollama — so
this runs fast and deterministically in CI. The real sandbox control plane
is covered by tests/test_k8s_sandbox.py; the real LLM path is only
exercised manually (see README.md), same as the single-agent CLI.
"""

import json
import threading
import time
from types import SimpleNamespace

import pytest

import mini_devin as md
from orchestrator import (
    AgentRecord,
    Orchestrator,
    Plan,
    Role,
    SharedSandbox,
    build_integrator_task,
    extract_actions,
    make_sandbox_resolver,
    parse_plan,
)


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

    token = "t3st-token"
    app = create_app(FakeOrch(), token)
    client = app.test_client()
    auth = {"Authorization": f"Bearer {token}"}

    # GET routes stay open — no token needed.
    res = client.get("/api/state")
    assert res.status_code == 200
    assert res.get_json()["max_concurrent"] == 3

    res = client.post("/api/tasks", json={"task": "do something"}, headers=auth)
    assert res.status_code == 202
    assert res.get_json()["job_id"] == "job123"

    res = client.post("/api/tasks", json={"task": "   "}, headers=auth)
    assert res.status_code == 400

    res = client.post("/api/tasks", data="not json", content_type="text/plain", headers=auth)
    assert res.status_code == 400

    assert client.post("/api/agents/unknown/stop", headers=auth).status_code == 404
    assert client.post("/api/agents/known/stop", headers=auth).status_code == 200
    assert client.post("/api/agents/unknown/kill", headers=auth).status_code == 404
    assert client.post("/api/agents/known/kill", headers=auth).status_code == 200


def test_dashboard_routes_require_token():
    from dashboard import create_app

    app = create_app(FakeOrch(), "correct-token")
    client = app.test_client()

    # No Authorization header at all.
    res = client.post("/api/tasks", json={"task": "do something"})
    assert res.status_code == 401

    # Wrong token.
    res = client.post("/api/tasks", json={"task": "do something"}, headers={"Authorization": "Bearer wrong"})
    assert res.status_code == 401

    # Right token but not a Bearer header.
    res = client.post("/api/tasks", json={"task": "do something"}, headers={"Authorization": "correct-token"})
    assert res.status_code == 401

    # GET routes still don't need it.
    assert client.get("/api/state").status_code == 200


# --- "collaborate" mode: parse_plan --------------------------------------

def role_dict(name, task, depends_on):
    return {"name": name, "task": task, "depends_on": depends_on}


def test_parse_plan_collaborate_valid_name_deps():
    raw = json.dumps({
        "mode": "collaborate", "reason": "r",
        "roles": [role_dict("A", "do a", []), role_dict("B", "do b", ["A"]), role_dict("C", "do c", ["A"])],
    })
    plan = parse_plan(raw, task="ORIGINAL")
    assert plan.mode == "collaborate"
    assert [r.name for r in plan.roles] == ["A", "B", "C"]
    assert [r.depends_on for r in plan.roles] == [[], [0], [0]]
    assert plan.subtasks == ["do a", "do b", "do c"]


def test_parse_plan_collaborate_int_deps_are_1_based():
    raw = json.dumps({
        "mode": "collaborate", "reason": "r",
        "roles": [role_dict("A", "a", []), role_dict("B", "b", [1]), role_dict("C", "c", [1, 2])],
    })
    plan = parse_plan(raw, task="ORIGINAL")
    assert plan.mode == "collaborate"
    assert plan.roles[1].depends_on == [0]
    assert plan.roles[2].depends_on == [0, 1]


@pytest.mark.parametrize("roles", [
    [role_dict("A", "a", []), role_dict("B", "b", ["Z"]), role_dict("C", "c", [])],  # unknown name
    [role_dict("A", "a", ["A"]), role_dict("B", "b", []), role_dict("C", "c", [])],  # self-ref
    [role_dict("A", "a", ["B"]), role_dict("B", "b", []), role_dict("C", "c", [])],  # forward ref (would cycle)
    [role_dict("A", "a", []), role_dict("A", "dup", []), role_dict("C", "c", [])],   # duplicate names
    [role_dict("A", "a", "not-a-list"), role_dict("B", "b", []), role_dict("C", "c", [])],  # bad type
])
def test_parse_plan_collaborate_bad_graph_forces_chain(roles):
    raw = json.dumps({"mode": "collaborate", "reason": "r", "roles": roles})
    plan = parse_plan(raw, task="ORIGINAL")
    assert plan.mode == "collaborate"
    assert [r.depends_on for r in plan.roles] == [[], [0], [1]]
    assert "dependency graph invalid" in plan.reason


def test_parse_plan_collaborate_wrong_role_count_falls_back_to_serial():
    raw = json.dumps({
        "mode": "collaborate", "reason": "r",
        "roles": [role_dict("A", "do a", []), role_dict("B", "do b", [])],
    })
    plan = parse_plan(raw, task="ORIGINAL")
    assert plan.mode == "serial"
    assert plan.subtasks == ["do a", "do b"]
    assert "collaboration plan incomplete" in plan.reason


def test_parse_plan_collaborate_missing_roles_falls_back_to_serial_with_original_task():
    raw = json.dumps({"mode": "collaborate", "reason": "r"})
    plan = parse_plan(raw, task="ORIGINAL")
    assert plan.mode == "serial"
    assert plan.subtasks == ["ORIGINAL"]


def test_parse_plan_collaborate_roles_not_a_list_falls_back():
    raw = json.dumps({"mode": "collaborate", "reason": "r", "roles": "oops"})
    plan = parse_plan(raw, task="ORIGINAL")
    assert plan.mode == "serial"
    assert plan.subtasks == ["ORIGINAL"]


def test_parse_plan_collaborate_empty_task_role_dropped_changes_count():
    raw = json.dumps({
        "mode": "collaborate", "reason": "r",
        "roles": [role_dict("A", "", []), role_dict("B", "do b", []), role_dict("C", "do c", [])],
    })
    plan = parse_plan(raw, task="ORIGINAL")
    assert plan.mode == "serial"
    assert plan.subtasks == ["do b", "do c"]


# --- "collaborate" mode: scheduling ---------------------------------------

def agent_by_role(orch, role):
    for a in orch.snapshot()["agents"]:
        if a["role"] == role:
            return a
    return None


def test_collaborate_respects_dependencies():
    plan = Plan("collaborate", "r", ["a", "b", "c"], roles=[
        Role("A", "do a", []),
        Role("B", "do b", [0]),
        Role("C", "do c", [0]),
    ])

    gate_a = threading.Event()
    lock = threading.Lock()
    state = {"active": 0, "peak": 0}
    prompts = {}

    def run_fn(orch, rec, log):
        prompts[rec.role] = rec.prompt
        if rec.role == "A":
            gate_a.wait(timeout=5)
            return "A-RESULT"
        if rec.role in ("B", "C"):
            with lock:
                state["active"] += 1
                state["peak"] = max(state["peak"], state["active"])
            time.sleep(0.1)
            with lock:
                state["active"] -= 1
            return f"{rec.role}-RESULT"
        return "INTEGRATOR-RESULT"

    orch = Orchestrator(run_fn=run_fn, decompose_fn=lambda t: plan, remove_container_fn=noop_remove)
    orch.submit("original task")

    wait_until(lambda: len(orch.snapshot()["agents"]) == 4)
    wait_until(lambda: agent_by_role(orch, "A")["status"] == "running")
    assert agent_by_role(orch, "B")["status"] == "waiting"
    assert agent_by_role(orch, "C")["status"] == "waiting"
    assert agent_by_role(orch, "integration and verification")["status"] == "waiting"

    gate_a.set()
    wait_until(lambda: agent_by_role(orch, "integration and verification")["status"] == "done")

    assert state["peak"] == 2  # B and C really overlapped, not just "allowed to"
    assert "A-RESULT" in prompts["B"]
    assert "A-RESULT" in prompts["C"]

    job = orch.snapshot()["jobs"][0]
    assert job["final_result"] == "INTEGRATOR-RESULT"
    assert job["final_status"] == "done"


def test_collaborate_shares_one_container():
    plan = Plan("collaborate", "r", ["a", "b", "c"], roles=[
        Role("A", "a", []), Role("B", "b", []), Role("C", "c", []),
    ])

    def run_fn(orch, rec, log):
        return "ok"

    orch = Orchestrator(run_fn=run_fn, decompose_fn=lambda t: plan, remove_container_fn=noop_remove)
    orch.submit("t")

    wait_until(lambda: len(orch.snapshot()["agents"]) == 4)
    wait_until(lambda: all(a["status"] == "done" for a in orch.snapshot()["agents"]))

    agents = orch.snapshot()["agents"]
    assert len({a["container_name"] for a in agents}) == 1
    assert sorted(a["kind"] for a in agents) == ["integrator", "role", "role", "role"]


def test_upstream_error_skips_dependents_integrator_still_runs():
    plan = Plan("collaborate", "r", ["a", "b", "c"], roles=[
        Role("A", "a", []),
        Role("B", "b", [0]),
        Role("C", "c", []),
    ])

    calls = []
    captured = {}

    def run_fn(orch, rec, log):
        calls.append(rec.role)
        captured[rec.role] = rec.prompt
        if rec.role == "A":
            raise RuntimeError("boom")
        return f"{rec.role}-ok"

    orch = Orchestrator(run_fn=run_fn, decompose_fn=lambda t: plan, remove_container_fn=noop_remove)
    orch.submit("t")

    wait_until(lambda: len(orch.snapshot()["agents"]) == 4)
    wait_until(lambda: orch.snapshot()["jobs"][0]["final_status"] is not None)

    assert agent_by_role(orch, "A")["status"] == "error"
    assert agent_by_role(orch, "B")["status"] == "skipped"
    assert agent_by_role(orch, "C")["status"] == "done"
    assert "B" not in calls  # the dependent itself never actually ran
    assert agent_by_role(orch, "integration and verification")["status"] == "done"
    assert "skipped" in captured["integration and verification"]


def test_all_roles_fail_skips_integrator():
    plan = Plan("collaborate", "r", ["a", "b", "c"], roles=[
        Role("A", "a", []), Role("B", "b", []), Role("C", "c", []),
    ])

    calls = []

    def run_fn(orch, rec, log):
        calls.append(rec.role)
        raise RuntimeError("boom")

    orch = Orchestrator(run_fn=run_fn, decompose_fn=lambda t: plan, remove_container_fn=noop_remove)
    orch.submit("t")

    wait_until(lambda: len(orch.snapshot()["agents"]) == 4)
    wait_until(lambda: orch.snapshot()["jobs"][0]["final_status"] == "failed")
    assert "integration and verification" not in calls
    assert orch.snapshot()["jobs"][0]["final_result"] is not None


def test_stop_waiting_role_cascades_skip_to_dependent():
    plan = Plan("collaborate", "r", ["a", "b", "c"], roles=[
        Role("A", "a", []),
        Role("B", "b", [0]),
        Role("C", "c", [1]),  # depends on B, not directly on A
    ])

    gate_a = threading.Event()

    def run_fn(orch, rec, log):
        if rec.role == "A":
            gate_a.wait(timeout=5)
        return "ok"

    orch = Orchestrator(run_fn=run_fn, decompose_fn=lambda t: plan, remove_container_fn=noop_remove)
    orch.submit("t")

    wait_until(lambda: len(orch.snapshot()["agents"]) == 4)
    wait_until(lambda: agent_by_role(orch, "A")["status"] == "running")
    assert agent_by_role(orch, "B")["status"] == "waiting"

    orch.stop(agent_by_role(orch, "B")["id"])
    wait_until(lambda: agent_by_role(orch, "B")["status"] == "stopped")
    wait_until(lambda: agent_by_role(orch, "C")["status"] == "skipped")

    gate_a.set()
    wait_until(lambda: agent_by_role(orch, "integration and verification")["status"] == "done")
    assert orch.snapshot()["jobs"][0]["final_status"] == "done"  # A succeeded, so integrator still ran


def test_stop_waiting_integrator_sets_final_status_without_running():
    plan = Plan("collaborate", "r", ["a", "b", "c"], roles=[
        Role("A", "a", []), Role("B", "b", []), Role("C", "c", []),
    ])

    gate = threading.Event()
    calls = []

    def run_fn(orch, rec, log):
        calls.append(rec.role)
        gate.wait(timeout=5)
        return "ok"

    orch = Orchestrator(run_fn=run_fn, decompose_fn=lambda t: plan, remove_container_fn=noop_remove)
    orch.submit("t")

    wait_until(lambda: len(orch.snapshot()["agents"]) == 4)
    assert agent_by_role(orch, "integration and verification")["status"] == "waiting"
    orch.stop(agent_by_role(orch, "integration and verification")["id"])
    assert agent_by_role(orch, "integration and verification")["status"] == "stopped"

    gate.set()
    wait_until(lambda: all(agent_by_role(orch, r)["status"] == "done" for r in ("A", "B", "C")))
    time.sleep(0.2)  # give _advance_job_locked a chance to (wrongly) relaunch it, if it would

    assert agent_by_role(orch, "integration and verification")["status"] == "stopped"
    assert "integration and verification" not in calls
    assert orch.snapshot()["jobs"][0]["final_status"] == "stopped"


def test_kill_collaborate_member_kills_whole_job():
    plan = Plan("collaborate", "r", ["a", "b", "c"], roles=[
        Role("A", "a", []), Role("B", "b", []), Role("C", "c", []),
    ])

    gate = threading.Event()
    removed = []

    def run_fn(orch, rec, log):
        gate.wait(timeout=5)
        return "ok"

    orch = Orchestrator(run_fn=run_fn, decompose_fn=lambda t: plan, remove_container_fn=removed.append)
    orch.submit("t")

    wait_until(lambda: len(orch.snapshot()["agents"]) == 4)
    wait_until(lambda: agent_by_role(orch, "A")["status"] == "running")

    shared_name = agent_by_role(orch, "A")["container_name"]
    orch.kill(agent_by_role(orch, "A")["id"])

    assert removed == [shared_name]  # removed exactly once, not once per member
    gate.set()


def test_no_pool_deadlock_with_waiting_roles():
    chain_plan = Plan("collaborate", "r", ["a", "b", "c"], roles=[
        Role("A", "a", []),
        Role("B", "b", [0]),
        Role("C", "c", [1]),
    ])
    parallel_plan = Plan("parallel", "r", ["p1", "p2"])

    def decompose_fn(task):
        return parallel_plan if task == "parallel-job" else chain_plan

    def run_fn(orch, rec, log):
        time.sleep(0.05)
        return "ok"

    orch = Orchestrator(run_fn=run_fn, decompose_fn=decompose_fn, remove_container_fn=noop_remove)
    orch.submit("chain-1")
    orch.submit("chain-2")
    orch.submit("parallel-job")

    wait_until(lambda: len(orch.snapshot()["agents"]) == 4 + 4 + 2, timeout=10)
    wait_until(lambda: all(a["status"] == "done" for a in orch.snapshot()["agents"]), timeout=10)


# --- shared-sandbox locking -------------------------------------------------

class _FakeInnerSandbox:
    host_port = 4321

    def __init__(self):
        self.start_calls = 0
        self.active = 0
        self.peak = 0
        self._lock = threading.Lock()

    def start(self, log, should_stop):
        with self._lock:
            self.start_calls += 1
        return "http://fake"

    def _touch(self):
        with self._lock:
            self.active += 1
            self.peak = max(self.peak, self.active)
        time.sleep(0.02)
        with self._lock:
            self.active -= 1

    def exec(self, command, stdin_bytes=None):
        self._touch()
        return 0, "ok"

    def put_archive(self, tar_bytes):
        self._touch()


def test_shared_sandbox_serializes_tool_calls():
    inner = _FakeInnerSandbox()
    shared = SharedSandbox(inner)
    stop_event = threading.Event()
    handles = [shared.handle(stop_event) for _ in range(3)]

    start_threads = [threading.Thread(target=h.start, args=(print, stop_event.is_set)) for h in handles]
    for t in start_threads:
        t.start()
    for t in start_threads:
        t.join()
    assert inner.start_calls == 1

    def worker(h):
        for i in range(5):
            if i % 2 == 0:
                h.exec("echo hi")
            else:
                h.put_archive(b"data")

    tool_threads = [threading.Thread(target=worker, args=(h,)) for h in handles]
    for t in tool_threads:
        t.start()
    for t in tool_threads:
        t.join()

    assert inner.peak == 1


def test_shared_sandbox_waiting_call_honors_stop():
    holder_in = threading.Event()
    release_holder = threading.Event()

    class BlockingInner:
        host_port = None

        def start(self, log, should_stop):
            return "http://fake"

        def exec(self, command, stdin_bytes=None):
            holder_in.set()
            release_holder.wait(timeout=5)
            return 0, "ok"

        def put_archive(self, tar_bytes):
            pass

    shared = SharedSandbox(BlockingInner())
    holder_stop = threading.Event()
    waiter_stop = threading.Event()
    holder = shared.handle(holder_stop)
    waiter = shared.handle(waiter_stop)

    t = threading.Thread(target=holder.exec, args=("cmd",))
    t.start()
    assert holder_in.wait(timeout=2)

    waiter_stop.set()
    started = time.time()
    with pytest.raises(md.AgentStopped):
        waiter.exec("cmd2")
    assert time.time() - started < 2

    release_holder.set()
    t.join(timeout=2)


def test_sandbox_resolver_builds_once_for_shared():
    calls = []

    def factory(rec):
        calls.append(rec.container_name)
        return object()

    resolve = make_sandbox_resolver(factory)

    shared_recs = [
        SimpleNamespace(container_name="shared-container", sandbox_id="job-shared", stop_event=threading.Event())
        for _ in range(4)
    ]
    plain_recs = [
        SimpleNamespace(container_name=f"plain-{i}", sandbox_id=None, stop_event=threading.Event())
        for i in range(2)
    ]

    for rec in shared_recs:
        resolve(rec)
    for rec in plain_recs:
        resolve(rec)

    assert calls.count("shared-container") == 1
    assert sorted(c for c in calls if c.startswith("plain-")) == ["plain-0", "plain-1"]


# --- integrator helpers ------------------------------------------------------

def test_integrator_verified_flag_true_when_two_check_categories_logged():
    plan = Plan("collaborate", "r", ["a", "b", "c"], roles=[
        Role("A", "a", []), Role("B", "b", []), Role("C", "c", []),
    ])

    def run_fn(orch, rec, log):
        if rec.kind == "integrator":
            log("[run_command] ls -la /app")
            log("[run_command] exit=0 output=''")
            log("[run_command] for p in /proc/[0-9]*; do true; done")
            log("[run_command] exit=0 output=''")
        return "ok"

    orch = Orchestrator(run_fn=run_fn, decompose_fn=lambda t: plan, remove_container_fn=noop_remove)
    orch.submit("t")
    wait_until(lambda: orch.snapshot()["jobs"][0]["final_status"] is not None)
    assert orch.snapshot()["jobs"][0]["final_verified"] is True


def test_integrator_verified_flag_false_when_only_one_check_category_logged():
    """A single `ls` is not enough — this is deliberately the exact failure
    mode observed in real testing: the integrator ran one real check, then
    fabricated the rest of its "verification" in its final text."""
    plan = Plan("collaborate", "r", ["a", "b", "c"], roles=[
        Role("A", "a", []), Role("B", "b", []), Role("C", "c", []),
    ])

    def run_fn(orch, rec, log):
        if rec.kind == "integrator":
            log("[run_command] ls -la /app")
            log("[run_command] exit=0 output=''")
        return "ok"

    orch = Orchestrator(run_fn=run_fn, decompose_fn=lambda t: plan, remove_container_fn=noop_remove)
    orch.submit("t")
    wait_until(lambda: orch.snapshot()["jobs"][0]["final_status"] is not None)
    assert orch.snapshot()["jobs"][0]["final_verified"] is False


def test_integrator_verified_flag_false_when_no_run_command_logged():
    plan = Plan("collaborate", "r", ["a", "b", "c"], roles=[
        Role("A", "a", []), Role("B", "b", []), Role("C", "c", []),
    ])

    def run_fn(orch, rec, log):
        return "ok"

    orch = Orchestrator(run_fn=run_fn, decompose_fn=lambda t: plan, remove_container_fn=noop_remove)
    orch.submit("t")
    wait_until(lambda: orch.snapshot()["jobs"][0]["final_status"] is not None)
    assert orch.snapshot()["jobs"][0]["final_verified"] is False


def test_count_verification_categories():
    from orchestrator import count_verification_categories

    assert count_verification_categories([]) == 0
    assert count_verification_categories(["[run_command] ls -la /app"]) == 1
    assert count_verification_categories([
        "[run_command] ls -la /app",
        "[run_command] cat /app/app.py",
    ]) == 1  # both are "files" — still just one category
    assert count_verification_categories([
        "[run_command] ls -la /app",
        "[run_command] for p in /proc/[0-9]*; do true; done",
        "[run_command] python3 -c \"import urllib.request as u; u.urlopen('http://x')\"",
    ]) == 3
    # the exit=/output= follow-up line must not itself count as a command
    assert count_verification_categories([
        "[run_command] echo hi",
        "[run_command] exit=0 output='/proc/1 urlopen'",
    ]) == 0


def test_extract_actions_filters_to_write_file_and_run_command_only():
    logs = [
        "[10:00:00] [sandbox] 容器已就绪",
        "[10:00:01] [write_file] /app/index.html (10 字节)",
        "[10:00:02] [run_command] cd /app && ls",
        "[10:00:02] [run_command] exit=0 output=''",
        "[10:00:03] [run_command] echo hi",
    ]
    assert extract_actions(logs) == [
        "[10:00:01] [write_file] /app/index.html (10 字节)",
        "[10:00:02] [run_command] cd /app && ls",
        "[10:00:03] [run_command] echo hi",
    ]


def test_extract_actions_respects_limit():
    logs = [f"[00:00:00] [run_command] cmd{i}" for i in range(20)]
    assert extract_actions(logs, limit=3) == logs[-3:]


def test_build_integrator_task_includes_untrusted_marker_and_statuses():
    role_a = AgentRecord(
        id="j-0", job_id="j", container_name="c", task="task a", max_steps=5,
        status="done", role="A", result="all good", logs=["[run_command] start server"],
    )
    role_b = AgentRecord(
        id="j-1", job_id="j", container_name="c", task="task b", max_steps=5,
        status="skipped", role="B", result="Upstream role did not complete successfully, skipped",
    )
    text = build_integrator_task("do the thing", [role_a, role_b])
    assert "for reference only" in text
    assert "A" in text and "B" in text
    assert "done" in text and "skipped" in text
    assert "start server" in text
