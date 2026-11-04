# mini-devin

A minimal "Devin"-style sandbox demo: a Python script that gives a local LLM
(via [Ollama](https://ollama.com)) two tools — run a shell command, write a
file — inside a sandbox, and lets it carry out a task end to end (e.g.
"write a webpage and run it"). Fully local, no API key needed. Every
sandbox, single-agent or multi-agent, is a real Kubernetes
Deployment/Service/Ingress — there's no Docker-container fallback anymore.

Two ways to run it:
- **`mini_devin.py`** — one agent, one Kubernetes sandbox, command line.
- **`dashboard.py`** — a local web UI that lets the model itself decide how
  to tackle a task: one agent (simple/serial work), several fully
  independent agents in parallel (unrelated deliverables), or a
  **collaboration** of 3 AI-named roles sharing one sandbox plus an
  automatic verifying integrator (one real project with substantial,
  differently-skilled parts). See "Multi-agent dashboard" below.

## How it works

`mini_devin.py`'s `Agent` talks to a local Ollama model through its
OpenAI-compatible API (`openai` SDK pointed at `http://localhost:11434/v1`),
passing two tool schemas — `run_command` and `write_file` — in OpenAI
function-calling format. Ollama has no automatic function-calling loop like
some hosted SDKs, so `Agent.run()` runs the loop by hand: send the
conversation, check if the model asked to call a tool, execute it against
the sandbox, feed the result back, repeat until the model returns a final
answer (capped at `max_steps` iterations).

The LLM loop always runs on the host — only `run_command`/`write_file`
reach into a sandbox, via a small `Sandbox` interface
(`start`/`exec`/`put_archive`/`remove`) that `k8s_backend.K8sSandbox`
implements (`write_file` pipes a tar archive into a `kubectl exec`-style
stream; there's no REST equivalent of Docker's `put_archive`). The CLI and
every agent the dashboard spawns go through the exact same `Agent` class —
the only difference is how many of them exist and whether they share a
sandbox (see "collaborate" mode below).

## Prerequisites

- macOS (scripts use `brew`/`open -a Docker`)
- A **Kubernetes cluster** (tested against [minikube](https://minikube.sigs.k8s.io/)
  with the `docker` driver) with the
  [ingress-nginx](https://kubernetes.github.io/ingress-nginx/) controller
  installed (IngressClass `nginx`). This is not optional — both the CLI and
  the dashboard create real Deployments/Services/Ingresses and need a
  working cluster to do anything at all. `install.sh` only *checks* for
  this (see below), it won't create a cluster for you:
  ```bash
  minikube start --driver=docker
  minikube addons enable ingress
  ```
- [Docker Desktop](https://www.docker.com/products/docker-desktop/) — can be
  installed automatically by `install.sh` if missing. The app itself no
  longer talks to Docker directly; this is required because minikube's
  `docker` driver runs the cluster's node *as* a Docker container.
- [Ollama](https://ollama.com) — can be installed automatically by
  `install.sh` if missing.

## Quick start

```bash
./install.sh
source .venv/bin/activate
python3 mini_devin.py "write a webpage and run it"
```

`install.sh` is idempotent — safe to re-run. It checks for Homebrew, Docker,
and Ollama (installing each via `brew` only if missing), makes sure the
Docker daemon and the Ollama server are running (with `OLLAMA_NUM_PARALLEL=3`
so the dashboard's concurrent agents can actually generate at once), pulls
the model if it isn't already local, *checks* (never installs) that
`kubectl`/a cluster/ingress-nginx are ready, creates a project-local virtual
environment at `.venv`, installs `requirements.txt` into it, and writes a
`.env` file with `OLLAMA_MODEL`. Everything it actually changes is recorded
in `.install_manifest` so `uninstall.sh` can undo exactly that — nothing
more.

The CLI runs its own `kubectl port-forward` to ingress-nginx *while it's
running*, so the printed URL (`http://localhost:8080/agents/cli/`) works
immediately — open it in another window as soon as the task finishes. The
port-forward stops the moment the CLI process exits (its sandbox Deployment
keeps running, same as any other agent, but nothing's forwarding a port to
it anymore). To come back to it later, forward the port yourself:
```bash
kubectl --context minikube -n ingress-nginx port-forward --address 127.0.0.1 svc/ingress-nginx-controller 8080:80
```
or just use `dashboard.py`, which keeps its port-forward alive for as long
as the dashboard itself runs.

## Multi-agent dashboard

```bash
source .venv/bin/activate
python3 dashboard.py
# prints the dashboard-token retrieval command — run it, paste the token
# into the page's token field, then open http://localhost:5050
```

Submitting a task triggers one extra LLM call (`orchestrator.py`'s
`decompose()`) that picks one of three modes and shows its reasoning on the
dashboard — that decision, and what happens for each mode, is the actual
point of the demo:

- **`serial`** — one agent does the whole task itself, possibly as several
  internal steps (e.g. write code → install deps → start it → test it). The
  default whenever the planner is unsure — a wrong split costs more than
  it's worth.
- **`parallel`** — the task is several **fully independent** deliverables
  (e.g. three unrelated static webpages). Each subtask gets its own
  isolated sandbox; they never see each other's files, and nothing merges
  the results afterward.
- **`collaborate`** — the task is **one project** that splits into exactly
  3 substantial, differently-skilled roles (the model names them itself —
  e.g. interface design/frontend/backend for a small web app). All 3 share **one sandbox**
  (so, say, the frontend role can see and build on the interface-design
  role's files), and the planner also declares which roles depend on which
  — roles with no dependency between them really do run concurrently (their
  LLM calls overlap; their actual sandbox tool calls are serialized on a
  lock so two agents are never mid-write on the same pod at the same
  instant — see `orchestrator.SharedSandbox`). Once every role finishes, a
  4th **integrator** agent — with full tool access to the same sandbox —
  is automatically launched to check the real state of things and produce
  the job's one final, consolidated report.

  **The integrator exists because this model lies about finishing.** Real
  testing surfaced this repeatedly: an agent writes a Flask app, never
  actually calls `run_command` to start it, and then claims in its summary
  that the server is running and tested. The integrator's system prompt
  names this exact failure mode and requires it to check files (`ls`/`cat`),
  processes (`/proc`, since there's no `ps` in the image), and reachability
  (`python3 -c "urllib..."`, since there's no `curl`) itself before writing
  anything as done. Even so, in one real run the integrator *also* only
  made one real tool call and then fabricated an entire fake verification
  transcript — including a `cat` output that didn't match the real file —
  while confidently reporting success. In response, `Agent.run()` gained a
  `min_tool_calls` parameter: the integrator isn't allowed to stop until it
  has made at least 3 real tool calls (nudged to continue if it tries to
  stop early), and `job.final_verified` now requires evidence of at least 2
  of the 3 required check *categories* (file/process/network) actually
  appearing in its real logged commands, not just "it called a tool at
  least once." **This raises the bar; it does not prove correctness.** The
  integrator is still the same small model — treat a `collaborate` job's
  final report as "probably checked, not guaranteed," especially if
  `final_verified` is false. One real trade-off observed: a nudge round
  that produces no tool call logs nothing (only real tool calls do), and
  each retry re-sends a growing message history — so a model that keeps
  trying to stop early can look "stuck" for a while on a local 7B model
  with no new log lines, even though it's genuinely working through
  `min_tool_calls`, not actually hung. `INTEGRATOR_MAX_STEPS=15` bounds the
  step count, but in one real run that still meant 9+ minutes of wall
  clock on only 2 logged tool calls — a verification phase with no time
  limit isn't practically usable, so `Agent.run()` also takes a
  `max_seconds` wall-clock budget: `orchestrator.INTEGRATOR_MAX_SECONDS =
  360` gives the integrator a hard 6-minute cap, checked before every LLM
  call (so it won't even start a call once the budget is gone) and passed
  as that call's own request `timeout=` (so a slow final call can't itself
  blow past the deadline). Hitting the cap ends the job with an honest
  "ran out of time, completed N real tool calls" result instead of an
  unbounded wait.

- **Concurrency cap**: at most 3 agents *run* at once
  (`orchestrator.MAX_CONCURRENT`) regardless of mode — a
  `ThreadPoolExecutor(max_workers=3)` queues the rest, and a `collaborate`
  job's roles compete for the same 3 slots as everything else (no separate
  accounting). This is a resource choice: one shared `qwen2.5:7b` Ollama
  instance backs every agent, and each concurrent generation needs its own
  KV cache, which gets tight past 2-3 way before the ~4.7GB of model
  weights do on a 16GB machine. Finished agents' sandboxes are deliberately
  left running (so you can still open their page) and don't count against
  the cap — only Kill or stopping `dashboard.py` removes them.
- **`OLLAMA_NUM_PARALLEL=3`**: see above — if Ollama was already running
  before you last ran `install.sh`, restart it (`pkill -f "ollama serve"`,
  then `OLLAMA_NUM_PARALLEL=3 ollama serve &`) or the demo still works, just
  serializes LLM calls while agents' sandbox commands keep overlapping.

### Kubernetes mechanics

Each non-collaborate agent is a real **Deployment** (1 replica) +
**ClusterIP Service** + per-agent **Ingress** (path `/agents/<id>/`); a
`collaborate` job's 3 roles + integrator share one set of these, named
`/agents/<job-id>-shared/`. All of it lives in a dedicated **`mini-devin`
namespace** (created if missing; never touches anything outside it — your
cluster may have unrelated workloads in `default`, this never looks at
them). Deleting an agent (Kill) deletes its Deployment; its Service and
Ingress are garbage-collected automatically via Kubernetes owner
references — one delete call, not three. Killing any member of a
`collaborate` job kills the whole job (you can't remove a shared sandbox
for just one role) — the dashboard surfaces this as one job-level button.

Reaching an agent's page from your browser needs a route from the host into
the cluster. Both the CLI and `dashboard.py` manage this themselves: they
run `kubectl port-forward ... svc/ingress-nginx-controller 8080:80` as a
subprocess (auto-restarted if the controller pod bounces), so agent URLs
(`http://localhost:8080/agents/<id>/`) just work without you running
anything extra.

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
     -H 'Content-Type: application/json' -d '{"task":"write a static webpage about cats and run it"}'
```

The dashboard binds to `127.0.0.1` only — this process can run arbitrary
shell commands inside sandboxes on your behalf, so it must never be
reachable from your LAN.

### Manual checklist (not covered by CI — see CI section)

```
1. python3 dashboard.py, open http://localhost:5050, paste the token
2. submit: write three unrelated static webpages: a cat webpage, a dog webpage, a weather webpage
   -> expect mode="parallel", 3 agents running concurrently, 3 distinct pages
3. submit: create a message board website: Flask backend provides message viewing and submission interfaces, frontend page can view and submit messages,
   served by Flask on port 8000
   -> expect mode="collaborate", 3 named roles (frontend+backend both
      depending only on interface-design, running concurrently), then the
      integrator; check job.final_verified and whether final_result
      actually matches `kubectl exec` into the pod yourself
4. Kill one agent/job, confirm `kubectl -n mini-devin get deploy,svc,ingress`
   drops the right resources
```

## Troubleshooting

### Common Issues

**"Unable to load kubeconfig" / "kubeconfig not found"**
- Ensure your Kubernetes cluster is running: `minikube status`
- Verify the context exists: `kubectl config get-contexts`
- Check the context name matches `MINI_DEVIN_KUBE_CONTEXT` (default: `minikube`)
- If using a different cluster, set the environment variable: `export MINI_DEVIN_KUBE_CONTEXT=your-context`

**"ingress-nginx not found"**
- The ingress-nginx controller is required for routing to work
- Install it on minikube: `minikube addons enable ingress`
- Verify it's running: `kubectl get ingressclass nginx`
- If using a different cluster, install ingress-nginx following their documentation

**"Ollama service not running"**
- Check if Ollama is installed: `which ollama`
- Start the Ollama server: `ollama serve`
- Verify it's responding: `curl http://localhost:11434/api/tags`
- For concurrent agents, ensure `OLLAMA_NUM_PARALLEL=3` is set when starting the server

**"Model not found"**
- List available models: `ollama list`
- Pull the required model: `ollama pull qwen2.5:7b`
- If using a different model, set `OLLAMA_MODEL` in `.env` or as an environment variable

**"Pod did not become ready within timeout"**
- Check pod status: `kubectl -n mini-devin get pods`
- View pod logs: `kubectl -n mini-devin logs <pod-name>`
- Common causes:
  - Image pull failure (check `docker images` and network connectivity)
  - Resource limits (increase CPU/memory in k8s_backend.py if needed)
  - Node not ready: `kubectl get nodes`

**"port-forward: connection refused"**
- Ensure ingress-nginx controller is running: `kubectl -n ingress-nginx get pods`
- Check the service exists: `kubectl -n ingress-nginx get svc ingress-nginx-controller`
- Verify the context is correct: `kubectl --context minikube cluster-info`

**Dashboard shows "Token invalid"**
- Retrieve the token using the command printed at dashboard startup
- The command is: `kubectl --context minikube -n mini-devin get secret mini-devin-dashboard-token -o jsonpath='{.data.token}' | base64 -d`
- Copy the entire token (no extra spaces or newlines)
- If the secret was deleted, restart `dashboard.py` to recreate it

**Agent URL returns 404 or 502**
- Check the agent pod is running: `kubectl -n mini-devin get pods -l mini-devin.agent-id=<agent-id>`
- Verify the Ingress exists: `kubectl -n mini-devin get ingress`
- Check ingress-nginx controller logs: `kubectl -n ingress-nginx logs -l app.kubernetes.io/name=ingress-nginx`
- Ensure the HTTP server inside the pod is actually running on port 8000

**"integrated verification failed" / "final_verified: false"**
- This means the integrator couldn't verify the work using file/process/network checks
- Check the integrator's logs in the dashboard to see what it actually ran
- Common causes:
  - Server not started (no process found in /proc)
  - Wrong port (service listening on port other than 8000)
  - Network unreachable (service bound to localhost instead of 0.0.0.0)
- The integrator can sometimes fix minor issues (missing app.run call), but major problems require manual intervention

**Tests fail with "MINI_DEVIN_REQUIRE_K8S"**
- Some tests require a real Kubernetes cluster
- Run with `MINI_DEVIN_REQUIRE_K8S=1 pytest tests/` to enable these tests
- Ensure your cluster context is set correctly: `export MINI_DEVIN_KUBE_CONTEXT=your-context`
- Without this flag, tests that require K8s will be skipped

**Python import errors after refactoring**
- After the config.py refactoring, ensure all files import from the new module
- Verify: `python3 -m py_compile config.py mini_devin.py orchestrator.py dashboard.py k8s_backend.py`
- If circular import errors persist, check that mini_devin.py no longer imports k8s_backend at the top level

### Debugging Tips

**View all mini-devin resources:**
```bash
kubectl -n mini-devin get all
kubectl -n mini-devin get ingress
```

**Check pod logs for an agent:**
```bash
kubectl -n mini-devin logs -l mini-devin.agent-id=<agent-id> --tail=100
```

**Exec into a pod to inspect the filesystem:**
```bash
kubectl -n mini-devin exec -it <pod-name> -- /bin/sh
ls -la /app
cat /app/*.py
```

**Check ingress-nginx controller logs:**
```bash
kubectl -n ingress-nginx logs -l app.kubernetes.io/name=ingress-nginx
```

**Manually test port-forward:**
```bash
kubectl --context minikube -n ingress-nginx port-forward svc/ingress-nginx-controller 8080:80
# Then open http://localhost:8080/agents/<id>/ in your browser
```

**Clean up stuck resources:**
```bash
kubectl -n mini-devin delete all --all
kubectl -n mini-devin delete ingress --all
# Or use the project's cleanup:
python3 -c "import k8s_backend as kb; kb.delete_labeled_resources()"
```

## Configuration

| Variable          | Required | Default        | Notes                                                        |
|-------------------|----------|-----------------|----------------------------------------------------------------|
| `OLLAMA_MODEL`    | no       | `qwen2.5:7b`     | Set in `.env` by `install.sh`; override by editing `.env` or exporting it. Tested: `qwen2.5:3b` reliably returns empty output and never calls a tool with this demo's system prompt; `qwen2.5:7b` reliably calls `write_file`/`run_command` and finishes the task. Don't drop below 7b without re-testing. |
| `OLLAMA_BASE_URL` | no       | `http://localhost:11434/v1` | Change if Ollama runs on a different host/port. |
| `MINI_DEVIN_KUBE_CONTEXT` | no | `minikube`  | kubectl context used for every sandbox. Always pinned explicitly — never whatever your current context happens to be. |
| `INGRESS_LOCAL_PORT` | no    | `8080`           | Local port the CLI/dashboard port-forward the ingress-nginx controller to. |
| `DASHBOARD_PORT`  | no       | `5050`           | Port the Flask dashboard itself listens on. |

## Manual setup (without install.sh)

```bash
brew install ollama
ollama serve &                 # if not already running
ollama pull qwen2.5:7b

minikube start --driver=docker  # if you don't already have a cluster
minikube addons enable ingress

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
namespace (everything this project created in your cluster) — and never
touches Homebrew or your minikube cluster itself.

## CI

`.github/workflows/ci.yml` runs on every push/PR to `main`:

- **lint** — Python/shell syntax checks, shellcheck, and a clean-venv install
  of `requirements.txt`.
- **test** — spins up a throwaway [KinD](https://kind.sigs.k8s.io/) cluster
  (`helm/kind-action`) and runs the whole `pytest tests/` against it with
  `MINI_DEVIN_REQUIRE_K8S=1` (a skip becomes a hard failure, so real
  Kubernetes coverage can't silently stop running):
  - `test_orchestrator.py` / `test_agent.py` — queueing/concurrency/stop
    logic, the collaborate-mode dependency scheduler and shared-sandbox
    locking, the `min_tool_calls` nudge mechanism, the dashboard's Flask
    routes (including token-auth) — all with fakes, no cluster or Ollama
    actually touched by these specific tests.
  - `test_k8s_manifests.py` — pure assertions on the Deployment/Service/
    Ingress builders (selectors, pod hardening, Ingress regex/
    rewrite-target, owner references). No cluster needed either.
  - `test_k8s_sandbox.py` — the real thing: exec-over-websocket, the
    `head -c N | tar -xf -` stdin trick, per-sandbox API client isolation,
    two sandboxes' DNS-based isolation — against KinD's real API server.
    Doesn't install ingress-nginx (these tests never touch Ingress
    routing, only create/delete the object, which is valid even with no
    controller watching it).

Neither the LLM/tool-calling path nor actual Ingress+port-forward routing is
covered by CI — pulling an Ollama model and depending on real model behavior
would make CI slow and flaky, and routing through ingress-nginx needs a
controller plus the macOS-specific port-forward dance. Both are meant to be
exercised locally (`./install.sh` + the manual checklist above).

## Files

- `mini_devin.py` — the CLI entry point and the `Agent` class every agent
  (CLI or dashboard-spawned) is built from
- `k8s_backend.py` — the Kubernetes sandbox (`K8sSandbox`), manifest
  builders, the dashboard-token Secret, and the Ingress port-forward manager
- `orchestrator.py` — task decomposition (parallel/serial/collaborate), the
  concurrency-capped agent pool, the collaborate-mode dependency scheduler,
  and `SharedSandbox` (the lock that lets collaborate roles share one pod)
- `dashboard.py` / `dashboard.html` — the local multi-agent web UI
- `requirements.txt` — `openai` (OpenAI-compatible client for Ollama),
  `flask` (dashboard), `kubernetes` (the sandbox backend)
- `install.sh` / `uninstall.sh` — setup and teardown
- `tests/test_agent.py` — CI unit tests for `Agent.run()`'s tool-calling
  loop and the `min_tool_calls` nudge mechanism (fake LLM + fake sandbox)
- `tests/test_orchestrator.py` — CI unit tests for the orchestrator/
  dashboard logic (fakes; no cluster/Ollama needed)
- `tests/test_k8s_manifests.py` — CI unit tests for the k8s manifest
  builders (pure functions, no cluster needed)
- `tests/test_k8s_sandbox.py` — integration tests for `K8sSandbox` against
  a real cluster (CI uses KinD; skips if none reachable and
  `MINI_DEVIN_REQUIRE_K8S` isn't set)
- `.github/workflows/ci.yml` — CI pipeline (see above)
- `.install_manifest` — generated by `install.sh`; tracks what it installed
- `.env` — generated by `install.sh`; non-secret config only

## Language policy

This project uses a bilingual approach:
- **User-facing content**: Chinese (system prompts, dashboard UI, user-facing error messages)
- **Developer-facing content**: English (code comments, technical error messages, API docs)

This design choice reflects the target user base (Chinese-speaking users interacting with the dashboard) while maintaining English as the language for code maintainability and international developer collaboration.

## Security notes

- Sandboxes (Kubernetes Pods) have modest resource requests/limits but no
  network isolation beyond the default — this is a demo, not a hardened
  sandbox. Don't point it at tasks you don't trust an LLM to execute
  unsupervised.
- There's no `NetworkPolicy`, and minikube's default CNI doesn't enforce
  them anyway, so agent pods can technically reach anything else in the
  cluster (e.g. unrelated workloads in other namespaces on a shared
  minikube). Not worth building a NetworkPolicy for a demo, but worth
  knowing.
- **A Deployment, not a Job, backs each agent.** If a pod is evicted or the
  node restarts it, Kubernetes quietly creates a fresh pod — the agent's
  files are simply gone (no persistent volume). This trades durability for
  the "stays alive so you can inspect it" behavior; don't rely on an
  agent's sandbox as durable storage.
- The dashboard requires a bearer token on every mutating route
  (`/api/tasks`, `/stop`, `/kill`) — this isn't decoration. Bound to
  `127.0.0.1` still means any page in your browser can auto-submit a
  same-origin POST to `127.0.0.1:5050` (no CORS preflight needed for a
  no-body POST) — including a page one of the agents just wrote and is
  serving. A required `Authorization: Bearer` header forces a CORS
  preflight that Flask never approves, closing that hole. The token lives
  in a real Kubernetes Secret (`mini-devin-dashboard-token`), so it
  survives dashboard restarts.
- **The integrator's report is not a correctness guarantee** (see
  "Multi-agent dashboard" above) — it's a same-sized local model checking
  another same-sized local model's work. `final_verified` catches "didn't
  even try to check," not "checked correctly."
- Everything here runs locally (Kubernetes + Ollama) — no data leaves your
  machine and no API key is needed anywhere in this project.
