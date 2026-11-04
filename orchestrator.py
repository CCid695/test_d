"""Multi-agent orchestration: decide parallel vs serial vs collaborate, run
up to 3 agents concurrently, track their state for a dashboard.

Kept deliberately dependency-injectable (run_fn/decompose_fn/remove_container_fn)
so tests/test_orchestrator.py can exercise the real queueing/concurrency/stop
logic with fakes — no Kubernetes or Ollama needed. make_run_fn() builds the
real run_fn against k8s_backend's Kubernetes-backed sandboxes, which is what
dashboard.py uses by default.
"""

import json
import threading
import time
import uuid
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass, field

import config as cfg
import k8s_backend as kb
import mini_devin as md

MAX_CONCURRENT = 3
MAX_SUBTASKS = 6  # clamp so a 7b model can't fan a task out into 20 pieces
MAX_ROLE_NAME = 12
MAX_ROLE_TASK = 400

_FAILED_STATUSES = {"error", "stopped", "skipped"}
_TERMINAL_STATUSES = _FAILED_STATUSES | {"done"}

# Three modes:
# - "parallel": fully independent subtasks, isolated sandboxes, no shared
#   files, no integration step.
# - "serial": one agent, multiple internal steps, one sandbox.
# - "collaborate": one project split into exactly 3 AI-named roles sharing
#   ONE sandbox (so later roles can see/keep building on earlier roles'
#   files), with an automatic verifying integrator at the end.
PLANNER_PROMPT = """你是任务规划器。根据用户任务，从三种执行方式里选一种：
1. "serial"：由一个助手在一个沙箱里从头做到尾，按顺序完成多个步骤。大多数任务都应该选这个——
   任务简单、步骤是一条线（写代码→装依赖→启动→测试）、或者拿不准时，都选 "serial"。
2. "parallel"：任务由几个【互相无关】的独立成果组成（比如三个主题不同的网页）。
   每个子任务会在一个【独立、互不相通】的沙箱容器里完成，彼此看不到文件或结果，最后也不合并。
   只有子任务之间不共享文件、不依赖彼此输出、没有先后顺序时，才选这个。
3. "collaborate"：任务是【同一个项目】，但明显能拆成三块分量相当、需要不同分工的工作
   （比如 接口设计 + 前端 + 后端）。三个角色共用【同一个沙箱】，能看到、能继续操作彼此的文件；
   全部完成后系统会自动安排一个整合者验收并汇报，所以角色里不要再安排"整合/汇总/验收"这种角色。

判断顺序：
- 几个成果之间毫无关系、不共享文件、不需要合并结果？→ "parallel"
- 同一个项目，能自然拆成三块、每块都有实打实的工作量？→ "collaborate"
- 其它所有情况，或者拿不准 → "serial"

parallel/serial 只输出：
{"mode": "parallel" 或 "serial", "reason": "一句话理由", "subtasks": ["...", "..."]}
parallel 最多 %d 个子任务；serial 的 subtasks 是按顺序执行的步骤。

collaborate 规则：
- 必须正好 3 个角色，按执行顺序列出，角色名由你根据任务起（2~6 个字）。
- depends_on 写这个角色必须等哪些角色先完成（写角色名），只能写排在它前面的角色；
  可以同时进行的角色之间不要互相写进 depends_on；第一个角色的 depends_on 必须是 []。
- 需要在 8000 端口启动服务时，只能写进【最后一个】角色的任务里，其它角色不要启动服务，
  避免多个角色同时抢占端口。
collaborate 只输出：
{"mode": "collaborate", "reason": "一句话理由", "roles": [{"name": "...", "task": "...", "depends_on": []}, ...]}

只输出 JSON，不要输出其它文字。

示例 1（parallel）：
用户任务："写三个互不相关的静态网页：一个猫网页、一个狗网页、一个天气网页"
{"mode": "parallel", "reason": "三个网页互不依赖、互不共享文件，可以分别独立完成", "subtasks": ["写一个关于猫的静态网页并运行", "写一个关于狗的静态网页并运行", "写一个天气主题的静态网页并运行"]}

示例 2（serial）：
用户任务："写一个 Flask 接口，装好依赖，跑起来，再测试一下能不能访问"
{"mode": "serial", "reason": "后面的步骤依赖前面步骤产出的同一个项目，必须在同一个容器里按顺序完成", "subtasks": ["编写 Flask 应用代码", "安装 Flask 依赖", "启动服务", "请求接口验证能否访问"]}

示例 3（collaborate，有分叉依赖）：
用户任务："做一个留言板网站：Flask 后端提供留言的查看和提交接口，前端页面能查看和提交留言，由 Flask 提供页面并在 8000 端口运行"
{"mode": "collaborate", "reason": "同一个项目，接口设计、前端、后端三块工作量相当；前端和后端都只依赖接口设计，彼此可以同时进行", "roles": [
 {"name": "接口设计", "task": "在 /app/API.md 写清楚留言接口：路径、方法、请求和返回的 JSON 格式", "depends_on": []},
 {"name": "前端", "task": "按 /app/API.md 编写 /app/static/index.html，用 fetch 调用接口查看和提交留言", "depends_on": ["接口设计"]},
 {"name": "后端", "task": "按 /app/API.md 编写 /app/app.py（Flask，数据存内存，/ 返回 static/index.html，结尾必须有 app.run(host='0.0.0.0', port=8000)），pip 安装 flask，用 nohup ... & 在后台启动", "depends_on": ["接口设计"]}
]}

示例 4（看起来能拆，但应该 serial）：
用户任务："写一个计算器网页并运行"
{"mode": "serial", "reason": "一个小网页，一个助手直接做更快，拆分只会增加协调成本", "subtasks": ["编写计算器网页", "启动 HTTP 服务"]}
""" % MAX_SUBTASKS


