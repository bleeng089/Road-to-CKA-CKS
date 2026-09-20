# Kubernetes Networking, Hands-On

A guided lesson on how traffic moves through a Kubernetes cluster — pod
networking, Services, CoreDNS, ClusterIP, NodePort, Ingress, the Gateway API,
and NetworkPolicy — taught through one simple three-tier app:

```text
        client
          |
          v
   +-------------+        +-------------+        +-------------+
   |  frontend   | -----> |     api     | -----> |     db      |
   |  web tier   |  /api/ |  logic tier |  /db/  |  data tier  |
   |  2 replicas |        |  2 replicas |        |  1 replica  |
   |  pod :8080  |        |  pod :9000  |        |  pod :9000  |
   +-------------+        +-------------+        +-------------+
     Service                 Service                Service
     frontend:80             api:80                 db:80
```

Every tier is a stock `nginx:1.27-alpine` whose config is injected from a
ConfigMap. Each one answers with its own identity, and the frontend and api
tiers proxy onward to the next Service by DNS name — so a single request can
be made to walk the whole chain, hopping through two Services on the way.

**Where to run this:** any cluster with a NetworkPolicy-capable CNI. It is
written for the cluster built by the Terraform project in the parent
directory, [`AWS/kubeadm/`](../README.md) (Kubernetes 1.33 + Calico, nodes
named `master` and `worker`), where every `kubectl` works from your own
machine. Anything that must run *on a node* is marked **[on a node]** (naming
the node when it matters) — get a node shell with
`terraform output session_manager_commands` or
`terraform output ssh_commands`, run from the parent directory. The node
tooling the lesson leans on (`nft`, `conntrack`) is preinstalled by the
bootstrap.

**Modules are self-contained.** Each one opens with a **Prerequisites**
block holding the exact commands to reach the state it needs — jump straight
to whichever module you care about and run its block first; nothing else
from earlier modules is assumed.

Run all `kubectl` commands below from this `k8/` directory.

---

## Module 1 — Pod networking: every pod gets a real IP

**Prerequisites:** a running cluster with kubectl access (main README,
sections 4–6). This module deploys the app itself.

Deploy the app and look at where things landed:

```text
kubectl apply -f app/
kubectl -n three-tier get pods -o wide --watch
```

Wait until all 5 pods are `Running` and `1/1` (Ctrl-C to stop watching).
Notice the `IP` column: every pod has its own address from the **pod CIDR**
(`192.168.0.0/16` in this lab — handed to Calico by kubeadm), regardless of
which node it runs on. That is the core promise of Kubernetes networking:

- every pod can reach every pod, across nodes, **without NAT**;
- containers in one pod share one network namespace (they are `localhost` to
  each other);
- nodes can reach all pods. Calico makes this true here by carrying pod
  traffic between nodes in VXLAN.

Prove pods are directly reachable — start a test pod you will use all lesson,
and curl a pod IP straight (substitute an IP from the output above):

```text
kubectl -n three-tier run tester --image=busybox:1.36 -- sleep 7200
kubectl -n three-tier wait --for=condition=Ready pod/tester --timeout=60s
kubectl -n three-tier get pod -l app=db -o jsonpath="{.items[0].status.podIP}"
kubectl -n three-tier exec tester -- wget -qO- --timeout=3 http://<that-pod-ip>:9000/
```

You get `[db] pod db-... - you reached the data tier...`. Now the problem:

```text
kubectl -n three-tier delete pod -l app=db
kubectl -n three-tier get pod -l app=db -o wide
```

The Deployment replaced the pod — **with a different IP**. Pod IPs are
ephemeral. Nothing should ever hardcode one. Enter Services.

## Module 2 — ClusterIP Services: a stable virtual IP in front of pods

**Prerequisites** — the app deployed. Jumping straight here? Run:

```text
kubectl apply -f app/
kubectl -n three-tier rollout status deploy/db deploy/api deploy/frontend --timeout=180s
```

The app manifests already created one Service per tier. Look at them:

```text
kubectl -n three-tier get svc
kubectl -n three-tier describe svc db
kubectl -n three-tier get endpointslices
```

Things to see in that output:

- **CLUSTER-IP** — a virtual IP from the service CIDR (`10.96.0.0/12` here).
  No container listens on it. On every node, kube-proxy programs
  iptables/IPVS rules that rewrite (DNAT) connections to a ClusterIP into
  connections to one of the backing pod IPs.
