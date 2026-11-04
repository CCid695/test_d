"""Mini Devin: local Ollama models control sandboxed agents via tool calling.

Usage:
    python3 mini_devin.py "write a webpage and run it"

Requires:
    - A Kubernetes cluster (e.g. minikube) with ingress-nginx, reachable at
      MINI_DEVIN_KUBE_CONTEXT (see k8s_backend.py) — the CLI's single agent
      runs as a real Deployment/Service/Ingress, same as every agent the
      dashboard spawns.
    - Ollama running locally (http://localhost:11434) with OLLAMA_MODEL pulled

For running multiple agents concurrently with a dashboard, see dashboard.py.
"""

import io
import json
import os
import sys
import tarfile
import threading
import time

from openai import OpenAI

import config as cfg


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

CLI_CONTAINER_NAME = "mini-devin-cli"
CLI_AGENT_ID = "cli"

# api_key is unused by Ollama; timeout guards against one hung generation
# pinning a worker thread forever when multiple agents run concurrently.
llm_client = OpenAI(base_url=OLLAMA_BASE_URL, api_key="ollama", timeout=180)


class AgentStopped(Exception):
    """Raised inside Agent.run() when stop_event is set, so a cooperative stop
    can unwind cleanly instead of running to completion."""


def build_system_instruction(url: str) -> str:
    return f"""\
You are a programming assistant running in a sandboxed container (mini Devin).
You have two tools:
- run_command: Execute shell commands inside the sandbox
- write_file: Write files inside the sandbox

Port {cfg.SANDBOX_PORT} inside the sandbox is mapped to {url}.
If the user asks you to write a webpage and run it, write static files to the /app directory,
then start an HTTP server in the background using a command like
`cd /app && nohup python3 -m http.server {cfg.SANDBOX_PORT} > /tmp/server.log 2>&1 &`
(note: add & to run in the background, don't block),
and finally tell the user they can access it via {url}.
For other files referenced in the webpage (e.g. style.css, images), use relative paths, don't start with /.
After completion, briefly summarize in English what you did.
"""


# Ollama's OpenAI-compatible endpoint has no automatic function calling, so
# the tool schema and the dispatch loop are both done by hand in Agent.run().
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "run_command",
            "description": (
                "Execute a shell command inside the sandbox and return its output. "
                "To start a long-running service in the background (e.g. a web server), append `&` "
                "to the command and redirect output, e.g.: "
                "`cd /app && nohup python3 -m http.server 8000 > /tmp/server.log 2>&1 &`"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "The shell command to run inside the sandbox."},
                },
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Create or overwrite a file inside the sandbox.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Absolute path inside the sandbox, e.g. /app/index.html."},
                    "content": {"type": "string", "description": "The full text content of the file."},
                },
                "required": ["path", "content"],
            },
        },
    },
]

def _build_tar(path: str, content: str) -> bytes:
    """Build an in-memory tar archive containing one file, so it can be
    extracted into a sandbox without shell-escaping its content."""
    data = content.encode()
    tar_stream = io.BytesIO()
    with tarfile.open(fileobj=tar_stream, mode="w") as tar:
        info = tarfile.TarInfo(name=path.lstrip("/"))
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    return tar_stream.getvalue()


