# Installing the webshop: what each step does

> **Step 4 of 8: Install the webshop**, each step explained · Run from: as in the quick start (`AWS/kubeadm/`, then `AWS/kubeadm/k8/helm/webshop/`) · Needs: the cluster (step 1)

The procedure is the README's
[quick start](../README.md#quick-start-on-the-aws-lab): six steps, every
block runnable as pasted. This page explains what each step does and why,
and then covers the variations - your own image, a private registry, a
throwaway install, Windows. The only commands here are for those variations
and for looking around; the install itself stays in the quick start.

---

**Contents**

- [Before you start: what you are deploying into](#before-you-start-what-you-are-deploying-into)
- [Step 1: point kubectl at the lab](#step-1-point-kubectl-at-the-lab)
- [Step 2: storage directories on the worker](#step-2-storage-directories-on-the-worker)
- [Step 3: metrics-server](#step-3-metrics-server)
- [Step 4: install](#step-4-install)
- [Step 5: verify](#step-5-verify)
- [Step 6: open the shop and Grafana](#step-6-open-the-shop-and-grafana)
- [Variations](#variations)

---

## Before you start: what you are deploying into

The chart's defaults were budgeted against exactly the AWS lab. To see it for
yourself (optional - the quick start does not need it):

```text
kubectl config current-context
kubectl get nodes -o wide
kubectl describe node worker
kubectl get node master -o jsonpath="{.spec.taints}"
kubectl get storageclass
kubectl get apiservice v1beta1.metrics.k8s.io
kubectl get ingressclass
kubectl get gatewayclass
```

| Question | Lab answer | Consequence |
|---|---|---|
| Context | `kubernetes-admin@kubernetes` (the kubeadm default the lab publishes) | you are pointed at the lab, not some other cluster |
| Worker allocatable | 2 CPU, ~3.7 GiB on a t3.medium | everything must fit here (README, "Scaling and capacity") |
| Master taint | `node-role.kubernetes.io/control-plane:NoSchedule` | kept: nothing in this lesson tolerates it |
| StorageClass | none ("No resources found") | durable components use local PVs (step 2) |
| Metrics API | absent until you install metrics-server | the HPAs need it (step 3) |
| Ingress/Gateway | only if a controller was left installed | port-forwarding is the default way in |

If the three-tier app from the networking lessons is deployed, it can stay:
it lives in its own namespace and requests 125m CPU and 160 Mi in total
(five pods at 25m / 32 Mi), well inside the headroom.

## Step 1: point kubectl at the lab

The control plane publishes a cluster-admin kubeconfig to SSM Parameter
Store when it boots; the quick start copies it into the git-ignored
`AWS/kubeadm/kubeadm-lab.kubeconfig` and points `KUBECONFIG` at it. Every
rebuild of the lab gets a new public IP, so fetch it again after each
`terraform apply`. `kubectl wait --for=condition=Ready node --all` returns once
both nodes have joined and Calico is up on them - right after `apply` the
worker can take a few minutes.

The kubeconfig is set per terminal: a new terminal does not know it, which is
why step 6 sets `KUBECONFIG` again in each port-forward terminal.

## Step 2: storage directories on the worker

The lab has no dynamic volume provisioner, so a PersistentVolumeClaim against
a StorageClass would stay `Pending` forever. Instead, `values/values-aws.yaml`
sets `storage.mode: localPV`: the chart creates one local PersistentVolume per
durable component (PostgreSQL, RabbitMQ, Prometheus, Loki, Tempo, Grafana),
pinned to the `worker` node and pre-bound to the claim that component will
make. A local volume is a directory that must already exist - hence the six
directories.

The quick start creates them through Session Manager's
`AWS-StartInteractiveCommand`, which runs one command on the worker (through
a shell, as `ssm-user` with `sudo`) and closes - no SSH, no open ports. For an
interactive shell on a node instead, which the networking track's **[on the
worker]** steps need, run the `worker` entry of
`terraform output session_manager_commands` (or `ssh_commands`, if you set
`ssh_enabled = true`; see
[AWS/kubeadm/README.md, section 8](../../../../README.md#8-shell-access-to-the-nodes)).

No `chown` is needed, and it does not matter who created the directories:
each pod sets an `fsGroup`, and the kubelet makes the local volume
group-writable for it when it mounts it (PostgreSQL keeps its data in a
subdirectory it creates itself, which is why `initdb` accepts the ownership).

What this storage does and does not survive:

| Event | Data |
|---|---|
| pod restart, rescheduling (the pod follows its volume to `worker`) | kept |
| `helm uninstall` (PVs, generated Secrets and PVCs are kept on purpose) | kept |
| deleting the PVs and the directories (README, "Cleanup") | gone |
| rebuilding the lab (`terraform destroy`/`apply`: new root EBS volume) | gone |

## Step 3: metrics-server

`kubectl autoscale`-style CPU scaling - the webshop's HPAs - reads the
resource metrics API (`metrics.k8s.io`), which **metrics-server** provides,
not Prometheus. It is installed once, into `kube-system`, as a cluster
component in its own Helm release, so uninstalling the webshop never takes it
away from anything else. On a lab that already has it, the quick start's
`helm upgrade --install` changes nothing.

**Why two waits.** `kubectl top nodes` needs two things. `rollout status` waits for
the metrics-server pod to be ready, which it only is after its first round of
node scrapes, up to a minute after the install. The second waits for the API
server's side: it reaches metrics-server through the `v1beta1.metrics.k8s.io`
APIService, which turns `Available` a few seconds after the pod. Run
`kubectl top nodes` before either and it prints `error: Metrics API not available`
or `Error from server (ServiceUnavailable): the server is currently unable to
handle the request (get nodes.metrics.k8s.io)`.

**`--kubelet-insecure-tls`** because kubeadm's kubelet serving certificates
are self-signed; the production fix is `serverTLSBootstrap: true` plus
approving the kubelets' CSRs.

## Step 4: install

`helm dependency build` fetches the pinned dependency charts (exact versions
in `chart/Chart.lock`) from the three repositories, which is why they are
added first - `helm repo add` for one you already have just says it is
skipping. It also repackages the local [`observability/`](../observability)
chart (Collector, Prometheus and Grafana configuration): re-run it whenever
you edit anything there.

`values-aws.yaml` is the lab profile: local PVs on `worker`, NetworkPolicy
CIDRs narrowed to the lab's control plane (`10.0.1.10/32`) and node subnet,
and the public, prebuilt image `docker.io/bleeng089/webshop:0.2.0`.

`--wait --timeout 10m` makes Helm return only when every workload is ready.
The first start takes 3-5 minutes: image pulls, PostgreSQL `initdb`, the
migration Job (`webshop-migrate-<revision>`, one per Helm revision, which ends
`Completed`). Services wait for the database and the schema before they
report ready; nothing crash-loops while they wait. Right after the install
the HPAs may briefly add replicas while the pods' start-up CPU settles.

## Step 5: verify

`helm test` places one real order and judges it by the SLI rules (README,
[What each signal contributes](../README.md#4-what-each-signal-contributes)). `python3 tools/lab.py verify` then checks the
whole telemetry path with that order; it first gives pods that are still
starting up two minutes:

| Check | What it proves |
|---|---|
| pods, NetworkPolicies, metrics.k8s.io | running under default-deny; HPA has its metrics API |
| synthetic journey | fulfilled with the requested item, charged exactly once, within 5 s |
| Prometheus targets, app metrics, instance identity | every Collector pod, exporter, kubelet and backend scraped; one series set per pod |
| exemplars, exemplar -> Tempo | histogram observations carry trace IDs that resolve in Tempo |
| journey trace, context across the queue | one trace spans all six services; `order.fulfill` is a child of `order.publish` |
| Tempo search by order ID; Loki logs for the order | investigation by order ID works in both stores |
| logs carry the trace ID; order ID is structured metadata | Loki -> Tempo and Tempo -> Loki links have something to join on; Loki's index stays small |
| Kubernetes events in Loki; alert rules; Collector export failures | infra signals and pipeline health are collected |
| Grafana data sources, links, dashboards | provisioning worked |

`python3 tools/lab.py check-dashboards` additionally runs every dashboard
query against the live stores and reports any that error.

## Step 6: open the shop and Grafana

Port-forwarding goes through the API server, the only port the lab's
security group opens to you, and needs nothing installed in the cluster.
A port-forward runs until you stop it (Ctrl+C), hence one terminal each.

- <http://localhost:8080> - pick a product **and a color**, check out, and
  watch the order move from *accepted* to *fulfilled*. The page compares what
  you ordered with what shipped.
- <http://localhost:3000> - Grafana. Anonymous access is an Editor (local,
  port-forward only; turn it off in `grafana.grafana.ini` before exposing it).
  The admin user is `admin`; the password is generated on the first install
  and kept in the `webshop-grafana-admin` Secret, which survives reinstalls.
  `python3 tools/lab.py grafana-password` prints it in any shell; on macOS or
  Linux you can also read both values from the Secret:

```text
kubectl -n webshop get secret webshop-grafana-admin -o jsonpath='{.data.admin-user}' | base64 --decode; echo
kubectl -n webshop get secret webshop-grafana-admin -o jsonpath='{.data.admin-password}' | base64 --decode; echo
```

Where to go in Grafana: README, "Dashboards and signal links".

---

## Variations

### If you want to build and publish your own image

Only needed if you change `src/`. Building on your workstation does not put
the image on the EC2 nodes: their containerd pulls it from a registry, like
any other image. Build for `linux/amd64` (the nodes are x86_64, even if your
laptop is ARM). Put your registry account in a variable once - the only
value you have to type yourself - then build and push:

```text
DOCKER_USER=your-docker-hub-account
docker login -u "${DOCKER_USER}"
python3 tools/images.py build --registry "docker.io/${DOCKER_USER}" --tag 0.2.0 --push
```

(Podman works too: `--tool podman`. The equivalent raw command is
`docker buildx build --platform linux/amd64 -t "docker.io/${DOCKER_USER}/webshop:0.2.0" --push src`.)

Then add `--set image.repository="${DOCKER_USER}/webshop"` to the quick
start's `helm upgrade` line. Every new image gets a new tag; to pin exactly
what runs, also set `image.digest` (`sha256:...`).

### If your registry is private

A public repository needs nothing else. For a private one, give the nodes a
pull credential before the install and reference it:

```text
read -rs -p "Docker Hub access token: " DOCKER_TOKEN; echo
kubectl create namespace webshop
kubectl -n webshop create secret docker-registry regcred --docker-server=https://index.docker.io/v1/ --docker-username="${DOCKER_USER}" --docker-password="${DOCKER_TOKEN}"
```

and add `--set imagePullSecrets[0].name=regcred` to the `helm upgrade` line.
Use an access token, not your password (`read -s` keeps it off the screen and
out of your shell history).

### If you want to see what Helm will do first

Render and validate against the live cluster without changing anything, then
install as usual:

```text
helm lint ./chart -f values/values-aws.yaml
helm upgrade --install webshop ./chart -n webshop --create-namespace -f values/values-aws.yaml --dry-run=server
```

### If you want a quick, throwaway install

Skip step 2 entirely with the overlay that puts every volume in an
`emptyDir` - nothing survives a pod restart:

```text
helm upgrade --install webshop ./chart -n webshop --create-namespace -f values/values-aws.yaml -f values/values-ephemeral.yaml --wait --timeout 10m
```

### If your cluster has a storage provisioner

The chart's own default is `storage.mode: storageClass`: every claim, the
subcharts' included, uses the cluster's default StorageClass. It is
`values-aws.yaml` that switches to local PVs and points the subcharts' claims
at the lab's `webshop-local` class - so on such a cluster start from your own
values file instead of `values-aws.yaml` (the storage section of
`chart/values.yaml` lists the settings), and skip step 2.

### If you are on Windows

The quick start is written for bash or zsh. In PowerShell:

- the kubeconfig: the PowerShell and cmd forms in
  [AWS/kubeadm/README.md, section 6](../../../../README.md#6-retrieve-the-kubeconfig);
- a shell variable: `$NAME = ...` instead of `NAME=$(...)`;
- Python: `py` instead of `python3`.

`tools/lab.py` and `tools/images.py` are standard-library Python and behave
the same everywhere.

---

> **Next:** [Incident 1, Slow inventory](incidents/01-slow-inventory.md), step 5: the incident exercises
