"""Local web dashboard for the mini-devin multi-agent orchestrator.

    python3 dashboard.py
    open http://localhost:5050

Binds to 127.0.0.1 only — this process can run arbitrary shell commands
inside containers on your behalf, so it must never be LAN-reachable.
"""

import os

from flask import Flask, jsonify, request, send_from_directory

import mini_devin as md
from orchestrator import Orchestrator

HERE = os.path.dirname(os.path.abspath(__file__))
DASHBOARD_PORT = int(os.environ.get("DASHBOARD_PORT", 5050))


def create_app(orch: Orchestrator) -> Flask:
    app = Flask(__name__)

    @app.get("/")
    def index():
        return send_from_directory(HERE, "dashboard.html")

    @app.get("/api/state")
    def state():
        return jsonify(orch.snapshot())

    @app.post("/api/tasks")
    def submit_task():
        data = request.get_json(silent=True)
        if not isinstance(data, dict) or not str(data.get("task", "")).strip():
            return jsonify({"error": "expected JSON body {\"task\": \"...\"}"}), 400
        job_id = orch.submit(data["task"].strip())
        return jsonify({"job_id": job_id}), 202

    @app.post("/api/agents/<agent_id>/stop")
    def stop_agent(agent_id):
        return ("", 200) if orch.stop(agent_id) else ("", 404)

    @app.post("/api/agents/<agent_id>/kill")
    def kill_agent(agent_id):
        return ("", 200) if orch.kill(agent_id) else ("", 404)

    return app


def remove_labeled_containers():
    """Clean up any agent containers left over from a previous crashed run."""
    client = md.get_docker()
    label_filter = [f"{k}={v}" for k, v in md.AGENT_LABEL.items()]
    for container in client.containers.list(all=True, filters={"label": label_filter}):
        container.remove(force=True)


def main():
    remove_labeled_containers()
    orch = Orchestrator()
    app = create_app(orch)
    try:
        app.run(host="127.0.0.1", port=DASHBOARD_PORT, threaded=True, use_reloader=False)
    finally:
        orch.shutdown()


if __name__ == "__main__":
    main()
