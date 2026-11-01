"""Kubernetes-backed sandbox for mini-devin agents: Deployment + Service +
Ingress per agent, in a dedicated namespace, exposed via ingress-nginx and a
kubectl port-forward. Imported lazily — only when MINI_DEVIN_BACKEND=k8s —
so Docker mode and CI never need a kubeconfig.

Deliberately mirrors mini_devin.DockerSandbox's interface
(start/exec/put_archive/remove) so Agent doesn't care which backend it runs
on.
"""

import base64
import os
import secrets
import subprocess
import threading
import time

from kubernetes import client, config
from kubernetes.client.rest import ApiException
from kubernetes.stream import stream

import mini_devin as md

NAMESPACE = "mini-devin"
# Always pin the context explicitly — never trust whatever happens to be
# the user's current kubectl context, so we never create resources in the
# wrong cluster.
KUBE_CONTEXT = os.environ.get("MINI_DEVIN_KUBE_CONTEXT", "minikube")
INGRESS_LOCAL_PORT = int(os.environ.get("INGRESS_LOCAL_PORT", 8080))
TOKEN_SECRET_NAME = "mini-devin-dashboard-token"

EXEC_TIMEOUT_S = 120
POD_READY_TIMEOUT_S = 180

_lock = threading.Lock()
_base_configuration = None
_core = None
_apps = None
_networking = None


def init():
    """One-time setup: load the kubeconfig and build shared API clients for
    everything except exec (see K8sSandbox._api for why exec gets its own)."""
    global _base_configuration, _core, _apps, _networking
    with _lock:
        if _core is not None:
            return
        cfg = client.Configuration()
        try:
            config.load_kube_config(context=KUBE_CONTEXT, client_configuration=cfg)
        except Exception as exc:
            raise RuntimeError(
                f"无法加载 kubeconfig（context={KUBE_CONTEXT}）：{exc}。"
                f"确认 `kubectl --context {KUBE_CONTEXT} cluster-info` 能连上，"
                f"或者设置 MINI_DEVIN_BACKEND=docker 改用 Docker 后端。"
            ) from exc
        _base_configuration = cfg
        api_client = client.ApiClient(cfg)
        _core = client.CoreV1Api(api_client)
        _apps = client.AppsV1Api(api_client)
        _networking = client.NetworkingV1Api(api_client)


def _new_core_api():
    """A CoreV1Api with its own ApiClient. kubernetes.stream.stream() mutates
    shared client state (it temporarily swaps api_client.request) while a
    call is in flight, so sharing one client across concurrent agent threads
    risks a normal REST call getting routed over the websocket path, or the
    other way round. One extra connection per agent is a cheap trade-off."""
    if _base_configuration is None:
        init()
    return client.CoreV1Api(client.ApiClient(_base_configuration))


def cluster_reachable() -> bool:
    try:
        init()
        _core.list_namespace(_request_timeout=5)
        return True
    except Exception:
        return False


def ensure_namespace():
    init()
    body = client.V1Namespace(metadata=client.V1ObjectMeta(name=NAMESPACE))
    try:
        _core.create_namespace(body)
    except ApiException as exc:
        if exc.status != 409:
            raise


# --- Pure manifest builders: no network calls, so these are unit-testable
# without a cluster (see tests/test_k8s_manifests.py). ---

def agent_labels(agent_id: str) -> dict:
    return {
        **md.AGENT_LABEL,  # {"mini-devin.role": "agent"}
        "app.kubernetes.io/name": "mini-devin-sandbox",
        "app.kubernetes.io/managed-by": "mini-devin",
        "mini-devin.agent-id": agent_id,
    }


def agent_url(agent_id: str) -> str:
    return f"http://localhost:{INGRESS_LOCAL_PORT}/agents/{agent_id}/"


