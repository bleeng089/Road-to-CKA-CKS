# Kubernetes Labs

Self-contained, disposable Kubernetes environments you build yourself with
Terraform, then take apart to see how they actually work. Written for CKA and
CKS practice, where knowing *why* a packet did or did not arrive matters more
than knowing which `kubectl` flag produces it.

## How this repo is organized

**Cloud first, then architecture:**

```text
<cloud>/<architecture>/
```

Each `<cloud>/<architecture>/` directory is one complete, standalone lab: its
own Terraform project, its own state, its own README with the full
walkthrough. Nothing is shared between labs, so you can build one, destroy
it, and never touch the others.

```text
AWS/kubeadm/        kubeadm on EC2 — two-node cluster, Calico CNI
AWS/kubeadm/k8/     hands-on networking lessons for that cluster
```

## Labs

| Lab | What it builds | Guide |
|---|---|---|
| **AWS / kubeadm** | Two-node cluster (`master` + `worker`) bootstrapped with `kubeadm` on Ubuntu EC2 — containerd, Calico CNI with NetworkPolicy enforcement, CoreDNS. No EKS, no managed control plane: every component is yours to inspect and break. | **[AWS/kubeadm/README.md](AWS/kubeadm/README.md)** |

Each lab's README is the complete guide — prerequisites, deploy, verify,
troubleshoot, destroy, and a variable reference. Start there, not here.

### Included lessons

The **AWS / kubeadm** lab ships a nine-module networking curriculum in
[`AWS/kubeadm/k8/`](AWS/kubeadm/k8/README.md), taught on one three-tier app
(frontend → api → db): pod networking, ClusterIP, CoreDNS, NodePort, Ingress,
the Gateway API, and NetworkPolicy. Most modules end by dropping you onto a
node to read the kernel state your `kubectl` command just produced — `nft`
for the rules kube-proxy and Calico programmed, `conntrack` for the
connections flowing through them. Modules are self-contained; each opens with
a Prerequisites block, so you can jump straight to the one you need.

## Before you start

- **You need an AWS account you can create VPC/EC2/IAM/SSM resources in**, and
  the tooling listed in the lab's prerequisites (Terraform, AWS CLI v2, the
  Session Manager plugin, kubectl).
- **These labs cost money while they exist.** The AWS/kubeadm lab runs about
  $0.09/hour. Run `terraform destroy` as soon as you stop practicing.
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
