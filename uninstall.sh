#!/usr/bin/env bash
# Reverts what install.sh did. Only removes what .install_manifest records
# install.sh itself installed/created — never touches a pre-existing Homebrew/Docker/Ollama/dependency.
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MANIFEST="$PROJECT_DIR/.install_manifest"
VENV_DIR="$PROJECT_DIR/.venv"
ENV_FILE="$PROJECT_DIR/.env"
OLLAMA_PID_FILE="$PROJECT_DIR/.ollama_serve.pid"
OLLAMA_LOG_FILE="$PROJECT_DIR/.ollama_serve.log"

if [ ! -f "$MANIFEST" ]; then
    echo "$MANIFEST not found: install.sh hasn't been run yet, or manifest has been cleaned up, nothing to do."
    exit 0
fi

has() { grep -qx "$1=1" "$MANIFEST" 2>/dev/null; }
pulled_model() { grep "^ollama_model_pulled=" "$MANIFEST" 2>/dev/null | cut -d= -f2-; }

echo "Reverting install.sh changes based on $MANIFEST:"

if [ -d "$VENV_DIR" ] && has venv_created; then
    echo "  Deleting virtual environment $VENV_DIR"
    rm -rf "$VENV_DIR"
fi

if [ -f "$ENV_FILE" ] && has env_file_created; then
    echo "  Deleting $ENV_FILE"
    rm -f "$ENV_FILE"
fi

if has ollama_server_started_by_script && [ -f "$OLLAMA_PID_FILE" ]; then
    pid="$(cat "$OLLAMA_PID_FILE")"
    if ps -p "$pid" >/dev/null 2>&1; then
        echo "  Stopping ollama serve process started by install.sh (pid $pid)"
        kill "$pid" 2>/dev/null || true
    fi
    rm -f "$OLLAMA_PID_FILE" "$OLLAMA_LOG_FILE"
fi

model="$(pulled_model)"
if [ -n "$model" ]; then
    read -r -p "install.sh pulled model $model, delete it now to free disk space? [y/N] " reply
    if [[ "$reply" =~ ^[Yy]$ ]]; then
        ollama rm "$model" || true
    else
        echo "  Skipping deletion of model $model"
    fi
fi

if has ollama_installed_by_script; then
    read -r -p "install.sh installed ollama via Homebrew, uninstall it now? [y/N] " reply
    if [[ "$reply" =~ ^[Yy]$ ]]; then
        brew uninstall ollama
    else
        echo "  Skipping uninstall of ollama"
    fi
fi

if has docker_cask_installed_by_script; then
    read -r -p "install.sh installed Docker Desktop via Homebrew, uninstall it now (will remove the entire Docker Desktop app)? [y/N] " reply
    if [[ "$reply" =~ ^[Yy]$ ]]; then
        brew uninstall --cask docker
    else
        echo "  Skipping uninstall of Docker Desktop"
    fi
fi

if command -v kubectl >/dev/null 2>&1 && kubectl --context minikube get namespace mini-devin >/dev/null 2>&1; then
    read -r -p "Delete mini-devin namespace (contains all Deployment/Service/Ingress/Secret created by dashboard)? [y/N] " reply
    if [[ "$reply" =~ ^[Yy]$ ]]; then
        kubectl --context minikube delete namespace mini-devin
    else
        echo "  Skipping deletion of mini-devin namespace"
    fi
fi

if has brew_installed_by_script; then
    echo "  Note: install.sh also installed Homebrew itself."
    echo "  Homebrew may be depended on by other software, this script will not automatically uninstall it;"
    echo "  if you need to uninstall it, run the official Homebrew uninstall script yourself."
fi

rm -f "$MANIFEST"
echo "Uninstall complete."
