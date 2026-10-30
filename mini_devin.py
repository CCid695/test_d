"""Mini Devin: local Ollama models control local Docker sandbox containers via tool calling.

Usage:
    python3 mini_devin.py "write a webpage and run it"

Requires:
    - Docker Desktop / Docker Engine running locally
    - Ollama running locally (http://localhost:11434) with OLLAMA_MODEL pulled

For running multiple agents concurrently with a dashboard, see dashboard.py.
"""

import io
import json
import os
import sys
import tarfile
import threading

import docker
from openai import OpenAI


def _load_dotenv(path):
    if not os.path.exists(path):
        return
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip())


_load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

# Tested against this task's system prompt: qwen2.5:3b consistently returned
# empty output and never called a tool at all. qwen2.5:7b reliably calls
# write_file/run_command and finishes the task. Don't drop below 7b without
# re-testing — override via the OLLAMA_MODEL env var if you do.
MODEL = os.environ.get("OLLAMA_MODEL", "qwen2.5:7b")
OLLAMA_BASE_URL = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434/v1")
MAX_STEPS = 10

CONTAINER_NAME = "mini-devin-sandbox"
IMAGE = "python:3.11-slim"
HOST_PORT = 8000
CONTAINER_PORT = "8000/tcp"

# Tag every agent-created container with this label so a dashboard/orchestrator
# can find and clean up containers left over from a previous run.
AGENT_LABEL = {"mini-devin.role": "agent"}

# api_key is unused by Ollama; timeout guards against one hung generation
# pinning a worker thread forever when multiple agents run concurrently.
llm_client = OpenAI(base_url=OLLAMA_BASE_URL, api_key="ollama", timeout=180)

_docker = None


def get_docker():
    """Lazily create the shared Docker client — not at import time, so importing
    this module doesn't fail just because Docker isn't running yet."""
    global _docker
    if _docker is None:
        _docker = docker.from_env()
    return _docker


class AgentStopped(Exception):
    """Raised inside Agent.run() when stop_event is set, so a cooperative stop
    can unwind cleanly instead of running to completion."""


def build_system_instruction(host_port: int) -> str:
    return f"""\
你是一个运行在本地 Docker 沙箱容器里的编程助手（mini Devin）。
你有两个工具：
- run_command: 在沙箱内执行 shell 命令
- write_file: 在沙箱内写文件

容器内 {CONTAINER_PORT} 端口已经映射到宿主机 {host_port} 端口。
如果用户要求写网页并运行，请把静态文件写到 /app 目录下，
然后用类似 `cd /app && nohup python3 -m http.server {CONTAINER_PORT.split('/')[0]} > /tmp/server.log 2>&1 &`
的命令在后台启动一个 HTTP 服务器（注意加 & 后台运行，不要阻塞），
最后告诉用户可以通过 http://localhost:{host_port} 访问。
完成后用中文简要总结你做了什么。
"""


# Ollama's OpenAI-compatible endpoint has no automatic function calling, so
# the tool schema and the dispatch loop are both done by hand in Agent.run().
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "run_command",
            "description": (
                "Execute a shell command inside the Docker sandbox container and return its output. "
                "To start a long-running service in the background (e.g. a web server), append `&` "
                "to the command and redirect output, e.g.: "
                "`cd /app && nohup python3 -m http.server 8000 > /tmp/server.log 2>&1 &`"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "The shell command to run inside the container."},
                },
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Create or overwrite a file inside the Docker sandbox container.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Absolute path inside the container, e.g. /app/index.html."},
                    "content": {"type": "string", "description": "The full text content of the file."},
                },
                "required": ["path", "content"],
            },
        },
    },
]

SYSTEM_INSTRUCTION = build_system_instruction(HOST_PORT)


