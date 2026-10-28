"""Mini Devin: a local Ollama model controls a local Docker sandbox container via tool calling.

Usage:
    python3 mini_devin.py "write a webpage and run it"

Requires:
    - Docker Desktop / Docker Engine running locally
    - Ollama running locally (http://localhost:11434) with OLLAMA_MODEL pulled
"""

import io
import json
import os
import sys
import tarfile

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

# qwen2.5:3b is the smallest Ollama model that still has usable tool-calling
# support for this kind of multi-step task. Below 3b, tool-call formatting
# gets unreliable; bump to qwen2.5:7b (OLLAMA_MODEL env var) if 3b misbehaves.
MODEL = os.environ.get("OLLAMA_MODEL", "qwen2.5:3b")
OLLAMA_BASE_URL = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434/v1")
MAX_STEPS = 10

CONTAINER_NAME = "mini-devin-sandbox"
IMAGE = "python:3.11-slim"
HOST_PORT = 8000
CONTAINER_PORT = "8000/tcp"

docker_client = docker.from_env()
llm_client = OpenAI(base_url=OLLAMA_BASE_URL, api_key="ollama")  # api_key is unused by Ollama


def get_container():
    return docker_client.containers.get(CONTAINER_NAME)


def start_sandbox():
    """Ensure the sandbox container exists and is running, with container port 8000 mapped to host port 8000."""
    try:
        container = get_container()
        if container.status != "running":
            container.start()
    except docker.errors.NotFound:
        container = docker_client.containers.run(
            IMAGE,
            name=CONTAINER_NAME,
            command="sleep infinity",
            ports={CONTAINER_PORT: HOST_PORT},
            detach=True,
        )
    print(f"[sandbox] 容器 {CONTAINER_NAME} 已就绪，http://localhost:{HOST_PORT}")
    return container


def run_command(command: str) -> str:
    print(f"[run_command] {command}")
    result = get_container().exec_run(["sh", "-c", command], demux=True)
    stdout, stderr = result.output
    output = (stdout or b"").decode(errors="replace") + (stderr or b"").decode(errors="replace")
    print(f"[run_command] exit={result.exit_code} output={output[:500]!r}")
    return output or f"(no output, exit code {result.exit_code})"


def write_file(path: str, content: str) -> str:
    print(f"[write_file] {path} ({len(content)} 字节)")
    data = content.encode()
    tar_stream = io.BytesIO()
    with tarfile.open(fileobj=tar_stream, mode="w") as tar:
        info = tarfile.TarInfo(name=path.lstrip("/"))
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    tar_stream.seek(0)
    get_container().put_archive("/", tar_stream)
    return f"已写入 {path}"


# Ollama's OpenAI-compatible endpoint has no automatic function calling, so
# the tool schema and the dispatch loop below are both done by hand.
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

TOOL_FUNCTIONS = {"run_command": run_command, "write_file": write_file}

SYSTEM_INSTRUCTION = f"""\
你是一个运行在本地 Docker 沙箱容器里的编程助手（mini Devin）。
你有两个工具：
- run_command: 在沙箱内执行 shell 命令
- write_file: 在沙箱内写文件

容器内 {CONTAINER_PORT} 端口已经映射到宿主机 {HOST_PORT} 端口。
如果用户要求写网页并运行，请把静态文件写到 /app 目录下，
然后用类似 `cd /app && nohup python3 -m http.server {CONTAINER_PORT.split('/')[0]} > /tmp/server.log 2>&1 &`
的命令在后台启动一个 HTTP 服务器（注意加 & 后台运行，不要阻塞），
最后告诉用户可以通过 http://localhost:{HOST_PORT} 访问。
完成后用中文简要总结你做了什么。
"""


def run_agent_loop(task: str):
    messages = [
        {"role": "system", "content": SYSTEM_INSTRUCTION},
        {"role": "user", "content": task},
    ]

    for _ in range(MAX_STEPS):
        response = llm_client.chat.completions.create(model=MODEL, messages=messages, tools=TOOLS)
        message = response.choices[0].message
        messages.append(message.model_dump(exclude_none=True))

        if not message.tool_calls:
            return message.content

        for call in message.tool_calls:
            fn = TOOL_FUNCTIONS[call.function.name]
            args = json.loads(call.function.arguments)
            result = fn(**args)
            messages.append({"role": "tool", "tool_call_id": call.id, "content": result})

    return "(reached max steps without a final answer)"


def main():
    task = sys.argv[1] if len(sys.argv) > 1 else "帮我写个网页并运行"

    start_sandbox()

    print(f"\n[user] {task}\n")
    final_answer = run_agent_loop(task)
    print(f"\n[{MODEL}] {final_answer}")


if __name__ == "__main__":
    main()
