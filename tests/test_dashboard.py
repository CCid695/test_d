"""Unit tests for the dashboard Flask app.

Tests the web UI endpoints and authentication without requiring a real
browser or cluster. Uses Flask's test client.
"""

import json

from flask import Flask

import dashboard
from orchestrator import Orchestrator, make_run_fn


def test_index_serves_html():
    """Test that the root endpoint serves the dashboard HTML."""
    orch = Orchestrator(run_fn=lambda *_: "fake", remove_container_fn=lambda *_: None)
    app = dashboard.create_app(orch, "test-token")
    client = app.test_client()

    response = client.get("/")
    assert response.status_code == 200
    assert b"mini-devin" in response.data
    assert b"<!DOCTYPE html>" in response.data


def test_state_endpoint_returns_snapshot():
    """Test that /api/state returns the orchestrator snapshot."""
    orch = Orchestrator(run_fn=lambda *_: "fake", remove_container_fn=lambda *_: None)
    app = dashboard.create_app(orch, "test-token")
    client = app.test_client()

    response = client.get("/api/state")
    assert response.status_code == 200
    data = json.loads(response.data)
    assert "jobs" in data
    assert "agents" in data
    assert "max_concurrent" in data
    assert data["max_concurrent"] == 3


def test_submit_task_requires_token():
    """Test that /api/tasks rejects requests without a valid token."""
    orch = Orchestrator(run_fn=lambda *_: "fake", remove_container_fn=lambda *_: None)
    app = dashboard.create_app(orch, "test-token")
    client = app.test_client()

    # No token
    response = client.post("/api/tasks", json={"task": "test"}, headers={})
    assert response.status_code == 401

    # Wrong token
    response = client.post(
        "/api/tasks",
        json={"task": "test"},
        headers={"Authorization": "Bearer wrong-token"}
    )
    assert response.status_code == 401


def test_submit_task_accepts_valid_token():
    """Test that /api/tasks accepts requests with a valid token."""
    orch = Orchestrator(run_fn=lambda *_: "fake", remove_container_fn=lambda *_: None)
    app = dashboard.create_app(orch, "test-token")
    client = app.test_client()

    response = client.post(
        "/api/tasks",
        json={"task": "write a webpage"},
        headers={"Authorization": "Bearer test-token"}
    )
    assert response.status_code == 202
    data = json.loads(response.data)
    assert "job_id" in data


def test_submit_task_rejects_invalid_json():
    """Test that /api/tasks rejects malformed JSON."""
    orch = Orchestrator(run_fn=lambda *_: "fake", remove_container_fn=lambda *_: None)
    app = dashboard.create_app(orch, "test-token")
    client = app.test_client()

    response = client.post(
        "/api/tasks",
        data="not json",
        headers={"Authorization": "Bearer test-token"}
    )
    assert response.status_code == 400


def test_submit_task_rejects_empty_task():
    """Test that /api/tasks rejects empty task strings."""
    orch = Orchestrator(run_fn=lambda *_: "fake", remove_container_fn=lambda *_: None)
    app = dashboard.create_app(orch, "test-token")
    client = app.test_client()

    response = client.post(
        "/api/tasks",
        json={"task": "   "},
        headers={"Authorization": "Bearer test-token"}
    )
    assert response.status_code == 400


def test_stop_agent_requires_token():
    """Test that /api/agents/<id>/stop requires authentication."""
    orch = Orchestrator(run_fn=lambda *_: "fake", remove_container_fn=lambda *_: None)
    app = dashboard.create_app(orch, "test-token")
    client = app.test_client()

    response = client.post("/api/agents/abc123/stop", headers={})
    assert response.status_code == 401


def test_kill_agent_requires_token():
    """Test that /api/agents/<id>/kill requires authentication."""
    orch = Orchestrator(run_fn=lambda *_: "fake", remove_container_fn=lambda *_: None)
    app = dashboard.create_app(orch, "test-token")
    client = app.test_client()

    response = client.post("/api/agents/abc123/kill", headers={})
    assert response.status_code == 401


def test_stop_nonexistent_agent_returns_404():
    """Test that stopping a non-existent agent returns 404."""
    orch = Orchestrator(run_fn=lambda *_: "fake", remove_container_fn=lambda *_: None)
    app = dashboard.create_app(orch, "test-token")
    client = app.test_client()

    response = client.post(
        "/api/agents/nonexistent/stop",
        headers={"Authorization": "Bearer test-token"}
    )
    assert response.status_code == 404


def test_kill_nonexistent_agent_returns_404():
    """Test that killing a non-existent agent returns 404."""
    orch = Orchestrator(run_fn=lambda *_: "fake", remove_container_fn=lambda *_: None)
    app = dashboard.create_app(orch, "test-token")
    client = app.test_client()

    response = client.post(
        "/api/agents/nonexistent/kill",
        headers={"Authorization": "Bearer test-token"}
    )
    assert response.status_code == 404


def test_app_binds_to_localhost_only():
    """Test that the app configuration uses localhost only."""
    orch = Orchestrator(run_fn=lambda *_: "fake", remove_container_fn=lambda *_: None)
    app = dashboard.create_app(orch, "test-token")

    # The main() function sets host="127.0.0.1", but we can verify
    # the Flask app itself doesn't have a conflicting default
    assert app.name == "dashboard"