@dataclass
class Role:
    name: str
    task: str
    depends_on: list  # 0-based indices into the roles list; backward refs only


@dataclass
class Plan:
    mode: str  # "parallel" | "serial" | "collaborate"
    reason: str
    subtasks: list = field(default_factory=list)
    roles: list = field(default_factory=list)  # only populated for "collaborate"


def _validate_roles_shape(raw_roles):
    """Validate that raw_roles is a list with exactly 3 usable entries.

    Returns the list of usable role dicts, or None if the shape is invalid.
    """
    if not isinstance(raw_roles, list):
        return None

    usable = [r for r in raw_roles if isinstance(r, dict) and str(r.get("task", "")).strip()]
    if len(usable) != 3:
        return None

    return usable


def _normalize_role_names(raw_names):
    """Deduplicate and normalize role names.

    Returns (normalized_names, had_duplicates) where had_duplicates is True
    if any non-empty names were duplicated.
    """
    non_empty_names = [n for n in raw_names if n]
    had_duplicate_names = len(set(non_empty_names)) != len(non_empty_names)

    seen = set()
    names = []
    for i, n in enumerate(raw_names):
        n = n or f"角色{i + 1}"
        if n in seen:
            n = f"{n}{i + 1}"
        seen.add(n)
        names.append(n)

    return names, had_duplicate_names


def _resolve_dependencies(usable, raw_names, name_to_index):
    """Resolve role dependencies from the raw role data.

    Returns (deps_list, graph_ok) where graph_ok is False if any dependency
    reference is invalid (unknown, forward, or self-reference).
    """
    deps_list = []
    graph_ok = True

    for i, r in enumerate(usable):
        raw_deps = r.get("depends_on")
        if raw_deps is None:
            raw_deps = []
        if not isinstance(raw_deps, list):
            graph_ok = False
            raw_deps = []

        resolved = []
        for d in raw_deps:
            idx = None
            if isinstance(d, str) and d.strip() in name_to_index:
                idx = name_to_index[d.strip()]
            elif isinstance(d, int) and not isinstance(d, bool) and 1 <= d <= 3:
                idx = d - 1
            if idx is None or idx >= i:  # unknown ref, or forward ref/self-ref (would be a cycle)
                graph_ok = False
                continue
            if idx not in resolved:
                resolved.append(idx)
        deps_list.append(resolved)

    return deps_list, graph_ok