def build_deployment(name: str, agent_id: str) -> client.V1Deployment:
    labels = agent_labels(agent_id)
    selector = {"mini-devin.agent-id": agent_id}

    container = client.V1Container(
        name="sandbox",
        image=md.IMAGE,
        image_pull_policy="IfNotPresent",
        command=["sleep", "infinity"],
        ports=[client.V1ContainerPort(name="http", container_port=8000)],
        resources=client.V1ResourceRequirements(
            requests={"cpu": "100m", "memory": "128Mi"},
            limits={"cpu": "1", "memory": "1Gi"},
        ),
    )
    pod_spec = client.V1PodSpec(
        # The LLM runs arbitrary shell in this pod — it should never have a
        # Kubernetes API token sitting in its filesystem.
        automount_service_account_token=False,
        enable_service_links=False,
        # "sleep infinity" as PID 1 ignores SIGTERM; without this, deleting
        # the Deployment (Kill) would wait out the full 30s default grace
        # period for no reason.
        termination_grace_period_seconds=1,
        containers=[container],
    )
    template = client.V1PodTemplateSpec(metadata=client.V1ObjectMeta(labels=labels), spec=pod_spec)
    spec = client.V1DeploymentSpec(
        replicas=1,
        strategy=client.V1DeploymentStrategy(type="Recreate"),
        selector=client.V1LabelSelector(match_labels=selector),
        template=template,
    )
    return client.V1Deployment(
        api_version="apps/v1",
        kind="Deployment",
        metadata=client.V1ObjectMeta(name=name, namespace=NAMESPACE, labels=labels),
        spec=spec,
    )


def build_service(name: str, agent_id: str, owner: client.V1OwnerReference = None) -> client.V1Service:
    labels = agent_labels(agent_id)
    metadata = client.V1ObjectMeta(
        name=name, namespace=NAMESPACE, labels=labels,
        owner_references=[owner] if owner else None,
    )
    spec = client.V1ServiceSpec(
        type="ClusterIP",
        selector={"mini-devin.agent-id": agent_id},
        ports=[client.V1ServicePort(name="http", port=8000, target_port="http")],
    )
    return client.V1Service(api_version="v1", kind="Service", metadata=metadata, spec=spec)


def build_ingress(name: str, agent_id: str, owner: client.V1OwnerReference = None) -> client.V1Ingress:
    labels = agent_labels(agent_id)
    metadata = client.V1ObjectMeta(
        name=name, namespace=NAMESPACE, labels=labels,
        owner_references=[owner] if owner else None,
        annotations={
            "nginx.ingress.kubernetes.io/use-regex": "true",
            # "/$2", not "/" — with a bare "/", every sub-path (e.g.
            # /agents/x/style.css) would be rewritten to "/" and always
            # return index.html.
            "nginx.ingress.kubernetes.io/rewrite-target": "/$2",
        },
    )
    path = client.V1HTTPIngressPath(
        path=f"/agents/{agent_id}(/|$)(.*)",
        # Required for a regex path on ingress-nginx — "Prefix" is rejected.
        path_type="ImplementationSpecific",
        backend=client.V1IngressBackend(
            service=client.V1IngressServiceBackend(
                name=name, port=client.V1ServiceBackendPort(number=8000),
            ),
        ),
    )
    rule = client.V1IngressRule(http=client.V1HTTPIngressRuleValue(paths=[path]))
    spec = client.V1IngressSpec(ingress_class_name="nginx", rules=[rule])
    return client.V1Ingress(api_version="networking.k8s.io/v1", kind="Ingress", metadata=metadata, spec=spec)


def owner_ref_for(deployment: client.V1Deployment) -> client.V1OwnerReference:
    return client.V1OwnerReference(
        api_version="apps/v1",
        kind="Deployment",
        name=deployment.metadata.name,
        uid=deployment.metadata.uid,
        controller=True,
        block_owner_deletion=True,
    )


