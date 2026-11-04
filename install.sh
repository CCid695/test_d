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

echo "==> Checking Homebrew"
if ! command -v brew >/dev/null 2>&1; then
    echo "Homebrew not detected, installing latest version..."
    /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
    record "brew_installed_by_script=1"
else
    echo "Homebrew already installed, skipping"
fi

echo "==> Checking Docker"
# The app itself no longer talks to Docker directly — every sandbox is a
# real Kubernetes Deployment/Pod now. Docker Desktop is still required
# because minikube's docker driver runs the cluster's node AS a Docker
# container; this check exists for that, not for the app.
if ! command -v docker >/dev/null 2>&1; then
    echo "docker command not detected, installing latest Docker Desktop via Homebrew..."
    brew install --cask docker
    record "docker_cask_installed_by_script=1"
else
    echo "docker command already exists, skipping installation"
fi

echo "==> Checking if Docker daemon is running (required by minikube's docker driver)"
if ! docker info >/dev/null 2>&1; then
    echo "Docker daemon not running, attempting to start Docker Desktop..."
    open -a Docker >/dev/null 2>&1 || true
    echo "Waiting for Docker to start (up to 60 seconds)..."
    for _ in $(seq 1 30); do
        docker info >/dev/null 2>&1 && break
        sleep 2
    done
    if ! docker info >/dev/null 2>&1; then
        echo "Warning: Docker still not ready, please manually open Docker Desktop and re-run this script."
    fi
else
    echo "Docker daemon running normally"
fi

echo "==> Checking Ollama"
if ! command -v ollama >/dev/null 2>&1; then
    echo "ollama command not detected, installing latest version via Homebrew..."
    brew install ollama
    record "ollama_installed_by_script=1"
else
    echo "ollama command already exists, skipping installation"
fi

echo "==> Checking if Ollama service is running"
if ! curl -fsS http://localhost:11434/api/tags >/dev/null 2>&1; then
    echo "Ollama service not running, starting in background (ollama serve)..."
    # OLLAMA_NUM_PARALLEL=3 lets the server actually generate for up to 3
    # concurrent agents (dashboard.py); without it the demo still works,
    # LLM calls just serialize while agents' container commands still overlap.
    OLLAMA_NUM_PARALLEL=3 nohup ollama serve >"$PROJECT_DIR/.ollama_serve.log" 2>&1 &
    echo $! > "$OLLAMA_PID_FILE"
    record "ollama_server_started_by_script=1"
    echo "Waiting for Ollama to start (up to 30 seconds)..."
    for _ in $(seq 1 15); do
        curl -fsS http://localhost:11434/api/tags >/dev/null 2>&1 && break
        sleep 2
    done
    if ! curl -fsS http://localhost:11434/api/tags >/dev/null 2>&1; then
        echo "Warning: Ollama service still not ready, please manually run 'ollama serve' and re-run this script."
    fi
else
    echo "Ollama service running normally"
fi

echo "==> Checking model $OLLAMA_MODEL"
if ! ollama list 2>/dev/null | awk '{print $1}' | grep -qx "$OLLAMA_MODEL"; then
    echo "$OLLAMA_MODEL not available locally, downloading (several GB, may take a few minutes depending on network speed)..."
    ollama pull "$OLLAMA_MODEL"
    record "ollama_model_pulled=$OLLAMA_MODEL"
else
    echo "$OLLAMA_MODEL already exists, skipping download"
fi

echo "==> Checking Kubernetes (required by both mini_devin.py and dashboard.py, nothing works without an available cluster)"
K8S_READY=1
if ! command -v kubectl >/dev/null 2>&1; then
    echo "kubectl not detected"
    K8S_READY=0
elif ! command -v minikube >/dev/null 2>&1; then
    echo "minikube not detected"
    K8S_READY=0
elif ! kubectl --context minikube cluster-info --request-timeout=5s >/dev/null 2>&1; then
    echo "minikube cluster not reachable (kubectl --context minikube cluster-info failed)"
    K8S_READY=0
elif ! kubectl --context minikube get ingressclass nginx >/dev/null 2>&1; then
    echo "ingress-nginx not found in minikube cluster (kubectl get ingressclass nginx failed)"
    K8S_READY=0
fi
if [ "$K8S_READY" = "0" ]; then
    cat <<'EOF'
Warning: Kubernetes cluster not ready. Both CLI and dashboard now rely on real K8s Pod/Service/Ingress
as sandboxes; without an available cluster, neither will work — this is not optional. Fix with:
  minikube start --driver=docker && minikube addons enable ingress
(This script does not automatically install/start minikube — cluster driver, resource allocation, etc. are
 machine-specific choices; automatic installation or deletion could affect other cluster loads running on your
 machine, so this script only checks without taking action. Fix and re-run this script.)
EOF
else
    echo "kubectl/minikube/ingress-nginx all normal"
fi

echo "==> Creating Python virtual environment ($VENV_DIR)"
if [ ! -d "$VENV_DIR" ]; then
    python3 -m venv "$VENV_DIR"
    record "venv_created=1"
else
    echo "Virtual environment already exists, skipping creation"
fi

echo "==> Installing Python dependencies in virtual environment"
"$VENV_DIR/bin/python3" -m pip install --upgrade pip
"$VENV_DIR/bin/python3" -m pip install -r "$PROJECT_DIR/requirements.txt"

echo "==> Checking .env configuration file"
if [ ! -f "$ENV_FILE" ]; then
    cat > "$ENV_FILE" <<EOF
# mini-devin config. Everything here runs locally — no API key needed.
OLLAMA_MODEL=$OLLAMA_MODEL
EOF
    record "env_file_created=1"
    echo "Created $ENV_FILE"
else
    echo ".env already exists, skipping creation"
fi

cat <<EOF

Installation complete. Both paths use real Kubernetes Pod/Service/Ingress sandboxes:
  Single agent (command line):
    source "$VENV_DIR/bin/activate"
    python3 mini_devin.py "write a webpage and run it"

  Multi-agent dashboard (web):
    source "$VENV_DIR/bin/activate"
    python3 dashboard.py
    Then open http://localhost:5050 (dashboard startup log will print the command to retrieve the token)

To uninstall, run ./uninstall.sh (will only remove what this script installed).
EOF
