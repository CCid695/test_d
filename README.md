# mini-devin

A minimal "Devin"-style sandbox demo: a Python script that gives a local LLM
(via [Ollama](https://ollama.com)) two tools — run a shell command, write a
file — inside a local Docker container, and lets it carry out a task end to
end (e.g. "write a webpage and run it"). Fully local, no API key needed.

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

## Prerequisites

- macOS (scripts use `brew`/`open -a Docker`)
- [Docker Desktop](https://www.docker.com/products/docker-desktop/) — can be
  installed automatically by `install.sh` if missing
- [Ollama](https://ollama.com) — can be installed automatically by
  `install.sh` if missing

## Quick start

```bash
./install.sh
source .venv/bin/activate
python3 mini_devin.py "write a webpage and run it"
```

`install.sh` is idempotent — safe to re-run. It checks for Homebrew, Docker,
and Ollama (installing each via `brew` only if missing), makes sure the
Docker daemon and the Ollama server are running, pulls the model if it isn't
already local, creates a project-local virtual environment at `.venv`,
installs `requirements.txt` into it, and writes a `.env` file with
`OLLAMA_MODEL`. Everything it actually changes is recorded in
`.install_manifest` so `uninstall.sh` can undo exactly that — nothing more.

Once the task finishes, open <http://localhost:8000> to see what the model
built.

## Multi-agent dashboard

`dashboard.py` runs a local web UI for submitting a task, letting the model
itself decide whether it splits into independent subtasks (run in parallel,
each in its own isolated Docker container) or must run serially (one agent,
multiple steps), and watching/stopping/killing the resulting agent(s) live.

```bash
source .venv/bin/activate
python3 dashboard.py
# open http://localhost:5050
```

- **Decision**: submitting a task triggers one extra LLM call
  (`orchestrator.py`'s `decompose()`) that returns `{"mode": "parallel" |
  "serial", "reason": ..., "subtasks": [...]}`. The dashboard shows the
  chosen mode and the model's stated reason for each job — that decision is
  the actual point of the demo. The prompt is written to bias toward
  "serial" whenever it's unsure, since a wrong "parallel" call means
  splitting coupled work into isolated containers that can't see each
  other's files.
- **Concurrency cap**: at most 3 agents run at once (`orchestrator.MAX_CONCURRENT`)
  — a `ThreadPoolExecutor(max_workers=3)` queues the rest. This is a
  resource choice, not an architectural limit: one shared `qwen2.5:7b`
  Ollama instance backs every agent, and each concurrent generation needs
  its own KV cache, which gets tight past 2-3 way before the ~4.7GB of model
  weights do on a 16GB machine.
- **`OLLAMA_NUM_PARALLEL=3`**: `install.sh` starts `ollama serve` with this
  set, so the server can actually generate for up to 3 agents at once. If
  Ollama was already running from before (e.g. you ran `install.sh` prior to
  this feature), restart it — `pkill -f "ollama serve"` then
  `OLLAMA_NUM_PARALLEL=3 ollama serve &` — or the demo still works, just
  serializes the LLM calls while agents' container commands keep overlapping.
- **Containers persist after a task finishes** so you can open their port
  and see what they built; they're only removed via the dashboard's Kill
  button or when you stop `dashboard.py` (Ctrl-C).
- Binds to `127.0.0.1` only — this process can run arbitrary shell commands
  inside containers on your behalf, so it must never be reachable from your
  LAN.

## Configuration

| Variable          | Required | Default        | Notes                                                        |
|-------------------|----------|-----------------|----------------------------------------------------------------|
| `OLLAMA_MODEL`    | no       | `qwen2.5:7b`     | Set in `.env` by `install.sh`; override by editing `.env` or exporting it. Tested: `qwen2.5:3b` reliably returns empty output and never calls a tool with this demo's system prompt; `qwen2.5:7b` reliably calls `write_file`/`run_command` and finishes the task. Don't drop below 7b without re-testing. |
| `OLLAMA_BASE_URL` | no       | `http://localhost:11434/v1` | Change if Ollama runs on a different host/port. |

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
y/N confirmation before deleting the pulled model (frees disk space) or
uninstalling Ollama/Docker Desktop themselves, and never touches Homebrew.

## CI

`.github/workflows/ci.yml` runs on every push/PR to `main`:

- **lint** — Python/shell syntax checks, shellcheck, and a clean-venv install
  of `requirements.txt`.
- **sandbox-smoke-test** — runs `pytest tests/`:
  - `test_sandbox.py` drives `start_sandbox`/`write_file`/`run_command`
    directly (no Ollama involved) to prove the Docker control plane actually
    works: write a page, serve it, fetch it back over the mapped port, and
    that two `Agent` instances get distinct ports and can't see each other's
    files. GitHub-hosted runners have Docker preinstalled, so this needs no
    extra setup.
  - `test_orchestrator.py` exercises the orchestrator's queueing/concurrency
    cap/stop/error-handling logic and the dashboard's Flask routes with fake
    `run_fn`/`decompose_fn` — no Docker, no Ollama, fully deterministic.

The LLM/tool-calling path isn't covered by CI on purpose — pulling an Ollama
model and depending on real model behavior would make CI slow and flaky;
that path is meant to be exercised locally via `./install.sh`.

## Files

- `mini_devin.py` — single-agent CLI + the `Agent` class every agent (single or multi) is built from
- `orchestrator.py` — task decomposition (parallel vs serial) and the concurrency-capped agent pool
- `dashboard.py` / `dashboard.html` — the local multi-agent web UI
- `requirements.txt` — `openai` (OpenAI-compatible client for Ollama), `docker`, `flask` (dashboard)
- `install.sh` / `uninstall.sh` — setup and teardown
- `tests/test_sandbox.py` — CI smoke test for the Docker sandbox mechanics (single- and multi-agent)
- `tests/test_orchestrator.py` — CI unit tests for the orchestrator/dashboard logic (no Docker/Ollama)
- `.github/workflows/ci.yml` — CI pipeline (see above)
- `.install_manifest` — generated by `install.sh`; tracks what it installed
- `.env` — generated by `install.sh`; non-secret config only

## Security notes

- The sandbox container has no resource limits or network restrictions
  beyond the default Docker bridge network — this is a demo, not a hardened
  sandbox. Don't point it at tasks you don't trust an LLM to execute
  unsupervised.
- Everything here runs locally (Docker + Ollama) — no data leaves your
  machine and no API key is needed.