- **Endpoints / EndpointSlices** — the live list of pod IPs behind the
  Service, maintained by matching the Service's `selector` against **Ready**
  pods. Readiness probes gate membership: an unready pod receives no traffic.
- **port vs targetPort** — clients talk to Service port `80`; the pods
  actually listen on `9000`/`8080` (`targetPort`). The DNAT rewrites both
  address and port.

Watch the endpoint list follow pod readiness:

```text
kubectl -n three-tier scale deploy/api --replicas=3
kubectl -n three-tier get endpointslices -l kubernetes.io/service-name=api -o wide
kubectl -n three-tier scale deploy/api --replicas=2
```

### Under the hood: nft and conntrack **[on a node]**

Two kernel-side tools turn Services from magic into mechanism; both are on
the lab nodes (`nft` ships with Ubuntu, the bootstrap installs `conntrack`).

kube-proxy here runs in **iptables mode** (kubeadm's default — check with
`kubectl -n kube-system get cm kube-proxy -o yaml | grep "mode:"`; the empty
string means iptables). On Ubuntu the iptables tooling is the *nftables
backend*, so the rules physically live in nft tables, and
`sudo nft list ruleset` is the honest, complete view of the node. Find db's
ClusterIP in it (get the IP from `kubectl -n three-tier get svc db`):

```text
sudo nft list table ip nat | grep <db-cluster-ip>
```
```text
meta l4proto tcp ip daddr 10.103.148.244  tcp dport 80 counter ... jump KUBE-SVC-4RZGLY6GHUVSFFD3
meta l4proto tcp ip saddr != 192.168.0.0/16 ip daddr 10.103.148.244  tcp dport 80 ... jump KUBE-MARK-MASQ
```

Rule 1: anything addressed to ClusterIP:80 jumps into that Service's
`KUBE-SVC-*` chain. Rule 2: traffic from outside the pod CIDR gets marked
for masquerade — you will meet that mark again in Module 5. Expect two
quirks in nft output: `# Warning: table ip nat is managed by iptables-nft,
do not touch!` (edit via iptables, not nft) and opaque `xt match ...`
fragments — parts of rules (comments included) written through iptables
extensions that nft cannot fully render. The decoded view of the same
rules:

```text
sudo iptables-save -t nat | grep "three-tier/db"
```
```text
-A KUBE-SERVICES -d 10.103.148.244/32 -p tcp -m comment --comment "three-tier/db:http cluster IP" -m tcp --dport 80 -j KUBE-SVC-4RZGLY6GHUVSFFD3
-A KUBE-SEP-CFKHJXXCSBEIJ4EH -p tcp -m comment --comment "three-tier/db:http" -m tcp -j DNAT --to-destination 192.168.36.2:9000
```

The walk: `KUBE-SERVICES` (consulted for every packet) → `KUBE-SVC-<hash>`
(one per Service; picks an endpoint, randomly when there are several — that
*is* the load balancing) → `KUBE-SEP-<hash>` (one per endpoint; performs
the DNAT to pod:9000). No process, no proxy — a Service is a chain walk.

**conntrack** is the other half. NAT is stateful: the kernel records every
flow so replies can be un-NATted on the way back. Make a connection, then
look up its entry:

```text
curl -s http://<db-cluster-ip>/ >/dev/null
sudo conntrack -L -d <db-cluster-ip>
```
```text
tcp  6 119 TIME_WAIT src=10.0.1.11 dst=10.103.148.244 sport=55402 dport=80 ... src=192.168.36.2 dst=10.0.1.11 sport=9000 ... [ASSURED]
```

One entry, two tuples: the **original** direction (what the client sent —
node → ClusterIP:80) and the **reply** direction (who actually answered —
pod:9000 → node). The difference between the tuples *is* the DNAT. You
will also see a steady background of `src=<node-ip> dst=<pod-ip>
dport=9000/8080` entries: kubelet's readiness probes. Learning to skim
past probe noise is half of reading conntrack.

