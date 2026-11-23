# Networking track: Kubernetes networking on the webshop

> **Step 6 of 8: Networking on the webshop** · Run from `AWS/kubeadm/k8/helm/webshop/` · Needs: the webshop (step 4); a bigger worker for modules 6 and 7 (Capacity, below)

The networking lessons in [`AWS/kubeadm/k8/`](../../../README.md) teach pod
networking, Services, CoreDNS, NodePort, Ingress, the Gateway API and
NetworkPolicy on a three-tier nginx app whose only job is to say which pod
answered. This track runs the same modules on the webshop instead: a real
order flow with six services, a database, a queue, 22 NetworkPolicies already
in force - and the telemetry to see what each networking decision does to
the users. Module 9 then switches the cluster's dataplane from iptables to
Calico's eBPF mode and back, with the SLI on screen.

Every number below was observed on the AWS lab (2026-10-25: kubeadm 1.33,
Calico v3.30.2 on its iptables dataplane, kube-proxy in iptables mode, nodes
on kernel 7.0). Addresses and counts on your cluster will differ; the shapes
will not.

**Conventions.** Commands run from `AWS/kubeadm/k8/helm/webshop/`; `kubectl`
is always written out, with the namespace right after it. **[on the worker]** / **[on the master]**
mark commands for a node shell: from `AWS/kubeadm`, run the matching entry of
`terraform output session_manager_commands` (or `ssh_commands`). The nodes'
private addresses are fixed: master `10.0.1.10`, worker `10.0.1.11`. Grafana
is opened as in the README quick start, step 6; "Explore" below means Grafana's Explore view
with the named data source.

**Copy whole blocks**, top to bottom, in one terminal where `KUBECONFIG`
points at the lab (README quick start, step 1). Every step that
changes the cluster waits until the change has landed (`--wait`,
`kubectl wait ...`, or a retry loop) before the next line uses it. Nothing below
needs you to fill in a value either: a block
that needs a pod name, an address or a port looks it up first and keeps it in
a shell variable (`INV=$(...)`), and the following lines use it as `${INV}`.
Run a block in one terminal so the variables are there for the lines after
them. Those assignment lines are bash/zsh (macOS, Linux, WSL); in PowerShell
write them as `$INV = kubectl ...` - the lines that *use* `${INV}` work unchanged.
Node shells cannot see your terminal's variables, so **[on the worker]**
blocks look their values up on the node, from kube-proxy's rules and the
routing table - the same places the modules teach you to read.

**Modules are self-contained.** Each opens with a **Prerequisites** block that
brings the cluster to the state it needs; run it and start anywhere. The base
prerequisite for all of them is the webshop installed with the README's quick
start.

**Capacity.** Modules 6 and 7 add an Ingress controller and Envoy Gateway to
the worker. On a t3.medium worker that leaves little headroom (README section
9, "A bigger worker"); `worker_instance_type = "t3.xlarge"` fixes that.

---

**Contents**