class K8sSandbox:
    """Kubernetes-backed Sandbox: a Deployment (+ its Pod) exposed via a
    Service and a per-agent Ingress path, implementing the same
    start/exec/put_archive/remove interface as mini_devin.DockerSandbox."""

    def __init__(self, name: str, agent_id: str):
        self.name = name
        self.agent_id = agent_id
        self.host_port = None  # unused in k8s mode; kept so Agent.host_port doesn't break
        self._pod_name = None
        self._core = None

    def _api(self):
        if self._core is None:
            self._core = _new_core_api()
        return self._core

    def start(self, log, should_stop) -> str:
        init()
        ensure_namespace()

        dep_body = build_deployment(self.name, self.agent_id)
        try:
            dep = _apps.create_namespaced_deployment(NAMESPACE, dep_body)
        except ApiException as exc:
            if exc.status != 409:
                raise
            dep = _apps.read_namespaced_deployment(self.name, NAMESPACE)

        owner = owner_ref_for(dep)

        try:
            _core.create_namespaced_service(NAMESPACE, build_service(self.name, self.agent_id, owner))
        except ApiException as exc:
            if exc.status != 409:
                raise

        try:
            _networking.create_namespaced_ingress(NAMESPACE, build_ingress(self.name, self.agent_id, owner))
        except ApiException as exc:
            if exc.status != 409:
                raise

        log(f"[sandbox] 创建 Deployment/Service/Ingress {self.name}，等待 Pod 就绪…")
        self._wait_for_pod(log, should_stop)

        url = agent_url(self.agent_id)
        log(f"[sandbox] Pod {self._pod_name} 已就绪，{url}")
        return url

    def _wait_for_pod(self, log, should_stop):
        deadline = time.time() + POD_READY_TIMEOUT_S
        last_log = 0.0
        while time.time() < deadline:
            if should_stop():
                raise md.AgentStopped(f"agent {self.name} was stopped while waiting for its pod")

            pods = _core.list_namespaced_pod(
                NAMESPACE, label_selector=f"mini-devin.agent-id={self.agent_id}"
            ).items

            waiting_reason = None
            for pod in pods:
                if pod.metadata.deletion_timestamp is not None:
                    continue
                statuses = pod.status.container_statuses or []
                if pod.status.phase == "Running" and statuses and all(c.ready for c in statuses):
                    self._pod_name = pod.metadata.name
                    return
                if statuses and statuses[0].state and statuses[0].state.waiting:
                    waiting_reason = statuses[0].state.waiting.reason

            now = time.time()
            if waiting_reason and now - last_log > 10:
                log(f"[sandbox] 等待 Pod 就绪…（{waiting_reason}）")
                last_log = now
            time.sleep(1)

        raise RuntimeError(f"pod for {self.name} did not become ready within {POD_READY_TIMEOUT_S}s")

    def exec(self, command: str, stdin_bytes: bytes = None):
        if self._pod_name is None:
            raise RuntimeError(f"sandbox {self.name} has no pod yet — call start() first")

        api = self._api()
        resp = stream(
            api.connect_get_namespaced_pod_exec,
            self._pod_name, NAMESPACE,
            container="sandbox",
            command=["sh", "-c", command],
            stdin=stdin_bytes is not None,
            stdout=True, stderr=True, tty=False,
            _preload_content=False,
        )
        try:
            if stdin_bytes is not None:
                resp.write_stdin(stdin_bytes)
            resp.run_forever(timeout=EXEC_TIMEOUT_S)
            output = (resp.read_stdout() or "") + (resp.read_stderr() or "")
            exit_code = resp.returncode
        finally:
            resp.close()

        if exit_code is None:
            # Usual cause: the model started a server with `&` but forgot to
            # redirect its output, so the exec session never sees EOF.
            output += f"\n(命令在 {EXEC_TIMEOUT_S} 秒后仍未结束，输出已截断)"
        return exit_code, output

    def put_archive(self, tar_bytes: bytes):
        # The exec websocket protocol can't half-close stdin, so a bare
        # `tar -xf -` can hang waiting for an EOF that never arrives.
        # `head -c N` exits after exactly N bytes, which gives `tar` a clean
        # EOF. Both tools are in python:3.11-slim.
        exit_code, output = self.exec(f"head -c {len(tar_bytes)} | tar -xf - -C /", stdin_bytes=tar_bytes)
        if exit_code != 0:
            raise RuntimeError(output)

    def remove(self):
        remove_agent_resources(self.name)


