"""Multi-agent orchestration: decide parallel vs serial, run up to 3 agents
concurrently, track their state for a dashboard.

Kept deliberately dependency-injectable (run_fn/decompose_fn/remove_container_fn)
so tests/test_orchestrator.py can exercise the real queueing/concurrency/stop
logic with fakes — no Docker or Ollama needed.
"""

import json
import threading
import time
import uuid
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field

import mini_devin as md

MAX_CONCURRENT = 3
MAX_SUBTASKS = 6  # clamp so a 7b model can't fan a task out into 20 pieces

# "Parallel" subtasks run in isolated containers that share no files or
# state, so the planner must only choose it when subtasks are truly
# independent — the prompt states this explicitly and biases toward serial.
PLANNER_PROMPT = """你是任务规划器。判断用户任务能否拆成多个【互相独立】的子任务并行执行。
重要：每个子任务会在一台【独立、互不相通】的 Docker 容器里由不同的助手完成，它们看不到彼此的文件或结果。
- 只有子任务之间不共享文件、不依赖彼此输出、没有先后顺序时，才选 "parallel"。
- 有先后依赖、要修改同一个项目、或任务本身很简单时，选 "serial"。拿不准就选 "serial"。
- 子任务最多 %d 个，每个都必须是能独立完成的完整描述。
只输出 JSON，不要输出其它文字：
{"mode": "parallel" 或 "serial", "reason": "一句话理由", "subtasks": ["...", "..."]}
serial 时 subtasks 是按顺序执行的步骤。

示例 1（可并行）：
用户任务："写三个互不相关的静态网页：一个猫网页、一个狗网页、一个天气网页"
{"mode": "parallel", "reason": "三个网页互不依赖、互不共享文件，可以分别独立完成", "subtasks": ["写一个关于猫的静态网页并运行", "写一个关于狗的静态网页并运行", "写一个天气主题的静态网页并运行"]}

示例 2（必须串行）：
用户任务："写一个 Flask 接口，装好依赖，跑起来，再测试一下能不能访问"
{"mode": "serial", "reason": "后面的步骤依赖前面步骤产出的同一个项目，必须在同一个容器里按顺序完成", "subtasks": ["编写 Flask 应用代码", "安装 Flask 依赖", "启动服务", "请求接口验证能否访问"]}
""" % MAX_SUBTASKS


@dataclass
class Plan:
    mode: str  # "parallel" | "serial"
    reason: str
    subtasks: list


def parse_plan(raw: str, task: str) -> Plan:
    """Pure function: turn the planner LLM's raw text into a validated Plan,
    falling back to a single serial agent on anything malformed."""
    fallback = Plan("serial", "fallback: could not parse planner output", [task])
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return fallback

    if not isinstance(data, dict):
        return fallback

    mode = data.get("mode")
    if mode not in ("parallel", "serial"):
        return fallback

    reason = str(data.get("reason", ""))[:300]
    subtasks = data.get("subtasks")
    if not isinstance(subtasks, list):
        return fallback

    cleaned = [s.strip() for s in subtasks if isinstance(s, str) and s.strip()]
    if not cleaned:
        return fallback
    cleaned = cleaned[:MAX_SUBTASKS]

    if mode == "parallel" and len(cleaned) < 2:
        mode = "serial"
        reason = reason or "only one usable subtask, running serially"

    return Plan(mode, reason, cleaned)


def decompose(task: str, llm=None) -> Plan:
    llm = llm or md.llm_client
    try:
        response = llm.chat.completions.create(
            model=md.MODEL,
            temperature=0,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": PLANNER_PROMPT},
                {"role": "user", "content": task},
            ],
        )
        raw = response.choices[0].message.content or ""
    except Exception as exc:
        return Plan("serial", f"fallback: planner call failed ({exc})", [task])
    return parse_plan(raw, task)


def plan_to_agent_tasks(plan: Plan, task: str):
    """Turn a Plan into a list of (agent_task_text, max_steps) pairs."""
    if plan.mode == "parallel":
        return [
            (f"{sub}\n\n（这是总任务「{task}」的一个独立子任务，只需完成这一部分）", md.MAX_STEPS)
            for sub in plan.subtasks
        ]

    steps = plan.subtasks
    if len(steps) <= 1:
        return [(task, md.MAX_STEPS)]

    numbered = "\n".join(f"{i}. {s}" for i, s in enumerate(steps, 1))
    serial_task = f"{task}\n\n请按以下步骤依次完成：\n{numbered}"
    max_steps = min(md.MAX_STEPS + 3 * len(steps), 25)
    return [(serial_task, max_steps)]


@dataclass
class AgentRecord:
    id: str
    job_id: str
    container_name: str
    task: str
    max_steps: int
    status: str = "queued"  # queued | running | done | error | stopped
    host_port: int = None
    created_at: float = 0.0
    started_at: float = None
    finished_at: float = None
    last_action: str = ""
    result: str = None
    logs: list = field(default_factory=list)
    stop_event: threading.Event = field(default_factory=threading.Event)
    future: Future = None

    def to_dict(self):
        return {
            "id": self.id,
            "job_id": self.job_id,
            "container_name": self.container_name,
            "task": self.task,
            "status": self.status,
            "host_port": self.host_port,
            "url": f"http://localhost:{self.host_port}" if self.host_port else None,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "last_action": self.last_action,
            "result": self.result,
            "logs": list(self.logs[-50:]),
        }