class Agent:
    """One LLM-driven worker with its own Docker sandbox container.

    Multiple Agent instances can run concurrently (e.g. from an orchestrator's
    thread pool) as long as each has a distinct container_name.
    """

    def __init__(self, container_name=CONTAINER_NAME, host_port=HOST_PORT, *,
                 log=print, stop_event=None, labels=None, llm=None):
        self.container_name = container_name
        self.host_port = host_port  # None means "let Docker assign a free port"
        self.log = log
        self.stop_event = stop_event or threading.Event()
        self.labels = labels
        self.llm = llm or llm_client
        self.docker = docker.from_env()
        self.tool_functions = {"run_command": self.run_command, "write_file": self.write_file}

    def _check_stop(self):
        if self.stop_event.is_set():
            raise AgentStopped(f"agent for container {self.container_name} was stopped")

    def container(self):
        return self.docker.containers.get(self.container_name)

    def start_sandbox(self):
        """Ensure the sandbox container exists and is running, resolving self.host_port
        (picking a free one if it was None)."""
        ports = {CONTAINER_PORT: self.host_port}
        try:
            container = self.container()
            if container.status != "running":
                container.start()
        except docker.errors.NotFound:
            container = self.docker.containers.run(
                IMAGE,
                name=self.container_name,
                command="sleep infinity",
                ports=ports,
                detach=True,
                labels=self.labels,
            )

        if self.host_port is None:
            container.reload()
            bindings = container.attrs["NetworkSettings"]["Ports"][CONTAINER_PORT]
            self.host_port = int(bindings[0]["HostPort"])

        self.log(f"[sandbox] 容器 {self.container_name} 已就绪，http://localhost:{self.host_port}")
        return container

    def run_command(self, command: str) -> str:
        self._check_stop()
        self.log(f"[run_command] {command}")
        result = self.container().exec_run(["sh", "-c", command], demux=True)
        stdout, stderr = result.output
        output = (stdout or b"").decode(errors="replace") + (stderr or b"").decode(errors="replace")
        self.log(f"[run_command] exit={result.exit_code} output={output[:500]!r}")
        return output or f"(no output, exit code {result.exit_code})"

    def write_file(self, path: str, content: str) -> str:
        self._check_stop()
        self.log(f"[write_file] {path} ({len(content)} 字节)")
        data = content.encode()
        tar_stream = io.BytesIO()
        with tarfile.open(fileobj=tar_stream, mode="w") as tar:
            info = tarfile.TarInfo(name=path.lstrip("/"))
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
        tar_stream.seek(0)
        self.container().put_archive("/", tar_stream)
        return f"已写入 {path}"

    def run(self, task: str, max_steps: int = MAX_STEPS) -> str:
        messages = [
            {"role": "system", "content": build_system_instruction(self.host_port)},
            {"role": "user", "content": task},
        ]

        for _ in range(max_steps):
            self._check_stop()
            response = self.llm.chat.completions.create(model=MODEL, messages=messages, tools=TOOLS)
            message = response.choices[0].message
            messages.append(message.model_dump(exclude_none=True))

            if not message.tool_calls:
                return message.content

            for call in message.tool_calls:
                self._check_stop()
                try:
                    fn = self.tool_functions[call.function.name]
                    args = json.loads(call.function.arguments)
                    result = fn(**args)
                except AgentStopped:
                    raise
                except Exception as exc:
                    result = f"error calling {call.function.name}: {exc}"
                messages.append({"role": "tool", "tool_call_id": call.id, "content": result})

        return "(reached max steps without a final answer)"

    def remove(self):
        try:
            self.container().remove(force=True)
        except docker.errors.NotFound:
            pass


# --- Module-level compatibility wrappers so the existing CLI and tests,
# which call these as free functions against the one default sandbox
# container, keep working unchanged. ---

_default_agent = None


def _default():
    global _default_agent
    if _default_agent is None:
        _default_agent = Agent(CONTAINER_NAME, HOST_PORT)
    return _default_agent


def get_container():
    return _default().container()


def start_sandbox():
    return _default().start_sandbox()


def run_command(command: str) -> str:
    return _default().run_command(command)


def write_file(path: str, content: str) -> str:
    return _default().write_file(path, content)


def run_agent_loop(task: str) -> str:
    return _default().run(task)


def main():
    task = sys.argv[1] if len(sys.argv) > 1 else "帮我写个网页并运行"

    start_sandbox()

    print(f"\n[user] {task}\n")
    final_answer = run_agent_loop(task)
    print(f"\n[{MODEL}] {final_answer}")


if __name__ == "__main__":
    main()
