# Verification record

> **For maintainers:** the record of every check that was run; the lessons do not need it.\
> [All steps](../../../../../README.md#learning-path) · Back to the [webshop README](../README.md)

What was checked, where, and with what result: first on a Kind cluster
shaped like the lab, then on the AWS lab itself.

## Where it ran

| Environment | Status | Used for |
|---|---|---|
| **AWS kubeadm lab** (`AWS/kubeadm`, context `kubernetes-admin@kubernetes`, nodes `master`/`worker`) | executed on 2026-10-05, after the lab was rebuilt with `terraform apply` | install from a Docker Hub image, the full check list, all exercises, the blind scenarios, persistence, cleanup - see [Executed on the AWS lab](#executed-on-the-aws-lab-2026-10-05) |
| **Kind, shaped like the lab** (`tools/kind_cluster.py`, context `kind-webshop-lab`, nodes `webshop-lab-control-plane` (tainted) and `webshop-lab-worker`) | executed | full install, telemetry path, faults, scaling |
| Local Docker (PostgreSQL, RabbitMQ, Collector, Prometheus, Tempo, Loki containers) | executed | tests, pipeline probes before the chart existed |

The Kind cluster mirrors what matters about the lab: Kubernetes 1.33, Calico
v3.30.2 (Tigera operator, VXLAN, NetworkPolicy enforced), a control plane that
keeps its `NoSchedule` taint, a worker limited to 2 CPUs / 4 GiB with kubelet
reservations giving **2 CPU / 3.8 GiB allocatable** (the t3.medium budget), **no
StorageClass and no provisioner** (Kind's default removed), metrics-server
3.14.0 installed with `--kubelet-insecure-tls` as the README instructs. It
differs in: node names (handled by `values-kind.yaml`), image delivery
(side-loaded, not pulled from a registry), the API-server and node CIDRs, and
EC2's burstable CPU.

## Executed checks

### Application and tests

| Check | Result |
|---|---|
| `python -m pytest` - unit (SLI evaluator, retries/attempt spans, context propagation incl. outbox and AMQP, lab-control targeting/windows, dashboard generator sync, single export per signal) | **36 passed** |
| `python -m pytest` with PostgreSQL 17.11 + RabbitMQ 4.3.6 - integration (accepted vs fulfilled, trace continuity across the queue, checkout replay, declined card + compensation, reservation idempotency + tombstone, capture-key guard, duplicate/redelivered messages, crash after capture, concurrent outbox relays, immutable request) | **10 passed** (46 total) |
| Local end-to-end run: one journey, trace shape | 1 root span, all services, `order.fulfill` under `order.publish`; exemplars with `trace_id` in Prometheus; Loki structured metadata; Tempo TraceQL search by `webshop.order.id` |

### Chart and configuration (static)

| Check | Result |
|---|---|
| `helm dependency update/build ./chart` (Helm v4.3.0) | `Chart.lock` written; 5 upstream charts + local `observability` |
| `helm lint ./chart -f values/values-aws.yaml` | 0 failures (one `[INFO]` about a chart icon) |
| `helm template` with `values-aws.yaml`, `values-kind.yaml`, `values-existing-backends.yaml` | 97 / 97 / 43 objects; aws: 6 pre-bound local PVs, 22 NetworkPolicies |
| rendered Collector config: `otelcol-contrib validate` (0.161.0) | valid |
| rendered Prometheus config: `promtool check config --syntax-only`; rules: `promtool check rules` (3.15.0) | valid; 34 rules |

### Live on the lab-shaped Kind cluster

| Check | Result |
|---|---|
| install with `values/values-kind.yaml`, NetworkPolicies on | 16 pods Ready, 0 restarts, all 6 local PVs bound, migration Job completed |
| `helm test webshop -n webshop --logs` | Succeeded: journey `good`, fulfilled, requested SKU shipped, 1 capture |
| `python tools/lab.py verify` | **24 passed, 0 failed** (see the README's table for what each proves) |
| `python tools/lab.py check-dashboards` | every dashboard query runs; **0 errors** (empty results only where the condition is absent, e.g. no pending pods) |
| measured usage at 1 journey/s (final configuration, settled) | 16 webshop pods: ~150m CPU, ~1.6 GiB working set (largest: Tempo ~335 Mi, Prometheus ~230 Mi, Grafana ~220 Mi) |
| `helm uninstall` then `helm upgrade --install` onto the kept volumes | left behind on uninstall: all 6 PVCs and PVs (Bound) and the 3 generated Secrets; reinstall re-bound every claim to its own volume; Secret hashes unchanged; orders 32,923 before, 33,022 a minute after (data kept, traffic resumed); Grafana accepts the kept admin password on an admin-only API (200) and refuses a wrong one (403). The first attempt found the RabbitMQ cookie problem below; this is the run after the fix |
| storage growth, 22 min of steady state at the default rate (0.9 evaluated journeys/s), no faults | spans in = spans stored, 70.2/s (78 per journey); Tempo ~570 MiB/day (from the blocks written in the window - raw `du` is distorted by compaction), Prometheus ~180 MiB/day (blocks per covered hour; ~1,030 samples/s, 29.8k series), Loki ~30-40 MiB/day (370 B/s of chunks stored for 5.5 KB/s received), PostgreSQL ~2.5 KB per order, ~200 MiB/day. Every bounded store stays well inside its volume at its retention limit (README section 9). Retention *deletion* itself was not observed for Loki (72 h) or Prometheus (3 d) - the lab ran for hours, not days |
| Tempo cleanup of compacted blocks (after the crash loop) | the first retention pass ran 62 min after start-up (an hourly timer from process start) and deleted 291 compacted blocks: Tempo's directory 694 -> 425 MB. Steady state afterwards: 180 MiB of live blocks, 230 MiB of compacted blocks awaiting the next pass (about two hours of compaction inputs) |

### Fault exercises on Kind

All eight re-run on the final configuration (2026-10-05). Each preset ran for
3 minutes at the default load (unless the preset changes it), then
`fault reset`; evidence was captured with the queries in each
walkthrough. SLI = good / eligible journeys over the 2 minutes before capture.

| # | Exercise | SLI during | Decisive evidence observed | After reset (2.5 min) |
|---|---|---|---|---|
| 1 | slow-inventory | 80.4% (`checkout_failed`) | inventory `/reservations` p95 2.43 s with SQL p95 4.8 ms; 71 timed-out attempts; 45 idempotent replays | SLI 100%, checkout p95 ~30 ms |
| 2 | pool-exhaustion | 47.1% (`checkout_failed`, `deadline_exceeded`) | pool 5/5 on both pods, 7 waiting, 36 acquisition timeouts; hold p95 2.92 s vs SQL p95 4.8 ms, PostgreSQL connections all idle; 62 `charged_after_error` audits | SLI 100%, acquire wait p95 0.5 ms |
| 3 | retry-amplification | 85.9% | 1.74 / 1.84 attempts per authorize / capture; 29 capture replays = 29 capture timeouts; 0 duplicate charges | SLI 100%, 1.0 attempts per operation |
| 4 | bad-replica (1 ms inventory timeout on one pod) | 49.1% (`checkout_failed`, 57 of 110) | targeted pod 51% 5xx (every order; its order-status reads still succeed) and order p95 239 ms vs sibling 0% / 25 ms; all 117 inventory timeouts and 100 failure logs from that pod; same version and config revision on both; 95 traces with `timeout_ms = 1`, all from that pod. Re-run on the final configuration; earlier runs: 60 ms never tripped; 5 ms gave 70.1% with keep-alive skew, 56.6% balanced, and 100% once DNS was fixed (see below) | SLI 100%, both pods p95 30-40 ms |
| 5 | wrong-fulfillment | 67.9% (`wrong_item`) | 47 requested != shipped worker records = 47 `wrong_item` journeys = 47 traces from the TraceQL attribute comparison (0 after the reset); all six SKUs swapped to their sibling | SLI 100% |
| 6 | traffic-surge (30 journeys/s) | first minute 28% (`deadline_exceeded` 596 of 824), then 100% | storefront 45% throttled, `/api/checkout` p95 2.05 s before scale-out; every HPA at max within ~70 s; worker node 1.99 of 2 cores at 3 min; journey p95 4.65 s; load generator itself throttled (52%), evaluated 13/s of 30 (18/s, and 57% in the first minute, in an earlier run that still reused connections). (At 15/s: HPAs absorbed it, SLI 100%) | journey p95 0.50 s; HPAs scale down |
| 7 | worker-backlog | 0% (`deadline_exceeded`) | 56 ready / 70 s oldest (broker); wait p95 58 s vs process p95 1.47 s; in-progress max 1 | backlog drained in ~25 s; SLI 100% |
| 8 | duplicate-charges (bypass) | 59.6% (`duplicate_charge`) | 201 captures for 121 successful attempts (+79 timed out that had charged); 7 `late_duplicate_charge` audits | SLI 100%, 1 capture per order |

### Blind exercises on Kind (final configuration)

Each scenario was armed with `python tools/lab.py blind start --scenario <id>`
(hidden onset within 30 s); evidence was captured about 3.5 minutes after the
onset with the answer key's own queries, then `blind reveal` - instrumentation
fingerprint **unchanged** in all four runs - and `blind stop`. Every claim in
[answer-key/blind-answers.md](answer-key/blind-answers.md) was checked against
that evidence and the key rewritten where it was wrong; B1-B3 had never been
run before.

| Scenario | SLI during | Evidence confirmed | Corrected in the key |
|---|---|---|---|
| B1: slow inventory replica + serial worker | 30% (`deadline_exceeded` only) | slow pod `/reservations` p95 2.43 s vs 4.7 ms; all 89 slow `stock.reserve` spans from one pod, their SQL children < 1 ms; worker in-progress pinned at 1, waiting p95 9.5 s vs processing 1.5 s, `fulfillment.pick` ~1.05 s | the key promised `checkout_failed` and an unbounded queue: no checkout failed (2 of 162 reservation attempts timed out; their retries succeeded), and at the default rate the worker kept pace - the backlog stayed at 6 ready + 4 prefetched |
| B2: wrong variant + capture errors | 78% (`wrong_item`) | 43 of 206 fulfilment records with requested != shipped SKU; 41 traces from the TraceQL attribute comparison, always the sibling color; capture 106 `server_error` vs 151 `success`, authorize clean; 12 job retries; all 321 audits `clean` | the key promised late orders - there were none (capture retries within the job are quick); "RED stays green" holds only for the wrong items: payment shows 17% 5xx from the capture fault |
| B3: leaked payment pool + 4x traffic | 0% (`checkout_failed`) | both payment pods at 5/5 used with 12-15 waiting; 3,187 acquire timeouts; payment SQL rate 0; PostgreSQL 24 idle / 0 active; the payment pod the HPA added also at 5/5 | acquire spans end at the pool's own 2 s timeout, not "when the client gives up" (checkout gives up first, at 1.5 s); traffic rose 3-4x, not 4x everywhere |
| B4: mistuned checkout replica + slow commit | 33% (`checkout_failed` + `deadline_exceeded`) | mistuned pod 94 x 504 vs sibling 75 x 202; all 281 authorize timeouts and 94 `timeout_ms = 1` traces from that pod; payment saw ~122 of 356 attempts; commit p95 4.8 s vs reserve 5 ms, 89 commit timeouts, 93 replays | numbers now from this run (the earlier ones came from a separate probe, while Tempo was down) |

## Problems found by running it (and fixed)

| Found | Fix |
|---|---|
| **Every span and log record was exported twice.** FastAPI 0.142's native OpenTelemetry layer adds its own OTLP exporters from the `OTEL_*` environment at ASGI start-up; with the SDK already configured, each process ran two batch processors (`otel_sdk_processor_span_processed_total` by `otel_component_name` showed `batching_span_processor/0` and `/1`; SDK started 80 spans/s, exported 159/s; Tempo returned each span twice). Found by comparing SDK, Collector and Tempo counts after the exercises | `FastAPI(telemetry={"auto_configure": False, ...})` in every app incl. lab-control; `tests/test_single_export.py` drives the real ASGI lifespan and fails if any exporter is added (verified to fail without the fix). Re-checked live: 70/70 unique spans per journey trace |
| `FaultClient.active()` returns `{}` for a param-less fault, which is falsy: the idempotency bypass never engaged | `is_active()`; every check uses `is not None` |
| SDK default histogram buckets are millisecond-shaped; 0.25 s observations landed in `le=5` | explicit second-based buckets on every histogram |
| ASGI `http send/receive` sub-spans doubled span volume | `exclude_spans=["receive","send"]` |
| cAdvisor via the API-server node proxy: 403 (needs `nodes/proxy`, which also grants kubelet exec) | scrape kubelets directly with `nodes/metrics`; NetworkPolicy egress to the node CIDR |
| Collector 0.161 self-metrics lose their `_total`/unit suffixes by default | `without_type_suffix: false`, `without_units: false` |
| The Collector chart cannot checksum a ConfigMap from another chart: config edits were not applied | pod annotation with the release revision (restart on every upgrade) |
| SLI ratio "no data" (not 0) while healthy: the `bad` series did not exist yet | bounded outcome counters created at 0 at start-up |
| Loki PVC name differs from the guess (`storage-<release>-loki-0`) | PV claimRef corrected |
| `kind load docker-image` (kind 0.31) rejects containerd config v4 in kindest/node v1.33.12 | `kind_cluster.py load` streams `docker save` into `ctr images import` |
| Prometheus/Grafana PVCs (from Deployments) would be deleted by `helm uninstall`, leaving their kept PVs unbindable | `helm.sh/resource-policy: keep` on those PVCs |
| Grafana stores its admin password in its kept database; the chart's Secret was not kept | chart-owned, kept `<release>-grafana-admin` Secret |
| `helm dependency build` (Helm 4.3) needs `helm repo add`; `update` does not | documented (README quick start, step 4; docs/install.md) |
| Worker's runtime `set_qos` (prefetch) never took effect: RabbitMQ applies prefetch to consumers created after `basic.qos` (seen as 4 unacked with concurrency 1) | removed; documented in the backlog walkthrough |
| Blind scenario B4's "one replica" fault hit nothing: checkout still had 3 replicas from a previous surge, the fault was resolved to the 3rd pod, and the HPA scaled that pod away | `blind start` pins checkout/inventory at exactly 2 (HPA min = max) for the exercise, waits for them to settle, and resolves `replica:N` from the live Ready pod list instead of lab-control's 30 s instance cache |
| Bad-replica preset with a 60 ms timeout never tripped (inventory answers in 3-8 ms): SLI stayed 100%, drift visible only on spans. Lowered to 5 ms, it tripped - but once the DNS fix (below) made calls fast, 5 ms stopped tripping too (1 timeout in two minutes, SLI 100%): a timeout near the typical latency fails or not depending on the environment. B4's 3 ms payment timeout had the same weakness | both presets now use **1 ms**, below the fastest possible call, so every attempt on the targeted pod fails on any cluster; the latent-drift observation is kept in the walkthrough |
| Traffic surge to 6/s was absorbed by the HPAs - no capacity problem to diagnose | preset raised to 30/s (15/s result kept as a comparison) |
| **`ContainerCPUThrottled` flagged an idle sidecar on AWS**: the PostgreSQL exporter is runnable in only ~0.3 CFS periods a second and throttled in a third of them, so the ratio crossed 25% while the container lost almost nothing (the alert went pending at baseline) | the alert now also requires the container to run in at least one CFS period a second |
| **`verify` failed right after an install on AWS**: the HPAs add replicas while the pods' start-up CPU is high, and the check saw a replica that was still starting | `verify` gives not-ready and pending pods up to two minutes before judging them (tested: a deleted pod's replacement, 0/1 at the start, passed after 49 s) |
| **RabbitMQ crash-looped after an uninstall/reinstall** onto its kept volume: `Cookie file /var/lib/rabbitmq/.erlang.cookie must be accessible by owner only`. Mounting the existing volume again, the kubelet applied the pod's `fsGroup` to the files on it and the cookie went from `0400` to `0660`. A first install never shows it (the cookie does not exist yet), so only the persistence test found it; checkout and the worker then waited for the broker until their probes restarted them. The `helm upgrade` with the fix did not replace the crashing pod either: a StatefulSet rolling update waits for the old pod to become Ready | init container `cookie-permissions` (`chmod 0400` on the cookie before every start) and `fsGroupChangePolicy: OnRootMismatch`; the stuck pod needed `kubectl -n webshop delete pod webshop-rabbitmq-0` (README troubleshooting) |
| **The new alerts earned their keep during the persistence test**: the RabbitMQ crash loop took the shop down for ~25 minutes (1,415 `checkout_failed` journeys); `ContainerRestarted` fired for the broker and for the checkout and worker pods restarted while waiting for it; the late payment audit found 3 orders charged although the client saw an error and 28 charged after their deadline (orders accepted around the uninstall, processed after the reinstall). `ContainerNearMemoryLimit` also fired for Prometheus' config-reload sidecar: 92% of its 32 Mi limit for over 10 minutes | sidecar limit raised to 64 Mi |
| **Every new connection paid ~4 ms of DNS**, found after connections stopped being reused: checkout's inventory calls sat in the 10-25 ms histogram bucket on the client while inventory's own server time was 2.7 ms (p50); timed directly in a checkout pod, one request on a new connection took 7.2 ms (p50) against 1.3 ms on a reused one. The service URLs are fully qualified but have four dots, fewer than the pod default `ndots:5`, so each lookup first tried every search domain - including the node's own (`lan` on Kind, `ec2.internal` on AWS), which CoreDNS forwards out of the cluster: 4.0 ms p50 / 6.2 ms p95 per lookup, measured in a checkout pod. It also ate into checkout's own timeouts (the 5 ms bad-replica timeout above all) | pod `dnsConfig` `ndots: 2` for the webshop's pods (`dnsConfig` value): the same lookup now takes 0.2 / 0.3 ms, a request on a new connection ~2.7 ms (p50), and client-side inventory calls fall in the 5-10 ms bucket |
| **Grafana OOM-killed twice** (15:32 and 19:43, both while dashboards were in use): the kernel log shows a memory-cgroup kill of Grafana's container at its 384 Mi limit - Grafana (~260 MB) plus 13 bundled data source plugin processes (`gpx_*`, ~120 MB), 10 of them for data sources the lab never provisions. Kubernetes recorded reason `Error`, exit 137, not `OOMKilled`; no event named the cause | unused plugins disabled (`grafana.ini` `plugins.disable_plugins`), plugin preinstall off (no internet egress anyway), 192 Mi request / 512 Mi limit, `GOMEMLIMIT=400MiB`. After the change: 3 plugin processes instead of 13, anonymous memory 183 MiB after start-up and a full `check-dashboards` pass (0 errors); working set 362 MiB of 512 Mi, of which ~170 MiB is page cache (mostly Grafana's own binary) |
| **No alert for restarts or memory pressure**, although kube-state-metrics and cAdvisor had the data: the Tempo crash loop surfaced only as telemetry-loss alerts, and Grafana's kill not at all | rule group `webshop-kubernetes`: `ContainerRestarted`, `ContainerNearMemoryLimit` (working set > 90% of limit for 10 m: Grafana was at 90% when checked; every other container <= 46%), `ContainerCPUThrottled` |
| **One replica took almost all the traffic** (98 of 99 checkouts on one of two pods): service clients reused keep-alive connections, and kube-proxy balances connections, not requests. It made "bad replica" impact depend on luck and left new HPA replicas idle | service clients open a connection per request by default (`app.httpKeepalive: false`, `WEBSHOP_HTTP_KEEPALIVE`); keep-alive can be switched on to show the skew. All eight exercises were re-run on the final configuration (the table above); the clearest cost showed in exercise 6, where opening a connection per request on a saturated node lowered what the load generator could drive (13/s instead of 18/s) |
| **Tempo OOM crash loop** (33 restarts over ~2.5 h, found while measuring storage): on start-up its backend worker resumed a compaction job merging two busy blocks (the doubled-span period plus the 30/s surge); the merge needed ~800 Mi against a 640 Mi limit; the restarted pod was handed the same job and died again. Collector trace export then failed and its memory limiter refused data. The telemetry alerts fired as designed: `TelemetryExportFailing{exporter="otlp/traces"}`, `TelemetryRefusedByCollector`, `ScrapeTargetDown{job="tempo"}` | compaction capped (`max_block_bytes` 100 MiB, `max_input_blocks` 2, `max_compaction_objects` 500k), memory 256 Mi request / 1 Gi limit, `GOMEMLIMIT=800MiB`. After the upgrade the stuck job completed in 4.4 s (peak ~793 Mi) and Tempo stayed up |

## Executed on the AWS lab (2026-10-05)

The lab built with `terraform apply` in `AWS/kubeadm` (27 resources): two
t3.medium nodes (CPU credits `unlimited`), Ubuntu 24.04.5, Kubernetes
v1.33.13, containerd 2.2.1, Calico; `master` (control plane, `NoSchedule`
taint kept) and `worker` (2 CPU, 3.64 GiB allocatable, 29 GB root disk). The
image was pushed to Docker Hub as `docker.io/bleeng089/webshop:0.1.0`
(public) and installed with `values/values-aws.yaml --set image.repository=bleeng089/webshop`.

| Check | Result |
|---|---|
| cluster before changing it (docs/install.md, "Before you start") | both nodes Ready; no StorageClass, no metrics API, no IngressClass or Gateway API - as the README expects |
| `/var/lib/webshop/*` on the worker; metrics-server 3.14.0 | created over SSH; metrics API `Available` |
| install (`helm upgrade --install`, revision 1) | all pods Running about 2 minutes after install, 0 restarts; the worker's containerd pulled the image from Docker Hub; 6 claims bound to the pre-created local PVs; 22 NetworkPolicies |
| `helm test webshop -n webshop --logs` | Succeeded, journey `good` |
| `python tools/lab.py verify` | **24 passed** - the first attempt reported one pod not ready: the HPAs add replicas during start-up and one was still starting (verify now waits up to two minutes for that, see below) |
| `python tools/lab.py check-dashboards` | 0 errors |
| NetworkPolicy CIDRs `10.0.1.10/32` (API server) and `10.0.1.0/24` (kubelets) | proven by `verify`: Kubernetes events reach Loki (Collector -> API server) and all 13 Prometheus targets are up, cAdvisor on both kubelets included |
| Grafana admin login | kept password 200, wrong password 403 |
| all eight exercises | same failure reasons as on Kind and SLI 100% after each reset (table below) |
| four blind scenarios (B1-B4) | instrumentation fingerprint unchanged in all four; evidence as in the answer key, which now quotes the AWS runs where they differed from Kind (B1) |
| uninstall -> reinstall onto the kept volumes | orders 7,199 -> 7,305 (data kept, traffic resumed); all 6 claims re-bound to their own local PVs on the worker's disk; Secrets unchanged; RabbitMQ 0 restarts; Grafana kept password works |
| README cleanup (section 11) | removed exactly the 11 cluster objects the release owned - namespace, 6 PVs, 2 ClusterRoles, 2 ClusterRoleBindings - and nothing else; metrics-server and the metrics API untouched; `/var/lib/webshop` gone. A fresh install afterwards passed `helm test` and `verify` 24/24 |
| baseline at 1 journey/s | storefront `/api/checkout` p50 51 ms / p95 210 ms; checkout `POST /orders` 43 / 155 ms; inventory `/reservations` 8 / 24 ms; payment `/authorizations` 8 / 23 ms; journey p95 0.99 s; SLI 100%. Webshop pods ~430m CPU, 1.71 GiB (payment and storefront settle at 2 replicas); system pods on the worker 36m, 195 Mi; worker node 0.42 cores |

| # | Exercise | SLI during, AWS | SLI during, Kind |
|---|---|---|---|
| 1 | slow-inventory | 77.0% (`checkout_failed`) | 80.4% |
| 2 | pool-exhaustion | 54.1% (`checkout_failed`, `deadline_exceeded`) | 47.1% |
| 3 | retry-amplification | 90.5% | 85.9% |
| 4 | bad-replica | 47.4% (`checkout_failed`) | 49.1% |
| 5 | wrong-fulfillment | 73.3% (`wrong_item`) | 67.9% |
| 6 | traffic-surge (30/s) | **0% for the whole surge** - the load generator reached 6.7/s, the node 1.88 of 2 cores, every HPA at its maximum | 28% in the first minute, then 100% |
| 7 | worker-backlog | 0% (`deadline_exceeded`) | 0% |
| 8 | duplicate-charges | 63.9% (`duplicate_charge`) | 59.6% |

| Blind scenario | SLI during, AWS | SLI during, Kind |
|---|---|---|
| B1 | 1.8% - and the worker fell behind (179 accepted, 156 fulfilled in three minutes; queue growing) | 30% - the worker kept pace |
| B2 | 74% (`wrong_item`) | 78% |
| B3 | 0% (`checkout_failed`) | 0% |
| B4 | 33% (`checkout_failed`, `deadline_exceeded`) | 33% |

Healthy latencies on AWS are 3-6x those on Kind (slower t3 cores, the same
CPU limits), which is why B1's worker falls behind there and why the surge
never recovers. The walkthroughs quote the Kind numbers and point here.

## Networking additions, on Kind (2026-10-25, release 0.2.0)

Image `webshop:0.2.0` (adds the `X-Served-By` response header), on a fresh
`tools/kind_cluster.py` cluster, then pushed as `docker.io/bleeng089/webshop:0.2.0`.
AWS results follow in the next section.

| Check | Result |
|---|---|
| pytest | 47 passed (unit + integration); `test_two_relays_never_publish_the_same_row_twice` failed on the first run against freshly started containers and passed on the next two - the same first-run failure as on 2026-10-05, not yet explained |
| `helm test`, `verify`, `check-dashboards` | journey good; 24/24 (with NodePort, Ingress and HTTPRoute all enabled); 0 query errors |
| `exposure.nodePort` | storefront answers on `:30080` from both nodes, including the control plane (no storefront pod); with 2 replicas, 10 curls per node split 6/4 by `X-Served-By` |
| NodePort without `allowFrom` | from the control plane: timeout (masqueraded source, denied); from the worker: 200 (Calico admits a node's traffic to its own pods) - the ipBlock rule is required |
| Envoy Gateway v1.4.2 + `networking/gateway/` + `exposure.httpRoute` | Gateway `Programmed`, route `Accepted`/`ResolvedRefs`; `Host: webshop.local` reaches both storefront pods, another host gets 404 |
| ingress-nginx v1.13.3 + `exposure.ingress` | `Host: webshop.local` through the controller's NodePort reaches both storefront pods |
| capacity, both controllers + webshop on the worker | requests 77% CPU / 78% memory after lowering the Envoy proxy request to 128Mi (`proxy-config.yaml`; it was 88% memory with Envoy's default 512Mi, used ~70Mi); no Pending pods |
| `policy-drop` (exercise 9) | SLI 0% (`deadline_exceeded`); worker -> payment 100% `ConnectTimeout`, checkout -> payment 100% success; Calico's drop counter grew; `fault reset` restored the policy, queue empty 20 s later, SLI 100% the next minute; 32 orders stayed `failed` (all voided, none captured) |

Found while doing it: after a `kubectl patch` of a Helm-managed HPA, the next
`helm upgrade` fails (`conflict with "kubectl-patch" ... .spec.minReplicas`) -
Helm 4 applies server-side and will not take over a field another manager
owns. Set the value through the chart (`components.<name>.autoscaling.*`)
and pass `--force-conflicts` once.

## Networking additions, on the AWS lab (2026-10-25)

The release from the quick start (0.1.0, revision 1) upgraded in place with
the quick-start command, then Envoy Gateway v1.4.2 and ingress-nginx v1.13.3
installed with the pinned manifests of the networking lessons.

| Check | Result |
|---|---|
| upgrade 0.1.0 -> 0.2.0 (`helm upgrade --install ... -f values/values-aws.yaml`) | deployed, all 7 app Deployments on `bleeng089/webshop:0.2.0`; `helm test` good, `verify` 24/24, `check-dashboards` 0 errors |
| `exposure.nodePort` with 2 storefront replicas | `:30080` answers on master and worker (and on the worker's private IP from both); 10 curls per node split 6/4 by `X-Served-By` |
| HPA minimum raised from 1 to 2 by `helm upgrade` | the second replica came about a minute later: the new pod had no CPU metrics yet, and an HPA without metrics does not rescale, not even to its own minimum |
| NodePort without `allowFrom` | master: timeout; worker: 200. On the master, conntrack shows `SYN_SENT ... [UNREPLIED]` with the reply tuple from storefront pod `192.168.171.90:8080` to the master's tunnel address `192.168.219.64` - the masqueraded source the ipBlock rule has to admit |
| `policy-drop` (fault active 3 min 47 s) | SLI 0% (`deadline_exceeded`, 115 journeys in 2 min); worker -> payment: 144 `payment.capture` and 88 `payment.lookup` attempts, all `timeout`; checkout -> payment: 126 `success`; 0 captures at payment; queue 170 ready; Calico drop counter on the worker's `cali-fw-*` chain 787 -> 833 in 10 s (60-byte SYNs); no `SYN_SENT` in conntrack |
| recovery after `fault reset` | policy restored; queue empty within 30 s; SLI 100% in the next minute; 20 orders stayed `failed` (all authorizations voided, none captured); `verify` 24/24 afterwards |
| capacity, webshop + 2 storefront replicas + NodePort | worker requests 1335m CPU (66%) / 2640Mi (70%); in use 389m / 2075Mi |
| `networking/gateway/` + `exposure.httpRoute` | Gateway `Programmed` (address 10.0.1.10), route `Accepted`/`ResolvedRefs`; `Host: webshop.local` through Envoy's NodePort reaches both storefront pods from master and worker (8 curls: 2/6 split); another host: 404 |
| `exposure.ingress` | through ingress-nginx's NodePort, both storefront pods from master and worker (3/5, 4/4); another host: 404 |
| `networkPolicy.ingressControllerNamespaces` left empty | with the NodePort off: both controllers time out (curl gave up after 8 s), so the setting is required. With the NodePort on: both still answer 200 - the NodePort's default `allowFrom: 0.0.0.0/0` ipBlock also admits pod addresses (Calico), opening the storefront to every pod; documented in `chart/values.yaml` and the README |
| capacity with both controllers, NodePort, 2 storefront replicas | worker requests 1595m CPU (79%) / 3146Mi (84%), no Pending pods; in use 476m / 2579Mi; envoy-gateway 65Mi, Envoy proxy 49Mi, ingress-nginx 41Mi. Little room left for HPA scale-outs |
| `helm test`, `verify`, `check-dashboards` with all of it on | journey good; 24/24; 0 query errors |
| worker resized t3.medium -> t3.xlarge (`worker_instance_type`, `terraform apply`: 1 in-place change, 2 min 6 s) | node back `Ready` as 4 CPU / 15.3 GiB, same name and private IP, new public IP; all pods back; PostgreSQL kept all 8820 orders (oldest from the original install); `helm test` good, `verify` 24/24, `check-dashboards` 0 errors; NodePort, Gateway and Ingress 200 from both nodes; worker requests 1595m (39%) / 3146Mi (20%), in use 468m / 3.7 GiB |

## Networking track, run on the AWS lab (2026-10-25)

Every module of [networking.md](networking.md) run in order on the lab
(worker t3.xlarge; Calico v3.30.2, kube-proxy iptables mode, kernel 7.0),
starting from the quick-start release with both controllers removed, and
ending with Module 10's cleanup. The numbers quoted in the track are from this
run.

| Module | Result |
|---|---|
| 1 | tester reaches an inventory pod by IP; PostgreSQL times out after 3 s (policy); a replaced pod comes back with a new IP; per-node blocks `192.168.171.64/26` (worker), `192.168.219.64/26` (master, CoreDNS) |
| 2 | named targetPort resolves to 8080; EndpointSlice conditions through a pod replacement (terminating pod `serving` during preStop, new pod `ready` after ~6 s); `X-Served-By` 2/8; checkout split 0.78/0.81 req/s without keep-alive, 1.40/0.27 with; `KUBE-SVC` probability 0.5; 229 conntrack entries for one ClusterIP |
| 3 | `ndots:5` (tester) vs `ndots:2` (chart); headless and StatefulSet pod names resolve to the pod IP; checkout -> inventory client p50 10.4 ms / server 7.6 ms with `ndots:2`, 17.6 / 7.7 ms with `ndots:5`; DNS flows on the worker 380 vs 1277; replies from CoreDNS on the master |
| 4 | one journey = one trace across six services and the queue; client spans name Services, server spans carry pods |
| 5 | NodePort answers on both nodes; `client.address` per policy as tabled in the track (Cluster: `10.0.1.11` / `192.168.219.64`; Local: `10.0.1.10` / `192.168.219.64`), each re-checked with the exact documented commands; `allowFrom` removed: master times out with `SYN_SENT [UNREPLIED]`, worker 200 |
| 6 | ingress-nginx v1.13.3; both pods reached, 404 without Host; hop 2 goes from the controller to a **pod IP**, pooled (`ESTABLISHED`) - the networking lessons' Module 6/7 text said ClusterIP and was corrected |
| 7 | `values-gateway.yaml` before the CRDs: upgrade fails, release untouched; Envoy Gateway v1.4.2, GatewayClass accepted, Gateway programmed, route accepted; 3/3 split, 404 for other hosts; Envoy also goes to pod IPs |
| 8 | 22 chart policies + 2 tester; storefront allowed, lab-control and Prometheus denied; deleting only `tester-egress` denies the storefront; 138 `cali-*` chains |
| 9 | **first attempt failed**: Felix panicked in BPF mode (`failed to attach connect-time load balancer ... invalid argument`, Calico v3.30.2 on kernel 7.0); kube-proxy's leftover rules kept the SLI at 100% while it was reverted. With `bpfConnectTimeLoadBalancing: Disabled`: BPF mode up, `KUBE-SVC` 43 -> 0, `cali-*` 138 -> 16, tc programs on pod interfaces, Service table in the BPF NAT map; `helm test`, `verify` 24/24, `check-dashboards` 0 errors; policies enforced; NodePorts on `localhost` fail, on node IPs work; policy-drop evidence via `calico-node -bpf counters dump` (dropped by policy 69 -> 129 in 15 s). Switch cost ~6 of 83 journeys; switching back ~7 of 97; no pod restarts |
| 10 | cleanup commands as written; `helm upgrade` without overlays removed the HTTPRoute after its CRD was already gone, without error; storefront back to ClusterIP; `verify` 24/24, `check-dashboards` 0 errors |

Found while writing it: busybox `nslookup` applies the search list only to
dotless names (documented); `kubectl describe endpointslices` shows `Ready`
but not `serving`/`terminating` (the track uses `-o yaml`); patches are files
under `networking/ebpf/` because inline JSON does not survive cmd's quoting
(each validated with `--dry-run=server`).

2026-11-01: every placeholder in the track (`<inventory-pod-ip>`, `<nodeport>`, ...) replaced by a
line that looks the value up (kubectl `jsonpath` in your terminal; kube-proxy's `KUBE-SERVICES`,
`KUBE-NODEPORTS` and `DNAT` rules, `ip link`, or `ip route get` inside calico-node on the node side),
after a user pasted a block and bash read `<inventory-pod-ip>` as a redirection. Tested on the
rebuilt lab as written: the client lines in an interactive bash, the worker lines on the worker; the
Ingress/Envoy lookups against those controllers' rule lines from the 2026-10-25 run (controllers not
installed at the time).

---

> [All steps](../../../../../README.md#learning-path) · Back to the [webshop README](../README.md)
