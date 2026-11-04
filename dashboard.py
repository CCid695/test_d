"""Local web dashboard for the mini-devin multi-agent orchestrator.

    python3 dashboard.py
    open http://localhost:5050

Binds to 127.0.0.1 only — this process can run arbitrary shell commands
inside sandboxes on your behalf, so it must never be LAN-reachable. Every
mutating route (/api/tasks, /stop, /kill) requires a bearer token (see
README.md's "Multi-agent dashboard" section for how to retrieve it) — this
closes an otherwise-real cross-site hole: any page, including one an agent
just wrote and served, could otherwise auto-submit a same-origin POST to
127.0.0.1:5050 and kill/redirect your agents.
"""

import hmac
import os

from flask import Flask, jsonify, request, send_from_directory

import k8s_backend as kb
from orchestrator import Orchestrator, make_run_fn

HERE = os.path.dirname(os.path.abspath(__file__))
DASHBOARD_PORT = int(os.environ.get("DASHBOARD_PORT", 5050))


def create_app(orch: Orchestrator, token: str) -> Flask:
    app = Flask(__name__)

    def require_token():
        header = request.headers.get("Authorization", "")
        supplied = header[len("Bearer "):] if header.startswith("Bearer ") else ""
        if not supplied or not hmac.compare_digest(supplied, token):
            return jsonify({"error": "unauthorized"}), 401
        return None

    @app.get("/")
    def index():
        return send_from_directory(HERE, "dashboard.html")

    @app.get("/api/state")
    def state():
        return jsonify(orch.snapshot())

    @app.post("/api/tasks")
    def submit_task():
        unauthorized = require_token()
        if unauthorized:
            return unauthorized
        data = request.get_json(silent=True)
        if not isinstance(data, dict) or not str(data.get("task", "")).strip():
            return jsonify({"error": "expected JSON body {\"task\": \"...\"}"}), 400
        job_id = orch.submit(data["task"].strip())
        return jsonify({"job_id": job_id}), 202

    @app.post("/api/agents/<agent_id>/stop")
    def stop_agent(agent_id):
        unauthorized = require_token()
        if unauthorized:
            return unauthorized
        return ("", 200) if orch.stop(agent_id) else ("", 404)

    @app.post("/api/agents/<agent_id>/kill")
    def kill_agent(agent_id):
        unauthorized = require_token()
        if unauthorized:
            return unauthorized
        return ("", 200) if orch.kill(agent_id) else ("", 404)

    return app


def main():
    kb.init()
    kb.ensure_namespace()
    kb.delete_labeled_resources()

    token = kb.get_or_create_dashboard_token()
    print(
        "[dashboard] Get dashboard token:\n"
        f"  kubectl --context {kb.KUBE_CONTEXT} -n {kb.NAMESPACE} get secret "
        f"{kb.TOKEN_SECRET_NAME} -o jsonpath='{{.data.token}}' | base64 -d"
    )

    port_forward = kb.IngressPortForward()
    port_forward.start()
    print(f"[dashboard] Started kubectl port-forward, agent page entry: http://localhost:{kb.INGRESS_LOCAL_PORT}/agents/<id>/")

    orch = Orchestrator(
        run_fn=make_run_fn(kb.sandbox_factory),
        remove_container_fn=kb.remove_agent_resources,
    )

    app = create_app(orch, token)
    try:
        app.run(host="127.0.0.1", port=DASHBOARD_PORT, threaded=True, use_reloader=False)
    finally:
        orch.shutdown()
        port_forward.stop()


if __name__ == "__main__":
    main()
