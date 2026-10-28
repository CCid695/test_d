#!/usr/bin/env bash
# Reverts what install.sh did. Only removes what .install_manifest records
# install.sh itself installed/created — never touches a pre-existing Homebrew/Docker/dependency.
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MANIFEST="$PROJECT_DIR/.install_manifest"
VENV_DIR="$PROJECT_DIR/.venv"
ENV_FILE="$PROJECT_DIR/.env"

if [ ! -f "$MANIFEST" ]; then
    echo "未找到 $MANIFEST：install.sh 还没运行过，或 manifest 已被清理，无事可做。"
    exit 0
fi

has() { grep -qx "$1=1" "$MANIFEST" 2>/dev/null; }

echo "根据 $MANIFEST 撤销 install.sh 的改动："

if [ -d "$VENV_DIR" ] && has venv_created; then
    echo "  删除虚拟环境 $VENV_DIR"
    rm -rf "$VENV_DIR"
fi

if [ -f "$ENV_FILE" ] && has env_file_created; then
    echo "  删除 $ENV_FILE"
    rm -f "$ENV_FILE"
fi

if has docker_cask_installed_by_script; then
    read -r -p "install.sh 当时通过 Homebrew 安装了 Docker Desktop，是否现在卸载它（会移除整个 Docker Desktop 应用）？[y/N] " reply
    if [[ "$reply" =~ ^[Yy]$ ]]; then
        brew uninstall --cask docker
    else
        echo "  跳过卸载 Docker Desktop"
    fi
fi

if has brew_installed_by_script; then
    echo "  注意: install.sh 当时还安装了 Homebrew 本身。"
    echo "  Homebrew 可能被其他软件依赖，本脚本不会自动卸载它；"
    echo "  如需卸载请自行运行 Homebrew 官方卸载脚本。"
fi

rm -f "$MANIFEST"
echo "卸载完成。"