@dataclass
class JobRecord:
    id: str
    task: str
    status: str = "planning"  # planning | dispatched | failed
    mode: str = None
    reason: str = ""
    agent_ids: list = field(default_factory=list)
    created_at: float = 0.0

    def to_dict(self):
        return {
            "id": self.id,
            "task": self.task,
            "status": self.status,
            "mode": self.mode,
            "reason": self.reason,
            "agent_ids": list(self.agent_ids),
            "created_at": self.created_at,
        }


def default_run_fn(orch, rec, log):
    agent = md.Agent(
        rec.container_name,
        host_port=None,
        log=log,
        stop_event=rec.stop_event,
        labels=md.AGENT_LABEL,
    )
    agent.start_sandbox()
    orch.set_port(rec.id, agent.host_port)
    return agent.run(rec.task, max_steps=rec.max_steps)


def default_remove_container_fn(container_name):
    try:
        md.get_docker().containers.get(container_name).remove(force=True)
    except Exception:
        pass


class Orchestrator:
    def __init__(self, run_fn=None, decompose_fn=decompose,
                 remove_container_fn=None, max_concurrent=MAX_CONCURRENT):
        self.run_fn = run_fn or default_run_fn
        self.decompose_fn = decompose_fn
        self.remove_container_fn = remove_container_fn or default_remove_container_fn
        self.max_concurrent = max_concurrent

        self._lock = threading.Lock()
        self._jobs = {}
        self._agents = {}
        self._pool = ThreadPoolExecutor(max_workers=max_concurrent, thread_name_prefix="agent")
        self._planner = ThreadPoolExecutor(max_workers=1, thread_name_prefix="planner")

    def submit(self, task: str) -> str:
        job = JobRecord(id=uuid.uuid4().hex[:8], task=task, created_at=time.time())
        with self._lock:
            self._jobs[job.id] = job
        self._planner.submit(self._plan, job)
        return job.id

    def _plan(self, job: JobRecord):
        try:
            plan = self.decompose_fn(job.task)
        except Exception as exc:
            plan = Plan("serial", f"fallback: decompose raised ({exc})", [job.task])

        agent_tasks = plan_to_agent_tasks(plan, job.task)

        with self._lock:
            job.mode = plan.mode
            job.reason = plan.reason
            job.status = "dispatched"
            for i, (agent_task, max_steps) in enumerate(agent_tasks):
                agent_id = f"{job.id}-{i}"
                rec = AgentRecord(
                    id=agent_id,
                    job_id=job.id,
                    container_name=f"mini-devin-agent-{agent_id}",
                    task=agent_task,
                    max_steps=max_steps,
                    created_at=time.time(),
                )
                self._agents[agent_id] = rec
                job.agent_ids.append(agent_id)
                rec.future = self._pool.submit(self._run_agent, rec)

    def _run_agent(self, rec: AgentRecord):
        with self._lock:
            if rec.stop_event.is_set():
                rec.status = "stopped"
                return
            rec.status = "running"
            rec.started_at = time.time()

        def log(line):
            self._log(rec.id, line)

        try:
            result = self.run_fn(self, rec, log)
            with self._lock:
                rec.result = result
                rec.status = "done"
        except md.AgentStopped:
            with self._lock:
                rec.status = "stopped"
        except Exception as exc:
            with self._lock:
                rec.status = "error"
                rec.result = str(exc)
        finally:
            with self._lock:
                rec.finished_at = time.time()

    def _log(self, agent_id: str, line: str):
        timestamped = f"[{time.strftime('%H:%M:%S')}] {line}"
        print(f"[{agent_id}] {line}")
        with self._lock:
            rec = self._agents.get(agent_id)
            if rec is None:
                return
            rec.logs.append(timestamped)
            rec.last_action = line[:120]

    def set_port(self, agent_id: str, port: int):
        with self._lock:
            rec = self._agents.get(agent_id)
            if rec is not None:
                rec.host_port = port

    def stop(self, agent_id: str) -> bool:
        with self._lock:
            rec = self._agents.get(agent_id)
            if rec is None:
                return False
            rec.stop_event.set()
            if rec.status == "queued" and rec.future is not None and rec.future.cancel():
                rec.status = "stopped"
                rec.finished_at = time.time()
        return True

    def kill(self, agent_id: str) -> bool:
        with self._lock:
            rec = self._agents.get(agent_id)
            if rec is None:
                return False
            container_name = rec.container_name
        self.stop(agent_id)
        self.remove_container_fn(container_name)
        return True

    def snapshot(self) -> dict:
        with self._lock:
            jobs = [j.to_dict() for j in self._jobs.values()]
            agents = [a.to_dict() for a in self._agents.values()]
            running = sum(1 for a in self._agents.values() if a.status == "running")
            queued = sum(1 for a in self._agents.values() if a.status == "queued")
        return {
            "max_concurrent": self.max_concurrent,
            "running": running,
            "queued": queued,
            "jobs": jobs,
            "agents": agents,
        }

    def shutdown(self):
        with self._lock:
            container_names = [a.container_name for a in self._agents.values()]
            for rec in self._agents.values():
                rec.stop_event.set()
        self._pool.shutdown(wait=False, cancel_futures=True)
        self._planner.shutdown(wait=False, cancel_futures=True)
        for name in container_names:
            self.remove_container_fn(name)