1. [Pod networking: every pod gets a real IP](#module-1---pod-networking-every-pod-gets-a-real-ip)
2. [ClusterIP Services: one virtual IP, many endpoints](#module-2---clusterip-services-one-virtual-ip-many-endpoints)
3. [CoreDNS: names, search lists and what ndots costs](#module-3---coredns-names-search-lists-and-what-ndots-costs)
4. [Walk the chain: one order, every hop](#module-4---walk-the-chain-one-order-every-hop)
5. [NodePort: the way in from outside](#module-5---nodeport-the-way-in-from-outside)
6. [Ingress: HTTP routing behind one entry point](#module-6---ingress-http-routing-behind-one-entry-point)
7. [Gateway API: the successor](#module-7---gateway-api-the-successor)
8. [NetworkPolicy: the default-deny you have been working under](#module-8---networkpolicy-the-default-deny-you-have-been-working-under)
9. [Switch the dataplane to eBPF and back](#module-9---switch-the-dataplane-to-ebpf-and-back)
10. [Cleanup](#module-10---cleanup)

---

## Module 1 - Pod networking: every pod gets a real IP

**Prerequisites:** the webshop installed (README quick start).

The webshop namespace is **default-deny**: the chart's NetworkPolicies allow
exactly the flows the shop needs and nothing else (Module 8 takes them
apart). A pod nobody planned for can resolve names, but reach nothing. So the
test pod comes with its own allow-lists -
[`networking/tester.yaml`](../networking/tester.yaml) holds a busybox pod and
two NetworkPolicies that let it talk to storefront, checkout, inventory and
payment:

```text
kubectl apply -f networking/tester.yaml
kubectl -n webshop wait --for=condition=Ready pod/tester --timeout=90s
kubectl -n webshop get pods -o wide
```

Every pod has its own address from the **pod CIDR** (`192.168.0.0/16`, handed
to Calico by kubeadm). Calico gives each node a block of it - ask it which:

```text
kubectl get ipamblocks.crd.projectcalico.org -o custom-columns=CIDR:.spec.cidr,NODE:.spec.affinity
```

```text
CIDR                NODE
192.168.171.64/26   host:worker
192.168.219.64/26   host:master
```

The webshop runs entirely on the worker (the master keeps its NoSchedule
taint), so its pods share `192.168.171.x`. CoreDNS runs on the master
(`kubectl -n kube-system get pods -o wide -l k8s-app=kube-dns`): its
`192.168.219.x` addresses are the other block, and every DNS lookup in
Module 3 crosses between the nodes in Calico's VXLAN tunnel.

Reach a pod directly by its IP - no Service, no DNS:

```text
kubectl -n webshop get pods -l app.kubernetes.io/component=inventory -o wide
INV=$(kubectl -n webshop get pods -l app.kubernetes.io/component=inventory -o jsonpath='{.items[0].status.podIP}')
echo "inventory pod IP: ${INV}"
kubectl -n webshop exec tester -- wget -qO- --timeout=3 http://${INV}:8080/livez; echo
kubectl -n webshop exec tester -- wget -qO- --timeout=3 http://${INV}:8080/products; echo
```

The IP the variable picked up (the same one `-o wide` lists), then
`{"status":"alive"}`, then the whole catalog as one long line of JSON. (The
`; echo` only ends each answer with a newline - the service sends none, so
without it the next output would start on the same line.)

Now try a pod the tester was *not* given access to, PostgreSQL:

```text
PG=$(kubectl -n webshop get pod webshop-postgresql-0 -o jsonpath='{.status.podIP}')
echo "postgresql pod IP: ${PG}"
kubectl -n webshop exec tester -- wget -qO- --timeout=3 http://${PG}:5432/
```

```text
wget: download timed out
command terminated with exit code 1
```

after exactly 3 seconds - **this is the expected result**: the second line is
just kubectl passing on wget's failure. Not "connection refused" - nothing
refused anything; a NetworkPolicy dropped the packet and the client waited.
Remember the shape: policy problems look like timeouts.

Pod IPs do not last. Note the inventory pod's IP, replace the pod, wait for
its replacement to be ready, and compare:

```text
BEFORE=$(kubectl -n webshop get pods -l app.kubernetes.io/component=inventory -o jsonpath='{range .items[*]}{.metadata.name} {.status.podIP}   {end}')
kubectl -n webshop delete pod -l app.kubernetes.io/component=inventory
kubectl -n webshop wait --for=condition=Ready pod -l app.kubernetes.io/component=inventory --timeout=120s
AFTER=$(kubectl -n webshop get pods -l app.kubernetes.io/component=inventory -o jsonpath='{range .items[*]}{.metadata.name} {.status.podIP}   {end}')
echo "before: ${BEFORE}"
echo "after:  ${AFTER}"
```

```text
pod "webshop-inventory-7c6f4bcb78-dwc6b" deleted
pod/webshop-inventory-7c6f4bcb78-hv8cj condition met
before: webshop-inventory-7c6f4bcb78-dwc6b 192.168.171.115
after:  webshop-inventory-7c6f4bcb78-hv8cj 192.168.171.116
```

A different pod (the name's last part) with a different address. The two
addresses are often neighbours - Calico hands out the next free one in the
worker's block - so compare the last number. `delete` returns once the old pod
is gone; by then the Deployment has already started a replacement, and `wait`
holds until it is `Ready` (listed straight after the delete it would still
show `0/1`, starting up). Same Deployment, same labels, new pod, new address. Nothing in the webshop knows a pod IP; everything
goes through Services. (If the HPA is running two inventory pods at that
moment, both are replaced and both addresses are listed.)

### Where it comes from: the pod's address and its `/etc/hosts`

Three places decide a pod's address, and only one of them is the one you
might guess.

```text
echo "--- 1. kubeadm: the cluster's networks"; kubectl -n kube-system get configmap kubeadm-config -o jsonpath='{.data.ClusterConfiguration}' | grep -A3 '^networking:'
echo "--- 2. controller-manager: the podCIDR it gave each node"; kubectl get nodes -o custom-columns=NODE:.metadata.name,PODCIDR:.spec.podCIDR
echo "--- 3. Calico: its own IP pool"; kubectl get ippools.crd.projectcalico.org -o custom-columns=POOL:.metadata.name,CIDR:.spec.cidr,BLOCK-SIZE:.spec.blockSize,VXLAN:.spec.vxlanMode
echo "--- 4. Calico: the blocks it actually handed out"; kubectl get ipamblocks.crd.projectcalico.org -o custom-columns=BLOCK:.spec.cidr,NODE:.spec.affinity
```

```text
--- 1. kubeadm: the cluster's networks
networking:
  dnsDomain: cluster.local
  podSubnet: 192.168.0.0/16
  serviceSubnet: 10.96.0.0/12
--- 2. controller-manager: the podCIDR it gave each node
NODE     PODCIDR
master   192.168.0.0/24
worker   192.168.1.0/24
--- 3. Calico: its own IP pool
POOL                  CIDR             BLOCK-SIZE   VXLAN
default-ipv4-ippool   192.168.0.0/16   26           Always
--- 4. Calico: the blocks it actually handed out
BLOCK               NODE
192.168.171.64/26   host:worker
192.168.219.64/26   host:master
```

(`grep -A3 '^networking:'` keeps only the networking part of kubeadm's whole
`ClusterConfiguration`; drop it to see the rest - API server certificate
names, Kubernetes version, etcd location.)

- **kubeadm** recorded the pod network, `podSubnet: 192.168.0.0/16`, in the
  `kubeadm-config` ConfigMap when the cluster was built, and passed it on to
  the controller-manager (`--cluster-cidr`).
- The **controller-manager** gave each node a slice of it: `spec.podCIDR`,
  `192.168.1.0/24` for the worker.
- **Calico** ignores that slice. Its IPAM hands out addresses from its own
  pool (same `/16`, carved into `/26` blocks of 64 addresses, one or more per
  node) - which is why the worker's pods are `192.168.171.x`, not
  `192.168.1.x`. The node's `podCIDR` is only used by plugins that ask for it;
  Calico's `calico-ipam` does not. When an IP does not match the node's
  `podCIDR`, this is why.

`/etc/hosts` is not the image's file either - the kubelet writes it for
every pod:

```text
kubectl -n webshop exec tester -- cat /etc/hosts
```

```text
# Kubernetes-managed hosts file.
127.0.0.1	localhost
...
192.168.171.85	tester
```

The pod's own IP and name (its hostname), plus the localhost entries.
Nothing about Services - those are DNS's job (Module 3). A pod spec's
`hostAliases` adds lines here; editing the file inside a container does not
survive a restart.

## Module 2 - ClusterIP Services: one virtual IP, many endpoints

**Prerequisites** - the webshop with the track's overlay (two storefront
replicas, plus the settings Modules 5-8 use), and the tester:

```text
helm upgrade webshop ./chart -n webshop -f values/values-aws.yaml -f networking/values-networking.yaml --wait --timeout 10m
kubectl apply -f networking/tester.yaml
kubectl -n webshop wait --for=condition=Ready pod/tester --timeout=90s
```

[`networking/values-networking.yaml`](../networking/values-networking.yaml)
is layered on top of the lab profile: later files win key by key, so it only
needs the settings it changes. `helm get values webshop -n webshop` shows the
merged result. (It also turns the storefront into a NodePort Service -
Module 5. A NodePort Service *is* a ClusterIP Service plus a port on every
node, so nothing here changes.)

```text
kubectl -n webshop get svc webshop-checkout
kubectl -n webshop describe svc webshop-checkout
kubectl -n webshop get endpointslices -l kubernetes.io/service-name=webshop-checkout
```

```text
IP:                10.108.193.81
Port:              http  80/TCP
TargetPort:        http/TCP
Endpoints:         192.168.171.72:8080,192.168.171.89:8080
```

- **ClusterIP** `10.108.193.81` belongs to no pod and no interface; on every
  node, kube-proxy rewrites (DNATs) connections to it into connections to one
  endpoint.
- **TargetPort `http`** is a *port name*, not a number - and not a label. The
  selector picks the pods by their labels; `targetPort: http` then picks, in
  each of those pods, the container port declared as `name: http`
  (`containerPort: 8080`). The EndpointSlice controller does that lookup and
  writes the resulting number, 8080, into the EndpointSlice - which is why
  the listing shows `8080`, not `http`. Details in the next section.
- **Endpoints** come from the EndpointSlice the control plane keeps for the
  Service: every Ready pod matching the selector.

### Named ports: how `targetPort` finds the container port

A Service answers two different questions, with two different mechanisms:

| Question | Service field | Matched against | How |
|---|---|---|---|
| *Which pods?* | `spec.selector` | the pods' `metadata.labels` | label match |
| *Which port in each of them?* | `spec.ports[].targetPort` | the pods' `spec.containers[].ports[]` | a number is used as is; a **string is looked up as a container port name**, separately in every selected pod |

See the three pieces side by side:

```text
echo "--- 1. the Service: which pods (selector) and which port in them (targetPort)"; kubectl -n webshop get svc webshop-checkout -o jsonpath='selector:   {.spec.selector}{"\n"}port:       {.spec.ports[0].name} {.spec.ports[0].port} -> targetPort {.spec.ports[0].targetPort}{"\n"}'
echo "--- 2. each selected pod: its container ports, by name"; kubectl -n webshop get pods -l app.kubernetes.io/component=checkout -o jsonpath='{range .items[*]}{.metadata.name}: {range .spec.containers[*].ports[*]}{.name}={.containerPort} {end}{"\n"}{end}'
echo "--- 3. the EndpointSlice: the name, resolved to a number"; kubectl -n webshop get endpointslices -l kubernetes.io/service-name=webshop-checkout -o jsonpath='{range .items[*]}{.metadata.name}: ports={.ports} endpoints={range .endpoints[*]}{.addresses[0]} {end}{"\n"}{end}'
```

```text
--- 1. the Service: which pods (selector) and which port in them (targetPort)
selector:   {"app.kubernetes.io/component":"checkout","app.kubernetes.io/instance":"webshop","app.kubernetes.io/name":"webshop"}
port:       http 80 -> targetPort http
--- 2. each selected pod: its container ports, by name
webshop-checkout-54777c649f-4844c: http=8080
webshop-checkout-54777c649f-vfmrz: http=8080
--- 3. the EndpointSlice: the name, resolved to a number
webshop-checkout-x29wq: ports=[{"name":"http","port":8080,"protocol":"TCP"}] endpoints=192.168.171.104 192.168.171.112
```

Neither the Service nor kube-proxy ever resolves the name. The
**EndpointSlice controller** does: for each pod the selector matches, it
finds the container port called `http`, and writes that pod's address *and
the port's number* into the EndpointSlice. kube-proxy reads only the slice -
Module 2's `DNAT --to-destination 192.168.171.66:8080` is that number - so by
the time a packet is rewritten, the name is long gone.

What follows from it:

- **The pods can move without the Service.** Change the container's port to
  9090 but keep `name: http`, and the new pods' endpoints say 9090 while the
  Service still says `port: 80, targetPort: http`. The Kubernetes docs: you
  can "change the port numbers that Pods expose in the next version of your
  backend software, without breaking clients."
- **Pods behind one Service may even disagree.** It "works even if there is a
  mixture of Pods in the Service using a single configured name ... available
  via different port numbers" - during a rollout from 8080 to 9090, say.
  EndpointSlices "group network endpoints together by unique combinations of
  IP family, protocol, port number, and Service name", so the controller
  simply keeps one slice per number. A numeric `targetPort` cannot do this:
  it is the same number for every pod. (Seen on the lab: one Service, pods
  declaring `web=8080` and `web=9090` - two EndpointSlices, and kube-proxy
  splitting the ClusterIP 50/50 between `:8080` and `:9090`.)
- **A selected pod without a port of that name** is still listed - in a
  slice of its own with *no ports at all* - but kube-proxy needs a number to
  DNAT to, so it writes no rule for that pod and the pod receives none of the
  Service's traffic. `kubectl describe svc` shows it as a bare address without
  `:port`. A quiet failure when someone renames a port in one place only.
- **The name has rules.** A container port name must be an `IANA_SVC_NAME`
  (RFC 6335): at most 15 characters, lowercase letters, digits and `-`, at
  least one letter, no leading, trailing or doubled `-`. It must be **unique
  within the pod** - across all its containers, not just one.
- **Two different things are called "port name".** The *Service's* port name
  (`spec.ports[].name`, a DNS label, unique within the Service, required once
  a Service has more than one port) names the Service's own port: it is the
  name in the EndpointSlice's `ports` and in SRV records (`_http._tcp...`,
  Module 3). The *container's* port name is what `targetPort` points at. The
  webshop calls both `http`, which hides that they are independent.
- **Other things accept the name too.** Probes (`httpGet: {port: http}` - the
  chart's liveness and readiness probes do exactly that) and NetworkPolicy
  ports ("a numerical or named port on a pod"); in a policy the name is
  resolved per pod as well. The chart's policies use the number 8080 instead,
  so they read the same as the DNAT rules they are checked against.
- **Defaults and exceptions.** Leave `targetPort` out and it equals `port`.
  For a headless Service (`clusterIP: None`) it "is ignored ... and should be
  omitted or set equal to the 'port' field" - clients connect to the pods
  directly, to whatever port they ask for.
- **Only with a selector.** For a Service without one, nobody resolves names:
  you write the EndpointSlice yourself, numbers included.

References:
[Services: port definitions](https://kubernetes.io/docs/concepts/services-networking/service/),
[Service API: `targetPort`, port `name`](https://kubernetes.io/docs/reference/kubernetes-api/service-resources/service-v1/),
[Pod API: container port `name`](https://kubernetes.io/docs/reference/kubernetes-api/workload-resources/pod-v1/),
[EndpointSlices](https://kubernetes.io/docs/concepts/services-networking/endpoint-slices/),
[NetworkPolicy API: `ports`](https://kubernetes.io/docs/reference/kubernetes-api/policy-resources/network-policy-v1/).

**Readiness decides membership.** Replace one checkout pod and print the
slice's endpoint `conditions` every 3 seconds for half a minute while it
happens:

```text
CO=$(kubectl -n webshop get pods -l app.kubernetes.io/component=checkout -o jsonpath='{.items[0].metadata.name}')
kubectl -n webshop delete pod ${CO} --wait=false
for i in 1 2 3 4 5 6 7 8 9 10; do kubectl -n webshop get endpointslices -l kubernetes.io/service-name=webshop-checkout -o jsonpath='{range .items[*].endpoints[*]}{.addresses[0]} ready={.conditions.ready} serving={.conditions.serving} terminating={.conditions.terminating}   {end}{"\n"}'; sleep 3; done
```

Observed, three seconds apart:

| t | old pod | new pod |
|---|---|---|
| +3 s | ready=false, serving=true, terminating=true | - |
| +6 s | same | listed, ready=false |
| +9 s | gone | ready=false |
| +12 s | - | ready=true |

The old pod leaves the rotation the moment it starts terminating, but keeps
serving for the 5 s its `preStop` hook waits, so connections already on their
way still land. The new pod is listed at once and receives traffic only when
its readiness probe passes.

**Load balancing.** Every webshop response carries `X-Served-By`, the name of
the pod that answered. Ten requests, one line each:

```text
for i in 1 2 3 4 5 6 7 8 9 10; do kubectl -n webshop exec tester -- wget -S -qO /dev/null http://webshop-storefront/livez 2>&1 | grep -i x-served-by; done
```

Ten requests split 2 and 8 between the two storefront pods in the lab run: a
random pick per connection, not round robin.

**Connections, not requests.** kube-proxy picks a pod when a TCP
*connection* is opened - not for each HTTP *request*. Every request sent over
that connection afterwards goes to the same pod. So how evenly a Service
spreads load depends on how the client uses connections:

- **A new connection for every request** - the webshop's default
  (`app.httpKeepalive: false`): every request is a fresh random pick, and the
  load spreads evenly.
- **Connections kept open and reused** (HTTP keep-alive): the client opens a
  few connections and sends many requests over each. A few random picks, made
  once, then decide where *all* the requests go - and the split can be very
  uneven.

See it with checkout, which the storefront calls. Open **Webshop / Service
health -> Requests by pod** in Grafana, service `checkout` - requests per
second arriving at each checkout pod (in Explore, Prometheus:
`sum by (k8s_pod_name) (rate(http_server_request_duration_seconds_count{service_name="checkout"}[2m]))`).
Note the two lines, then let the webshop's services reuse connections:

```text
helm upgrade webshop ./chart -n webshop -f values/values-aws.yaml -f networking/values-networking.yaml --set app.httpKeepalive=true --wait --timeout 10m
```

The upgrade restarts the pods. Once it returns, wait three minutes - the
panel averages over two - and compare. In the lab run:

| Requests per second at each checkout pod | pod A | pod B |
|---|---|---|
| a new connection per request (default) | 0.78 | 0.81 - even |
| keep-alive | 1.40 | 0.27 - one pod gets 84% |

Same traffic, same two pods, same Service; only the clients' connection
habit changed. Switch back to the default:

```text
helm upgrade webshop ./chart -n webshop -f values/values-aws.yaml -f networking/values-networking.yaml --wait --timeout 10m
```

**Why it matters in production:** anything with long-lived connections -
HTTP keep-alive, HTTP/2 and gRPC, database connection pools - defeats
kube-proxy's balancing, and adding replicas does not help: the existing
connections stay pinned to the old pods. The usual fixes are balancing per
request in the client (a headless Service, so the client sees every pod -
Module 3), or an L7 proxy or service mesh in between. The webshop avoids the
problem the simple way, with a new connection per call - and pays for it in
DNS lookups and one conntrack entry per request (below, and Module 3).

### Where it comes from: the ClusterIP and the EndpointSlice

```text
echo "--- the range ClusterIPs come from"; kubectl get servicecidrs
CIP=$(kubectl -n webshop get svc webshop-checkout -o jsonpath='{.spec.clusterIP}')
echo "--- who owns checkout's ClusterIP"; kubectl get ipaddress ${CIP} -o jsonpath='{.metadata.name} belongs to {.spec.parentRef.resource}/{.spec.parentRef.namespace}/{.spec.parentRef.name}{"\n"}'
echo "--- two fixed addresses"; kubectl get svc kubernetes -o jsonpath='kubernetes (the API server): {.spec.clusterIP}{"\n"}'; kubectl -n kube-system get svc kube-dns -o jsonpath='kube-dns (CoreDNS): {.spec.clusterIP}{"\n"}'
```

```text
--- the range ClusterIPs come from
NAME         CIDRS          AGE
kubernetes   10.96.0.0/12   3h39m
--- who owns checkout's ClusterIP
10.107.168.248 belongs to services/webshop/webshop-checkout
--- two fixed addresses
kubernetes (the API server): 10.96.0.1
kube-dns (CoreDNS): 10.96.0.10
```

kubeadm's `serviceSubnet` (the same ConfigMap as in Module 1) became the API
server's `--service-cluster-ip-range` and, on this cluster's Kubernetes
version, also a `ServiceCIDR` object. Creating a Service makes the API server take a free
address from that range and record it as an `IPAddress` object pointing back
at the Service - so two Services can never get the same ClusterIP. The first
address, `10.96.0.1`, always goes to the API server's own `kubernetes`
Service; kubeadm gives CoreDNS's `kube-dns` the tenth, `10.96.0.10`.
**[on the master]** the flag itself, in the API server's static pod
manifest:

```text
sudo grep service-cluster-ip-range /etc/kubernetes/manifests/kube-apiserver.yaml
```

The endpoints are someone else's job:

```text
kubectl -n webshop get endpointslices -l kubernetes.io/service-name=webshop-checkout -o jsonpath='managed by: {.items[0].metadata.labels.endpointslice\.kubernetes\.io/managed-by}{"\n"}owner: {.items[0].metadata.ownerReferences[0].kind}/{.items[0].metadata.ownerReferences[0].name}{"\n"}'
```

```text
managed by: endpointslice-controller.k8s.io
owner: Service/webshop-checkout
```

The **EndpointSlice controller** - one of the controllers inside
kube-controller-manager - watches every Service and every pod. For each
Service it keeps slices listing the pods that match the selector, with the
`ready`/`serving`/`terminating` conditions you just watched change. Nobody
writes them by hand, and the Service owns them: delete the Service and its
slices go with it. kube-proxy (and Calico in Module 9) only *read* them.

### Under the hood: the NAT rules and their flows **[on the worker]**

> New to NAT rules and conntrack? [nat-and-conntrack.md](nat-and-conntrack.md) explains how to read them from first principles - hooks, chains, every field of a rule and of a conntrack entry - with this lab's real output.

kube-proxy runs in iptables mode; on Ubuntu the iptables tooling writes nft
rules underneath (`sudo nft list ruleset` is the complete view;
`iptables-save` decodes kube-proxy's rules more readably):

```text
sudo iptables-save -t nat | grep "webshop/webshop-checkout"
```

```text
-A KUBE-SERVICES -d 10.108.193.81/32 -p tcp ... --dport 80 -j KUBE-SVC-3LRHZRJFPN53GT2Z
-A KUBE-SVC-3LRHZRJFPN53GT2Z ! -s 192.168.0.0/16 -d 10.108.193.81/32 ... -j KUBE-MARK-MASQ
-A KUBE-SVC-3LRHZRJFPN53GT2Z ... -m statistic --mode random --probability 0.50000000000 -j KUBE-SEP-KDB5QMFN6ZJOHPZF
-A KUBE-SVC-3LRHZRJFPN53GT2Z ... -j KUBE-SEP-5YKMFWNJOG2ZY2Q5
-A KUBE-SEP-KDB5QMFN6ZJOHPZF -p tcp ... -j DNAT --to-destination 192.168.171.66:8080
```

`KUBE-SERVICES` matches the ClusterIP, `KUBE-SVC-*` rolls the dice (one half
to the first endpoint, the rest falls through to the second), `KUBE-SEP-*`
rewrites the destination. Traffic from outside the pod CIDR is marked for
masquerade first. Every connection then lives in conntrack - take the
ClusterIP from the `KUBE-SERVICES` rule you just read:

```text
CO=$(sudo iptables-save -t nat | awk '/^-A KUBE-SERVICES/ && /"webshop\/webshop-checkout:http cluster IP"/ {sub("/32", "", $4); print $4; exit}')
sudo conntrack -L -d ${CO}
```

```text
tcp 6 114 TIME_WAIT src=192.168.171.106 dst=10.108.193.81 sport=41130 dport=80 ... src=192.168.171.66 dst=192.168.171.106 sport=8080 dport=41130 ... [ASSURED]
```

Original tuple: a storefront pod to the ClusterIP. Reply tuple: from a checkout
pod's real address. In the lab run there were **229** such entries for this
one Service - one per request, the price of a connection per call.

## Module 3 - CoreDNS: names, search lists and what ndots costs

**Prerequisites** - as Module 2:

```text
helm upgrade webshop ./chart -n webshop -f values/values-aws.yaml -f networking/values-networking.yaml --wait --timeout 10m
kubectl apply -f networking/tester.yaml
kubectl -n webshop wait --for=condition=Ready pod/tester --timeout=90s
```

Every pod's `/etc/resolv.conf` points at the kube-dns Service. Compare the
tester (cluster defaults) with a webshop pod:

```text
kubectl -n webshop exec tester -- cat /etc/resolv.conf
kubectl -n webshop exec deploy/webshop-checkout -- cat /etc/resolv.conf
```

```text
search webshop.svc.cluster.local svc.cluster.local cluster.local ec2.internal
nameserver 10.96.0.10
options ndots:5        <- tester
options ndots:2        <- webshop pod (the chart's dnsConfig)
```

A name with fewer dots than `ndots` is tried with each search suffix *first*,
and only then as written. Resolve a Service by its short name:

```text
kubectl -n webshop exec tester -- nslookup webshop-checkout
```

```text
Name:    webshop-checkout.webshop.svc.cluster.local
Address: 10.108.193.81
** server can't find webshop-checkout.svc.cluster.local: NXDOMAIN
** server can't find webshop-checkout.cluster.local: NXDOMAIN
```

busybox's `nslookup` prints every suffix it tried; the first answer is the
real one. (It applies the search list only to names *without* dots - for
anything dotted give it the full name. The resolvers inside real programs
follow `ndots`.)

**Headless Services** have no ClusterIP; DNS returns the pods themselves.
RabbitMQ's StatefulSet uses one, which also gives each member a stable name:

```text
kubectl -n webshop exec tester -- nslookup webshop-rabbitmq-headless
kubectl -n webshop exec tester -- nslookup webshop-rabbitmq-0.webshop-rabbitmq-headless.webshop.svc.cluster.local
kubectl -n webshop get pod webshop-rabbitmq-0 -o wide
```

Both return the pod's own IP - no virtual IP, no load balancing.

**What ndots costs.** The webshop's service URLs are fully qualified
(`webshop-inventory.webshop.svc.cluster.local`) - four dots. With the
cluster's `ndots:5` that is still "short", so every lookup first tries
`...cluster.local.webshop.svc.cluster.local` and the other suffixes, each an
NXDOMAIN round trip to CoreDNS on the other node. And since the services open
a new connection per call (Module 2), they look the name up per call. Measure
it: in Explore (Prometheus), checkout's view of inventory against
inventory's own:

```promql
histogram_quantile(0.5, sum by (le) (rate(http_client_request_duration_seconds_bucket{service_name="checkout", server_address=~".*inventory.*"}[2m])))
histogram_quantile(0.5, sum by (le) (rate(http_server_request_duration_seconds_bucket{service_name="inventory"}[2m])))
```

Then drop the chart's `dnsConfig` (back to the cluster default) and measure
again three minutes after the upgrade returns:

```text
helm upgrade webshop ./chart -n webshop -f values/values-aws.yaml -f networking/values-networking.yaml --set dnsConfig=null --wait --timeout 10m
```

| | client p50 (checkout) | server p50 (inventory) | gap |
|---|---|---|---|
| `ndots:2` (chart default) | 10.4 ms | 7.6 ms | 2.8 ms |
| `ndots:5` (cluster default) | 17.6 ms | 7.7 ms | 9.9 ms |

Inventory did not get slower; seven milliseconds of DNS appeared in front of
every call. In a trace it is the gap between checkout's CLIENT span starting
and inventory's SERVER span starting. Restore the default with the
Prerequisites command.

### Where it comes from: from kubeadm to CoreDNS

Every line of the pod's `/etc/resolv.conf`, and every answer, can be traced
to a piece of configuration. In order:

**1. kubeadm** chose the cluster's DNS domain and its Service range - the
`kubeadm-config` ConfigMap from Module 1: `dnsDomain: cluster.local`,
`serviceSubnet: 10.96.0.0/12`.

**2. The kubelet** was told where cluster DNS is. kubeadm wrote that into the
`kubelet-config` ConfigMap, and every node has a copy as
`/var/lib/kubelet/config.yaml`:

```text
kubectl -n kube-system get configmap kubelet-config -o jsonpath='{.data.kubelet}' | grep -A1 -E '^(clusterDNS|clusterDomain|resolvConf):'
```

```text
clusterDNS:
- 10.96.0.10
clusterDomain: cluster.local
...
resolvConf: /run/systemd/resolve/resolv.conf
```

(The `grep` keeps the three DNS settings of the kubelet's whole configuration;
`...` is the line after `clusterDomain` that `-A1` also prints.)

**3. The kubelet writes each pod's `resolv.conf`** from those values (for
pods with the default `dnsPolicy: ClusterFirst`, like the tester):

- `nameserver 10.96.0.10` - its `clusterDNS`;
- `search webshop.svc.cluster.local svc.cluster.local cluster.local` - the
  pod's namespace and `clusterDomain`, then the node's own search domain
  (`ec2.internal`), taken from the file `resolvConf` names;
- `options ndots:5` - Kubernetes' default, unless the pod spec has a
  `dnsConfig`. The webshop's chart sets `ndots: 2` there (`dnsConfig` in
  `chart/values.yaml`); the tester has none.

**[on the worker]** the node's side of it:

```text
sudo grep -A1 -E "^clusterDNS|^clusterDomain|^resolvConf" /var/lib/kubelet/config.yaml
grep -v "^#" /run/systemd/resolve/resolv.conf
```

```text
nameserver 10.0.0.2
search ec2.internal
```

`10.0.0.2` is the AWS VPC's DNS server (the VPC's base address plus two).

**4. `10.96.0.10` is the `kube-dns` Service**, and like any Service its
endpoints are pods - CoreDNS's, both on the master:

```text
echo "--- kube-dns's endpoints"; kubectl -n kube-system get endpointslices -l kubernetes.io/service-name=kube-dns
echo "--- the CoreDNS pods behind them"; kubectl -n kube-system get pods -l k8s-app=kube-dns -o custom-columns=POD:.metadata.name,IP:.status.podIP,NODE:.spec.nodeName
```

**5. CoreDNS answers from its Corefile**, a ConfigMap:

```text
kubectl -n kube-system get configmap coredns -o jsonpath='{.data.Corefile}'
```

```text
.:53 {
    ...
    kubernetes cluster.local in-addr.arpa ip6.arpa {
       pods insecure
       fallthrough in-addr.arpa ip6.arpa
       ttl 30
    }
    forward . /etc/resolv.conf {
       max_concurrent 1000
    }
    cache 30 { ... }
    loop
    reload
    loadbalance
}
```

- `kubernetes cluster.local in-addr.arpa ip6.arpa` - CoreDNS answers names in
  the cluster domain itself, from the Services and EndpointSlices it watches,
  and reverse lookups of their addresses. `pods insecure` adds a name for
  every pod IP.
- `forward . /etc/resolv.conf` - everything else goes upstream. CoreDNS's
  own pods run with `dnsPolicy: Default`
  (`kubectl -n kube-system get deployment coredns -o jsonpath='{.spec.template.spec.dnsPolicy}'`),
  so their `/etc/resolv.conf` is the node's: `10.0.0.2`, the VPC resolver. A
  CoreDNS pod using `ClusterFirst` would forward to itself - `loop` exists to
  catch exactly that.
- `reload` - edit the ConfigMap and CoreDNS picks it up within a couple of
  minutes, no restart.

Check each claim from the tester:

```text
echo "--- 1. a name outside the cluster: forwarded upstream"; kubectl -n webshop exec tester -- nslookup aws.amazon.com
TIP=$(kubectl -n webshop get pod tester -o jsonpath='{.status.podIP}')
echo "--- 2. the tester's own pod name ('pods insecure')"; kubectl -n webshop exec tester -- nslookup $(echo ${TIP} | tr . -).webshop.pod.cluster.local
CIP=$(kubectl -n webshop get svc webshop-checkout -o jsonpath='{.spec.clusterIP}')
echo "--- 3. a ClusterIP back to its Service (the reverse zone)"; kubectl -n webshop exec tester -- nslookup ${CIP}
```

```text
Non-authoritative answer:            <- forwarded to the VPC resolver
...
Name:   192-168-171-85.webshop.pod.cluster.local
Address: 192.168.171.85              <- "pods insecure": the tester's own pod name
248.168.107.10.in-addr.arpa  name = webshop-checkout.webshop.svc.cluster.local
                                     <- reverse zone: a ClusterIP back to its Service
```

The external lookup works even under default-deny: the tester only ever
talks to `kube-dns` (allowed by `webshop-allow-dns`); CoreDNS, outside the
namespace, is the one that reaches the internet.

### Making sense of "DNS for Services and Pods"

Kubernetes' own page on the subject,
[DNS for Services and Pods](https://kubernetes.io/docs/concepts/services-networking/dns-pod-service/),
is a list of rules. Each one can be checked on the lab:

```text
echo "--- 1. a short name only searches your own namespace"; kubectl -n webshop exec deploy/webshop-checkout -- python -c "import socket; print('kube-dns ->', socket.gethostbyname('kube-dns'))" 2>/dev/null || echo "kube-dns -> not found (there is no kube-dns Service in webshop)"
kubectl -n webshop exec deploy/webshop-checkout -- python -c "import socket; print('kube-dns.kube-system ->', socket.gethostbyname('kube-dns.kube-system'))"
echo "--- 2. SRV record of a named port, normal Service"; kubectl -n webshop exec tester -- nslookup -type=SRV _http._tcp.webshop-checkout.webshop.svc.cluster.local
echo "--- 3. SRV record of a named port, headless Service"; kubectl -n webshop exec tester -- nslookup -type=SRV _amqp._tcp.webshop-rabbitmq-headless.webshop.svc.cluster.local
CO=$(kubectl -n webshop get pods -l app.kubernetes.io/component=checkout -o jsonpath='{.items[0].status.podIP}')
echo "--- 4. a pod's record under its Service's name"; kubectl -n webshop exec tester -- nslookup $(echo ${CO} | tr . -).webshop-checkout.webshop.svc.cluster.local
echo "--- 5. hostname and subdomain: the StatefulSet pod"; kubectl -n webshop get pod webshop-rabbitmq-0 -o jsonpath='hostname={.spec.hostname} subdomain={.spec.subdomain}{"\n"}'; kubectl -n webshop exec webshop-rabbitmq-0 -c rabbitmq -- hostname; kubectl -n webshop exec webshop-rabbitmq-0 -c rabbitmq -- grep rabbitmq-0 /etc/hosts
echo "--- 6. dnsPolicy, four pods"; kubectl -n webshop get pod tester -o jsonpath='tester:      {.spec.dnsPolicy}{"\n"}'; kubectl -n webshop get deploy webshop-checkout -o jsonpath='checkout:    {.spec.template.spec.dnsPolicy}  dnsConfig={.spec.template.spec.dnsConfig}{"\n"}'; kubectl -n kube-system get deploy coredns -o jsonpath='coredns:     {.spec.template.spec.dnsPolicy}{"\n"}'; kubectl -n calico-system get ds calico-node -o jsonpath='calico-node: {.spec.template.spec.dnsPolicy}  hostNetwork={.spec.template.spec.hostNetwork}{"\n"}'
```

| The page says | On the lab |
|---|---|
| **Namespaces of Services:** a name without a namespace only finds Services in the pod's own namespace; from elsewhere use `<service>.<namespace>` | (1) `kube-dns` is not found from `webshop`; `kube-dns.kube-system` is `10.96.0.10`. (busybox's `nslookup` needs the full `kube-dns.kube-system.svc.cluster.local`, see above.) |
| **A records:** `<service>.<namespace>.svc.<cluster-domain>` is the ClusterIP; for a headless Service, the pods' IPs | Module 3's first lookups: `webshop-checkout` -> its ClusterIP, `webshop-rabbitmq-headless` -> the pod |
| **SRV records** exist for *named* ports: `_<port-name>._<protocol>.<service>.<namespace>.svc.<cluster-domain>`, answering port and target name | (2) `_http._tcp.webshop-checkout...` -> `0 100 80 webshop-checkout.webshop.svc.cluster.local`; (3) for the headless Service the target is the pod's own name, `webshop-rabbitmq-0.webshop-rabbitmq-headless...`, port `5672`. Every port in the namespace is named (`http`, `amqp`, ...; the subcharts' too), so every Service has them |
| **Pod records:** `<dashed-ip>.<namespace>.pod.<cluster-domain>`, and from CoreDNS also `<dashed-ip>.<service>.<namespace>.svc.<cluster-domain>` | the first form in the Corefile section above (`pods insecure`); (4) the second form for a checkout pod. A pod with a hostname behind a headless Service is published under that hostname instead (3) |
| **hostname and subdomain:** a pod with `hostname: foo` and `subdomain: bar`, plus a headless Service named `bar`, gets the FQDN `foo.bar.<namespace>.svc.<cluster-domain>` | (5) a StatefulSet sets both for each member: `webshop-rabbitmq-0` / `webshop-rabbitmq-headless`. The kubelet writes the FQDN into its `/etc/hosts`, yet `hostname` prints the short name - **`setHostnameAsFQDN: true`** would change that (and fail pods whose FQDN is over 64 characters) |
| **dnsPolicy:** `ClusterFirst` (the default when unset), `Default` (the node's resolver), `ClusterFirstWithHostNet` (for `hostNetwork` pods), `None` (only `dnsConfig`) | (6) three of the four are in use: tester and webshop `ClusterFirst`, CoreDNS `Default` (Corefile section above), calico-node `ClusterFirstWithHostNet` - it runs in the node's network but still resolves cluster names. `None` is unused |
| **dnsConfig:** `nameservers` (up to 3), `searches` (up to 32), `options`, merged into the generated `resolv.conf` | (6) the webshop's `options: [{name: ndots, value: "2"}]`, the source of its `ndots:2` |
| **Search list limits:** up to 32 domains, 2048 characters | the lab's pods have four: `webshop.svc.cluster.local svc.cluster.local cluster.local ec2.internal` |
| **Windows nodes** resolve differently | not relevant here: both lab nodes are Linux |

### Your turn: fix the FQDNs

A CKA-style exercise. [`networking/dns-quiz.yaml`](../networking/dns-quiz.yaml)
creates a namespace `dns-quiz` with a `resolver` Deployment that reads four
DNS names from a ConfigMap and resolves them every 15 seconds - and a headless
Service `webshop-checkout-pods` over the two checkout pods. All four names
start as `todo`:

```text
kubectl apply -f networking/dns-quiz.yaml
kubectl -n dns-quiz rollout status deployment resolver --timeout=120s
kubectl -n dns-quiz logs deployment/resolver --tail=5
```

```text
DNS_1=todo -> NOT RESOLVED
DNS_2=todo -> NOT RESOLVED
DNS_3=todo -> NOT RESOLVED
DNS_4=todo -> NOT RESOLVED
---
```

Put the right **fully qualified** name into each value of the ConfigMap
`dns-quiz`, and make the resolver use them:

1. `DNS_1`: the Service `kubernetes` in namespace `default`
2. `DNS_2`: the headless Service `webshop-checkout-pods` in namespace `webshop`
3. `DNS_3`: the pod `webshop-rabbitmq-0` in namespace `webshop` - a name that
   keeps working when the pod's IP changes
4. `DNS_4`: a pod with IP `1.2.3.4` in namespace `kube-system`

Try candidates from inside the resolver
(`kubectl -n dns-quiz exec deploy/resolver -- nslookup <name>`), edit the
ConfigMap (`kubectl -n dns-quiz edit configmap dns-quiz`, as in the exam), and
check the logs. Everything you need is in this module: the record formats in
"Making sense of 'DNS for Services and Pods'", the StatefulSet's
`hostname`/`subdomain`, and `pods insecure` in the Corefile. Two traps the
exam sets: a headless Service's IPs never go into its name, and a pod IP
record uses dashes. And if the logs still say `todo` after your edit, ask
yourself when a container reads its environment. Answers:
[`answer-key/dns-quiz.md`](answer-key/dns-quiz.md).

**Why production clusters use these names**

| Pattern | Who needs it | Why |
|---|---|---|
| Fully qualified names in configuration | anything calling across namespaces | `orders.data.svc.cluster.local` means the same from every namespace; a short `orders` silently resolves to whatever is called `orders` in the caller's own namespace. With enough dots it also skips the search-list walk (`ndots`, above) - the webshop's config is all FQDNs for both reasons |
| Headless Services | clients that must reach *every* member, or pick one themselves | database and broker clusters (Kafka bootstrap and broker addresses, Cassandra seeds, Elasticsearch and RabbitMQ peer discovery); **gRPC and HTTP/2 clients**, whose long-lived connections defeat kube-proxy's per-connection balancing (Module 2's keep-alive skew) - with a headless Service the client resolves every pod and balances requests itself |
| Stable per-pod names (`hostname` + `subdomain`, set for you by a StatefulSet) | members that must be addressed individually and survive restarts | Kafka's `advertised.listeners`, ZooKeeper's `server.N=zk-N.zk-headless...`, etcd peer URLs, a PostgreSQL primary and its replicas; TLS certificates with one SAN per member |
| Pod IP records (`<dashed-ip>.<namespace>.pod...`) | TLS to a pod addressed by IP | CoreDNS keeps them "to facilitate use of SSL certs when connecting directly to pods". `pods insecure` answers for any IP and is there for compatibility with the old kube-dns; CoreDNS warns it can be abused together with wildcard certificates. Hardened clusters use `pods verified` (only real pods, at the cost of watching all pods) or `disabled` |
| Restart on configuration change | every Deployment that reads a ConfigMap as environment variables | Kubernetes does not update those in running pods. Production charts roll the pods automatically by putting a hash of the ConfigMap in the pod template - the webshop's `webshop.lab/config-revision` annotation does exactly that - or use a controller that restarts on change |

An example architecture with all of it:

```text
namespace shop                                namespace data
+-----------------------------+               +-----------------------------------------------+
| checkout (Deployment)       |   gRPC        | orders-api (Deployment, 3 pods)                |
|  ORDERS=orders-api-grpc     |-------------->|   headless Service orders-api-grpc:            |
|   .data.svc.cluster.local   |  client-side  |   one A record per pod, the client balances    |
|  KAFKA=kafka-headless       |  balancing    |                                                |
|   .data.svc.cluster.local   |               | kafka (StatefulSet, 3 brokers)                 |
|  config: ConfigMap with     |-------------->|   headless Service kafka-headless              |
|   FQDNs, pods rolled by a   |  bootstrap,   |   kafka-0.kafka-headless.data.svc.cluster.local|
|   checksum annotation       |  then each    |   kafka-1.kafka-headless...  (advertised       |
+-----------------------------+  broker by    |   kafka-2.kafka-headless...   listeners)       |
                                 its own name +-----------------------------------------------+
```

References:
[DNS for Services and Pods](https://kubernetes.io/docs/concepts/services-networking/dns-pod-service/),
[Headless Services](https://kubernetes.io/docs/concepts/services-networking/service/),
[StatefulSet: stable network ID](https://kubernetes.io/docs/concepts/workloads/controllers/statefulset/),
[CoreDNS `kubernetes` plugin (`pods`, endpoint names)](https://coredns.io/plugins/kubernetes/),
[the DNS specification](https://github.com/kubernetes/dns/blob/master/docs/specification.md),
[ConfigMaps as environment variables](https://kubernetes.io/docs/tasks/configure-pod-container/configure-pod-configmap/),
[Helm: automatically roll deployments](https://helm.sh/docs/howto/charts_tips_and_tricks/),
[gRPC load balancing](https://grpc.io/blog/grpc-load-balancing/),
[gRPC load balancing on Kubernetes](https://kubernetes.io/blog/2018/11/07/grpc-load-balancing-on-kubernetes-without-tears/).

When you are done: `kubectl delete -f networking/dns-quiz.yaml`.

### Your turn: add a cluster domain to CoreDNS

Another CKA-style task, and a change to the whole cluster's DNS - CoreDNS
restarts twice, so do it when a minute of DNS hiccups is acceptable, and keep
**Webshop / User reliability** open to watch what the webshop makes of it.
Make `<service>.<namespace>.svc.webshop.lab` resolve exactly like
`<service>.<namespace>.svc.cluster.local`, in addition to it.

**1. Back up the configuration** (into your home directory, not the repo):

```text
kubectl -n kube-system get configmap coredns -o yaml > ~/coredns-backup.yaml
```

**2. Add the domain** to the `kubernetes` plugin's zone list - one `sed`, no
editor - and check the line:

```text
kubectl -n kube-system get configmap coredns -o yaml | sed 's/kubernetes cluster.local in-addr.arpa/kubernetes cluster.local webshop.lab in-addr.arpa/' | kubectl replace -f -
kubectl -n kube-system get configmap coredns -o jsonpath='{.data.Corefile}' | grep 'kubernetes '
```

```text
configmap/coredns replaced
    kubernetes cluster.local webshop.lab in-addr.arpa ip6.arpa {
```

**3. Restart CoreDNS and test.** The `reload` plugin would pick the change up
by itself after a while; a restart makes the moment deterministic. Even then,
answers can lag a few seconds (pods still shutting down, cached answers), so
the test retries until the new name resolves - at most a minute:

```text
kubectl -n kube-system rollout restart deployment coredns
kubectl -n kube-system rollout status deployment coredns --timeout=180s
timeout 60 sh -c 'until kubectl -n webshop exec tester -- nslookup webshop-checkout.webshop.svc.webshop.lab >/dev/null 2>&1; do sleep 3; done'
kubectl -n webshop exec tester -- nslookup webshop-checkout.webshop.svc.webshop.lab
kubectl -n webshop exec tester -- nslookup webshop-checkout.webshop.svc.cluster.local
```

Both names, one ClusterIP. Nothing else changed: the kubelets still hand
every pod `cluster.local` in its search list, so short names keep using the
old domain - `webshop.lab` only works when spelled out.

**4. Restore from the backup.** The obvious `kubectl replace -f
~/coredns-backup.yaml` (or `apply`) is refused -
`Operation cannot be fulfilled on configmaps "coredns": the object has been
modified` - because the backup still carries the `resourceVersion` of the
moment it was taken, and step 2 changed the object since. Drop that one line
and the replace becomes unconditional:

```text
grep -v '^  resourceVersion:' ~/coredns-backup.yaml | kubectl replace -f -
kubectl -n kube-system rollout restart deployment coredns
kubectl -n kube-system rollout status deployment coredns --timeout=180s
timeout 60 sh -c 'until kubectl -n webshop exec tester -- nslookup webshop-checkout.webshop.svc.webshop.lab 2>&1 | grep -q NXDOMAIN; do sleep 3; done'
kubectl -n webshop exec tester -- nslookup webshop-checkout.webshop.svc.webshop.lab
```

`NXDOMAIN`: back to the original. (The restore waits for `NXDOMAIN`
specifically: a lookup that merely fails could also be CoreDNS still
restarting.) The usual exam answer instead deletes the ConfigMap and applies
the backup to create it anew, which sidesteps the stale `resourceVersion` the
other way - with a moment in which CoreDNS's ConfigMap does not exist.

**Why production clusters edit the Corefile**

| Change | Why |
|---|---|
| **An extra cluster domain**, as here | Clusters that are connected to each other (multi-cluster service discovery, a service mesh across clusters) need names that say which cluster they mean - e.g. `prod-eu1.corp.internal` instead of everyone's `cluster.local`. Serving the new domain *next to* the old one lets applications move over one at a time. (A full domain change also touches the kubelets' `clusterDomain` and kubeadm's `dnsDomain` - Module 3's chain.) |
| **Stub domains** (conditional forwarding) - the most common edit | Names the cluster cannot answer but a specific server can: an on-premises zone over the VPN, a Consul catalog. The Kubernetes docs' own example forwards `consul.local` to a Consul server at `10.150.0.1` |
| **A different upstream** (`forward . <ip>`) | Send every non-cluster lookup through a resolver that filters or logs, instead of the node's |
| **`rewrite`** | Keep an old name working while a Service is renamed or moved |
| **Caching nearer the pods** (NodeLocal DNSCache) | At scale, a DNS cache on every node takes load off CoreDNS and avoids the UDP conntrack issues of Module 3's ops note |

An example, a hybrid AWS setup - one Corefile, three kinds of answer:

```text
pod --> kube-dns 10.96.0.10 --> CoreDNS
          cluster.local, prod-eu1.corp.internal --> kubernetes plugin: Services and pods
          corp.example.com                      --> forward to 10.20.0.2 10.20.0.3
                                                    (on-premises DNS, over VPN / Direct Connect)
          everything else                       --> forward . /etc/resolv.conf
                                                    --> 10.0.0.2, the VPC resolver (Route 53,
                                                        including private hosted zones)
```

with, next to the default `.:53` block, a stub-domain block like the docs'
example:

```text
corp.example.com:53 {
    errors
    cache 30
    forward . 10.20.0.2 10.20.0.3
}
```

How teams keep such a change safe: the Corefile lives in version control and
is applied by a pipeline rather than by `kubectl edit`; a backup is taken
first, as here; the change is tested from a pod before anyone depends on it;
and after a kubeadm upgrade, which manages this ConfigMap, the Corefile is
checked again. CoreDNS is on the path of almost every request - Module 3
showed a single extra lookup costing every call seven milliseconds.

References:
[Customizing DNS Service](https://kubernetes.io/docs/tasks/administer-cluster/dns-custom-nameservers/),
[CoreDNS `kubernetes` plugin](https://coredns.io/plugins/kubernetes/),
[`forward`](https://coredns.io/plugins/forward/),
[`reload`](https://coredns.io/plugins/reload/),
[`rewrite`](https://coredns.io/plugins/rewrite/),
[NodeLocal DNSCache](https://kubernetes.io/docs/tasks/administer-cluster/nodelocaldns/),
[Route 53 Resolver](https://docs.aws.amazon.com/Route53/latest/DeveloperGuide/resolver.html).

### Under the hood: DNS is NATted too **[on the worker]**

> New to NAT rules and conntrack? [nat-and-conntrack.md](nat-and-conntrack.md) explains how to read them from first principles - hooks, chains, every field of a rule and of a conntrack entry - with this lab's real output.

kube-dns is a ClusterIP Service like any other, so each query is a UDP flow
through the same DNAT:

```text
sudo conntrack -L -p udp -d 10.96.0.10 --dport 53
```

```text
udp 17 14 src=192.168.171.117 dst=10.96.0.10 sport=35108 dport=53 ... src=192.168.219.67 dst=192.168.171.117 sport=53 ...
```

The reply comes from `192.168.219.67` - a CoreDNS pod on the master, across
the VXLAN tunnel. Count the flows (`... | wc -l`) under each setting: **1277**
with `ndots:5`, **380** with `ndots:2` - the same traffic, 3.4 times the DNS.

**Ops note:** stale UDP conntrack entries are the classic "DNS flaky on one
node" incident after CoreDNS pods are replaced;
`sudo conntrack -D -p udp --dport 53` flushes them.

## Module 4 - Walk the chain: one order, every hop

**Prerequisites:** the webshop installed (README quick start).

The networking lessons walk a request through three Services with curl and
conntrack. The webshop's request path is a trace. Place one order and open
it:

```text
python3 tools/lab.py journey
```

It prints the order ID and a `trace_id`. In Explore (Tempo), paste the trace
ID. One trace, six services:

```text
loadgen -> storefront -> checkout -> inventory           (reserve stock)
                                  -> payment             (authorize)
                                  -> PostgreSQL          (order + outbox, one transaction)
                                  -> RabbitMQ            order.publish
           RabbitMQ -> fulfillment-worker -> inventory   (commit)
                                          -> payment     (capture)
```

Every CLIENT span names a *Service* (`server.address =
webshop-inventory.webshop.svc.cluster.local`) - the client never knows which
pod it reached. The SERVER span under it carries the pod (its resource
attribute `k8s.pod.name`). The trace is where the Service abstraction and
kube-proxy's choice meet. The queue hop is the one conntrack would never show
you as part of the request: the order is published, the HTTP response goes
back to the user, and a different process picks the message up later - yet
it is the same trace, because the trace context travels in the message.

### Where it comes from: each hop's address, and the trace context

Nothing in the code knows where the other services are. Every destination is
configuration - the chart's `webshop-config` ConfigMap, which every webshop
pod gets as environment variables:

```text
echo "--- where each service sends its calls (the webshop-config ConfigMap)"; kubectl -n webshop get configmap webshop-config -o jsonpath='CHECKOUT_URL={.data.CHECKOUT_URL}{"\n"}INVENTORY_URL={.data.INVENTORY_URL}{"\n"}PAYMENT_URL={.data.PAYMENT_URL}{"\n"}PGHOST={.data.PGHOST}{"\n"}AMQP_HOST={.data.AMQP_HOST}{"\n"}OTEL_EXPORTER_OTLP_ENDPOINT={.data.OTEL_EXPORTER_OTLP_ENDPOINT}{"\n"}'
```

```text
--- where each service sends its calls (the webshop-config ConfigMap)
CHECKOUT_URL=http://webshop-checkout.webshop.svc.cluster.local
INVENTORY_URL=http://webshop-inventory.webshop.svc.cluster.local
PAYMENT_URL=http://webshop-payment.webshop.svc.cluster.local
PGHOST=webshop-postgresql.webshop.svc.cluster.local
AMQP_HOST=webshop-rabbitmq.webshop.svc.cluster.local
OTEL_EXPORTER_OTLP_ENDPOINT=http://webshop-collector.webshop.svc.cluster.local:4318
```

The pods take the whole ConfigMap as environment variables (`envFrom`) -
check one from inside:

```text
echo "--- how the pods get it"; kubectl -n webshop get deploy webshop-checkout -o jsonpath='{.spec.template.spec.containers[0].envFrom}{"\n"}'
echo "--- what checkout sees"; kubectl -n webshop exec deploy/webshop-checkout -- printenv INVENTORY_URL PAYMENT_URL AMQP_HOST
```

Each one is a Service name: resolved by CoreDNS (Module 3) to a ClusterIP
(Module 2), DNATed by kube-proxy to a pod. The telemetry travels the same
way - the last line is the Collector's Service.

**How one trace follows the request.** Every outgoing call carries a
`traceparent` HTTP header - `00-<trace ID>-<the calling span's ID>-01` (the
W3C Trace Context format) - and the server that receives it continues that
trace instead of starting a new one. Prove it by sending a header of your
own:

```text
TRACE=$(python3 -c "import secrets; print(secrets.token_hex(16))")
PARENT=$(python3 -c "import secrets; print(secrets.token_hex(8))")
echo "our trace ID: ${TRACE}"
kubectl -n webshop exec tester -- wget -qO /dev/null --timeout=5 --header "traceparent: 00-${TRACE}-${PARENT}-01" http://webshop-storefront/api/products && echo "request sent with traceparent 00-${TRACE}-${PARENT}-01"
```

Paste the printed trace ID into Grafana Explore (Tempo): the trace exists,
with *your* ID - the storefront's `GET /api/products` and `catalog.list`, and
under it inventory's `GET /products` and its SQL. The tester started that
trace by sending the header; the storefront and inventory only carried it on.
Across RabbitMQ the same context rides in the message's headers, which is why
`order.fulfill` appears as a child of `order.publish` in Module 4's trace
(`python3 tools/lab.py verify` checks exactly that: "context across the
queue").

**[on the worker]** The same chain as flows: each synchronous hop is a
conntrack entry like Module 2's, keyed by its Service's ClusterIP. A small
function reads a Service's ClusterIP from kube-proxy's `KUBE-SERVICES` rule:

```text
svcip() { sudo iptables-save -t nat | awk -v c="\"webshop/webshop-$1:http cluster IP\"" '/^-A KUBE-SERVICES/ && index($0, c) {sub("/32", "", $4); print $4; exit}'; }
sudo conntrack -L -d $(svcip storefront)
sudo conntrack -L -d $(svcip checkout)
sudo conntrack -L -d $(svcip inventory)
```

When one hop of a multi-tier request hangs, the trace says *which* hop; the
broken flow sits there as `SYN_SENT [UNREPLIED]` instead of
`ESTABLISHED`/`TIME_WAIT`.

## Module 5 - NodePort: the way in from outside

**Prerequisites:**

```text
helm upgrade webshop ./chart -n webshop -f values/values-aws.yaml -f networking/values-networking.yaml --wait --timeout 10m
```

The overlay sets `exposure.nodePort.enabled: true`: the storefront Service
becomes type NodePort on port 30080, on **every** node.

```text
kubectl -n webshop get svc webshop-storefront
```

`80:30080/TCP`. **[on the master]** - which runs no webshop pod at all:

```text
curl -s -o /dev/null -D - http://10.0.1.10:30080/livez
curl -s -o /dev/null -D - http://10.0.1.11:30080/livez
```

Both answer 200, with `X-Served-By` naming a storefront pod on the worker.
(Use the node addresses, not `localhost`: Module 9's eBPF dataplane does not
serve NodePorts on localhost.)

**[on the master]** NodePort is a shim in front of the ClusterIP machinery:

```text
sudo iptables-save -t nat | grep 30080
```

```text
-A KUBE-NODEPORTS -p tcp -m comment --comment "webshop/webshop-storefront:http" -m tcp --dport 30080 -j KUBE-EXT-MX3FQJXDI6GSY32B
-A KUBE-EXT-MX3FQJXDI6GSY32B -m comment --comment "masquerade traffic for webshop/webshop-storefront:http external destinations" -j KUBE-MARK-MASQ
```

Port 30080 on any node jumps to `KUBE-EXT-*`, which marks the packet for
masquerade and joins the same `KUBE-SVC-*` chain as ClusterIP traffic.

**Who does the pod think is calling?** The storefront's SERVER spans record
`client.address`. Send requests you can find again, then look:

```text
curl -s -o /dev/null -A np-test http://10.0.1.11:30080/api/products
curl -s -o /dev/null -A np-test-via-master http://10.0.1.10:30080/api/products
```

(**[on the master]**; then in Explore, Tempo:
`{ span.user_agent.original =~ "np-test.*" }` and open a span.)

| `externalTrafficPolicy` | from the master to... | storefront sees `client.address` |
|---|---|---|
| `Cluster` (default) | the worker's NodePort | `10.0.1.11` - the worker, which masqueraded it |
| `Cluster` | its own NodePort | `192.168.219.64` - the master's tunnel address |
| `Local` | the worker's NodePort | `10.0.1.10` - the real client |
| `Local` | its own NodePort | `192.168.219.64` - still masqueraded |

```text
helm upgrade webshop ./chart -n webshop -f values/values-aws.yaml -f networking/values-networking.yaml --set exposure.nodePort.externalTrafficPolicy=Local --wait --timeout 10m
```

With `Cluster`, any node accepts and forwards - and hides the client.
`Local` keeps the client address by refusing to forward: a node only sends
NodePort traffic to its *own* pods. (A client outside the cluster hitting the
master would then get nothing. Traffic the node originates itself - your curl
on the master - is exempt: kube-proxy's `--src-type LOCAL` rules route it the
cluster-wide way.) Put `Cluster` back with the Prerequisites command.

**The policy side.** Under default-deny, the storefront must also *admit*
those sources. That is `exposure.nodePort.allowFrom` (an ipBlock, default
`0.0.0.0/0`). Remove it and the master's curl times out, while the worker's
still works (Calico always lets a node reach its own local pods):

```text
helm upgrade webshop ./chart -n webshop -f values/values-aws.yaml -f networking/values-networking.yaml --set exposure.nodePort.allowFrom=null --wait --timeout 10m
```

**[on the master]**, during that:

```text
sudo conntrack -L -p tcp --dport 30080
```

```text
tcp 6 112 SYN_SENT src=... dport=30080 ... [UNREPLIED] src=192.168.171.90 dst=192.168.219.64 sport=8080 ...
```

DNAT done (to storefront pod `192.168.171.90`), source masqueraded to the
master's tunnel address - and nothing ever came back: the pod's ingress
policy dropped the SYN on the worker. Restore with the Prerequisites command.

One consequence worth knowing: Calico applies ipBlocks to pod addresses too,
so the default `0.0.0.0/0` admits *every pod in the cluster* to the
storefront while the NodePort is on - narrowing it to the node subnet would
also drop the masqueraded traffic, whose tunnel source lies in the pod CIDR.
The storefront is the public edge; the other 21 policies are unaffected.

### Where it comes from: the node port and kube-proxy's settings

```text
echo "--- the Service (from the chart's exposure.nodePort values)"; kubectl -n webshop get svc webshop-storefront -o jsonpath='type={.spec.type} port={.spec.ports[0].port} nodePort={.spec.ports[0].nodePort} externalTrafficPolicy={.spec.externalTrafficPolicy}{"\n"}'
echo "--- kube-proxy's configuration (a ConfigMap kubeadm wrote)"; kubectl -n kube-system get configmap kube-proxy -o jsonpath='{.data.config\.conf}' | grep -E '^(mode|clusterCIDR|nodePortAddresses):'
```

```text
--- the Service (from the chart's exposure.nodePort values)
type=NodePort port=80 nodePort=30080 externalTrafficPolicy=Cluster
--- kube-proxy's configuration (a ConfigMap kubeadm wrote)
clusterCIDR: 192.168.0.0/16
mode: ""
nodePortAddresses: null
```

- **`nodePort: 30080`** is the chart's `exposure.nodePort.port`. Leave a
  Service's `nodePort` out and the API server picks a free one from its
  `--service-node-port-range`; kubeadm does not set that flag, so the default
  range applies, `30000-32767` - which is why the controllers' NodePorts in
  Modules 6 and 7 are random numbers in it. **[on the master]**:
  `sudo grep -c service-node-port-range /etc/kubernetes/manifests/kube-apiserver.yaml`
  prints `0`.
- **`mode: ""`** means kube-proxy's default on Linux: iptables (here written
  through the nft backend).
- **`clusterCIDR: 192.168.0.0/16`** is where the `! -s 192.168.0.0/16` in
  Module 2's masquerade rule comes from: traffic from outside the pod network
  is masqueraded, pod-to-pod traffic is not.
- **`nodePortAddresses: null`** opens NodePorts on every address of every
  node - which is why both `10.0.1.10` and `10.0.1.11` answer.

**Why not from your laptop?** The lab's security group opens only 6443 (and
optionally 22) to your address; NodePorts are reachable from the nodes only,
as in Module 5 of the networking lessons.

## Module 6 - Ingress: HTTP routing behind one entry point

**Prerequisites:**

```text
helm upgrade webshop ./chart -n webshop -f values/values-aws.yaml -f networking/values-networking.yaml --wait --timeout 10m
```

The overlay already created an Ingress (`exposure.ingress.enabled`); with no
controller, nothing implements it:

```text
kubectl -n webshop get ingress webshop
```

Install ingress-nginx, as Module 6 of the networking lessons does:

```text
kubectl apply -f https://raw.githubusercontent.com/kubernetes/ingress-nginx/controller-v1.13.3/deploy/static/provider/baremetal/deploy.yaml
kubectl -n ingress-nginx wait --for=condition=Ready pod -l app.kubernetes.io/component=controller --timeout=300s
kubectl -n ingress-nginx get svc ingress-nginx-controller
```

Note the NodePort mapped to port 80 (random; `30682` in the lab run). The
controller's proxies connect to the storefront from the `ingress-nginx`
namespace, which the overlay lists in
`networkPolicy.ingressControllerNamespaces` - without it the default-deny
policies would drop them. **[on a node]** - the NodePort read from
kube-proxy's `KUBE-NODEPORTS` rule:

```text
NP=$(sudo iptables-save -t nat | awk '/^-A KUBE-NODEPORTS/ && /"ingress-nginx\/ingress-nginx-controller:http"/ {for (i = 1; i <= NF; i++) if ($i == "--dport") {print $(i+1); exit}}')
timeout 120 bash -c "until curl -sf -o /dev/null -H 'Host: webshop.local' http://10.0.1.11:${NP}/livez; do sleep 2; done"
for i in 1 2 3 4 5 6; do curl -s -o /dev/null -D - -H "Host: webshop.local" http://10.0.1.11:${NP}/livez | grep -i x-served-by; done
curl -s -o /dev/null -w "%{http_code}\n" http://10.0.1.11:${NP}/
```

The `until` line waits for the controller to have loaded the route (a few
seconds after it is Ready). Requests for `webshop.local` reach both
storefront pods - two names in the `X-Served-By` lines, in any mix (4/2 in
the lab run). Without the Host header no Ingress rule matches, so nginx
answers `404`. The `Host` header stands in for a DNS record pointing at a
load balancer in front of the controller.

### Where it comes from: from an Ingress to nginx's backends

```text
echo "--- the IngressClass names a controller"; kubectl get ingressclass nginx -o jsonpath='{.metadata.name} -> {.spec.controller}{"\n"}'
echo "--- the controller was started for that class"; kubectl -n ingress-nginx get deploy ingress-nginx-controller -o jsonpath='{.spec.template.spec.containers[0].args}' | tr ',' '\n' | grep -- '--controller-class'
echo "--- the Ingress (from the chart's exposure.ingress values)"; kubectl -n webshop get ingress webshop -o jsonpath='class={.spec.ingressClassName} host={.spec.rules[0].host} path={.spec.rules[0].http.paths[0].path} -> {.spec.rules[0].http.paths[0].backend.service.name}:{.spec.rules[0].http.paths[0].backend.service.port.name}{"\n"}'
echo "--- what the controller generated from it: an nginx server block"; kubectl -n ingress-nginx exec deploy/ingress-nginx-controller -- grep -n 'server_name webshop.local' /etc/nginx/nginx.conf
echo "--- the backend it balances over"; kubectl -n ingress-nginx exec deploy/ingress-nginx-controller -- /dbg backends get webshop-webshop-storefront-http | python3 -c "import json,sys; print([e['address'] + ':' + e['port'] for e in json.load(sys.stdin)['endpoints']])"
echo "--- the storefront EndpointSlice, for comparison"; kubectl -n webshop get endpointslices -l kubernetes.io/service-name=webshop-storefront -o jsonpath='{range .items[*].endpoints[*]}{.addresses[0]} {end}{"\n"}'
```

```text
--- the IngressClass names a controller
nginx -> k8s.io/ingress-nginx
--- the controller was started for that class
"--controller-class=k8s.io/ingress-nginx"
--- the Ingress (from the chart's exposure.ingress values)
class=nginx host=webshop.local path=/ -> webshop-storefront:http
--- what the controller generated from it: an nginx server block
337:		server_name webshop.local ;
--- the backend it balances over
['192.168.171.103:8080', '192.168.171.123:8080']
--- the storefront EndpointSlice, for comparison
192.168.171.103 192.168.171.123
```

The chain: the Ingress names a class; the class names a controller; only the
controller started with that `--controller-class` acts on it. That controller
watches Ingresses *and* EndpointSlices and renders both into nginx's
configuration - the `server_name` from the Ingress's host, the backend's
addresses straight from the storefront's EndpointSlice (`/dbg` is the
controller's own debug tool). Which is why the second hop below goes to pod
IPs: nginx never sees the ClusterIP.

### Under the hood: two flows, and no ClusterIP **[on the worker]**

> New to NAT rules and conntrack? [nat-and-conntrack.md](nat-and-conntrack.md) explains how to read them from first principles - hooks, chains, every field of a rule and of a conntrack entry - with this lab's real output.

The controller pod's address is the target of its Service's `DNAT` rule:

```text
NP=$(sudo iptables-save -t nat | awk '/^-A KUBE-NODEPORTS/ && /"ingress-nginx\/ingress-nginx-controller:http"/ {for (i = 1; i <= NF; i++) if ($i == "--dport") {print $(i+1); exit}}')
CIP=$(sudo iptables-save -t nat | awk '/"ingress-nginx\/ingress-nginx-controller:http"/ && /DNAT/ {sub(/:[0-9]+$/, "", $NF); print $NF; exit}')
curl -s -o /dev/null -H "Host: webshop.local" http://10.0.1.11:${NP}/livez
sudo conntrack -L -p tcp --dport ${NP}
sudo conntrack -L -p tcp -s ${CIP} --dport 8080
```

```text
TIME_WAIT   src=127.0.0.1 dst=127.0.0.1 dport=30682 ... src=192.168.171.77 dst=10.0.1.11 sport=80 ...
ESTABLISHED src=192.168.171.77 dst=192.168.171.80 sport=57008 dport=8080 ...
```

Hop 1: you to the controller pod, through its NodePort. Hop 2: the
controller to a storefront pod - **directly to the pod's address**, not to the
storefront ClusterIP. ingress-nginx watches the EndpointSlices itself and
balances per request; kube-proxy is out of that hop. And the connection stays
`ESTABLISHED`: the controller keeps a pool to its backends, which is why its
balancing is per request although the connections are long-lived. A proxy
terminates TCP and opens its own connection - two flows where kube-proxy's NAT
makes one.

## Module 7 - Gateway API: the successor

**Prerequisites:**

```text
helm upgrade webshop ./chart -n webshop -f values/values-aws.yaml -f networking/values-networking.yaml --wait --timeout 10m
```

Install Envoy Gateway (its manifest brings the Gateway API CRDs;
`--server-side` because they are large), then the webshop's own Gateway:

```text
kubectl apply --server-side -f https://github.com/envoyproxy/gateway/releases/download/v1.4.2/install.yaml
kubectl wait --for=condition=Established crd/gatewayclasses.gateway.networking.k8s.io crd/gateways.gateway.networking.k8s.io crd/httproutes.gateway.networking.k8s.io crd/envoyproxies.gateway.envoyproxy.io --timeout=120s
kubectl -n envoy-gateway-system wait --for=condition=Available deploy/envoy-gateway --timeout=300s
kubectl apply -f networking/gateway/
```

[`networking/gateway/`](../networking/gateway) splits the job the way the
API intends:

| File | Resource | Owner | Says |
|---|---|---|---|
| `gatewayclass.yaml` | GatewayClass `eg` | infrastructure | Envoy Gateway implements this class |
| `gateway.yaml` | Gateway `webshop-gateway` | platform team | one HTTP listener on port 80, for routes in this namespace |
| `proxy-config.yaml` | EnvoyProxy | platform team | expose it as a NodePort, size the proxy for the lab |
| the chart (`exposure.httpRoute`) | HTTPRoute | app team | `webshop.local/` goes to the storefront |

The route comes from the chart, so it is the third values file:

```text
helm upgrade webshop ./chart -n webshop -f values/values-aws.yaml -f networking/values-networking.yaml -f networking/values-gateway.yaml --wait --timeout 10m
kubectl -n webshop wait --for=condition=Programmed gateway/webshop-gateway --timeout=300s
kubectl get gatewayclass eg
kubectl -n webshop get gateway webshop-gateway
kubectl -n webshop get httproute webshop-storefront -o jsonpath="{.status.parents[0].conditions}"
kubectl -n envoy-gateway-system get pods,svc -l gateway.envoyproxy.io/owning-gateway-name=webshop-gateway
```

Order matters: before Envoy Gateway's CRDs exist the third file makes the
whole upgrade fail (`no matches for kind "HTTPRoute" ... ensure CRDs are
installed first`) and the release stays as it was. Then: GatewayClass
`ACCEPTED True`, Gateway `PROGRAMMED True` with a node address, route
`Accepted` and `ResolvedRefs`, and an Envoy pod and NodePort Service that
Envoy Gateway created for the Gateway. **[on a node]**, with that NodePort
(its Service is named after the Gateway, plus a hash):

```text
GW=$(sudo iptables-save -t nat | awk '/^-A KUBE-NODEPORTS/ && /"envoy-gateway-system\/envoy-webshop-webshop-gateway-/ {for (i = 1; i <= NF; i++) if ($i == "--dport") {print $(i+1); exit}}')
timeout 120 bash -c "until curl -sf -o /dev/null -H 'Host: webshop.local' http://10.0.1.11:${GW}/livez; do sleep 2; done"
for i in 1 2 3 4 5 6; do curl -s -o /dev/null -D - -H "Host: webshop.local" http://10.0.1.11:${GW}/livez | grep -i x-served-by; done
curl -s -o /dev/null -w "%{http_code}\n" -H "Host: other.local" http://10.0.1.11:${GW}/
```

`webshop.local` reaches both storefront pods (3/3 in six tries: Envoy
balances per request); any other host gets 404. If Module 6 is still
installed, the Ingress and the HTTPRoute now serve side by side - routing APIs
are just resources; controllers make them real.

### Where it comes from: from a Gateway to Envoy's clusters

```text
echo "--- the GatewayClass names a controller"; kubectl get gatewayclass eg -o jsonpath='{.metadata.name} -> {.spec.controllerName}{"\n"}'
echo "--- the Gateway: class, listener, implementation settings, address"; kubectl -n webshop get gateway webshop-gateway -o jsonpath='class={.spec.gatewayClassName} listener={.spec.listeners[0].name}:{.spec.listeners[0].port} parameters={.spec.infrastructure.parametersRef.kind}/{.spec.infrastructure.parametersRef.name} address={.status.addresses[0].value}{"\n"}'
echo "--- the HTTPRoute (from the chart's exposure.httpRoute values)"; kubectl -n webshop get httproute webshop-storefront -o jsonpath='parent={.spec.parentRefs[0].name} host={.spec.hostnames[0]} -> {.spec.rules[0].backendRefs[0].name}:{.spec.rules[0].backendRefs[0].port}{"\n"}'
echo "--- what Envoy Gateway created for the Gateway"; kubectl -n envoy-gateway-system get deploy,svc -l gateway.envoyproxy.io/owning-gateway-name=webshop-gateway -o custom-columns='KIND:.kind,NAME:.metadata.name,FOR-GATEWAY-IN:.metadata.labels.gateway\.envoyproxy\.io/owning-gateway-namespace'
```

```text
--- the GatewayClass names a controller
eg -> gateway.envoyproxy.io/gatewayclass-controller
--- the Gateway: class, listener, implementation settings, address
class=eg listener=http:80 parameters=EnvoyProxy/proxy-nodeport address=10.0.1.10
--- the HTTPRoute (from the chart's exposure.httpRoute values)
parent=webshop-gateway host=webshop.local -> webshop-storefront:80
--- what Envoy Gateway created for the Gateway
KIND         NAME                                     FOR-GATEWAY-IN
Deployment   envoy-webshop-webshop-gateway-4ff940ea   webshop
Service      envoy-webshop-webshop-gateway-4ff940ea   webshop
```

Envoy Gateway (the `envoy-gateway` Deployment) is a control plane: it reads
the GatewayClass, Gateway, EnvoyProxy and HTTPRoute, starts an Envoy proxy
per Gateway, and pushes it its configuration over a gRPC API (xDS). The
proxy image has no shell, but Envoy's admin interface listens on the pod's
own localhost - which `kubectl port-forward` can reach. This block opens one,
waits until Envoy answers, reads what Envoy was told, and closes it:

```text
EP=$(kubectl -n envoy-gateway-system get pods -l gateway.envoyproxy.io/owning-gateway-name=webshop-gateway -o jsonpath='{.items[0].metadata.name}')
kubectl -n envoy-gateway-system port-forward pod/${EP} 19000:19000 >/dev/null 2>&1 &
PF=$!
timeout 30 bash -c 'until curl -s -o /dev/null http://127.0.0.1:19000/ready; do sleep 1; done'
echo "--- the routes Envoy was given (hosts -> clusters)"; curl -s http://127.0.0.1:19000/config_dump?resource=dynamic_route_configs | python3 -c "import json,sys; [print(vh['domains'], '->', [r['route']['cluster'] for r in vh['routes']]) for rc in json.load(sys.stdin)['configs'] for vh in rc['route_config'].get('virtual_hosts',[])]"
echo "--- the endpoints Envoy sends that cluster to"; curl -s http://127.0.0.1:19000/clusters | grep -E 'storefront.*::[0-9.]+:8080::health_flags'
kill ${PF}
echo "--- the storefront EndpointSlice, for comparison"; kubectl -n webshop get endpointslices -l kubernetes.io/service-name=webshop-storefront -o jsonpath='{range .items[*].endpoints[*]}{.addresses[0]} {end}{"\n"}'
```

```text
--- the routes Envoy was given (hosts -> clusters)
['webshop.local'] -> ['httproute/webshop/webshop-storefront/rule/0']
--- the endpoints Envoy sends that cluster to
httproute/webshop/webshop-storefront/rule/0::192.168.171.103:8080::health_flags::healthy
httproute/webshop/webshop-storefront/rule/0::192.168.171.123:8080::health_flags::healthy
--- the storefront EndpointSlice, for comparison
192.168.171.103 192.168.171.123
```

The route's host became an Envoy virtual host; its `backendRefs` became a
cluster named after the HTTPRoute and rule; and that cluster's members are
the storefront's pod addresses, from the EndpointSlice - as with nginx, no
ClusterIP in between.

**[on the worker]** Same picture as Module 6: Envoy connects straight to
storefront pod addresses over pooled connections:

```text
EIP=$(sudo iptables-save -t nat | awk '/"envoy-gateway-system\/envoy-webshop-webshop-gateway-/ && /DNAT/ {sub(/:[0-9]+$/, "", $NF); print $NF; exit}')
sudo conntrack -L -p tcp -s ${EIP} --dport 8080
```

## Module 8 - NetworkPolicy: the default-deny you have been working under

**Prerequisites:**

```text
helm upgrade webshop ./chart -n webshop -f values/values-aws.yaml -f networking/values-networking.yaml --wait --timeout 10m
kubectl apply -f networking/tester.yaml
kubectl -n webshop wait --for=condition=Ready pod/tester --timeout=90s
```

The networking lessons build a default-deny up from nothing. The webshop has
been running under one all along:

```text
kubectl -n webshop get networkpolicy
```

22 policies from the chart (the pattern is described at the top of
[`chart/templates/networkpolicies.yaml`](../chart/templates/networkpolicies.yaml))
plus the tester's two:

- `webshop-default-deny` selects every pod, both directions; `webshop-allow-dns`
  gives every pod CoreDNS.
- One `<service>-ingress` per receiver (who may call it) and one
  `<service>-egress` per sender (whom it may call) - a connection needs both.
- Data stores, telemetry (Collector, Prometheus, Loki, Tempo, Grafana) and
  the API server each get their own.

Probe it from the tester:

```text
kubectl -n webshop exec tester -- wget -qO- --timeout=3 http://webshop-storefront/livez; echo
kubectl -n webshop exec tester -- wget -qO- --timeout=3 http://webshop-lab-control/livez; echo
kubectl -n webshop exec tester -- wget -qO- --timeout=3 http://webshop-prometheus-server/-/healthy; echo
```

The storefront answers `{"status":"alive"}`; lab-control and Prometheus
time out (`wget: download timed out`, `command terminated with exit code 1` -
expected, as in Module 1). **Both sides must agree.** Delete only the tester's *egress* policy - the storefront's
policies still accept the tester:

```text
kubectl -n webshop delete networkpolicy tester-egress
kubectl -n webshop exec tester -- wget -qO- --timeout=3 http://webshop-storefront/livez; echo
kubectl apply -f networking/tester.yaml
kubectl -n webshop wait --for=condition=Ready pod/tester --timeout=90s
```

Timed out until the policy was back. Policies are additive allow-lists: once
any policy selects a pod for a direction, that direction allows only what some
policy grants - on the sender *and* the receiver. (Ports in the policies are
pod ports, 8080, not Service ports: enforcement happens after kube-proxy's
DNAT. Kubelet probes keep working regardless: Calico lets a node reach its
own pods.)

**[on the worker]** Calico compiles the policies into filter chains next to
kube-proxy's NAT chains:

```text
sudo iptables-save -t filter | grep -c "^:cali-"
sudo iptables-save -t filter | grep -o "^:cali-[a-z]*-" | sort | uniq -c
```

138 chains in the lab run: `cali-pi-*` / `cali-po-*` per policy (15 each),
`cali-tw-*` / `cali-fw-*` (to / from workload) per pod (23 each), plus
dispatch chains.

### Where it comes from: from a NetworkPolicy to an ip set

The NetworkPolicy objects are only intent; Calico's agent (Felix, inside each
calico-node pod) turns them into rules on the node. Follow one - the
storefront's ingress policy - from object to kernel, from your terminal
(calico-node shares the worker's network, so commands run in it see the
worker's rules):

```text
CN=$(kubectl -n calico-system get pods -l k8s-app=calico-node --field-selector spec.nodeName=worker -o jsonpath='{.items[0].metadata.name}')
RULE=$(kubectl -n calico-system exec ${CN} -c calico-node -- iptables-nft-save -t filter | grep "Policy webshop/knp.default.webshop-storefront-ingress ingress")
SET=$(echo "${RULE}" | grep -o "cali40s:[^ ]*")
echo "--- the rule Calico wrote for the policy:"; echo "${RULE}"
echo "--- the source pods it allows (ip set ${SET}):"; [ -n "${SET}" ] && kubectl -n calico-system exec ${CN} -c calico-node -- ipset list ${SET} | sed -n '/Members:/,$p' || echo "no such rule - is the webshop installed, and Calico on its iptables dataplane?"
echo "--- the pods the policy's 'from' selects:"; kubectl -n webshop get pods -l 'app.kubernetes.io/component in (loadgen,journey-test)' --field-selector status.phase=Running -o custom-columns=NAME:.metadata.name,IP:.status.podIP
```

```text
--- the rule Calico wrote for the policy:
-A cali-pi-_row78HYf8CrNZLouqfL -p tcp ... --comment "Policy webshop/knp.default.webshop-storefront-ingress ingress" -m set --match-set cali40s:JJE8ugv1155ymsxrrYGOFgl src -m multiport --dports 8080 -j MARK --set-xmark 0x10000/0x10000
--- the source pods it allows (ip set cali40s:JJE8ugv1155ymsxrrYGOFgl):
Members:
192.168.171.108
--- the pods the policy's 'from' selects:
NAME                              IP
webshop-loadgen-b4d756dbb-jn82c   192.168.171.108
```

Read it left to right:

- **`knp.default.webshop-storefront-ingress`** - Calico's name for the
  Kubernetes NetworkPolicy `webshop-storefront-ingress` ("knp" = Kubernetes
  NetworkPolicy). Each policy gets its own chain: `cali-pi-*` for inbound,
  `cali-po-*` for outbound - the chains counted above.
- **`--match-set cali40s:... src`** - the policy's `from` (pods labelled
  loadgen or journey-test) is not in the rule as labels: Felix keeps an **ip
  set** of the matching pods' current addresses and updates it as pods come
  and go. Right now only the load generator runs; the Helm test pod is
  `Completed`, so it has no address and is not in the set.
- **`--dports 8080`** - the policy's port, the pod port.
- **`MARK ... 0x10000`** - "accepted by a policy"; the pod's `cali-tw-*`
  chain lets marked packets through and drops the rest.

(`iptables-nft-save` because this lab's iptables writes through the nft
backend; the worker itself has no `ipset` tool, calico-node does.)

**Now break one.** Exercise 9 of the incident track removes payment from the
fulfillment worker's egress policy - the kind of "tightening" that removes a
line too many - and asks you to find it from the telemetry first:

```text
python3 tools/lab.py fault apply policy-drop
```

The walkthrough is [`incidents/09-policy-drop.md`](incidents/09-policy-drop.md):
the SLI falls to 0, only one caller-to-callee path fails, every attempt ends
in `ConnectTimeout` with no server span, and payment's own metrics show it
never saw the requests. On the node, Calico's drop counter for that pod's
`cali-fw-*` chain climbs while `conntrack` shows *nothing* - the opposite of
Module 5's `SYN_SENT [UNREPLIED]`: there the receiver's ingress policy dropped
the SYN after it had left another node; here the sender's egress policy drops
it before a connection is ever committed. End it with
`python3 tools/lab.py fault reset`.

## Module 9 - Switch the dataplane to eBPF and back

**Prerequisites:**

```text
helm upgrade webshop ./chart -n webshop -f values/values-aws.yaml -f networking/values-networking.yaml --wait --timeout 10m
kubectl apply -f networking/tester.yaml
kubectl -n webshop wait --for=condition=Ready pod/tester --timeout=90s
kubectl get installation default -o jsonpath="{.spec.calicoNetwork.linuxDataplane}"
```

The last command must print `Iptables`.

Everything so far ran on Calico's iptables dataplane with kube-proxy. In
**eBPF mode** Calico replaces both: BPF programs attached to every interface
do policy *and* Service load balancing, and kube-proxy is switched off. Same
NetworkPolicies, same Services - a different machine underneath. The webshop
tells you what the switch costs.

Keep **Webshop / User reliability** open in Grafana for the whole module.

**1. Give Calico the API server's real address.** In eBPF mode Calico is the
thing that implements the `kubernetes` Service - so Calico itself must not
depend on it:

```text
kubectl apply -f networking/ebpf/kubernetes-services-endpoint.yaml
kubectl wait --for=condition=Progressing tigerastatus/calico --timeout=90s || echo "nothing to roll out"
kubectl wait --for=condition=Available tigerastatus/calico --timeout=300s
```

Calico's operator restarts calico-node *after* `apply` returns, so
`rollout status` right away could report the old, finished rollout. The
first `wait` waits for the operator to start (`Progressing`); the second for
it to finish. (Run again with the ConfigMap already in place, nothing
restarts: the first wait gives up after 90 s and says so - harmless.)

**2. Turn off the connect-time load balancer.** With Calico v3.30.2 on the
lab's kernel 7.0, Felix cannot load that BPF program; skip this step and the
switch panics (`failed to attach connect-time load balancer ... invalid
argument`) - see Troubleshooting. Without it Calico still load-balances
traffic from the nodes themselves (`bpfHostNetworkedNATWithoutCTLB`):

```text
kubectl patch felixconfiguration default --type merge --patch-file networking/ebpf/felix-ctlb-off.yaml
```

**3 and 4. Stop kube-proxy, switch the dataplane** - one right after the
other. kube-proxy's DaemonSet gets a nodeSelector no node matches; its rules
stay in the kernel until Calico removes them, so Services keep working in
between:

```text
kubectl -n kube-system patch ds kube-proxy --patch-file networking/ebpf/kube-proxy-off.yaml
kubectl patch installation.operator.tigera.io default --type merge --patch-file networking/ebpf/dataplane-bpf.yaml
kubectl wait --for=condition=Progressing tigerastatus/calico --timeout=90s || echo "nothing to roll out"
kubectl wait --for=condition=Available tigerastatus/calico --timeout=300s
```

About two minutes. **What changed [on the worker]** - Calico removes
kube-proxy's leftover rules a few seconds after it reports Available, so wait
for that first:

```text
timeout 120 bash -c 'until [ "$(sudo iptables-save -t nat | grep -c "^:KUBE-SVC-")" = 0 ]; do sleep 2; done'
sudo iptables-save -t nat | grep -c "^:KUBE-SVC-"
sudo iptables-save | grep -c "^:cali-"
```

kube-proxy's chains: **43 -> 0**. Calico's: **138 -> 16** (the policies now
live in BPF programs). On a pod interface, the first one `ip` lists:

```text
IF=$(ip -o link | awk -F': ' '/cali[0-9a-f]+@/ {split($2, a, "@"); print a[1]; exit}')
sudo tc filter show dev ${IF} ingress
```

`bpf ... name cali_tc_preambl` - the program every packet from that pod now
runs through. The Service table moved into a BPF map:

```text
CN=$(kubectl -n calico-system get pods -l k8s-app=calico-node --field-selector spec.nodeName=worker -o jsonpath='{.items[0].metadata.name}')
kubectl -n calico-system exec ${CN} -c calico-node -- calico-node -bpf nat dump
```

(Straight after the switch it can answer `Failed to dump NAT maps ... no such
file or directory`: the maps are created a few seconds after Calico reports
`Available`. Run it again.)

```text
10.108.193.81 port 80 proto 6 id 8 count 2 local 2
	8:0	 192.168.171.114:8080
	8:1	 192.168.171.119:8080
```

checkout's ClusterIP and its two endpoints - what `KUBE-SVC-*` used to say.

**What still works:** everything the webshop checks (`helm test`,
`python3 tools/lab.py verify`: 24/24, `check-dashboards`: 0 errors), the
tester's allowed and denied paths from Module 8, Services, and the NodePort,
Ingress and Gateway paths - **through the node addresses**.
`curl http://localhost:30080/` fails on every node in eBPF mode: Calico does
not serve NodePorts on localhost, which is why this track uses `10.0.1.x`.

**What looks different:**

- *Client addresses.* Module 5's requests from the master now all arrive as
  `192.168.219.64`, even the one sent to the *worker's* NodePort: Calico
  resolves the Service on the sending node and sends the packet straight to
  the pod, so the worker's NodePort never handles it. (A client outside the
  cluster would show the difference best; the security group keeps that out
  of reach.)
- *Policy drops.* Run exercise 9 again (`python3 tools/lab.py fault apply
  policy-drop`). The telemetry evidence is identical - the users and the
  application cannot tell which dataplane dropped the packets. On the node the
  iptables counters are gone; ask Calico instead, for the fulfillment pod's
  interface. calico-node shares the worker's network, so its `ip route get`
  names the interface that leads to the pod:

  ```text
  CN=$(kubectl -n calico-system get pods -l k8s-app=calico-node --field-selector spec.nodeName=worker -o jsonpath='{.items[0].metadata.name}')
  FUL=$(kubectl -n webshop get pods -l app.kubernetes.io/component=fulfillment -o jsonpath='{.items[0].status.podIP}')
  IF=$(kubectl -n calico-system exec ${CN} -c calico-node -- ip -o route get ${FUL} | awk '{print $3}')
  kubectl -n calico-system exec ${CN} -c calico-node -- calico-node -bpf counters dump --iface=${IF}
  ```

  (PowerShell: `$IF = (kubectl -n calico-system exec $CN -c calico-node -- ip -o route get $FUL).Split(' ')[2]`.)

  `Dropped ... by policy` climbed from 69 to 129 in 15 seconds - in the
  `INGRESS` column, because traffic *leaving* the pod *enters* the host side
  of its interface. `python3 tools/lab.py fault reset` when done.

**What it cost:** the switch broke the connections in flight. In the
90 seconds around it about 6 of 83 journeys went bad (`checkout_failed` and
`deadline_exceeded`) - Grafana's SLI panel dips to ~89% for a minute, and the
load generator's warnings in Loki spike at the same moment. No pod restarted,
and nothing needed a hand: `verify` passed right after.

### Where it comes from: who switched the dataplane

You patched three things; the operator did the rest. Run this in eBPF mode,
and again after switching back:

```text
echo "--- the operator's input: the Installation resource"; kubectl get installation default -o jsonpath='dataplane={.spec.calicoNetwork.linuxDataplane}  version={.status.calicoVersion}{"\n"}'
echo "--- what the operator renders from it"; kubectl -n calico-system get daemonset calico-node -o jsonpath='calico-node is owned by {.metadata.ownerReferences[0].kind}/{.metadata.ownerReferences[0].name}{"\n"}'
echo "--- Felix's eBPF settings"; kubectl get felixconfiguration default -o jsonpath='bpfConnectTimeLoadBalancing={.spec.bpfConnectTimeLoadBalancing}  bpfHostNetworkedNATWithoutCTLB={.spec.bpfHostNetworkedNATWithoutCTLB}{"\n"}'
echo "--- kube-proxy's node selector"; kubectl -n kube-system get daemonset kube-proxy -o jsonpath='{.spec.template.spec.nodeSelector}{"\n"}'
echo "--- Calico's own address for the API server (step 1)"; kubectl -n tigera-operator get configmap kubernetes-services-endpoint -o jsonpath='{.data}{"\n"}' 2>/dev/null || echo "not set yet (step 1)"
```

- **The Installation resource** is the Tigera operator's input: you set
  `linuxDataplane` there (step 4), not on calico-node.
- **calico-node is owned by `Installation/default`**: the operator generates
  the DaemonSet from it and restarts it on every change - which is why the
  waits watch the operator's `tigerastatus`, not the DaemonSet.
- **FelixConfiguration** holds Felix's tunables; step 2 changed
  `bpfConnectTimeLoadBalancing` there. (In the lab run its `bpfEnabled`
  field still read `false` while eBPF mode was running - judge the dataplane
  by the Installation's `linuxDataplane`, or by Felix's log line
  `BPF enabled`, not by this field.)
- **kube-proxy's node selector** gains `non-calico: "true"` while eBPF runs -
  the DaemonSet still exists, it just matches no node.
- **`kubernetes-services-endpoint`** is step 1: `10.0.1.10:6443`, Calico's way
  to the API server that does not depend on a Service.

**Back to iptables** - in this order, and **wait between the two halves**:
first take Calico out of eBPF mode on every node, only then bring kube-proxy
back.

```text
kubectl patch installation.operator.tigera.io default --type merge --patch-file networking/ebpf/dataplane-iptables.yaml
kubectl wait --for=condition=Progressing tigerastatus/calico --timeout=90s || echo "nothing to roll out"
kubectl wait --for=condition=Available tigerastatus/calico --timeout=300s
kubectl -n kube-system patch ds kube-proxy --type json --patch-file networking/ebpf/kube-proxy-on.json
kubectl -n kube-system rollout status ds/kube-proxy --timeout=180s
kubectl patch felixconfiguration default --type merge --patch-file networking/ebpf/felix-ctlb-default.yaml
```

Why the wait matters - found the hard way on the lab: while a node's Calico
is still in eBPF mode it keeps *removing* kube-proxy's rules (that is how it
replaced them in step 4). Re-enable kube-proxy too early and Calico deletes
its fresh hook rules again on that node: the `KUBE-SVC-*` chains are there,
but the `PREROUTING`/`OUTPUT` jumps into `KUBE-SERVICES` are not, so **every
ClusterIP on that node silently stops working** - DNS included - while pod
IPs still answer. Check both nodes **[on each node]**; each must print `3`:

```text
sudo iptables-save -t nat | grep -cE '^-A (PREROUTING|OUTPUT|POSTROUTING) .*KUBE'
```

A node printing `1` (only `POSTROUTING`) has lost them; restarting kube-proxy
makes it write them again at start-up:

```text
kubectl -n kube-system rollout restart daemonset kube-proxy
kubectl -n kube-system rollout status daemonset kube-proxy --timeout=120s
```

Then `python3 tools/lab.py verify` passes again; switching back also costs a
short SLI dip (about 7 of 97 journeys bad, mostly `deadline_exceeded`). The
ConfigMap from step 1 can stay. Section 2 of
[nat-and-conntrack.md](nat-and-conntrack.md) explains the hooks.

**The takeaway** is the observability one: a dataplane change is invisible in
every Service and policy object - `kubectl get` shows the same cluster before
and after - and visible in exactly two places: the node's tools, and the
users' SLI. Only one of those tells you whether it mattered.

## Module 10 - Cleanup

Back to the webshop as the quick start left it. If Module 9 was left in eBPF
mode, run its "Back to iptables" block first. Then, **in this order**:

```text
python3 tools/lab.py fault reset
helm upgrade webshop ./chart -n webshop -f values/values-aws.yaml --wait --timeout 10m
kubectl delete -f networking/gateway/ --ignore-not-found
kubectl delete -f https://github.com/envoyproxy/gateway/releases/download/v1.4.2/install.yaml --ignore-not-found
kubectl delete -f https://raw.githubusercontent.com/kubernetes/ingress-nginx/controller-v1.13.3/deploy/static/provider/baremetal/deploy.yaml --ignore-not-found
kubectl delete -f networking/tester.yaml --ignore-not-found
kubectl delete -f networking/dns-quiz.yaml --ignore-not-found
```

The `helm upgrade` without the overlays turns the storefront back into a
ClusterIP Service and removes the Ingress and the HTTPRoute - and it has to
come **before** Envoy Gateway is deleted. Envoy Gateway's manifest carries the
Gateway API CRDs; delete them first and Helm can no longer read its own
release, which still contains an HTTPRoute:
`UPGRADE FAILED: unable to build kubernetes objects from current release manifest: ... no matches for kind "HTTPRoute"`.
(If that happens: apply Envoy Gateway's manifest again, run the upgrade, then
delete it.) Removing the webshop itself is README section 11.

---

## What each module showed, and where

| Module | Seen on the node | Seen in the telemetry |
|---|---|---|
| 1 Pod IPs | per-node pod CIDR blocks | - |
| 2 ClusterIP | `KUBE-SVC` random split, one conntrack entry per connection | per-pod request split, with and without keep-alive |
| 3 CoreDNS | DNS flows across the tunnel, 3.4x with `ndots:5` | client p50 +7 ms against an unchanged server p50 |
| 4 The chain | one conntrack entry per synchronous hop | one trace across six services, including the queue |
| 5 NodePort | `KUBE-EXT` chains, masquerade in conntrack | the client address each policy shows the pod |
| 6-7 Ingress, Gateway | two flows; the proxy goes straight to pod IPs | per-request balancing over pooled connections |
| 8 NetworkPolicy | `cali-*` chains, drop counters, no conntrack entry | timeouts with no server span (exercise 9) |
| 9 eBPF | NAT and policy move into BPF maps and programs | the SLI dip of each switch; nothing else changes |

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `wget: download timed out` from the tester | a NetworkPolicy - did `networking/tester.yaml` get applied (and its `tester-egress` re-applied after Module 8)? Is the target one of storefront, checkout, inventory, payment? |
| Only one storefront pod | the overlay was not applied, or its HPA has no CPU metrics yet (a new pod for the first minute): an HPA without metrics does not rescale, not even to its minimum |
| `helm upgrade` fails: `conflict with "kubectl-patch"` | a field of a Helm-managed object was changed with `kubectl patch`/`edit`; Helm 4 applies server-side and will not take it over silently. Set it through the chart's values and add `--force-conflicts` once |
| `UPGRADE FAILED: unable to build kubernetes objects from current release manifest: ... no matches for kind "HTTPRoute"` | Envoy Gateway (and with it the Gateway API CRDs) was deleted while the release still had its HTTPRoute. Apply Envoy Gateway's manifest again, run the `helm upgrade`, then delete it - Module 10's order |
| `no matches for kind "HTTPRoute"` | `networking/values-gateway.yaml` used before Envoy Gateway (Module 7) installed the Gateway API CRDs |
| Ingress or Gateway curl times out | the controller's namespace is missing from `networkPolicy.ingressControllerNamespaces` (the overlay sets both) |
| Gateway `PROGRAMMED False (AddressNotAssigned)` | `networking/gateway/proxy-config.yaml` not applied: without it Envoy Gateway asks for a LoadBalancer Service the lab cannot provide |
| `localhost:30080` fails, node IP works | eBPF mode (Module 9): Calico does not serve NodePorts on localhost |
| Module 9: calico-node `PANIC ... failed to attach connect-time load balancer` | step 2 was skipped. Revert at once (the "Back to iptables" block; kube-proxy's leftover rules keep Services up meanwhile), apply `felix-ctlb-off.yaml`, switch again |
| After switching back from eBPF: ClusterIPs (and DNS) fail on one node, pod IPs still work | kube-proxy was re-enabled before Calico left eBPF mode there, and Calico removed its hook rules. `kubectl -n kube-system rollout restart daemonset kube-proxy` - Module 9, "Back to iptables" |
| `kube-proxy-on.json`: `the request is invalid` | kube-proxy was not switched off (there is no `non-calico` selector to remove) - nothing to do |
| `curl` from your laptop to a NodePort fails | the security group admits only 6443/22; use a node shell |

---

> **Next:** [opentelemetry.md](opentelemetry.md), step 7: OpenTelemetry