def remove_agent_resources(name: str):
    init()
    try:
        _apps.delete_namespaced_deployment(
            name, NAMESPACE,
            body=client.V1DeleteOptions(propagation_policy="Background", grace_period_seconds=0),
        )
    except ApiException as exc:
        if exc.status != 404:
            raise
    # Covers the case where creation failed before the owner reference was
    # set, so cascading GC wouldn't have caught these anyway.
    try:
        _core.delete_namespaced_service(name, NAMESPACE)
    except ApiException as exc:
        if exc.status != 404:
            raise
    try:
        _networking.delete_namespaced_ingress(name, NAMESPACE)
    except ApiException as exc:
        if exc.status != 404:
            raise


def delete_labeled_resources():
    """Clean up any agent Deployments/Services/Ingresses left over from a
    previous crashed run. Scoped to label mini-devin.role=agent inside the
    mini-devin namespace only — never touches the dashboard token Secret or
    anything outside this namespace."""
    init()
    ensure_namespace()
    selector = "mini-devin.role=agent"
    calls = (
        lambda: _apps.delete_collection_namespaced_deployment(NAMESPACE, label_selector=selector),
        lambda: _core.delete_collection_namespaced_service(NAMESPACE, label_selector=selector),
        lambda: _networking.delete_collection_namespaced_ingress(NAMESPACE, label_selector=selector),
    )
    for call in calls:
        try:
            call()
        except ApiException as exc:
            if exc.status != 404:
                raise


def sandbox_factory(rec):
    # sandbox_id, when set, is the shared id collaborate-mode roles use
    # instead of each role's own agent id (they all point at one sandbox).
    return K8sSandbox(rec.container_name, getattr(rec, "sandbox_id", None) or rec.id)


def get_or_create_dashboard_token() -> str:
    init()
    ensure_namespace()
    try:
        secret = _core.read_namespaced_secret(TOKEN_SECRET_NAME, NAMESPACE)
        return base64.b64decode(secret.data["token"]).decode()
    except ApiException as exc:
        if exc.status != 404:
            raise

    token = secrets.token_urlsafe(32)
    body = client.V1Secret(
        metadata=client.V1ObjectMeta(
            name=TOKEN_SECRET_NAME, namespace=NAMESPACE,
            # Deliberately NOT mini-devin.role=agent, so orphan cleanup
            # (delete_labeled_resources) never deletes this.
            labels={"app.kubernetes.io/managed-by": "mini-devin", "mini-devin.role": "dashboard-auth"},
        ),
        type="Opaque",
        string_data={"token": token},
    )
    try:
        _core.create_namespaced_secret(NAMESPACE, body)
    except ApiException as exc:
        if exc.status == 409:
            secret = _core.read_namespaced_secret(TOKEN_SECRET_NAME, NAMESPACE)
            return base64.b64decode(secret.data["token"]).decode()
        raise
    return token


class IngressPortForward:
    """Runs `kubectl port-forward` to the ingress-nginx controller Service as
    a subprocess, so agent URLs (http://localhost:<port>/agents/<id>/) just
    work without the user running an extra command. A port-forward is pinned
    to one controller pod and dies if that pod restarts, so a supervisor
    thread restarts it until stop() is called."""

    def __init__(self, local_port: int = None):
        self.local_port = local_port or INGRESS_LOCAL_PORT
        self._proc = None
        self._stop_requested = False
        self._thread = None

    def start(self):
        self._stop_requested = False
        self._spawn()
        self._thread = threading.Thread(target=self._supervise, daemon=True)
        self._thread.start()

    def _spawn(self):
        self._proc = subprocess.Popen(
            [
                "kubectl", "--context", KUBE_CONTEXT, "-n", "ingress-nginx",
                "port-forward", "--address", "127.0.0.1",
                "svc/ingress-nginx-controller", f"{self.local_port}:80",
            ],
            stdout=subprocess.DEVNULL,
        )

    def _supervise(self):
        while not self._stop_requested:
            if self._proc is not None:
                self._proc.wait()
            if self._stop_requested:
                return
            time.sleep(2)
            if not self._stop_requested:
                self._spawn()

    def stop(self):
        self._stop_requested = True
        if self._proc is not None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._proc.kill()
