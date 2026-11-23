# Incident 6 - Insufficient capacity

> **Step 5 of 8: Incident and blind exercises** · Exercise 6 of 9 · Run from `AWS/kubeadm/k8/webshop/` · Needs: the webshop (step 4)\
> [All steps](../../../../../../README.md#learning-path) · [All exercises](../../README.md#5-incident-exercises) · Previous: [Incident 5, Incorrect fulfillment](05-incorrect-fulfillment.md) · Next: [Incident 7, Worker backlog](07-worker-backlog.md)

**Start:** `python3 tools/lab.py fault apply traffic-surge`
**Injected (known-fault exercise):** nothing breaks. The synthetic traffic
rises from 1 to 30 journeys per second - more than the initial replicas were
sized for, and more than this two-vCPU node can serve at full speed. The HPAs (CPU, 70% of request) are free to react within their
bounds: storefront 1-2, checkout 2-3, inventory 1-2, payment 1-2.

Numbers quoted below were observed on the lab-shaped Kind cluster during the
verification run (2 vCPU worker; 1-minute windows, captured 60 s and 180 s
into the surge).

**On the AWS lab it is starker.** The same surge on the t3.medium worker
never recovered while it lasted: the load generator reached only 6.7
journeys/s, and every journey ended `deadline_exceeded` - in the first minute
and still after three - with the worker node at 1.88 of 2 cores and every HPA
at its maximum. Slower cores put the node's ceiling much lower. The lesson is
the same, only clearer: once the node itself is the bottleneck, the
autoscalers have nothing left to give.

## What the user experiences

For the first minute, the shop gets slow: pages that took tens of
milliseconds take most of a second, and many orders are fulfilled after the
5 s deadline. Then, as the autoscalers add replicas, it recovers - slower than
usual, but within the deadline - while the node runs close to its limit.

## Where to begin

**Webshop / User reliability**: *Synthetic rate (target)* jumps to 30, journey
p95 rises to **6.0 s** in the first minute, and **596 of 824** journeys in that
minute end `deadline_exceeded`. Two minutes later the bad journeys have
stopped (760 good in a minute, none bad) but journey p95 is still **4.65 s**
(normal: 0.8 s) - just inside the 5 s deadline. Then **Kubernetes and scaling**.

Look at *Synthetic client* too: the evaluated rate is **14/s** after one
minute and **13/s** after three, not 30. The load generator is a pod with a
300m CPU limit, and it is itself throttled (36%, later 52%) on a node that is
by then at its 2-core ceiling. (An earlier run, while service clients still
reused connections, reached 18/s with 57% good journeys in the first minute -
a new connection per request costs CPU, and a saturated node shows it.) A probe that
shares the cluster's CPU saturates with it - it under-generates load, and some
of its early deadline misses include its own starvation. The server-side
latency below shows the users' pain was real, but keep that caveat in mind.

## Queries and navigation

1. Saturation per pod - CPU against its limit, and throttling:

   ```promql
   sum by (pod) (rate(container_cpu_usage_seconds_total{namespace="webshop", container!="", container!="POD"}[2m]))
   sum by (pod) (rate(container_cpu_cfs_throttled_periods_total{namespace="webshop", container!=""}[2m])) / sum by (pod) (rate(container_cpu_cfs_periods_total{namespace="webshop", container!=""}[2m]))
   ```

   After 60 s: storefront throttled **45%** of CFS periods, checkout 15%,
   loadgen 36%; storefront p95 **2.05 s** on `/api/checkout` and **0.70 s** on
   `/api/orders/{order_id}` (normal: ~40 ms and ~10 ms).

2. What did the autoscalers decide, and did it happen?

   ```promql
   max by (horizontalpodautoscaler) (kube_horizontalpodautoscaler_status_desired_replicas{namespace="webshop"})
   max by (horizontalpodautoscaler) (kube_horizontalpodautoscaler_status_current_replicas{namespace="webshop"})
   sum by (pod) (kube_pod_status_phase{namespace="webshop", phase="Pending"}) > 0
   ```

   Within about 70 s every HPA reached its maximum and stayed there:
   checkout 3, storefront 2, inventory 2, payment 2 - desired = current, no
   Pending pods. The *Kubernetes events* panel shows the decisions as they
   happened:

   ```logql
   {service_name="kubernetes-events"} |= "Rescale"
   ```

3. Is the node itself the limit? Node CPU (all pods, every namespace):

   ```promql
   sum by (node) (rate(node_cpu_usage_seconds_total[2m]))
   ```

   Worker: **1.10** cores after 60 s (the containers' own limits were the
   bottleneck), **1.99** of 2 after 180 s (now the node itself is the
   ceiling).

4. After scaling: compare latency and throttling before and after the new
   replicas became ready (the *HPA: desired vs current* panel marks the moment).

## Evidence and scope

| Claim | Evidence |
|---|---|
| Demand rose beyond the initial replicas | target rate 30/s; HPAs at their maximums within a minute |
| Saturation, not malfunction | throttling, latency and deadline misses rise together; no errors, no restarts |
| The autoscalers helped, up to a point | bad journeys stop once the replicas are ready; latency stays 3x normal |
| Where the ceiling is | first the containers' CPU limits (throttling with the node at 1.1 cores), then the node (1.99 of 2 cores with every HPA at its maximum) |

**A smaller surge for comparison:** at 15 journeys/s the same HPAs scaled to
their maximums and the SLI never dropped (journey p95 0.90 s vs 0.83 s) - the
initial replicas were too few, the maximums were enough.

**What this does not prove:** that more replicas would always help - on a
two-vCPU node, replicas share the same cores. Horizontal scaling moves the
bottleneck from a container's CPU limit to the node's capacity; past that
point only more nodes (or cheaper requests) help, and the scheduler's Pending
pods say so.

## Recovery

```text
python3 tools/lab.py fault reset
```

Observed 2.5 minutes after the reset: journey p95 **0.50 s**, storefront
`/api/checkout` p95 49 ms, worker CPU 0.30 cores. The HPAs scale back down after their 2-minute stabilization
window, one pod per minute.

---

> [All steps](../../../../../../README.md#learning-path) · [All exercises](../../README.md#5-incident-exercises) · Previous: [Incident 5, Incorrect fulfillment](05-incorrect-fulfillment.md) · Next: [Incident 7, Worker backlog](07-worker-backlog.md)