def _parse_roles(raw_roles):
    """Pure helper: validate the collaborate "roles" shape.

    Returns (roles, forced_chain) where forced_chain is True if the
    dependency graph had to be replaced with a straight chain because it
    was malformed (unknown/forward/self reference, duplicate names, bad
    types). Returns None if the shape can't be collaborate at all (not a
    list, or not exactly 3 usable entries) — caller falls back to serial.
    """
    usable = _validate_roles_shape(raw_roles)
    if usable is None:
        return None

    raw_names = [str(r.get("name") or "").strip()[:MAX_ROLE_NAME] for r in usable]
    tasks = [str(r["task"]).strip()[:MAX_ROLE_TASK] for r in usable]

    names, had_duplicate_names = _normalize_role_names(raw_names)

    # Look up depends_on against the ORIGINAL (pre-dedup) names, since
    # that's what the model actually wrote; duplicates already force a
    # chain fallback below, so any ambiguity here is moot.
    name_to_index = {n: i for i, n in enumerate(raw_names) if n}

    deps_list, graph_ok = _resolve_dependencies(usable, raw_names, name_to_index)

    forced_chain = not graph_ok or had_duplicate_names
    if forced_chain:
        deps_list = [[] if i == 0 else [i - 1] for i in range(3)]

    roles = [Role(names[i], tasks[i], deps_list[i]) for i in range(3)]
    return roles, forced_chain


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
    if mode not in ("parallel", "serial", "collaborate"):
        return fallback

    reason = str(data.get("reason", ""))[:300]

    if mode == "collaborate":
        parsed = _parse_roles(data.get("roles"))
        if parsed is None:
            raw_roles = data.get("roles")
            usable_tasks = []
            if isinstance(raw_roles, list):
                usable_tasks = [
                    str(r["task"]).strip()[:MAX_ROLE_TASK]
                    for r in raw_roles
                    if isinstance(r, dict) and str(r.get("task", "")).strip()
                ]
            new_reason = (reason or "collaborate 角色不完整") + "（协作计划不完整，改为单助手执行）"
            return Plan("serial", new_reason, usable_tasks[:MAX_SUBTASKS] or [task])

        roles, forced_chain = parsed
        if forced_chain:
            reason = (reason or "") + "（依赖关系无效，已改为依次执行）"
        return Plan("collaborate", reason, [r.task for r in roles], roles)

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
    """Turn a parallel/serial Plan into a list of (agent_task_text, max_steps)
    pairs. Collaborate plans are dispatched separately (see
    Orchestrator._dispatch_collaboration_locked); if one ever reached here
    it would be treated like serial over plan.subtasks, as a safety net."""
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
    status: str = "queued"  # queued|running|done|error|stopped|waiting|skipped
    url: str = None
    created_at: float = 0.0
    started_at: float = None
    finished_at: float = None
    last_action: str = ""
    result: str = None
    logs: list = field(default_factory=list)
    stop_event: threading.Event = field(default_factory=threading.Event)
    future: Future = None
    kind: str = "worker"  # worker | role | integrator
    role: str = None  # display name (role's own name, or "整合验收")
    depends_on: list = field(default_factory=list)  # agent ids
    sandbox_id: str = None  # set => this record shares the job's sandbox
    extra_system: str = None
    prompt: str = None  # full task text built at launch; task stays the short label
    min_tool_calls: int = None  # set => Agent.run() refuses to stop before this many real tool calls
    max_seconds: float = None  # set => Agent.run() stops at this wall-clock budget, no matter what

    def to_dict(self):
        return {
            "id": self.id,
            "job_id": self.job_id,
            "container_name": self.container_name,
            "task": self.task,
            "status": self.status,
            "url": self.url,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "last_action": self.last_action,
            "result": self.result,
            "logs": list(self.logs[-50:]),
            "kind": self.kind,
            "role": self.role,
            "depends_on": list(self.depends_on),
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
    final_result: str = None
    final_status: str = None  # done | error | stopped | failed
    final_verified: bool = None
    shared_container: str = None

    def to_dict(self):
        return {
            "id": self.id,
            "task": self.task,
            "status": self.status,
            "mode": self.mode,
            "reason": self.reason,
            "agent_ids": list(self.agent_ids),
            "created_at": self.created_at,
            "final_result": self.final_result,
            "final_status": self.final_status,
            "final_verified": self.final_verified,
            "shared_container": self.shared_container,
        }


ROLE_MAX_STEPS = md.MAX_STEPS + 2  # a bit more room: roles spend a step on `ls` first
INTEGRATOR_MAX_STEPS = 15
# The integrator has been observed to fabricate an elaborate-looking
# verification narrative in its final text after just one real tool call
# (e.g. one `ls`, then invented process/curl checks it never ran). Forcing
# at least this many real tool calls before it's allowed to stop makes that
# much harder — see Agent.run()'s min_tool_calls.
INTEGRATOR_MIN_TOOL_CALLS = 3
# A hard wall-clock cap, not just a step count — min_tool_calls alone can
# keep a slow local model "working" for a long time with no visible
# progress (a nudge round that logs nothing if it makes no tool call), and
# a verification step that might take forever isn't practically usable.
# Real observed worst case without this: 9+ minutes with only 2 tool calls
# logged. See Agent.run()'s max_seconds.
INTEGRATOR_MAX_SECONDS = 360
INTEGRATOR_NUDGE = (
    "你还没有真的完成要求的验证步骤——不要在文字里描述你「已经」检查/启动/验证了什么，"
    "必须真的调用 run_command/write_file，依据工具的真实返回结果判断，再给出结论。"
    "请继续：依次检查文件内容（ls/cat）、进程是否在跑（/proc）、服务是否能访问（urlopen），"
    "每一步都要有真实的工具调用和返回结果。"
)
UPSTREAM_SUMMARY_LIMIT = 600
ACTION_LOG_LIMIT = 12


def build_role_system(job_task: str, role_recs: list, idx: int) -> str:
    this_role = role_recs[idx]
    division = "\n".join(
        f"- {r.role}{'（你负责的部分）' if i == idx else ''}：{r.task}"
        for i, r in enumerate(role_recs)
    )
    return f"""\
你是多人协作中的「{this_role.role}」角色，共 {len(role_recs)} 人一起完成同一个总任务：
{job_task}

分工如下（只做你自己负责的部分，不要重复或覆盖别人的工作）：
{division}

你和其他角色共用同一个沙箱，彼此能看到对方写的文件。开始前先用 run_command 执行
`ls -R /app` 一类命令看看现在有什么，不要删除或整体覆盖不属于你这部分的文件。
上游角色给你的总结可能不准确，以沙箱里实际存在的文件为准。
如果你的任务不要求启动服务，就不要启动，避免和其他角色抢占端口。
完成后用中文简要总结你做了什么、写了哪些文件。
"""


def build_role_task(rec: "AgentRecord", upstream_recs: list) -> str:
    if not upstream_recs:
        return rec.task
    parts = [rec.task, "", "依赖的上游角色结果："]
    for up in upstream_recs:
        summary = (up.result or "")[:UPSTREAM_SUMMARY_LIMIT]
        parts.append(f"- {up.role}（{up.status}）：{summary}")
    return "\n".join(parts)


def extract_actions(logs: list, limit: int = ACTION_LOG_LIMIT) -> list:
    """Pull just the [write_file]/[run_command] lines out of an agent's log
    (not the exit=.../output=... follow-up lines) — a short, trustworthy
    record of what it actually did, as opposed to what it claims it did."""
    actions = [
        line for line in logs
        if "[write_file]" in line or ("[run_command]" in line and "exit=" not in line)
    ]
    return actions[-limit:]


# What INTEGRATOR_SYSTEM asks for: a file check, a process check, a network
# check. Matched against the actually-executed command text (not output, and
# definitely not the model's self-reported text) — a model that fabricates a
# verification narrative without really calling the tool leaves no trace here.
_VERIFICATION_CATEGORY_MARKERS = {
    "files": ("ls ", "ls\t", "cat "),
    "process": ("/proc",),
    "network": ("urlopen", "urllib", "http.client", "create_connection"),
}


def _executed_commands(logs: list) -> list:
    """The real shell commands this agent ran, parsed from its own
    [run_command] log lines — the one source of truth for what it actually
    executed, independent of anything it later claims in its final text."""
    commands = []
    for line in logs:
        if "[run_command]" in line and "exit=" not in line:
            commands.append(line.split("[run_command]", 1)[1].strip())
    return commands


def count_verification_categories(logs: list) -> int:
    """How many of the 3 required check categories (file/process/network)
    the agent ran at least one matching real command for."""
    commands_text = "\n".join(_executed_commands(logs))
    return sum(
        1 for markers in _VERIFICATION_CATEGORY_MARKERS.values()
        if any(m in commands_text for m in markers)
    )


INTEGRATOR_SYSTEM = """\
你是整合与验收角色。其他角色的自述【不可信】：之前多次出现"声称服务已启动/已测试"但实际上
从未执行启动命令、或代码缺少 app.run() 的情况。你必须亲自用 run_command 验证后才能下结论：
1. ls -la /app（及子目录），cat 关键文件，检查内容是否完整（例如 Flask 应用结尾是否有
   app.run(host='0.0.0.0', port=8000)）。
2. 查看进程（沙箱里没有 ps 命令）：
   for p in /proc/[0-9]*; do tr '\\0' ' ' < $p/cmdline 2>/dev/null; echo; done | grep -v '^$'
3. 请求服务验证是否真的能访问（沙箱里没有 curl）：
   python3 -c "import urllib.request as u; r=u.urlopen('http://127.0.0.1:8000/', timeout=5); print(r.status); print(r.read(300))"
发现小问题（服务没启动、代码缺 app.run 之类）可以直接修复并重新验证，不需要重写整个项目。
最终用中文报告，包含：
- 最终交付：用户应该如何访问/使用
- 验证结果：逐项写【通过】或【失败】，并附上你实际运行的命令和关键输出
- 你做的修复（如果有）
- 未完成的部分（包括失败或被跳过的角色）
没有亲自运行命令验证过的内容，不允许写"已完成"。
"""


def build_integrator_task(job_task: str, role_recs: list) -> str:
    parts = [f"总任务：{job_task}", "", "各角色情况（自述内容仅供参考，不可直接采信）："]
    for rec in role_recs:
        summary = (rec.result or "(无)")[:UPSTREAM_SUMMARY_LIMIT]
        parts.append(f"\n角色「{rec.role}」— 状态: {rec.status}")
        parts.append(f"任务: {rec.task}")
        parts.append(f"自述结果: {summary}")
        actions = extract_actions(rec.logs)
        if actions:
            parts.append("来自系统日志，可信的操作记录:")
            parts.extend(f"  {a}" for a in actions)
    parts.append("\n请验证以上内容，完成整合与验收，并按要求格式输出最终报告。")
    return "\n".join(parts)


class SharedSandbox:
    """One real sandbox (k8s_backend.K8sSandbox) shared by a collaborate
    job's 3 roles + integrator. start() runs exactly once; every exec/
    put_archive is serialized on a lock so two Agents are never mid-tool-call
    on the shared pod at the same instant.

    This prevents literal interleaving (two tar extractions, two concurrent
    k8s exec streams on one client) but NOT semantic conflicts (one role
    overwriting another's file, two roles both trying to bind port 8000) —
    those are prevented by the role prompts (build_role_system), not by
    this lock. LLM generation itself is never locked, so roles the planner
    declared independent still overlap in the one place that matters for
    speed (Ollama generation, which dominates wall-clock time).
    """

    def __init__(self, inner):
        self.inner = inner
        self._start_lock = threading.Lock()
        self._tool_lock = threading.Lock()
        self._url = None

    def start(self, log, should_stop):
        with self._start_lock:
            if self._url is None:
                self._url = self.inner.start(log, should_stop)
            return self._url

    @contextmanager
    def _turn(self, should_stop):
        while not self._tool_lock.acquire(timeout=0.25):
            if should_stop():
                raise md.AgentStopped("stopped while waiting for the shared sandbox")
        try:
            yield
        finally:
            self._tool_lock.release()

    def handle(self, stop_event: threading.Event):
        return SandboxHandle(self, stop_event)


class SandboxHandle:
    """What each Agent sees in collaborate mode: the Sandbox interface,
    routed through the shared SharedSandbox's lock."""

    def __init__(self, shared: SharedSandbox, stop_event: threading.Event):
        self._shared = shared
        self._stop_event = stop_event

    @property
    def host_port(self):
        return getattr(self._shared.inner, "host_port", None)

    def start(self, log, should_stop):
        return self._shared.start(log, should_stop)

    def exec(self, command, stdin_bytes=None):
        with self._shared._turn(self._stop_event.is_set):
            return self._shared.inner.exec(command, stdin_bytes)

    def put_archive(self, tar_bytes):
        with self._shared._turn(self._stop_event.is_set):
            self._shared.inner.put_archive(tar_bytes)

    def remove(self):
        self._shared.inner.remove()


def make_sandbox_resolver(sandbox_factory):
    """Returns resolve(rec) -> Sandbox-like object: today's one-sandbox-per-
    record passthrough when rec.sandbox_id is unset, or a shared, lock-
    guarded sandbox (cached by container_name) when it is set."""
    shared_cache = {}
    cache_lock = threading.Lock()

    def resolve(rec):
        if not rec.sandbox_id:
            return sandbox_factory(rec)
        with cache_lock:
            shared = shared_cache.get(rec.container_name)
            if shared is None:
                shared = SharedSandbox(sandbox_factory(rec))
                shared_cache[rec.container_name] = shared
        return shared.handle(rec.stop_event)

    return resolve


def make_run_fn(sandbox_factory, llm=None):
    """Build a run_fn for a given sandbox_factory (rec -> Sandbox) — the
    Orchestrator itself doesn't know or care how that sandbox is implemented,
    nor whether a given record shares its sandbox with others."""
    resolve = make_sandbox_resolver(sandbox_factory)

    def run_fn(orch, rec, log):
        agent = md.Agent(rec.container_name, sandbox=resolve(rec), log=log, stop_event=rec.stop_event, llm=llm)
        orch.set_url(rec.id, agent.start_sandbox())
        nudge = INTEGRATOR_NUDGE if rec.kind == "integrator" else None
        return agent.run(rec.prompt or rec.task, max_steps=rec.max_steps, extra_system=rec.extra_system,
                          min_tool_calls=rec.min_tool_calls, nudge=nudge, max_seconds=rec.max_seconds)
    return run_fn


default_run_fn = make_run_fn(kb.sandbox_factory)
default_remove_container_fn = kb.remove_agent_resources


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
        self._closed = False
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

        if plan.mode == "collaborate":
            with self._lock:
                job.mode = plan.mode
                job.reason = plan.reason
                job.status = "dispatched"
                self._dispatch_collaboration_locked(job, plan)
            return

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

    def _create_role_record(self, job: JobRecord, role: Role, idx: int, shared_name: str, sandbox_id: str) -> AgentRecord:
        """Create an AgentRecord for a single role in a collaborate job."""
        agent_id = f"{job.id}-{idx}"
        rec = AgentRecord(
            id=agent_id,
            job_id=job.id,
            container_name=shared_name,
            task=role.task,
            max_steps=ROLE_MAX_STEPS,
            status="waiting",
            created_at=time.time(),
            kind="role",
            role=role.name,
            depends_on=[f"{job.id}-{d}" for d in role.depends_on],
            sandbox_id=sandbox_id,
        )
        return rec

    def _create_integrator_record(self, job: JobRecord, role_recs: list, shared_name: str, sandbox_id: str) -> AgentRecord:
        """Create the integrator AgentRecord for a collaborate job."""
        integrator_id = f"{job.id}-int"
        integrator = AgentRecord(
            id=integrator_id,
            job_id=job.id,
            container_name=shared_name,
            task="整合与验收",
            max_steps=INTEGRATOR_MAX_STEPS,
            status="waiting",
            created_at=time.time(),
            kind="integrator",
            role="整合验收",
            depends_on=[rec.id for rec in role_recs],
            sandbox_id=sandbox_id,
            min_tool_calls=INTEGRATOR_MIN_TOOL_CALLS,
            max_seconds=INTEGRATOR_MAX_SECONDS,
        )
        return integrator

    def _dispatch_collaboration_locked(self, job: JobRecord, plan: Plan):
        shared_name = f"mini-devin-agent-{job.id}-shared"
        sandbox_id = f"{job.id}-shared"
        job.shared_container = shared_name

        role_recs = []
        for i, role in enumerate(plan.roles):
            rec = self._create_role_record(job, role, i, shared_name, sandbox_id)
            self._agents[rec.id] = rec
            job.agent_ids.append(rec.id)
            role_recs.append(rec)

        integrator = self._create_integrator_record(job, role_recs, shared_name, sandbox_id)
        self._agents[integrator.id] = integrator
        job.agent_ids.append(integrator.id)

        self._advance_job_locked(job.id)

    def _advance_job_locked(self, job_id: str):
        """Re-check a collaborate job's dependency graph: launch roles whose
        deps are all done, skip roles whose deps failed/stopped, and launch
        (or skip) the integrator once every role is terminal. No-op for
        parallel/serial jobs. Called with self._lock held."""
        job = self._jobs.get(job_id)
        if job is None or job.mode != "collaborate":
            return

        role_recs = [self._agents[aid] for aid in job.agent_ids if self._agents[aid].kind == "role"]
        integrator = next((self._agents[aid] for aid in job.agent_ids if self._agents[aid].kind == "integrator"), None)

        for rec in role_recs:
            if rec.status != "waiting":
                continue
            deps = [self._agents[d] for d in rec.depends_on if d in self._agents]
            dep_statuses = [d.status for d in deps]
            if any(s in _FAILED_STATUSES for s in dep_statuses):
                rec.status = "skipped"
                rec.result = "上游角色未成功完成，已跳过"
                rec.finished_at = time.time()
            elif all(s == "done" for s in dep_statuses):
                self._launch_locked(rec, build_role_task(rec, deps))

        if integrator is None or integrator.status != "waiting":
            return
        if not all(r.status in _TERMINAL_STATUSES for r in role_recs):
            return

        if any(r.status == "done" for r in role_recs):
            self._launch_locked(integrator, build_integrator_task(job.task, role_recs))
        else:
            integrator.status = "skipped"
            integrator.finished_at = time.time()
            job.final_status = "failed"
            job.final_result = "所有角色都未成功完成，未进行整合"

    def _launch_locked(self, rec: AgentRecord, prompt: str):
        if self._closed:
            rec.status = "stopped"
            rec.finished_at = time.time()
            return
        rec.prompt = prompt
        if rec.kind == "role":
            job = self._jobs[rec.job_id]
            role_recs = [self._agents[aid] for aid in job.agent_ids if self._agents[aid].kind == "role"]
            idx = next(i for i, r in enumerate(role_recs) if r.id == rec.id)
            rec.extra_system = build_role_system(job.task, role_recs, idx)
        elif rec.kind == "integrator":
            rec.extra_system = INTEGRATOR_SYSTEM
        rec.status = "queued"
        rec.future = self._pool.submit(self._run_agent, rec)

    def _run_agent(self, rec: AgentRecord):
        with self._lock:
            if rec.stop_event.is_set():
                rec.status = "stopped"
                rec.finished_at = time.time()
                self._finish_locked(rec)
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
                self._finish_locked(rec)

    def _finish_locked(self, rec: AgentRecord):
        """Called with self._lock held, on every exit path of _run_agent."""
        if rec.kind == "integrator":
            job = self._jobs.get(rec.job_id)
            if job is not None:
                job.final_status = rec.status
                job.final_result = rec.result
                job.final_verified = count_verification_categories(rec.logs) >= 2
        self._advance_job_locked(rec.job_id)

    def _log(self, agent_id: str, line: str):
        timestamped = f"[{time.strftime('%H:%M:%S')}] {line}"
        print(f"[{agent_id}] {line}")
        with self._lock:
            rec = self._agents.get(agent_id)
            if rec is None:
                return
            rec.logs.append(timestamped)
            rec.last_action = line[:120]

    def set_url(self, agent_id: str, url: str):
        with self._lock:
            rec = self._agents.get(agent_id)
            if rec is not None:
                rec.url = url

    def stop(self, agent_id: str) -> bool:
        with self._lock:
            rec = self._agents.get(agent_id)
            if rec is None:
                return False
            rec.stop_event.set()
            short_circuited = False
            if rec.status == "waiting":
                rec.status = "stopped"
                short_circuited = True
            elif rec.status == "queued" and rec.future is not None and rec.future.cancel():
                rec.status = "stopped"
                short_circuited = True
            if short_circuited:
                rec.finished_at = time.time()
                # This record will never reach _run_agent/_finish_locked, so
                # if it was the integrator, job.final_status must be set
                # here instead — otherwise it would stay None forever.
                if rec.kind == "integrator":
                    job = self._jobs.get(rec.job_id)
                    if job is not None:
                        job.final_status = "stopped"
                self._advance_job_locked(rec.job_id)
        return True

    def kill(self, agent_id: str) -> bool:
        with self._lock:
            rec = self._agents.get(agent_id)
            if rec is None:
                return False
            if rec.sandbox_id:
                # Can't remove a shared sandbox for just one role — killing
                # any member kills the whole collaborate job.
                job = self._jobs.get(rec.job_id)
                member_ids = list(job.agent_ids) if job is not None else [agent_id]
            else:
                member_ids = [agent_id]
            container_name = rec.container_name

        for aid in member_ids:
            self.stop(aid)
        self.remove_container_fn(container_name)
        return True

    def snapshot(self) -> dict:
        with self._lock:
            jobs = [j.to_dict() for j in self._jobs.values()]
            agents = [a.to_dict() for a in self._agents.values()]
            running = sum(1 for a in self._agents.values() if a.status == "running")
            queued = sum(1 for a in self._agents.values() if a.status == "queued")
            waiting = sum(1 for a in self._agents.values() if a.status == "waiting")
        return {
            "max_concurrent": self.max_concurrent,
            "running": running,
            "queued": queued,
            "waiting": waiting,
            "jobs": jobs,
            "agents": agents,
        }

    def shutdown(self):
        self._closed = True
        with self._lock:
            container_names = {a.container_name for a in self._agents.values()}
            for rec in self._agents.values():
                rec.stop_event.set()
        self._pool.shutdown(wait=False, cancel_futures=True)
        self._planner.shutdown(wait=False, cancel_futures=True)
        for name in container_names:
            self.remove_container_fn(name)
