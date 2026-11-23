# Kubernetes Labs

Self-contained, disposable Kubernetes environments you build yourself with
Terraform, then take apart to see how they actually work. Written for CKA and
CKS practice, where knowing *why* a packet did or did not arrive matters more
than knowing which `kubectl` flag produces it.

## Learning path

Work through the steps in order. Every step's page opens with the same
header: which step it is, the directory its commands run from, and what it
needs. Its **All steps** link brings you back to this table.

| Step | What you do | Run from | Needs |
|---|---|---|---|
| 1 | [**Build the cluster**](AWS/kubeadm/README.md): two nodes, `kubeadm` on EC2, Calico | `AWS/kubeadm/` | an AWS account |
| 2 | [**Networking on a three-tier app**](AWS/kubeadm/k8/README.md): pods, Services, CoreDNS, NodePort, Ingress, Gateway API and NetworkPolicy, down to `nft` and `conntrack` on the nodes | `AWS/kubeadm/k8/` | step 1 |
| 3 | [**Helm chart signing**](AWS/kubeadm/k8/helm/README.md): step 2's app as a GPG-signed chart, pushed to an OCI registry and verified on the way back | `AWS/kubeadm/k8/helm/` | a registry account; the cluster only from its section 9 |
| 4 | [**Install the webshop**](AWS/kubeadm/k8/webshop/README.md#quick-start-on-the-aws-lab): an order system instrumented with OpenTelemetry, with Prometheus, Loki, Tempo and Grafana | `AWS/kubeadm/`, then `AWS/kubeadm/k8/webshop/` | step 1 |
| 5 | [**Incident and blind exercises**](AWS/kubeadm/k8/webshop/README.md#5-incident-exercises): diagnose faults from telemetry that was already being collected | `AWS/kubeadm/k8/webshop/` | step 4 |
| 6 | [**Networking on the webshop**](AWS/kubeadm/k8/webshop/docs/networking.md): step 2's topics on a real system, plus Calico's eBPF dataplane | `AWS/kubeadm/k8/webshop/` | step 4; a bigger worker (below) |
| 7 | [**OpenTelemetry**](AWS/kubeadm/k8/webshop/docs/opentelemetry.md): the OpenTelemetry concepts, one script at a time, inside the cluster | `AWS/kubeadm/k8/webshop/` | step 4 |
| 8 | [**Tear down**](AWS/kubeadm/README.md#10-destroy): `terraform destroy` removes everything | `AWS/kubeadm/` | — |

- **Steps 5, 6 and 7 work in any order.** Each needs only step 4 and opens
  with its own prerequisites.
- **Steps 2 and 6 teach the same networking topics.** Step 2 shows each
  mechanism on three nginx tiers that only say which pod answered. Step 6
  runs the same modules on a real order flow with a database, a queue and
  telemetry, so you also see what each decision costs the users. New to
  Kubernetes networking? Do step 2 first.
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
walkthrough, and the lessons that run on it. Nothing is shared between labs,
so you can build one, destroy it, and never touch the others.

```text
AWS/kubeadm/              the Terraform project that builds the cluster (steps 1 and 8)
AWS/kubeadm/k8/           networking on a three-tier app (step 2); the other lessons sit inside it
AWS/kubeadm/k8/helm/      Helm chart signing (step 3)
AWS/kubeadm/k8/webshop/   the webshop: install, exercises, networking, OpenTelemetry (steps 4-7)
```

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
- Every lesson page opens with the same header (its step, where its commands
  run from, what it needs) and ends with links to the previous and next step.
