"""Mini Devin: Gemini controls a local Docker sandbox container via function calling.

Usage:
    export GEMINI_API_KEY=your_key
    python mini_devin.py "write a webpage and run it"

Requires Docker Desktop / Docker Engine to be installed and running locally.
"""

import io
import os
import sys
import tarfile

import docker
from google import genai


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

MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
CONTAINER_NAME = "mini-devin-sandbox"
IMAGE = "python:3.11-slim"
HOST_PORT = 8000
CONTAINER_PORT = "8000/tcp"

docker_client = docker.from_env()


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
    """Execute a shell command inside the Docker sandbox container and return its output.

    To start a long-running service in the background (e.g. a web server), append `&`
    to the command and redirect output, e.g.: `cd /app && nohup python3 -m http.server 8000 > /tmp/server.log 2>&1 &`

    Args:
        command: The shell command to run inside the container.
    """
    print(f"[run_command] {command}")
    result = get_container().exec_run(["sh", "-c", command], demux=True)
    stdout, stderr = result.output
    output = (stdout or b"").decode(errors="replace") + (stderr or b"").decode(errors="replace")
    print(f"[run_command] exit={result.exit_code} output={output[:500]!r}")
    return output or f"(no output, exit code {result.exit_code})"


def write_file(path: str, content: str) -> str:
    """Create or overwrite a file inside the Docker sandbox container.

    Args:
        path: Absolute path inside the container, e.g. /app/index.html.
        content: The full text content of the file.
    """
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


def main():
    task = sys.argv[1] if len(sys.argv) > 1 else "帮我写个网页并运行"

    if "GEMINI_API_KEY" not in os.environ:
        sys.exit("请先设置环境变量 GEMINI_API_KEY")

    start_sandbox()

    client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
    chat = client.chats.create(
        model=MODEL,
        config={
            "tools": [run_command, write_file],
            "system_instruction": SYSTEM_INSTRUCTION,
        },
    )

    print(f"\n[user] {task}\n")
    response = chat.send_message(task)
    print(f"\n[gemini] {response.text}")


if __name__ == "__main__":
    main()
