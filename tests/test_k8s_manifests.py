"""Pure assertions on k8s_backend's manifest builders — no cluster needed,
these never make a network call."""

from types import SimpleNamespace

from kubernetes import client

import mini_devin as md
from k8s_backend import (
    INGRESS_LOCAL_PORT,
    agent_labels,
    agent_url,
    build_deployment,
    build_ingress,
    build_service,
    owner_ref_for,
)

AGENT_ID = "abc123"
NAME = f"mini-devin-agent-{AGENT_ID}"


def test_deployment_selector_matches_pod_template_labels():
    dep = build_deployment(NAME, AGENT_ID)
    selector = dep.spec.selector.match_labels
    template_labels = dep.spec.template.metadata.labels
    assert selector == {"mini-devin.agent-id": AGENT_ID}
    for key, value in selector.items():
        assert template_labels[key] == value


def test_deployment_hardens_the_pod():
    dep = build_deployment(NAME, AGENT_ID)
    pod_spec = dep.spec.template.spec
    assert pod_spec.automount_service_account_token is False
    assert pod_spec.containers[0].image == md.IMAGE
    assert pod_spec.containers[0].command == ["sleep", "infinity"]


def test_service_selector_matches_deployment():
    dep = build_deployment(NAME, AGENT_ID)
    svc = build_service(NAME, AGENT_ID)
    assert svc.spec.selector == dep.spec.selector.match_labels
    assert svc.spec.type == "ClusterIP"
    assert svc.spec.ports[0].port == 8000


def test_ingress_path_regex_and_rewrite():
    ing = build_ingress(NAME, AGENT_ID)
    path = ing.spec.rules[0].http.paths[0]
    assert path.path == f"/agents/{AGENT_ID}(/|$)(.*)"
    assert path.path_type == "ImplementationSpecific"
    assert ing.metadata.annotations["nginx.ingress.kubernetes.io/rewrite-target"] == "/$2"
    assert ing.metadata.annotations["nginx.ingress.kubernetes.io/use-regex"] == "true"
    backend = path.backend.service
    assert backend.name == NAME
    assert backend.port.number == 8000
    assert ing.spec.ingress_class_name == "nginx"


def test_owner_reference_present_on_service_and_ingress():
    # owner_ref_for() only reads deployment.metadata.{name,uid} — a bare
    # stand-in avoids depending on V1Deployment's other required fields,
    # which differ across kubernetes client versions.
    dep = SimpleNamespace(metadata=client.V1ObjectMeta(name=NAME, uid="fake-uid"))
    owner = owner_ref_for(dep)
    assert owner.uid == "fake-uid"
    assert owner.kind == "Deployment"

    svc = build_service(NAME, AGENT_ID, owner)
    ing = build_ingress(NAME, AGENT_ID, owner)
    assert svc.metadata.owner_references[0].uid == "fake-uid"
    assert ing.metadata.owner_references[0].uid == "fake-uid"


def test_labels_include_agent_role_for_cleanup():
    labels = agent_labels(AGENT_ID)
    assert labels["mini-devin.role"] == "agent"
    assert labels["mini-devin.agent-id"] == AGENT_ID


def test_agent_url_shape():
    url = agent_url(AGENT_ID)
    assert url == f"http://localhost:{INGRESS_LOCAL_PORT}/agents/{AGENT_ID}/"
