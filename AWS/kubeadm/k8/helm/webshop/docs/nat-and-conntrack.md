# Reading NAT rules and conntrack, from first principles

> **Part of step 6: Networking on the webshop**, background reading · Commands run on a node and only read · Needs: the webshop (step 4)

The networking track's "Under the hood" sections show kube-proxy's NAT rules
and conntrack entries. This page explains how to read them - from what a
packet goes through on a node, to every field of a rule and of a conntrack
entry. Every rule and entry below was captured on the AWS lab (kube-proxy in
iptables mode, Calico on its iptables dataplane); your hashes and addresses
will differ, the structure will not.

All commands here run **[on the worker]** (or **[on the master]** where
marked) and only read.

---

## 1. The problem kube-proxy solves

A ClusterIP such as `10.107.168.248` (checkout's) belongs to no interface,
no pod and no node. Nothing listens on it. Yet a pod that connects to
`10.107.168.248:80` reaches a checkout pod at `192.168.171.102:8080`. Something
on the node has to **rewrite the destination** of that connection - that is
**DNAT** (destination NAT) - and, in some cases, also rewrite the **source** -
**SNAT** (source NAT; Kubernetes uses its `MASQUERADE` form). kube-proxy's
job is to write the rules that do this into the Linux kernel's packet filter,
**netfilter**, on every node.

## 2. Netfilter in one picture

Every packet passes a few fixed **hooks** inside the kernel. At each hook, the
rules of a **table** are consulted - the `nat` table for address rewriting,
the `filter` table for accepting and dropping. Rules live in **chains**:
ordered lists, read top to bottom, where a rule can jump to another chain.

```text
 packet from a pod or the network               packet created on this node
              |                                  (curl on the node, kubelet)
              v                                              |
       [ PREROUTING ]  nat: DNAT happens here           [ OUTPUT ]  nat: DNAT for local traffic
              |                                              |
       routing decision: local, or forward?          routing decision
              |                                              |
       [ FORWARD ]  filter: Calico's policies             |
              |                                              |
              +----------------------+-----------------------+
                                     v
                             [ POSTROUTING ]  nat: SNAT / MASQUERADE happens here
                                     |
                                     v
                     out to a pod (veth), or to the other node (VXLAN)
```

kube-proxy hooks into exactly three of them - one rule each, the entry points
to everything else:

```text
sudo iptables-save -t nat | grep -E '^-A (PREROUTING|OUTPUT|POSTROUTING) .*KUBE'
```

```text
-A PREROUTING -m comment --comment "kubernetes service portals" -j KUBE-SERVICES
-A OUTPUT -m comment --comment "kubernetes service portals" -j KUBE-SERVICES
-A POSTROUTING -m comment --comment "kubernetes postrouting rules" -j KUBE-POSTROUTING
```

Every packet arriving or created on the node is sent to `KUBE-SERVICES`
(DNAT candidates); every packet leaving goes through `KUBE-POSTROUTING`
(SNAT candidates).

## 3. How to read one rule

`iptables-save` prints each rule as the command that would add it. Take the
first rule a packet to checkout's ClusterIP meets:

```text
-A KUBE-SERVICES -d 10.107.168.248/32 -p tcp -m comment --comment "webshop/webshop-checkout:http cluster IP" -m tcp --dport 80 -j KUBE-SVC-3LRHZRJFPN53GT2Z
```

| Part | Meaning |
|---|---|
| `-A KUBE-SERVICES` | this rule is **A**ppended to chain `KUBE-SERVICES` |
| `-d 10.107.168.248/32` | match: **d**estination address is the ClusterIP |
| `-p tcp` ... `-m tcp --dport 80` | match: TCP, destination port 80 (the Service port) |
| `-m comment --comment "..."` | no match at all - a label kube-proxy adds: namespace/Service:port. **This is what you grep for** |
| `-j KUBE-SVC-3LRHZRJFPN53GT2Z` | action: **j**ump to that chain. Its name is a hash of the Service, port and protocol - stable, but unreadable |

A rule's action (its *target*) is either another chain, or one of a few
built-ins: `DNAT --to-destination IP:port` (rewrite the destination),
`MARK` (tag the packet), `MASQUERADE` (rewrite the source to the outgoing
interface's address), `RETURN` (leave this chain, continue where it was
called from). Matches are ANDed; if a rule does not match, the next rule in
the chain is tried.

## 4. The chain tree for one ClusterIP

All rules for checkout, on the worker:

```text
sudo iptables-save -t nat | grep "webshop/webshop-checkout"
```

```text
-A KUBE-SEP-HZPIFXNCD6O4SQS5 -s 192.168.171.120/32 -m comment --comment "webshop/webshop-checkout:http" -j KUBE-MARK-MASQ
-A KUBE-SEP-HZPIFXNCD6O4SQS5 -p tcp -m comment --comment "webshop/webshop-checkout:http" -m tcp -j DNAT --to-destination 192.168.171.120:8080
-A KUBE-SEP-LY44EDMY6OPBBO2J -s 192.168.171.102/32 -m comment --comment "webshop/webshop-checkout:http" -j KUBE-MARK-MASQ
-A KUBE-SEP-LY44EDMY6OPBBO2J -p tcp -m comment --comment "webshop/webshop-checkout:http" -m tcp -j DNAT --to-destination 192.168.171.102:8080
-A KUBE-SERVICES -d 10.107.168.248/32 -p tcp -m comment --comment "webshop/webshop-checkout:http cluster IP" -m tcp --dport 80 -j KUBE-SVC-3LRHZRJFPN53GT2Z
-A KUBE-SVC-3LRHZRJFPN53GT2Z ! -s 192.168.0.0/16 -d 10.107.168.248/32 -p tcp -m comment --comment "webshop/webshop-checkout:http cluster IP" -m tcp --dport 80 -j KUBE-MARK-MASQ
-A KUBE-SVC-3LRHZRJFPN53GT2Z -m comment --comment "webshop/webshop-checkout:http -> 192.168.171.102:8080" -m statistic --mode random --probability 0.50000000000 -j KUBE-SEP-LY44EDMY6OPBBO2J
-A KUBE-SVC-3LRHZRJFPN53GT2Z -m comment --comment "webshop/webshop-checkout:http -> 192.168.171.120:8080" -j KUBE-SEP-HZPIFXNCD6O4SQS5
```

`iptables-save` prints chains alphabetically, not in the order a packet sees
them. In packet order, the same eight lines form a tree:

```text
PREROUTING / OUTPUT
 └─ KUBE-SERVICES                 "is it for 10.107.168.248:80?"           (one rule per Service port)
     └─ KUBE-SVC-3LRH...          the Service: choose an endpoint
         ├─ ! -s 192.168.0.0/16 -> KUBE-MARK-MASQ    not from a pod? mark for SNAT, keep going
         ├─ 50% random  -> KUBE-SEP-LY44...          endpoint 1
         │                   ├─ -s 192.168.171.102 -> KUBE-MARK-MASQ   (hairpin, see below)
         │                   └─ DNAT -> 192.168.171.102:8080
         └─ otherwise   -> KUBE-SEP-HZPI...          endpoint 2
                             ├─ -s 192.168.171.120 -> KUBE-MARK-MASQ
                             └─ DNAT -> 192.168.171.120:8080
POSTROUTING
 └─ KUBE-POSTROUTING              marked? MASQUERADE (section 5)
```

- **`KUBE-SVC-*` = the Service, `KUBE-SEP-*` = one endpoint** ("service
  endpoint"), one per pod address in the Service's EndpointSlice. When a pod
  is added or removed, kube-proxy rewrites exactly these chains.
- **The random split is a cascade.** With 2 endpoints the first rule takes
  half (`--probability 0.5`), the last takes whatever is left. With 3 it
  would read `0.333`, then `0.5`, then the rest - every endpoint gets an equal
  share. It is evaluated **once per connection** (section 6): this is the
  "connections, not requests" of Module 2.
- **`! -s 192.168.0.0/16 -> KUBE-MARK-MASQ`**: `192.168.0.0/16` is the pod
  network (`clusterCIDR` in kube-proxy's config, Module 5). A connection that
  did *not* come from a pod - from the node itself, say - is marked to be
  source-NATed too.
- **The `-s <pod IP>` rule in each `KUBE-SEP`** is the *hairpin* case: a pod
  that connects to its own Service and is sent to itself. Without SNAT the
  pod would see a packet from its own address and answer itself directly,
  bypassing the NAT that has to be undone; with SNAT the reply goes back
  through the node.
- **The DNAT** rewrites address *and* port: Service port 80 becomes the pod's
  8080 - the number the EndpointSlice controller resolved from
  `targetPort: http` (Module 2, "Named ports").

## 5. Masquerade: a mark now, the rewrite later

Source NAT can only happen at POSTROUTING, after the routing decision, but
kube-proxy decides about it earlier - so it tags the packet and acts later:

```text
sudo iptables-save -t nat | grep -E '^-A KUBE-MARK-MASQ|^-A KUBE-POSTROUTING'
```

```text
-A KUBE-MARK-MASQ -j MARK --set-xmark 0x4000/0x4000
-A KUBE-POSTROUTING -m mark ! --mark 0x4000/0x4000 -j RETURN
-A KUBE-POSTROUTING -j MARK --set-xmark 0x4000/0x0
-A KUBE-POSTROUTING -m comment --comment "kubernetes service traffic requiring SNAT" -j MASQUERADE --random-fully
```

1. `KUBE-MARK-MASQ` sets bit `0x4000` on the packet - nothing else.
2. In `KUBE-POSTROUTING`: no mark -> `RETURN`, leave the packet alone (most
   pod-to-pod traffic). Marked -> clear the mark, then `MASQUERADE`: replace
   the source address with the address of the interface the packet leaves
   through, and (`--random-fully`) pick a random source port.

**Why masquerade at all?** The reply must come back through the node that
did the DNAT, because only that node can undo it. A client outside the pod
network (a node, a NodePort client) would otherwise receive a reply from the
pod's address - a connection it never opened - and drop it.

## 6. conntrack: why the rules only see the first packet

The NAT rules above are evaluated for the **first packet of a connection
only** (the TCP SYN, the first UDP datagram). The kernel then records the
connection in its **connection tracking table** - conntrack - with both
directions' addresses, and translates every later packet, and every reply,
from that record. No rule is consulted again. That is why load balancing is
per connection, and why changing a rule never affects connections already
open.

List the connections to checkout's ClusterIP:

```text
CO=$(sudo iptables-save -t nat | awk '/^-A KUBE-SERVICES/ && /"webshop\/webshop-checkout:http cluster IP"/ {sub("/32", "", $4); print $4; exit}')
sudo conntrack -L -d ${CO}
```

You will see many lines - one per recent connection to checkout, since the
storefront calls it all the time (188 in the lab run, almost all
`TIME_WAIT`). Any line will do; one of them, broken down:

```text
tcp 6 118 TIME_WAIT src=192.168.171.85 dst=10.107.168.248 sport=60754 dport=80 packets=7 bytes=456 src=192.168.171.120 dst=192.168.171.85 sport=8080 dport=60754 packets=5 bytes=478 [ASSURED] mark=0 use=1
```

| Field | Value | Meaning |
|---|---|---|
| protocol | `tcp 6` | name and protocol number |
| timeout | `118` | seconds until the entry is forgotten (resets on traffic) |
| state | `TIME_WAIT` | TCP state as conntrack saw it: here, closed normally a moment ago |
| **original** direction | `src=192.168.171.85 dst=10.107.168.248 sport=60754 dport=80` | the packet *as the client sent it*: the tester, to the ClusterIP |
| counters | `packets=7 bytes=456` | client -> server |
| **reply** direction | `src=192.168.171.120 dst=192.168.171.85 sport=8080 dport=60754` | the packet *as the server sends its answer*: from the real checkout pod |
| counters | `packets=5 bytes=478` | server -> client |
| `[ASSURED]` | | traffic was seen both ways; the entry will not be evicted early |
| `mark=0 use=1` | | the connection mark; how many references the kernel holds |

**The trick for reading any entry:** compare the two directions.

- original `dst` != reply `src` -> **DNAT** happened: `10.107.168.248:80`
  became `192.168.171.120:8080`. Which endpoint the dice chose is in the reply.
- original `src` != reply `dst` -> **SNAT** happened. Here they are equal
  (`192.168.171.85` both times): pod to pod, no masquerade.

A masqueraded one - a NodePort request made on the master (next section):

```text
tcp 6 119 TIME_WAIT src=10.0.1.10 dst=10.0.1.10 sport=51940 dport=30080 ... src=192.168.171.98 dst=192.168.219.64 sport=8080 dport=61713 ... [ASSURED]
```

- DNAT: `10.0.1.10:30080` (the node's own NodePort) became storefront pod
  `192.168.171.98:8080`, on the *other* node.
- SNAT: the client `10.0.1.10:51940` became `192.168.219.64:61713` - the
  master's Calico tunnel address and a random port (`--random-fully`) - so the
  pod's reply comes back across the tunnel to the master, which undoes both.

**The states you will meet:**

| State | Means |
|---|---|
| `ESTABLISHED` | an open connection, packets both ways |
| `TIME_WAIT` | closed normally; kept for about two minutes |
| `SYN_SENT ... [UNREPLIED]` | the first packet went out, **nothing came back**: dropped somewhere after the NAT (a policy on the receiving pod - Module 5 and 8), or the pod is dead. The reply tuple is filled in (the NAT decision was made) but its counters stay at zero |
| UDP, with or without `[UNREPLIED]` | UDP has no handshake; entries simply age out (about 30 s unanswered, longer once answered) - which is how stale DNS entries outlive a replaced CoreDNS pod (Module 3) |
| *no entry at all* | the packet was dropped **before** conntrack committed it - for example by the sender's own egress policy (exercise 9) |

## 7. NodePort: one more chain in front

```text
sudo iptables-save -t nat | grep -E 'KUBE-NODEPORTS.*webshop-storefront|KUBE-EXT-.*webshop-storefront|^-A KUBE-SERVICES .*nodeports'
```

```text
-A KUBE-SERVICES -m comment --comment "kubernetes service nodeports; NOTE: this must be the last rule in this chain" -m addrtype --dst-type LOCAL -j KUBE-NODEPORTS
-A KUBE-NODEPORTS -p tcp -m comment --comment "webshop/webshop-storefront:http" -m tcp --dport 30080 -j KUBE-EXT-MX3FQJXDI6GSY32B
-A KUBE-EXT-MX3FQJXDI6GSY32B -m comment --comment "masquerade traffic for webshop/webshop-storefront:http external destinations" -j KUBE-MARK-MASQ
```

```text
PREROUTING / OUTPUT
 └─ KUBE-SERVICES
     ├─ ... one rule per ClusterIP (section 4) ...
     └─ last rule: destination is one of THIS node's addresses? -> KUBE-NODEPORTS
         └─ tcp dport 30080 -> KUBE-EXT-MX3F...       "external" traffic for the storefront
             ├─ KUBE-MARK-MASQ                        always SNAT (externalTrafficPolicy: Cluster)
             └─ KUBE-SVC-MX3F...                      the SAME chain the ClusterIP uses
```

A NodePort adds no new load balancing: it marks the packet for masquerade
and joins the Service's ordinary `KUBE-SVC` chain. With
`externalTrafficPolicy: Local` (Module 5) the `KUBE-EXT` chain changes: no
mark for traffic from outside, and only the node's own endpoints.

## 8. The same rules in nft

On this lab's Ubuntu, the `iptables` command writes its rules into the kernel's
newer framework, **nftables** ("iptables-nft"). `iptables-save` translates
them back into iptables syntax - the readable form used above. `nft` shows
the same rule natively, with live **counters**:

```text
sudo nft list chain ip nat KUBE-SERVICES | grep -m1 "$(sudo iptables-save -t nat | awk '/^-A KUBE-SERVICES/ && /"webshop\/webshop-checkout:http cluster IP"/ {sub("/32", "", $4); print $4; exit}')"
```

```text
meta l4proto tcp ip daddr 10.107.168.248  tcp dport 80 counter packets 69 bytes 4140 jump KUBE-SVC-3LRHZRJFPN53GT2Z
```

`packets 69` is how many *new connections* to checkout this rule has
matched (only first packets reach it - section 6). Run it twice: a counter
that does not move means no new connections reach this node for that
Service. (`nft` warns that these tables are "managed by iptables-nft, do not
touch": change them through iptables, never with `nft`.)

## 9. Which tool answers which question

| Question | Look at | Healthy | Unhealthy |
|---|---|---|---|
| Does the node know the Service? | `iptables-save -t nat \| grep "<ns>/<svc>"` | a `KUBE-SERVICES` rule for the ClusterIP | none: the Service does not exist, or kube-proxy is not running here |
| Does it have endpoints? | the `KUBE-SVC` chain's `KUBE-SEP` jumps | one per ready pod | none: no ready pods (EndpointSlice, readiness - Module 2) |
| Do new connections reach the rule? | `nft list chain ip nat KUBE-SERVICES` counters | rising | flat: the traffic never gets to this node |
| Was a connection NATed, and to which pod? | `conntrack -L -d <ClusterIP>` | reply `src` = a pod IP | no entry: dropped before NAT |
| Did the other side answer? | the entry's state and reply counters | `ESTABLISHED` / `TIME_WAIT`, reply packets > 0 | `SYN_SENT [UNREPLIED]`, reply packets = 0 |

In eBPF mode (Module 9) none of these chains exist: Calico implements the
Services in BPF programs, and `calico-node -bpf nat dump` takes the place of
section 4.

References:
[Virtual IPs and service proxies](https://kubernetes.io/docs/reference/networking/virtual-ips/),
[iptables(8)](https://man7.org/linux/man-pages/man8/iptables.8.html),
[iptables-extensions(8)](https://man7.org/linux/man-pages/man8/iptables-extensions.8.html) (`statistic`, `comment`, `mark`, `addrtype`),
[conntrack(8)](https://manpages.ubuntu.com/manpages/noble/man8/conntrack.8.html),
[nft(8)](https://manpages.ubuntu.com/manpages/noble/man8/nft.8.html).