class Agent:
    """One LLM-driven worker with its own sandbox.

    Multiple Agent instances can run concurrently (e.g. from an orchestrator's
    thread pool) as long as each has a distinct sandbox. `sandbox` must
    implement the Sandbox interface (start/exec/put_archive/remove) — in
    practice, k8s_backend.K8sSandbox. Agent itself doesn't know or care what
    backend the sandbox is on. Multiple Agents can also share one sandbox
    (see orchestrator.SharedSandbox) for collaborative multi-role tasks — the
    caller is responsible for serializing concurrent tool calls in that case.
    """

    def __init__(self, container_name, *, sandbox, log=print, stop_event=None, llm=None):
        self.container_name = container_name
        self.log = log
        self.stop_event = stop_event or threading.Event()
        self.llm = llm or llm_client
        self.sandbox = sandbox
        self.url = None
        self.tool_functions = {"run_command": self.run_command, "write_file": self.write_file}

    def _check_stop(self):
        if self.stop_event.is_set():
            raise AgentStopped(f"agent {self.container_name} was stopped")

    def start_sandbox(self) -> str:
        self._check_stop()
        self.url = self.sandbox.start(self.log, self.stop_event.is_set)
        return self.url

    def run_command(self, command: str) -> str:
        self._check_stop()
        self.log(f"[run_command] {command}")
        exit_code, output = self.sandbox.exec(command)
        self.log(f"[run_command] exit={exit_code} output={output[:500]!r}")
        return output or f"(no output, exit code {exit_code})"

    def write_file(self, path: str, content: str) -> str:
        self._check_stop()
        self.log(f"[write_file] {path} ({len(content)} bytes)")
        self.sandbox.put_archive(_build_tar(path, content))
        return f"Wrote {path}"

    def _build_messages(self, task: str, extra_system: str = None) -> list:
        """Build the initial message list for the LLM conversation."""
        url = self.url or "(unknown URL — start_sandbox() wasn't called first)"
        system_content = build_system_instruction(url)
        if extra_system:
            system_content += "\n\n" + extra_system
        return [
            {"role": "system", "content": system_content},
            {"role": "user", "content": task},
        ]

    def _check_time_budget(self, start: float, max_seconds: float, tool_call_count: int) -> tuple:
        """Check if time budget is exhausted.

        Returns (should_stop, timeout_value) where should_stop is True if
        the budget is exhausted, and timeout_value is the remaining time
        (or None if no max_seconds is set).
        """
        if max_seconds is None:
            return False, None

        remaining = max_seconds - (time.monotonic() - start)
        if remaining <= 0:
            return True, None
        return False, remaining

    def _make_llm_call(self, messages: list, max_seconds: float = None, start: float = None) -> tuple:
        """Make an LLM call with optional timeout budget.

        Returns (message, timed_out) where timed_out is True if the call
        hit the time budget.
        """
        call_kwargs = {"model": MODEL, "messages": messages, "tools": TOOLS}
        if max_seconds is not None and start is not None:
            should_stop, timeout = self._check_time_budget(start, max_seconds, 0)
            if should_stop:
                return None, True
            call_kwargs["timeout"] = timeout

        try:
            response = self.llm.chat.completions.create(**call_kwargs)
            return response.choices[0].message, False
        except Exception:
            if max_seconds is not None and start is not None:
                if time.monotonic() - start >= max_seconds - 0.5:
                    return None, True
            raise

    def _handle_tool_calls(self, tool_calls, messages: list) -> int:
        """Execute tool calls and append results to messages.

        Returns the number of tool calls executed.
        """
        tool_call_count = 0
        for call in tool_calls:
            self._check_stop()
            try:
                fn = self.tool_functions[call.function.name]
                args = json.loads(call.function.arguments)
                result = fn(**args)
                tool_call_count += 1
            except AgentStopped:
                raise
            except Exception as exc:
                result = f"error calling {call.function.name}: {exc}"
            messages.append({"role": "tool", "tool_call_id": call.id, "content": result})
        return tool_call_count

    def _build_nudge_message(self, tool_call_count: int, nudge: str = None) -> str:
        """Build a nudge message when the model tries to stop early."""
        return nudge or (
            f"You have only actually called tools {tool_call_count} times, "
            "and haven't really completed the required verification steps. "
            "Don't describe in text what you 'did' — you must actually call run_command/write_file, "
            "use the real return results from the tools as evidence, then reach a conclusion."
        )

    def run(self, task: str, max_steps: int = MAX_STEPS, extra_system: str = None,
            min_tool_calls: int = None, nudge: str = None, max_seconds: float = None) -> str:
        """Run the tool-calling loop.

        If min_tool_calls is set, a model that tries to stop (no tool_calls)
        before reaching that many real tool calls gets a `nudge` message
        appended instead of being allowed to return — this exists because
        we've observed the model fabricate an elaborate-looking verification
        narrative in its final text after only one or two real tool calls,
        instead of actually running the checks its own system prompt asked
        for. Each retry still counts against max_steps.

        If max_seconds is set, this is a hard wall-clock budget, not just a
        step count — because min_tool_calls alone can keep a slow local
        model "working" for a long time with no visible progress (a nudge
        round that produces no tool call logs nothing). Before each LLM
        call we check the remaining budget: if it's already gone, we stop
        without even attempting the call; otherwise the *remaining* time is
        passed as that call's own `timeout=`, so a slow final call can't
        itself blow past the deadline. A timeout on that call is treated as
        "ran out of time," not an error.
        """
        messages = self._build_messages(task, extra_system)

        start = time.monotonic()
        tool_call_count = 0
        for _ in range(max_steps):
            self._check_stop()

            message, timed_out = self._make_llm_call(messages, max_seconds, start)
            if timed_out:
                return (f"(Exceeded {max_seconds:.0f} second time limit, verification stopped; "
                         f"completed {tool_call_count} real tool calls during this time)")

            messages.append(message.model_dump(exclude_none=True))

            if not message.tool_calls:
                if min_tool_calls is not None and tool_call_count < min_tool_calls:
                    messages.append({"role": "user", "content": self._build_nudge_message(tool_call_count, nudge)})
                    continue
                return message.content

            tool_call_count += self._handle_tool_calls(message.tool_calls, messages)

        return "(reached max steps without a final answer)"

    def remove(self):
        self.sandbox.remove()


# --- Module-level compatibility wrappers so the existing CLI and tests keep
# calling these as free functions against the one default sandbox. Always
# Kubernetes-backed. ---

_default_agent = None


def _default():
    global _default_agent
    if _default_agent is None:
        import k8s_backend as kb
        _default_agent = Agent(CLI_CONTAINER_NAME, sandbox=kb.K8sSandbox(CLI_CONTAINER_NAME, CLI_AGENT_ID))
    return _default_agent


def start_sandbox() -> str:
    return _default().start_sandbox()


def run_command(command: str) -> str:
    return _default().run_command(command)


def write_file(path: str, content: str) -> str:
    return _default().write_file(path, content)


def run_agent_loop(task: str) -> str:
    return _default().run(task)


def main():
    task = sys.argv[1] if len(sys.argv) > 1 else "write a webpage and run it"

    import k8s_backend as kb
    port_forward = kb.IngressPortForward()
    port_forward.start()
    try:
        start_sandbox()
        print(f"\n[user] {task}\n")
        final_answer = run_agent_loop(task)
        print(f"\n[{MODEL}] {final_answer}")
    finally:
        port_forward.stop()


if __name__ == "__main__":
    main()