**When you'll reach for these:** every "the Service doesn't answer"
incident. No `KUBE-SVC` chain → the Service doesn't exist on this node;
chain exists but no `KUBE-SEP` lines → no Ready endpoints (look at
readiness probes); rules fine but the conntrack reply tuple never moves →
the pod isn't answering, or something is dropping traffic (Module 8 shows
that signature). Capacity matters too: every flow costs a slot — compare
`sudo conntrack -C` with `sysctl net.netfilter.nf_conntrack_max` on busy
nodes; a full table means dropped connections. (kube-proxy also has a
native `mode: nftables`, GA since Kubernetes 1.33 — with it these rules
appear as first-class nft chains instead of iptables translations.)

## Module 3 — CoreDNS: names instead of virtual IPs

**Prerequisites** — the app deployed and the tester pod running. Jumping
straight here? Run:

```text
kubectl apply -f app/
kubectl -n three-tier rollout status deploy/db deploy/api deploy/frontend --timeout=180s
kubectl -n three-tier run tester --image=busybox:1.36 -- sleep 7200
kubectl -n three-tier wait --for=condition=Ready pod/tester --timeout=60s
```

(An `AlreadyExists` error on the tester just means an earlier module
created it.)

Nobody wants to pass ClusterIPs around either. Every Service gets DNS:

```text
<service>.<namespace>.svc.cluster.local  ->  its ClusterIP
```

CoreDNS serves these records. It runs as pods in `kube-system` and is itself
reached through a ClusterIP Service (bootstrapped-by-hand irony included):

```text
kubectl -n kube-system get deploy coredns
kubectl -n kube-system get svc kube-dns
kubectl -n kube-system get configmap coredns -o yaml   # the Corefile
```

Every container's `/etc/resolv.conf` points at that Service IP. Look at it
from the tester pod, then resolve some names:

```text
kubectl -n three-tier exec tester -- cat /etc/resolv.conf
kubectl -n three-tier exec tester -- nslookup db
kubectl -n three-tier exec tester -- nslookup kubernetes.default
```

The first `nslookup` prints something like:

```text
Name:   db.three-tier.svc.cluster.local
Address: 10.109.180.207

** server can't find db.svc.cluster.local: NXDOMAIN
** server can't find db.cluster.local: NXDOMAIN
...
```

Read that closely — it is the whole DNS story in one screenful. The `search`
line in resolv.conf lists `three-tier.svc.cluster.local svc.cluster.local
cluster.local ...`, and `ndots:5` makes the resolver try each suffix.
busybox's nslookup queries *all* of them and prints every result: the first
`Name/Address` pair is the real answer (short name + own-namespace suffix);
the NXDOMAIN lines are the rest of the suffix walk. (It also exits non-zero
because some of those queries failed — harmless here.) Same-namespace
lookups can use the short name `db`; cross-namespace ones need at least
`<service>.<namespace>`.

### Under the hood: your DNS query was NATted too **[on a node]**

kube-dns is just another ClusterIP Service, so DNS rides the same DNAT
machinery as Module 2:

```text
nslookup db.three-tier.svc.cluster.local 10.96.0.10 >/dev/null
sudo conntrack -L -p udp | grep 10.96.0.10
```
```text
udp  17 29 src=10.0.1.11 dst=10.96.0.10 sport=57293 dport=53 ... src=192.168.194.132 dst=192.168.36.0 sport=53 ...
```

