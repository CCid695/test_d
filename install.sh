#!/usr/bin/env bash
# Installs all the environment dependencies needed for the mini-devin demo.
# Everything this script actually does gets recorded to .install_manifest, so
# uninstall.sh can roll it back precisely without touching anything that was
# already there before this script ran.
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MANIFEST="$PROJECT_DIR/.install_manifest"
VENV_DIR="$PROJECT_DIR/.venv"
ENV_FILE="$PROJECT_DIR/.env"
OLLAMA_PID_FILE="$PROJECT_DIR/.ollama_serve.pid"

# qwen2.5:3b was tested and does NOT reliably call tools with this demo's
# system prompt (returns empty output); qwen2.5:7b does. Override by
# exporting OLLAMA_MODEL before running this script.
OLLAMA_MODEL="${OLLAMA_MODEL:-qwen2.5:7b}"

: > "$MANIFEST"
record() { echo "$1" >> "$MANIFEST"; }

echo "==> 检查 Homebrew"
if ! command -v brew >/dev/null 2>&1; then
    echo "未检测到 Homebrew，正在安装最新版..."
    /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
    record "brew_installed_by_script=1"
else
    echo "Homebrew 已安装，跳过"
fi

echo "==> 检查 Docker"
if ! command -v docker >/dev/null 2>&1; then
    echo "未检测到 docker 命令，正在通过 Homebrew 安装最新版 Docker Desktop..."
    brew install --cask docker
    record "docker_cask_installed_by_script=1"
else
    echo "docker 命令已存在，跳过安装"
fi

echo "==> 检查 Docker 守护进程是否在运行"
if ! docker info >/dev/null 2>&1; then
    echo "Docker 守护进程未运行，尝试启动 Docker Desktop..."
    open -a Docker >/dev/null 2>&1 || true
    echo "等待 Docker 启动（最多 60 秒）..."
    for _ in $(seq 1 30); do
        docker info >/dev/null 2>&1 && break
        sleep 2
    done
    if ! docker info >/dev/null 2>&1; then
        echo "警告: Docker 仍未就绪，请手动打开 Docker Desktop 后重新运行本脚本。"
    fi
else
    echo "Docker 守护进程运行正常"
fi

echo "==> 检查 Ollama"
if ! command -v ollama >/dev/null 2>&1; then
    echo "未检测到 ollama 命令，正在通过 Homebrew 安装最新版..."
    brew install ollama
    record "ollama_installed_by_script=1"
else
    echo "ollama 命令已存在，跳过安装"
fi

echo "==> 检查 Ollama 服务是否在运行"
if ! curl -fsS http://localhost:11434/api/tags >/dev/null 2>&1; then
    echo "Ollama 服务未运行，正在后台启动 (ollama serve)..."
    nohup ollama serve >"$PROJECT_DIR/.ollama_serve.log" 2>&1 &
    echo $! > "$OLLAMA_PID_FILE"
    record "ollama_server_started_by_script=1"
    echo "等待 Ollama 启动（最多 30 秒）..."
    for _ in $(seq 1 15); do
        curl -fsS http://localhost:11434/api/tags >/dev/null 2>&1 && break
        sleep 2
    done
    if ! curl -fsS http://localhost:11434/api/tags >/dev/null 2>&1; then
        echo "警告: Ollama 服务仍未就绪，请手动运行 'ollama serve' 后重新运行本脚本。"
    fi
else
    echo "Ollama 服务运行正常"
fi

echo "==> 检查模型 $OLLAMA_MODEL"
if ! ollama list 2>/dev/null | awk '{print $1}' | grep -qx "$OLLAMA_MODEL"; then
    echo "本地没有 $OLLAMA_MODEL，正在下载（体积几 GB，视网速可能需要几分钟）..."
    ollama pull "$OLLAMA_MODEL"
    record "ollama_model_pulled=$OLLAMA_MODEL"
else
    echo "$OLLAMA_MODEL 已存在，跳过下载"
fi

echo "==> 创建 Python 虚拟环境 ($VENV_DIR)"
if [ ! -d "$VENV_DIR" ]; then
    python3 -m venv "$VENV_DIR"
    record "venv_created=1"
else
    echo "虚拟环境已存在，跳过创建"
fi

echo "==> 在虚拟环境中安装 Python 依赖"
"$VENV_DIR/bin/python3" -m pip install --upgrade pip
"$VENV_DIR/bin/python3" -m pip install -r "$PROJECT_DIR/requirements.txt"

echo "==> 检查 .env 配置文件"
if [ ! -f "$ENV_FILE" ]; then
    cat > "$ENV_FILE" <<EOF
# mini-devin config. Everything here runs locally — no API key needed.
OLLAMA_MODEL=$OLLAMA_MODEL
EOF
    record "env_file_created=1"
    echo "已创建 $ENV_FILE"
else
    echo ".env 已存在，跳过创建"
fi

cat <<EOF

安装完成。运行方式:
  source "$VENV_DIR/bin/activate"
  python3 mini_devin.py "帮我写个网页并运行"

卸载请运行 ./uninstall.sh（只会移除本脚本自己安装的东西）。
EOF
