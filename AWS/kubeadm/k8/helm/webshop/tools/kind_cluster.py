#!/usr/bin/env python3
"""Optional: a local Kind cluster shaped like the AWS kubeadm lab.

The AWS lab is the primary target. This is for working without it:

    python tools/kind_cluster.py create      # cluster + Calico + metrics-server + node dirs
    python tools/kind_cluster.py load        # side-load the webshop image (Kind only)
    python tools/kind_cluster.py delete

What makes it lab-shaped:
  * one control plane (keeps its NoSchedule taint) + one worker, Kubernetes 1.33;
  * each node container is limited to 2 CPUs / 4 GiB (like a t3.medium), and
    the kubelet reserves the rest of this machine, so allocatable is ~2 CPU /
    ~3.7 GiB and the scheduler budgets like it would on EC2;
  * Calico (Tigera operator, VXLAN, NetworkPolicy enforced), same version as the lab;
  * no default StorageClass (Kind's local-path provisioner is removed), so the
    chart's localPV path is exercised exactly as on kubeadm;
  * metrics-server from its Helm chart with --kubelet-insecure-tls, as the
    README tells you to install it on the lab.

Needs docker, kind, kubectl and helm on PATH. Works from PowerShell, cmd,
macOS and Linux (no shell features are used).
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
import time

NAME = "webshop-lab"
NODE_IMAGE = "kindest/node:v1.33.12"
CALICO = "v3.30.2"
METRICS_SERVER_CHART = "3.14.0"
STORAGE_DIRS = ["postgresql", "rabbitmq", "prometheus", "loki", "tempo", "grafana"]
NODE_CPUS, NODE_MEMORY_GIB = 2, 4

CALICO_INSTALLATION = """\
apiVersion: operator.tigera.io/v1
kind: Installation
metadata:
  name: default
spec:
  calicoNetwork:
    bgp: Disabled
    ipPools:
      - name: default-ipv4-ippool
        blockSize: 26
        cidr: 192.168.0.0/16
        encapsulation: VXLAN
        natOutgoing: Enabled
        nodeSelector: all()
"""


def run(*args: str, check: bool = True, capture: bool = False, input_text: str | None = None) -> str:
    print("+", " ".join(args), flush=True)
    result = subprocess.run(args, check=check, text=True, input=input_text,
                            stdout=subprocess.PIPE if capture else None)
    return result.stdout or ""


def host_resources() -> tuple[int, int]:
    """CPUs and memory (MiB) Docker gives a container on this machine."""
    out = run("docker", "info", "--format", "{{.NCPU}} {{.MemTotal}}", capture=True).split()
    return int(out[0]), int(out[1]) // (1024 * 1024)


def kind_config(cpus: int, mem_mib: int) -> str:
    # The kubelet reads the host's capacity, not the container's limit; reserve
    # the difference so allocatable matches a t3.medium (2 vCPU, ~3.7 GiB).
    reserve_cpu = max(0, cpus - NODE_CPUS)
    reserve_mem = max(0, mem_mib - (NODE_MEMORY_GIB * 1024 - 200))
    return f"""\
kind: Cluster
apiVersion: kind.x-k8s.io/v1alpha4
name: {NAME}
networking:
  disableDefaultCNI: true
  podSubnet: 192.168.0.0/16
  serviceSubnet: 10.96.0.0/12
kubeadmConfigPatches:
  - |
    kind: KubeletConfiguration
    systemReserved:
      cpu: "{reserve_cpu}"
      memory: "{reserve_mem}Mi"
nodes:
  - role: control-plane
    image: {NODE_IMAGE}
  - role: worker
    image: {NODE_IMAGE}
"""


def create(_: argparse.Namespace) -> None:
    for tool in ("docker", "kind", "kubectl", "helm"):
        if shutil.which(tool) is None:
            sys.exit(f"{tool} is not on PATH")
    cpus, mem = host_resources()
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
        f.write(kind_config(cpus, mem))
        cfg = f.name
    try:
        run("kind", "create", "cluster", "--config", cfg, "--wait", "0s")
    finally:
        os.unlink(cfg)
    ctx = f"kind-{NAME}"
    for node in (f"{NAME}-control-plane", f"{NAME}-worker"):
        run("docker", "update", "--cpus", str(NODE_CPUS), "--memory", f"{NODE_MEMORY_GIB}g",
            "--memory-swap", f"{NODE_MEMORY_GIB}g", node)

    run("kubectl", "--context", ctx, "create", "-f",
        f"https://raw.githubusercontent.com/projectcalico/calico/{CALICO}/manifests/tigera-operator.yaml")
    for attempt in range(30):
        try:
            run("kubectl", "--context", ctx, "apply", "-f", "-", input_text=CALICO_INSTALLATION)
            break
        except subprocess.CalledProcessError:
            time.sleep(10)
    run("kubectl", "--context", ctx, "wait", "--for=condition=Ready", "nodes", "--all", "--timeout=600s")

    # Like kubeadm: no StorageClass, no provisioner.
    run("kubectl", "--context", ctx, "delete", "storageclass", "standard", "--ignore-not-found")
    run("kubectl", "--context", ctx, "delete", "namespace", "local-path-storage", "--ignore-not-found", "--wait=false")

    run("helm", "--kube-context", ctx, "upgrade", "--install", "metrics-server", "metrics-server",
        "--repo", "https://kubernetes-sigs.github.io/metrics-server/", "--version", METRICS_SERVER_CHART,
        "-n", "kube-system", "--set", "args[0]=--kubelet-insecure-tls", "--wait")

    worker = f"{NAME}-worker"
    run("docker", "exec", worker, "mkdir", "-p", *[f"/var/lib/webshop/{d}" for d in STORAGE_DIRS])
    print(f"\nCluster ready: kubectl --context {ctx} get nodes")
    print("Next: python tools/images.py build --tag 0.1.0 && python tools/kind_cluster.py load")


def load(args: argparse.Namespace) -> None:
    """Copy local images into every node's containerd (Kind only).

    Streams `docker save` into `ctr images import` on each node - what
    `kind load docker-image` does, minus its containerd-config check (kind
    v0.31 rejects the version-4 containerd config in newer node images)."""
    nodes = run("kind", "get", "nodes", "--name", NAME, capture=True).split()
    for image in args.images:
        for node in nodes:
            print(f"+ docker save {image} | docker exec -i {node} ctr -n k8s.io images import -", flush=True)
            save = subprocess.Popen(["docker", "save", image], stdout=subprocess.PIPE)
            imp = subprocess.run(["docker", "exec", "-i", node, "ctr", "-n", "k8s.io", "images", "import",
                                  "--digests", "-"], stdin=save.stdout, capture_output=True, text=True)
            save.stdout.close()
            if save.wait() != 0 or imp.returncode != 0:
                sys.exit(f"loading {image} into {node} failed: {imp.stderr.strip()}")


def delete(_: argparse.Namespace) -> None:
    run("kind", "delete", "cluster", "--name", NAME)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("create").set_defaults(fn=create)
    p = sub.add_parser("load")
    p.add_argument("images", nargs="*", default=["webshop:0.1.0"], help="local images (default webshop:0.1.0)")
    p.set_defaults(fn=load)
    sub.add_parser("delete").set_defaults(fn=delete)
    args = parser.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
