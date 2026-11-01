# mini-devin

A minimal "Devin"-style sandbox demo: a Python script that gives a local LLM
(via [Ollama](https://ollama.com)) two tools — run a shell command, write a
file — inside a sandbox, and lets it carry out a task end to end (e.g.
"write a webpage and run it"). Fully local, no API key needed.

Two ways to run it:
- **`mini_devin.py`** — single agent, one Docker container, command line.
- **`dashboard.py`** — a local web UI that lets the model itself decide
  whether a task splits into independent agents (run in parallel, up to 3 at
  once) or must run as one agent (serial steps), each agent's sandbox a real
  **Kubernetes Deployment + Service + Ingress** by default (or Docker
  containers, same as the CLI, via `MINI_DEVIN_BACKEND=docker`).

## How it works

`mini_devin.py` starts (or reuses) a long-running `python:3.11-slim` Docker
container named `mini-devin-sandbox`, with the container's port `8000` mapped
to `localhost:8000`. It then talks to a local Ollama model through its
OpenAI-compatible API (`openai` SDK pointed at `http://localhost:11434/v1`),
passing two tool schemas — `run_command` and `write_file` — in OpenAI
function-calling format. Ollama has no automatic function-calling loop like
some hosted SDKs, so `mini_devin.py` runs the loop by hand: send the
conversation, check if the model asked to call a tool, execute it against the
container, feed the result back, repeat until the model returns a final
answer (capped at `MAX_STEPS` iterations).

The LLM loop always runs on the host — only `run_command`/`write_file`
reach into a sandbox, via a small `Sandbox` interface
(`start`/`exec`/`put_archive`/`remove`) that `mini_devin.DockerSandbox` and
`k8s_backend.K8sSandbox` both implement. This is what lets `dashboard.py`
run the exact same agent loop against either backend — see "Multi-agent
dashboard" below for the Kubernetes details.

## Prerequisites

- macOS (scripts use `brew`/`open -a Docker`)
- [Docker Desktop](https://www.docker.com/products/docker-desktop/) — can be
  installed automatically by `install.sh` if missing. Always required (the
  CLI always uses it; the dashboard's Docker backend does too).
- [Ollama](https://ollama.com) — can be installed automatically by
  `install.sh` if missing.
- **For the dashboard's Kubernetes backend (the default)**: `kubectl`, a
  running cluster, and the [ingress-nginx](https://kubernetes.github.io/ingress-nginx/)
  controller with IngressClass `nginx`. `install.sh` only *checks* for these
  (see below) — it won't create or start a cluster for you. No cluster? Run
  the dashboard with `MINI_DEVIN_BACKEND=docker` instead, or set one up:
  ```bash
  minikube start --driver=docker
  minikube addons enable ingress
  ```

## Quick start

```bash
./install.sh
source .venv/bin/activate
python3 mini_devin.py "write a webpage and run it"
```

`install.sh` is idempotent — safe to re-run. It checks for Homebrew, Docker,
and Ollama (installing each via `brew` only if missing), makes sure the
Docker daemon and the Ollama server are running (with `OLLAMA_NUM_PARALLEL=3`
so the dashboard's 3 concurrent agents can actually generate at once), pulls
the model if it isn't already local, creates a project-local virtual
environment at `.venv`, installs `requirements.txt` into it, and writes a
`.env` file with `OLLAMA_MODEL`. It also *checks* (never installs) whether
`kubectl`/a cluster/ingress-nginx are ready for the dashboard's Kubernetes
backend. Everything it actually changes is recorded in `.install_manifest`
so `uninstall.sh` can undo exactly that — nothing more.

Once the task finishes, open <http://localhost:8000> to see what the model
built.

## Multi-agent dashboard

`dashboard.py` runs a local web UI for submitting a task, letting the model
itself decide whether it splits into independent subtasks (run in parallel,
each agent in its own isolated sandbox) or must run serially (one agent,
multiple steps), and watching/stopping/killing the resulting agent(s) live.

```bash
source .venv/bin/activate
python3 dashboard.py
# prints the dashboard-token retrieval command — run it, paste the token
# into the page's token field, then open http://localhost:5050
```

- **Decision**: submitting a task triggers one extra LLM call
  (`orchestrator.py`'s `decompose()`) that returns `{"mode": "parallel" |
  "serial", "reason": ..., "subtasks": [...]}`. The dashboard shows the
  chosen mode and the model's stated reason for each job — that decision is
  the actual point of the demo. The prompt is written to bias toward
  "serial" whenever it's unsure, since a wrong "parallel" call means
  splitting coupled work into sandboxes that can't see each other's files.
- **Concurrency cap**: at most 3 agents *run* at once (`orchestrator.MAX_CONCURRENT`)
  — a `ThreadPoolExecutor(max_workers=3)` queues the rest. This is a
  resource choice, not an architectural limit: one shared `qwen2.5:7b`
  Ollama instance backs every agent, and each concurrent generation needs
  its own KV cache, which gets tight past 2-3 way before the ~4.7GB of model
  weights do on a 16GB machine. Finished agents' sandboxes are deliberately
  left running (so you can still open their page) and don't count against
  the cap — only Kill or stopping `dashboard.py` removes them.
- **`OLLAMA_NUM_PARALLEL=3`**: see above — if Ollama was already running
  before you last ran `install.sh`, restart it (`pkill -f "ollama serve"`,
  then `OLLAMA_NUM_PARALLEL=3 ollama serve &`) or the demo still works, just
  serializes LLM calls while agents' sandbox commands keep overlapping.

### Two backends: Kubernetes (default) or Docker

`MINI_DEVIN_BACKEND=k8s` (default) or `docker`, read once at `dashboard.py`
startup.

**Kubernetes backend** — each agent is a real **Deployment** (1 replica) +
**ClusterIP Service** + per-agent **Ingress** (path `/agents/<id>/`), all in
a dedicated **`mini-devin` namespace** (created if missing; never touches
anything outside it). `write_file`/`run_command` go through the Kubernetes
exec API (`kubectl exec`'s underlying mechanism) instead of Docker's SDK.
Deleting an agent (Kill) deletes its Deployment; its Service and Ingress are
garbage-collected automatically via Kubernetes owner references — one
delete call, not three.

Reaching an agent's page from your browser needs a route from the host into
the cluster. `dashboard.py` manages this itself: it runs `kubectl
port-forward ... svc/ingress-nginx-controller 8080:80` as a subprocess
(auto-restarted if the controller pod bounces), so agent URLs
(`http://localhost:8080/agents/<id>/`) just work.

A **Kubernetes Secret** (`mini-devin-dashboard-token` in the `mini-devin`
namespace) backs the dashboard's auth — see Security notes below for why
this is a real fix, not decoration. Retrieve it:

```bash
kubectl --context minikube -n mini-devin get secret mini-devin-dashboard-token \
  -o jsonpath='{.data.token}' | base64 -d
```

(`dashboard.py` prints this exact command at startup.) Paste the token into
the page's token field (saved to that browser's `localStorage`), or pass it
yourself:

```bash
TOKEN=$(kubectl --context minikube -n mini-devin get secret mini-devin-dashboard-token -o jsonpath='{.data.token}' | base64 -d)
curl -X POST http://127.0.0.1:5050/api/tasks -H "Authorization: Bearer $TOKEN" \
     -H 'Content-Type: application/json' -d '{"task":"写一个关于猫的静态网页并运行"}'
```

**Docker backend** (`MINI_DEVIN_BACKEND=docker python3 dashboard.py`) —
same as the CLI's mechanism, just N containers instead of one. No Secret
involved; the dashboard token is generated in-memory and printed once at
startup instead.

Both backends bind the dashboard to `127.0.0.1` only — this process can run
arbitrary shell commands inside sandboxes on your behalf, so it must never
be reachable from your LAN.

### Manual checklist (not covered by CI — see CI section)

```
1. python3 dashboard.py, open http://localhost:5050, paste the token
2. submit: 写三个互不相关的静态网页：一个猫网页、一个狗网页、一个天气网页
3. confirm mode="parallel", 3 agents running concurrently
4. open all three http://localhost:8080/agents/<id>/ links — each distinct
5. Kill one, confirm `kubectl -n mini-devin get deploy,svc,ingress` drops it
```

## Configuration

| Variable          | Required | Default        | Notes                                                        |
|-------------------|----------|-----------------|----------------------------------------------------------------|
| `OLLAMA_MODEL`    | no       | `qwen2.5:7b`     | Set in `.env` by `install.sh`; override by editing `.env` or exporting it. Tested: `qwen2.5:3b` reliably returns empty output and never calls a tool with this demo's system prompt; `qwen2.5:7b` reliably calls `write_file`/`run_command` and finishes the task. Don't drop below 7b without re-testing. |
| `OLLAMA_BASE_URL` | no       | `http://localhost:11434/v1` | Change if Ollama runs on a different host/port. |
| `MINI_DEVIN_BACKEND` | no    | `k8s`            | `dashboard.py` only. `k8s` or `docker` — which sandbox backend agents run on. |
| `MINI_DEVIN_KUBE_CONTEXT` | no | `minikube`  | kubectl context to use for the k8s backend. Always pinned explicitly — never whatever your current context happens to be. |
| `INGRESS_LOCAL_PORT` | no    | `8080`           | Local port `dashboard.py` port-forwards the ingress-nginx controller to. |
| `DASHBOARD_PORT`  | no       | `5050`           | Port the Flask dashboard itself listens on. |

## Manual setup (without install.sh)

```bash
brew install ollama
ollama serve &                 # if not already running
ollama pull qwen2.5:7b

python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python3 mini_devin.py "write a webpage and run it"
```

## Uninstall

```bash
./uninstall.sh
```

Removes only what `install.sh` recorded installing: your project's own
`.venv` and `.env`, and the `ollama serve` process it started. It asks for
y/N confirmation before deleting the pulled model (frees disk space),
uninstalling Ollama/Docker Desktop themselves, or deleting the `mini-devin`
namespace (everything `dashboard.py`'s Kubernetes backend created) — and
never touches Homebrew or your minikube cluster itself.

## CI

`.github/workflows/ci.yml` runs on every push/PR to `main`:

- **lint** — Python/shell syntax checks, shellcheck, and a clean-venv install
  of `requirements.txt`.
- **sandbox-smoke-test** — runs `pytest tests/` (minus `test_k8s_sandbox.py`,
  which skips itself here — no cluster on a plain `ubuntu-latest` runner):
  - `test_sandbox.py` drives `start_sandbox`/`write_file`/`run_command`
    directly (no Ollama involved) to prove the Docker control plane actually
    works: write a page, serve it, fetch it back over the mapped port, and
    that two `Agent` instances get distinct ports and can't see each other's
    files. GitHub-hosted runners have Docker preinstalled, so this needs no
    extra setup.
  - `test_orchestrator.py` exercises the orchestrator's queueing/concurrency
    cap/stop/error-handling logic and the dashboard's Flask routes (including
    the token-auth 401 cases) with fakes — no Docker, Kubernetes, or Ollama,
    fully deterministic.
  - `test_k8s_manifests.py` asserts on the Deployment/Service/Ingress
    builders — selectors match, pod hardening, Ingress regex/rewrite-target,
    owner references. Pure functions, no cluster needed.
- **k8s-sandbox-test** — spins up a throwaway [KinD](https://kind.sigs.k8s.io/)
  cluster (`helm/kind-action`) and runs `test_k8s_sandbox.py` against it with
  `MINI_DEVIN_REQUIRE_K8S=1` (a skip becomes a hard failure, so this can't
  silently stop being covered). Targets the riskiest new code — exec-over-
  websocket, the `head -c N | tar -xf -` stdin trick, per-sandbox API client
  isolation — against a real API server. Doesn't install ingress-nginx:
  these tests never touch Ingress routing, only create/delete the object
  (valid with no controller watching it).

Neither the LLM/tool-calling path nor actual Ingress+port-forward routing is
covered by CI — pulling an Ollama model and depending on real model behavior
would make CI slow and flaky, and routing through ingress-nginx needs a
controller + the macOS-specific port-forward dance. Both are meant to be
exercised locally (`./install.sh` + the manual checklist above).

## Files

- `mini_devin.py` — single-agent CLI, the `Agent` class every agent is built
  from, and `DockerSandbox` (the Docker-backed sandbox implementation)
- `k8s_backend.py` — the Kubernetes-backed sandbox (`K8sSandbox`), manifest
  builders, dashboard-token Secret, and the Ingress port-forward manager
- `orchestrator.py` — task decomposition (parallel vs serial) and the
  concurrency-capped agent pool, backend-agnostic (`make_run_fn()` plugs in
  either sandbox backend)
- `dashboard.py` / `dashboard.html` — the local multi-agent web UI
- `requirements.txt` — `openai` (OpenAI-compatible client for Ollama),
  `docker`, `flask` (dashboard), `kubernetes` (k8s backend)
- `install.sh` / `uninstall.sh` — setup and teardown
- `tests/test_sandbox.py` — CI smoke test for the Docker sandbox mechanics (single- and multi-agent)
- `tests/test_orchestrator.py` — CI unit tests for the orchestrator/dashboard logic (no Docker/K8s/Ollama)
- `tests/test_k8s_manifests.py` — CI unit tests for the k8s manifest builders (no cluster needed)
- `tests/test_k8s_sandbox.py` — integration tests for `K8sSandbox` against a real cluster (CI uses KinD; skips if none reachable)
- `.github/workflows/ci.yml` — CI pipeline (see above)
- `.install_manifest` — generated by `install.sh`; tracks what it installed
- `.env` — generated by `install.sh`; non-secret config only

## Security notes

- Sandboxes (Docker containers or k8s Pods) have modest resource
  requests/limits in k8s mode but no network isolation beyond the
  default — this is a demo, not a hardened sandbox. Don't point it at tasks
  you don't trust an LLM to execute unsupervised.
- **k8s mode specifically**: there's no `NetworkPolicy`, and minikube's
  default CNI doesn't enforce them anyway, so agent pods can technically
  reach anything else in the cluster (e.g. unrelated workloads in other
  namespaces on a shared minikube). Not worth building a NetworkPolicy for a
  demo, but worth knowing.
- **k8s mode: a Deployment, not a Job, backs each agent.** If a pod is
  evicted or the node restarts it, Kubernetes quietly creates a fresh pod —
  and unlike Docker (where a stopped container keeps its filesystem), the
  agent's files are simply gone (no persistent volume). This trades
  durability for the "stays alive so you can inspect it" behavior; don't
  rely on an agent's sandbox as durable storage.
- The dashboard requires a bearer token on every mutating route
  (`/api/tasks`, `/stop`, `/kill`) — this isn't decoration. Bound to
  `127.0.0.1` still means any page in your browser can auto-submit a
  same-origin POST to `127.0.0.1:5050` (no CORS preflight needed for a
  no-body POST) — including a page one of the agents just wrote and is
  serving. A required `Authorization: Bearer` header forces a CORS
  preflight that Flask never approves, closing that hole. In k8s mode the
  token lives in a real Kubernetes Secret (`mini-devin-dashboard-token`);
  in Docker mode it's generated in-memory and printed once per run.
- Everything here runs locally (Docker/Kubernetes + Ollama) — no data
  leaves your machine and no API key is needed anywhere in this project.
