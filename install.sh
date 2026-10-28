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
    cat > "$ENV_FILE" <<'EOF'
# mini-devin config. GEMINI_API_KEY is intentionally not written here —
# for security, always set it with `export` in your shell, never store it in a file.
GEMINI_MODEL=gemini-2.5-flash
EOF
    record "env_file_created=1"
    echo "已创建 $ENV_FILE"
else
    echo ".env 已存在，跳过创建"
fi

echo "==> 检查 GEMINI_API_KEY 环境变量"
if [ -z "${GEMINI_API_KEY:-}" ]; then
    echo "警告: 尚未设置 GEMINI_API_KEY。请在运行前执行:"
    echo "  export GEMINI_API_KEY=你的key"
else
    echo "GEMINI_API_KEY 已设置"
fi

cat <<EOF

安装完成。运行方式:
  source "$VENV_DIR/bin/activate"
  export GEMINI_API_KEY=your_key   # if not already set
  python3 mini_devin.py "帮我写个网页并运行"

卸载请运行 ./uninstall.sh（只会移除本脚本自己安装的东西）。
EOF