Original tuple: a query to the kube-dns ClusterIP. Reply tuple: from
`192.168.194.132` — a CoreDNS **pod on the master**. (Each node owns a
block of the pod CIDR: `192.168.194.x` is the master's, `192.168.36.x` the
worker's — cross-check with `kubectl -n kube-system get pods -o wide`.)
And the reply returns to `192.168.36.0`, the node's Calico VXLAN address —
that is Module 2's masquerade rule at work. One nslookup: DNAT, cross-node
VXLAN, and SNAT.

**Ops note:** stale **UDP** conntrack entries are the classic cluster-DNS
incident. UDP has no handshake, so when CoreDNS pods get replaced, cached
flows can keep steering queries at pod IPs that no longer exist.
`sudo conntrack -D -p udp --dport 53` flushes them — the first thing to
try when DNS is "flaky on one node only". (NodeLocal DNSCache exists
largely to make this whole failure class disappear.)

**Headless Services (aside):** set `clusterIP: None` and DNS returns the pod
IPs themselves — no virtual IP, no load balancing; this is how StatefulSets
address individual members:

```text
kubectl apply -f services/db-headless.yaml
kubectl -n three-tier scale deploy/db --replicas=2
kubectl -n three-tier exec tester -- nslookup db            # one ClusterIP
kubectl -n three-tier exec tester -- nslookup db-headless   # the pod IPs
kubectl -n three-tier scale deploy/db --replicas=1
```

## Module 4 — Walk the chain

**Prerequisites** — the app deployed and the tester pod running. Jumping
straight here? Run:

```text
kubectl apply -f app/
kubectl -n three-tier rollout status deploy/db deploy/api deploy/frontend --timeout=180s
kubectl -n three-tier run tester --image=busybox:1.36 -- sleep 7200
kubectl -n three-tier wait --for=condition=Ready pod/tester --timeout=60s
```

Now use names + Services the way the app does. From the tester pod:

```text
kubectl -n three-tier exec tester -- wget -qO- --timeout=3 http://frontend/
kubectl -n three-tier exec tester -- wget -qO- --timeout=3 http://frontend/api/
kubectl -n three-tier exec tester -- wget -qO- --timeout=3 http://frontend/api/db/
```

Three answers from three depths: `[frontend]`, `[api]`, `[db]`. The last one
traversed: tester → (DNS + ClusterIP) → a frontend pod → (ClusterIP) → an api
pod → (ClusterIP) → the db pod, with kube-proxy DNAT at every Service hop and
Calico moving packets between nodes. (Trailing slashes matter — the nginx
configs use them to strip the matched prefix.)

Run the first curl a few times and watch `pod ...` change: Service load
balancing across the two frontend replicas.

### Under the hood: one curl, a stack of flows **[on the worker]**

Run the chain curl once more, then list flows addressed to Service port 80
on the node where the pods run:

```text
kubectl -n three-tier exec tester -- wget -qO- --timeout=3 http://frontend/api/db/
```
```text
sudo conntrack -L -p tcp --dport 80
```

Three entries with Module 2's shape, one per hop: origin `dst=` is
frontend's, api's, then db's ClusterIP, each replied by a different
pod:8080/9000. Every arrow in the chain exists as a conntrack entry on the
node where its client runs. That makes conntrack the tool for "this
multi-tier request sometimes hangs" — the broken hop is the one whose entry
sits in `SYN_SENT [UNREPLIED]` instead of `ESTABLISHED`/`TIME_WAIT`.

## Module 5 — NodePort: the simplest way in from outside

**Prerequisites** — the app deployed (namespace, Deployments, and ClusterIP
Services). Jumping straight here? Run:

```text
kubectl apply -f app/
kubectl -n three-tier rollout status deploy/db deploy/api deploy/frontend --timeout=180s
```

A ClusterIP is unreachable from outside the cluster. A **NodePort** Service
additionally opens the same port on **every node** (default range
30000-32767) and forwards it to the Service:

```text
kubectl apply -f services/frontend-nodeport.yaml
kubectl -n three-tier get svc frontend
```

`PORT(S)` now shows `80:30080/TCP`. **[on a node]**:

```text
curl http://localhost:30080/
curl http://localhost:30080/api/db/
curl http://10.0.1.11:30080/          # the worker's private IP — every node answers
```

Every node answers, whether or not it runs a frontend pod — kube-proxy on
each node forwards NodePort traffic into the Service like any other hop.

### Under the hood: NodePort is a shim on the ClusterIP machinery **[on the master]**

The master is the instructive place to look — it runs **no** frontend pod,
yet port 30080 answers there:

```text
sudo nft list table ip nat | grep 30080
```
```text
meta l4proto tcp ip daddr 127.0.0.0/8  tcp dport 30080 xt match "nfacct" ... jump KUBE-EXT-U4H5QCYWQTKRL3CD
meta l4proto tcp  tcp dport 30080 counter ... jump KUBE-EXT-U4H5QCYWQTKRL3CD
```

Any tcp/30080 arriving on this node jumps to `KUBE-EXT-*`, which marks the
packet for masquerade (Module 2's mark) and then joins the **same**
`KUBE-SVC-*` chain that ClusterIP traffic uses — NodePort adds one chain in
front of machinery you already know. Now watch a connection:

```text
curl -s http://localhost:30080/ >/dev/null
sudo conntrack -L -p tcp --dport 30080
```
```text
tcp  6 119 TIME_WAIT src=127.0.0.1 dst=127.0.0.1 sport=39336 dport=30080 ... src=192.168.36.6 dst=192.168.194.128 sport=8080 ... [ASSURED]
```

Both NATs in one line. DNAT: `:30080` became pod `192.168.36.6:8080` — a
frontend pod **on the worker**, so the packet crossed the VXLAN. SNAT: the
reply goes to `192.168.194.128`, the *master's* Calico address — the client
was masqueraded so the return path leads back through the node that
accepted the connection. That masquerade is also why the app sees the node,
not the real client, as the source — exactly the problem
`externalTrafficPolicy: Local` exists to solve ("Where to go next").

**When you'll reach for this:** "NodePort not answering" triage in three
questions. Is the rule programmed (`nft`/`iptables-save` grep the port)?
Does a conntrack entry appear when you curl? Does its reply tuple ever move
past `[UNREPLIED]`? The three answers point at kube-proxy, the network
path, or the pod, respectively.

**Why not from your laptop?** This lab's security group only opens 6443 (API)
and optionally 22 to your `admin_cidr` — NodePorts are deliberately not
exposed. To try it from your machine anyway (run from the parent directory;
PowerShell users set the two values manually):

```text
aws ec2 authorize-security-group-ingress --group-id $(terraform output -raw security_group_id) --protocol tcp --port 30000-32767 --cidr <your-ip>/32 --region $(terraform output -raw aws_region)
```

then `http://<node-public-ip>:30080/`. Undo with the same command and
`revoke-security-group-ingress`. (`terraform destroy` cleans it up
regardless.)

NodePort limitations are what the next two modules fix: weird high ports, one
Service per port, no host/path routing, no TLS termination.

## Module 6 — Ingress: HTTP routing behind one entry point

**Prerequisites** — the app deployed. Jumping straight here? Run:

```text
kubectl apply -f app/
kubectl -n three-tier rollout status deploy/db deploy/api deploy/frontend --timeout=180s
```

An **Ingress** is a set of L7 routing rules (host + path → Service). The
resource does nothing by itself — an **ingress controller** must be installed
to implement it; the `ingressClassName` field says which controller a given
Ingress belongs to. Install ingress-nginx (bare-metal flavor, pinned):

```text
kubectl apply -f https://raw.githubusercontent.com/kubernetes/ingress-nginx/controller-v1.13.3/deploy/static/provider/baremetal/deploy.yaml
kubectl -n ingress-nginx wait --for=condition=Ready pod -l app.kubernetes.io/component=controller --timeout=180s
```

The controller is itself just pods behind a NodePort Service — nothing
magical:

```text
kubectl -n ingress-nginx get pods,svc
```

Note the NodePort mapped to port 80 of `ingress-nginx-controller` (e.g.
`80:3XXXX/TCP` — yours is random). Now apply routing rules for our app:

```text
kubectl apply -f ingress/app-ingress.yaml
kubectl -n three-tier get ingress three-tier
```

`ingress/app-ingress.yaml` routes host `app.local`: path `/api` → the api
Service, `/` → frontend. Longest path match wins. **[on a node]** (replace
`3XXXX` with the controller's NodePort for port 80):

```text
curl -H "Host: app.local" http://localhost:3XXXX/
curl -H "Host: app.local" http://localhost:3XXXX/api/
curl -H "Host: app.local" http://localhost:3XXXX/anything-else
```

`/` and `/anything-else` answer `[frontend]`; `/api/` answers `[api]` — the
controller, not the app, did that routing. The `Host` header stands in for
real DNS: in production `app.local` would be an A record pointing at a load
balancer in front of the controller, and the Ingress would also hold the TLS
certificate (`spec.tls`) and terminate HTTPS.

**[on a node]** Under the hood there is nothing new: the controller's
Service rides the same `KUBE-EXT-*`/`KUBE-SVC-*` chains at its own
nodePort, and after a curl `sudo conntrack -L -p tcp --dport 3XXXX` shows
hop one (you → controller pod, masqueraded) while `--dport 80` shows hop
two (controller → frontend/api ClusterIP). Two separate flows — because an
ingress controller is a real userspace proxy that terminates TCP and opens
a new connection, unlike kube-proxy's in-kernel NAT.

Ingress got HTTP routing standardized, but it shows its age: it only speaks
HTTP, anything interesting hides in controller-specific annotations, and one
resource mixes infrastructure concerns (listeners, TLS) with app concerns
(routes). That critique produced the Gateway API.

## Module 7 — Gateway API: the successor

**Prerequisites** — the app deployed. Jumping straight here? Run:

```text
kubectl apply -f app/
kubectl -n three-tier rollout status deploy/db deploy/api deploy/frontend --timeout=180s
```

The **Gateway API** splits the job across three resources matching three
roles:

| Resource | Owner | Answers |
|---|---|---|
| `GatewayClass` | infrastructure provider | "which controller implements this?" |
| `Gateway` | platform team | "what listeners exist (port/protocol/TLS)?" |
| `HTTPRoute` (+ `GRPCRoute`, `TLSRoute`, ...) | app team | "which requests go to which Service?" |

Routes attach to Gateways (`parentRefs`), Gateways to a class; typed fields
replace annotation soup, and non-HTTP protocols are first-class. Install
Envoy Gateway as the implementation (its manifest bundles the Gateway API
CRDs; `--server-side` because those CRDs are huge):

```text
kubectl apply --server-side -f https://github.com/envoyproxy/gateway/releases/download/v1.4.2/install.yaml
kubectl -n envoy-gateway-system wait --for=condition=Available deploy/envoy-gateway --timeout=180s
kubectl get crd | grep gateway
```

Apply our resources (`gateway/` holds four files — class, gateway, route,
plus an Envoy-specific `proxy-config.yaml` explained below) and check their
status conditions — Gateway API resources report rich status:

```text
kubectl apply -f gateway/
kubectl get gatewayclass eg
kubectl -n three-tier get gateway app-gateway
kubectl -n three-tier get httproute three-tier -o jsonpath="{.status.parents[0].conditions}"
```

Within a minute or two the Gateway shows `PROGRAMMED: True` and an ADDRESS
(a node IP). Envoy Gateway reacted to the `Gateway` by launching an actual
Envoy proxy Deployment + Service for it:

```text
kubectl -n envoy-gateway-system get pods,svc
```

**Why `proxy-config.yaml` exists:** by default Envoy Gateway exposes each
Gateway with a `LoadBalancer` Service. On a cloud that provisions a real LB;
on this bare kubeadm cluster it would sit at `<pending>` forever and the
Gateway would stay `Programmed: False (AddressNotAssigned)`. The `EnvoyProxy`
resource — attached through the Gateway's `infrastructure.parametersRef` —
switches the proxy Service to NodePort. That is the Gateway API extension
pattern: the standard resources stay portable, implementation-specific knobs
live in referenced vendor resources.

Find the allocated node port and test **[on a node]** (replace `3YYYY`):

```text
kubectl -n envoy-gateway-system get svc -l gateway.envoyproxy.io/owning-gateway-name=app-gateway
curl -H "Host: app.local" http://localhost:3YYYY/
curl -H "Host: app.local" http://localhost:3YYYY/api/
```

Same routing behavior as the Ingress — `[frontend]` and `[api]` — expressed
in the newer API. If you also ran Module 6, the Ingress (via ingress-nginx)
and the HTTPRoute (via Envoy) are serving side by side right now, which is
itself a lesson: routing APIs are just resources; controllers make them
real.

**[on a node]** And underneath, the same machinery again: Envoy's Service
rides the standard chains — after a curl, `sudo conntrack -L -p tcp
--dport 3YYYY` shows your hop to Envoy, `--dport 80` shows Envoy's hop to
the backend Service. Every router in this lesson, however fancy, ends in
the same nft rules and conntrack entries.

## Module 8 — NetworkPolicy: firewalling the tiers

**Prerequisites** — the app deployed, the tester pod running, and no
leftover policies. Jumping straight here? Run:

```text
kubectl apply -f app/
kubectl -n three-tier rollout status deploy/db deploy/api deploy/frontend --timeout=180s
kubectl -n three-tier run tester --image=busybox:1.36 -- sleep 7200
kubectl -n three-tier wait --for=condition=Ready pod/tester --timeout=60s
kubectl -n three-tier get netpol
```

(`AlreadyExists` on the tester is fine. If the last command lists policies
from earlier experiments, reset with
`kubectl -n three-tier delete netpol --all`.)

Everything so far could talk to everything. NetworkPolicies are namespaced
allow-lists enforced by the CNI (Calico here — the default kubeadm CNI-less
cluster or a bare flannel would silently *not* enforce them). Baseline —
confirm the chain works, then deny everything:

```text
kubectl -n three-tier exec tester -- wget -qO- --timeout=3 http://frontend/api/db/
kubectl apply -f netpol/00-default-deny.yaml
kubectl -n three-tier exec tester -- wget -qO- --timeout=3 http://frontend/
kubectl -n three-tier exec tester -- nslookup db
```

Both now fail — the deny-all selects every pod and denies ingress *and*
egress, DNS included. (Kubelet health probes keep passing: Calico always
allows a node to probe its own pods.) Build back up, least privilege at a
time:

```text
kubectl apply -f netpol/10-allow-dns.yaml
kubectl -n three-tier exec tester -- nslookup db                # DNS is back
kubectl -n three-tier exec tester -- wget -qO- --timeout=3 http://frontend/   # still denied

kubectl apply -f netpol/20-db-policy.yaml
kubectl apply -f netpol/30-api-policy.yaml
kubectl apply -f netpol/40-frontend-policy.yaml
kubectl -n three-tier exec tester -- wget -qO- --timeout=3 http://frontend/api/db/
```

**Still denied!** Every tier is now allowed to talk to the next — but the
*tester* pod is in this namespace too, and the default-deny is blocking its
egress. Frontend being willing to accept is not enough; the sender must also
be allowed to send. This bites real clusters constantly (debug pods,
monitoring agents, migration Jobs). Apply the last policy, which grants the
tester egress to the app's edge only:

```text
kubectl apply -f netpol/50-tester-policy.yaml
kubectl -n three-tier exec tester -- wget -qO- --timeout=3 http://frontend/api/db/
```

Now the full chain works again — and *only* the chain:

```text
kubectl -n three-tier exec tester -- wget -qO- --timeout=3 http://api/   # DENIED: tester is not frontend
kubectl -n three-tier exec tester -- wget -qO- --timeout=3 http://db/    # DENIED: tester is not api
```

What the five policies teach (read them, they are commented):

- Policies are **additive allow-lists**; there is no "deny" rule. Once any
  policy selects a pod for a direction, that direction is default-deny except
  for what is allowed.
- A connection needs **egress on the sender and ingress on the receiver**.
- Ports in policies are **pod ports** (9000/8080), not Service ports (80) —
  enforcement happens after kube-proxy's DNAT.
- An ingress rule with no `from` (frontend's) allows any source — that is
  what keeps the NodePort/Ingress/Gateway paths working at the edge.
- Don't forget **DNS egress** (the classic "my policies broke everything"
  bug).

### Under the hood: what a denial looks like **[on a node]**

Calico compiles your policies into the same kernel framework that
kube-proxy uses — `KUBE-*` chains NAT, `cali-*` chains filter:

```text
sudo nft list ruleset | grep -o "chain cali-[a-zA-Z0-9-]*" | sort -u | head -6
sudo nft list ruleset | grep -c "chain cali-"
```

Dozens of chains (46 on this lab's worker): dispatch chains like
`cali-FORWARD`, plus per-pod chains — `cali-tw-*` ("to workload": that
pod's ingress policy) and `cali-fw-*` ("from workload": its egress) — where
your rules actually live.

Now the signature of a drop. With `00-default-deny.yaml` still applied,
curl the frontend ClusterIP **from the master** (IP from
`kubectl -n three-tier get svc frontend`) and look up the flow:

```text
curl -s --max-time 2 http://<frontend-cluster-ip>/
sudo conntrack -L -p tcp --dport 80 | grep <frontend-cluster-ip>
```
```text
tcp  6 117 SYN_SENT src=10.0.1.10 dst=10.100.18.171 sport=36520 dport=80 [UNREPLIED] src=192.168.36.5 dst=192.168.194.128 sport=8080 ... packets=0 bytes=0
```

Read it like a crime scene. The DNAT **happened** — the reply tuple is
filled in and points at a real frontend pod, so kube-proxy did its job.
But the reply counters sit at `packets=0 bytes=0` and the flow is stuck in
`SYN_SENT [UNREPLIED]`: the SYN crossed to the worker and Calico's
`cali-tw-*` chain dropped it at the pod's front door. Nothing refused it;
it silently vanished — which is why policy problems always feel like
*timeouts*, never errors.

(Why curl from the *master*? Calico exempts a node's traffic to its own
local pods so kubelet probes keep working — from the worker, where the
frontend pods live, this curl would be allowed.)

**When you'll reach for this:** telling "a policy ate my traffic"
(`SYN_SENT [UNREPLIED]`) apart from "the app is down" (connection refused,
or `ESTABLISHED` with no HTTP answer). During an incident,
`sudo conntrack -L | grep -c UNREPLIED` spiking is the smoking gun, and
`sudo conntrack -E` streams flow events live while you reproduce.

## Module 9 — Cleanup

```text
kubectl delete -f gateway/
kubectl delete -f https://github.com/envoyproxy/gateway/releases/download/v1.4.2/install.yaml
kubectl delete -f https://raw.githubusercontent.com/kubernetes/ingress-nginx/controller-v1.13.3/deploy/static/provider/baremetal/deploy.yaml
kubectl delete namespace three-tier
```

(Deleting the namespace removes the app, Services, Ingress, policies, and the
tester pod in one go. Ignore "not found" noise if you skipped a module. The
Envoy Gateway delete can take a minute — it tears down CRDs.)

---

## Recap tables

**Service types** (each builds on the previous):

| Type | Reachable from | Adds | Typical use |
|---|---|---|---|
| `ClusterIP` | inside the cluster | stable VIP + DNS name | tier-to-tier traffic |
| `NodePort` | anyone who can reach a node | same port on every node (30000-32767) | dev/labs, or the layer under an external LB |
| `LoadBalancer` | the internet (via cloud LB) | provisioned external LB per Service | production L4 entry |
| Headless (`clusterIP: None`) | inside | DNS → pod IPs, no VIP | StatefulSets, client-side LB |
| `ExternalName` | inside | CNAME to an external DNS name | aliasing external deps |

**Ingress vs Gateway API:**

| | Ingress | Gateway API |
|---|---|---|
| Scope | HTTP(S) only | HTTP, gRPC, TLS, TCP/UDP (per-route-kind) |
| Extensibility | controller annotations | typed fields, policy attachments |
| Roles | one resource, one owner | class / gateway / route split by persona |
| Status | frozen (GA, maintenance) | actively evolving standard |
| Today | ubiquitous, battle-tested | the successor; new work goes here |

## Troubleshooting

- **Pods `Pending`**: `kubectl -n three-tier describe pod <p>` — usually
  resources on the two t3.mediums; scale something down.
- **`wget: download timed out`** where it should work: check a NetworkPolicy
  isn't still applied (`kubectl -n three-tier get netpol`), endpoints exist
  (`kubectl -n three-tier get endpointslices`), and the pod is Ready.
- **Ingress apply rejected** right after installing the controller: the
  admission webhook wasn't up yet — rerun the `kubectl wait`, then apply.
- **Gateway stuck `PROGRAMMED: False (AddressNotAssigned)`**: the proxy
  Service has no address — make sure `gateway/proxy-config.yaml` was applied
  with the rest of `gateway/` (Module 7). For other reasons:
  `kubectl -n envoy-gateway-system logs deploy/envoy-gateway | tail`.
- **NodePort curl fails from your laptop but works on the node**: that's the
  security group doing its job (Module 5).
- **DNS fails from a brand-new pod** while old ones resolve fine: you applied
  the deny-all but not `10-allow-dns.yaml`.
- **DNS flaky on one node after CoreDNS pods were replaced**: stale UDP
  conntrack entries — `sudo conntrack -D -p udp --dport 53` on that node
  (Module 3).
- **`nft` prints `managed by iptables-nft` warnings or opaque `xt match`
  rules**: normal on Ubuntu (Module 2) — read the decoded rules with
  `sudo iptables-save -t nat`.

## Where to go next

- Add `spec.tls` to the Ingress with a self-signed cert and terminate HTTPS.
- Convert the frontend policy to only accept traffic from the ingress
  controller's namespace (`namespaceSelector`) instead of any source.
- Try `sessionAffinity: ClientIP` on the frontend Service and watch repeated
  curls stick to one pod.
- Compare `externalTrafficPolicy: Local` vs `Cluster` on the NodePort
  Service and observe source-IP preservation.
- Scale db to 2 and point the api tier at `db-headless` to see client-side
  behavior without a VIP.
