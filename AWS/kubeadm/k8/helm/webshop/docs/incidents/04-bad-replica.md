# Incident 4 - Bad replica

> **Step 5 of 8: Incident and blind exercises** · Exercise 4 of 9 · Run from `AWS/kubeadm/k8/helm/webshop/` · Needs: the webshop (step 4)

**Start:** `python3 tools/lab.py fault apply bad-replica` (checkout runs two
replicas by default; the preset targets the first by pod name - pick another
with `--replica 1` or `--instance <pod>`)
**Injected (known-fault exercise):** one checkout pod calls inventory with a
1 ms timeout instead of 2 s - less than the fastest inventory call, so every
attempt it makes is abandoned. The kind of drift one misconfigured instance
produces. The other pod is untouched.

Numbers quoted below were observed on the lab-shaped Kind cluster during the
verification run (1 journey/s, 2-minute windows).
The same exercise on the AWS lab showed the same evidence with somewhat
different counts; healthy latencies there are 3-6x higher on its t3 CPU (AWS
results: [../verification.md](../verification.md)).

## What the user experiences

Every order placed through that pod fails at checkout
(`inventory_unavailable`); orders through the other pod are fine. From the
outside it looks random: about half of all checkouts fail, because each
request opens a new connection and kube-proxy spreads connections evenly.

## Where to begin

**Webshop / User reliability** shows the impact (observed: SLI **49.1%**, all
of it **`checkout_failed`** - 57 of 110 journeys in two minutes). The useful
dashboard is **Service health -> Per replica**: one checkout pod looks
different from its sibling.

## Queries and navigation

1. Compare the replicas:

   ```promql
   (sum by (k8s_pod_name) (rate(http_server_request_duration_seconds_count{service_name="checkout", http_response_status_code=~"5.."}[2m])) or 0 * sum by (k8s_pod_name) (rate(http_server_request_duration_seconds_count{service_name="checkout"}[2m]))) / sum by (k8s_pod_name) (rate(http_server_request_duration_seconds_count{service_name="checkout"}[2m]))
   histogram_quantile(0.95, sum by (le, k8s_pod_name) (rate(http_server_request_duration_seconds_bucket{service_name="checkout", http_route="/orders"}[2m])))
   sum by (k8s_pod_name) (increase(webshop_dependency_timeouts_total{webshop_dependency="inventory"}[2m]))
   ```

   Observed: the targeted pod (`...-77q2j`) answers **51%** of all its
   requests with 5xx - every order fails, while its order-status reads (which
   do not call inventory) still succeed - and its order p95 is **239 ms** (two
   abandoned attempts plus the retry back-off); its sibling (`...-lzrgj`)
   **0%** and **25 ms**. All **117** inventory timeouts in two minutes - two
   per failed order - come from the targeted pod.

2. Same build, same deployed configuration? The *Running instances* table:

   ```promql
   count by (k8s_pod_name, service_version, webshop_config_revision) (target_info{job="webshop/checkout"})
   ```

   Both pods report the same `service_version` and the same
   `webshop_config_revision`. So it is not a different image and not a
   different deployed configuration.

3. What is different, then? Every dependency span records the timeout it
   used:

   ```traceql
   { name = "inventory.reserve" && span.webshop.dependency.timeout_ms < 1000 }
   ```

   Observed: **95** traces in the three faulty minutes. Every match has
   `webshop.dependency.timeout_ms = 1` - only on that pod's spans (its
   `k8s.pod.name` resource attribute) - while the other pod's spans say
   2000. Inventory itself is healthy: its server spans take a few ms and
   succeed; checkout gave up on them.

4. The logs agree: `{service_name="checkout"} |= "inventory reservation failed"`
   - **100** records in four minutes, every one from the same `k8s_pod_name`,
   each with `webshop_outcome="timeout"`.

## Evidence and scope

| Claim | Evidence |
|---|---|
| Users affected only through one replica | errors and timeouts by pod |
| Not the dependency | inventory server latency/errors normal |
| Not a different version or deployed config | equal `service_version` and `webshop_config_revision` |
| The pod uses a much shorter inventory timeout | `webshop.dependency.timeout_ms = 1` on its spans |

**A subtler variant, seen while tuning this exercise:** with 60 ms *nothing
failed* - inventory answers in a few ms, so the shorter timeout never
tripped. With 5 ms - about a typical call - it failed in one run and not in
the next, once DNS lookups had become faster (see *Problems found* in
[../verification.md](../verification.md)): a timeout close to normal latency
is a coin toss that depends on the environment. Either way the SLI can stay
at 100% while the spans' `webshop.dependency.timeout_ms` shows the drift. That is latent
misconfiguration: harmless until inventory slows down a little, then one pod
fails and the other does not. Recording the effective setting on every call is
what lets you find it before it matters (`{ span.webshop.dependency.timeout_ms < 1000 }`).

**How much one bad pod hurts depends on balancing.** With connection reuse
(`app.httpKeepalive: true`) a caller's requests stick to whichever pod holds
its connection - in the lab one checkout pod received 98 of 99 orders - so
the same fault breaks nearly everything or nearly nothing, and the per-pod
panels are lopsided even when all is well.

**What this does not prove:** how the setting got there. The deployed
configuration revision is the same, so this is runtime drift the revision hash
cannot see - which is exactly why recording the *effective* timeout on every
call is worth its few bytes. Without that attribute, you would know *which* pod
and *what* fails, not *why*.

## Recovery

```text
python3 tools/lab.py fault reset
```

Observed two minutes later: no inventory timeouts, SLI **100%**, both pods'
p95 back to 30-40 ms. (Replacing the bad pod - `kubectl -n webshop delete pod <pod>` -
would also have fixed it in real life: a fresh pod starts from the deployed
configuration.)

---

> **Next:** [Incident 5, Incorrect fulfillment](05-incorrect-fulfillment.md)
