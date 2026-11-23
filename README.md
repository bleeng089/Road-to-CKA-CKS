# Kubernetes Labs

Self-contained, disposable Kubernetes environments you build yourself with
Terraform, then take apart to see how they actually work. Written for CKA and
CKS practice, where knowing *why* a packet did or did not arrive matters more
than knowing which `kubectl` flag produces it.

## Where to start

Start in [`AWS/kubeadm/`](AWS/kubeadm/README.md). Each lesson's folder holds
the next lesson, and every README ends with a **Next** link into the folder
below it, so you go one folder deeper at each step:

- **1** [`AWS/kubeadm/`](AWS/kubeadm/README.md): build the cluster (step **8**, tearing it down, is its [section 10](AWS/kubeadm/README.md#10-destroy))
  - **2** [`k8/`](AWS/kubeadm/k8/README.md): networking on a three-tier app
    - **3** [`helm/`](AWS/kubeadm/k8/helm/README.md): Helm chart signing
      - **4** [`webshop/`](AWS/kubeadm/k8/helm/webshop/README.md): install the webshop, an order system instrumented with OpenTelemetry
        - **5–7** [`docs/`](AWS/kubeadm/k8/helm/webshop/docs/README.md): incident and blind exercises, networking on the webshop, OpenTelemetry

Every page opens with a one-line box: its step, the folder its commands run
from, and what it needs.

- **Steps 5, 6 and 7 work in any order.** Each needs only step 4.
- **Steps 2 and 6 teach the same networking topics.** Step 2 shows each
  mechanism on three nginx tiers that only say which pod answered. Step 6
  runs the same modules on a real order flow with a database, a queue and
  telemetry, so you also see what each decision costs the users. New to
  Kubernetes networking? Do step 2 first.
- **Step 3 needs no cluster** until its section 9.
- **A bigger worker for step 6.** Its Ingress and Gateway API modules add two
  controllers to the worker, which leaves a `t3.medium` little headroom. Set
  `worker_instance_type = "t3.xlarge"` in `terraform.tfvars` when you build
  (step 1, section 3). Setting it later works too: the worker is then resized
  in place.

## How this repo is organized

**Cloud first, then architecture:**

```text
<cloud>/<architecture>/
```

Each `<cloud>/<architecture>/` directory is one complete, standalone lab: its
own Terraform project, its own state, its own README with the full
walkthrough, and the lessons that run on it, nested inside it as shown above.
Nothing is shared between labs, so you can build one, destroy it, and never
touch the others.

The **AWS / kubeadm** cluster is two nodes (`master` + `worker`) bootstrapped
with `kubeadm` on Ubuntu EC2 — containerd, Calico CNI with NetworkPolicy
enforcement, CoreDNS. No EKS, no managed control plane: every component is
yours to inspect and break.

## Before you start

- **You need an AWS account you can create VPC/EC2/IAM/SSM resources in**, and
  the tooling listed in the lab's prerequisites (Terraform, AWS CLI v2, the
  Session Manager plugin, kubectl).
- **These labs cost money while they exist.** The AWS/kubeadm lab runs about
  $0.09/hour with its two default `t3.medium` nodes, and about $0.21/hour with
  the `t3.xlarge` worker that step 6 suggests. Run `terraform destroy` as soon
  as you stop practicing.
- **Treat every cluster as disposable.** Nothing here is built for
  persistence — public IPs rotate on rebuild, and rebuilding is the expected
  workflow, not a failure mode.
- **Nothing is exposed to `0.0.0.0/0`.** You set `admin_cidr` to your own
  IP/32; that is the only source allowed to reach the API server (and SSH, if
  you enable it).

## Conventions

- Terraform state, `terraform.tfvars`, plan files, and downloaded kubeconfigs
  are git-ignored — they hold personal or sensitive values. `.terraform.lock.hcl`
  **is** committed on purpose, so provider versions stay reproducible.
- All `terraform` and `aws` commands run from inside a lab's directory (e.g.
  `cd AWS/kubeadm`), never from the repo root.
- Every lab works identically from Windows PowerShell, Windows Command
  Prompt, macOS, and Linux. No Bash, WSL, or Git Bash required.
- Every lesson page opens with a one-line box (its step, the folder its
  commands run from, what it needs) and ends with a **Next** link: into the
  folder below, or to the next page in the same folder.

---

> **Next:** [`AWS/kubeadm/`](AWS/kubeadm/README.md), step 1: build the cluster
