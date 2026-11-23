# Webshop reference: values, tests, layout, versions, limitations

> **For maintainers:** what you need to change the chart or the code; the lessons do not need this page. Commands run from `AWS/kubeadm/k8/helm/webshop/`.

**Contents:** [Values reference](#values-reference) · [Tests](#tests) · [Folder layout](#folder-layout) · [Pinned versions](#pinned-versions) · [Where it was verified](#where-it-was-verified) · [Limitations, honestly](#limitations-honestly)

## Values reference

All values with comments: [`chart/values.yaml`](../chart/values.yaml). The ones
you are most likely to change:

| Value | Default | Purpose |
|---|---|---|
| `image.registry` / `image.repository` / `image.tag` / `image.digest` | `docker.io` / `CHANGE-ME/webshop` (`bleeng089/webshop` in `values-aws.yaml`) / `0.2.0` / `""` | where the nodes pull the webshop image |
| `imagePullSecrets` | `[]` | e.g. `[{name: regcred}]` |
| `global.webshop.environment` / `clusterName` | `lab` / `kubernetes` | `deployment.environment.name`, `k8s.cluster.name` on every signal |
| `components.<name>.replicas` / `.resources` / `.autoscaling` | see file | sizing and HPA bounds per component |
| `app.*` | pool 5, timeouts 2 s / 1.5 s, attempts 2 / 3 | dependency policy; changes the config revision |
| `app.httpKeepalive` | `false` | `true` reuses connections between services - and lets kube-proxy pin a caller's traffic to one replica (connection-level balancing) |
| `dnsConfig` | `ndots: 2` | pod DNS options for the webshop's pods; `{}` restores the cluster default (`ndots:5`: every service lookup walks the search list first) |
| `synthetic.ratePerSecond` / `deadlineSeconds` / `validationRatio` | `1.0` / `5` / `0.05` | the synthetic client and the SLI deadline |
| `labControl.enabled` | `true` | the fault controller (faults are still off until set) |
| `storage.mode` | `storageClass` (`localPV` in values-aws) | `storageClass` / `localPV` / `ephemeral` |
| `storage.localPV.nodeName` / `basePath` | `worker` / `/var/lib/webshop` | where local PVs live |
| `networkPolicy.enabled` | `true` | default-deny plus per-flow allow-lists |
| `networkPolicy.apiServer.cidrs` / `kubelet.cidrs` | `0.0.0.0/0` (aws: `10.0.1.10/32`, `10.0.1.0/24`) | egress for API and kubelet access |
| `postgresql.enabled` / `rabbitmq.enabled` + `external.*` + `existingSecret` | bundled | use existing servers |
| `otel.endpoint` / `otel.headersSecret` | bundled Collector | send app telemetry elsewhere |
| `observability.collector.{traces,logs,metrics}.*` | in-release backends | where the Collector exports (existing stack) |
| `observability.alerts.sloTarget` | `0.99` | SLO used by the burn-rate alert |
| `collector.enabled` / `prometheus.enabled` / `loki.enabled` / `tempo.enabled` / `grafana.enabled` | `true` | bundled telemetry components |
| `exposure.nodePort.*` | disabled; `30080`, `Cluster`, `[0.0.0.0/0]` | storefront as a NodePort Service, and the sources its NetworkPolicy admits |
| `exposure.ingress.*` / `exposure.httpRoute.*` | disabled | optional Ingress / HTTPRoute for the storefront (and Grafana) |

## Tests

macOS / Linux:

```text
python3 -m venv .venv
source .venv/bin/activate
pip install --no-deps -r src/requirements.txt
pip install -r src/requirements-dev.txt
python -m pytest
```

(Windows PowerShell: `py -m venv .venv`, then `.venv\Scripts\Activate.ps1`,
then the same `pip` and `pytest` lines. Inside the activated venv the
interpreter is `python` on every system.)

Unit tests need nothing else. The integration tests (`tests/test_integration.py`:
accepted vs fulfilled, idempotent checkout/reservations/captures, redelivery
after a crash, the outbox under concurrent relays, trace continuity across the
queue, the immutable request) need a PostgreSQL and a RabbitMQ. With Docker,
in the same terminal - each `until` line waits for its server to accept
connections:

```text
docker run -d --name webshop-test-pg -p 15432:5432 -e POSTGRES_USER=webshop -e POSTGRES_PASSWORD=webshop -e POSTGRES_DB=webshop postgres:17.11-trixie
docker run -d --user rabbitmq --name webshop-test-mq -p 15672:5672 rabbitmq:4.3.6
until docker exec webshop-test-pg pg_isready -U webshop -q; do sleep 2; done
until docker exec webshop-test-mq rabbitmq-diagnostics -q check_port_connectivity >/dev/null 2>&1; do sleep 2; done
export WEBSHOP_TEST_PGHOST=localhost WEBSHOP_TEST_PGPORT=15432 WEBSHOP_TEST_PGUSER=webshop WEBSHOP_TEST_PGPASSWORD=webshop WEBSHOP_TEST_AMQP_HOST=localhost WEBSHOP_TEST_AMQP_PORT=15672
python -m pytest
```

(`--user rabbitmq`: with Docker's containerd image store the RabbitMQ image
otherwise fails with a permission error on its data directory. PowerShell:
`$env:NAME="value"` for each variable.) Without the variables those tests are
skipped and say why. One of them,
`test_two_relays_never_publish_the_same_row_twice`, has failed on the first
run against freshly started containers and passed on every run after - run
`python -m pytest` once more before reading anything into it. Remove the
containers afterwards: `docker rm -f webshop-test-pg webshop-test-mq`.

## Folder layout

```text
webshop/
├── README.md                the lesson: quick start, exercises, the tracks
├── src/                     the application (one Python package, one Dockerfile)
├── chart/                   the Helm chart, Chart.lock, templates
├── observability/           local chart: Collector pipeline, Prometheus config + alert rules,
│                            Grafana data sources + dashboards (built by tools/build_dashboards.py)
├── values/                  values-aws.yaml (primary), values-existing-backends.yaml,
│                            values-ephemeral.yaml (overlay), values-kind.yaml (optional)
├── tools/                   lab.py (faults, load, blind, verify), images.py, kind_cluster.py, build_dashboards.py, draw_diagrams.py
├── tests/                   pytest: SLI, retries, propagation, lab-control, single export,
│                            dashboards in sync, integration (PostgreSQL + RabbitMQ)
├── networking/              the networking track's tester, values overlays, Gateway, eBPF patches
├── otel/                    the OpenTelemetry track's playground pod and scripts
└── docs/                    README.md (the order to read them in), install.md, observability.md, incidents/,
                             blind-exercises.md, answer-key/, networking.md, nat-and-conntrack.md, opentelemetry.md,
                             reference.md (this page), verification.md, diagrams/ (the two Kubernetes diagrams
                             and their KubeDiagrams configs)
```

## Pinned versions

Python 3.13.16 (slim), OpenTelemetry Python 1.45.0 /
instrumentation 0.66b0, FastAPI 0.142.2, asyncpg 0.31.0, aio-pika 10.1.0;
PostgreSQL 17.11, RabbitMQ 4.3.6, postgres_exporter 0.20.1; OpenTelemetry
Collector contrib 0.161.0 (chart 0.175.0), Prometheus 3.15.0 (chart 29.35.0,
kube-state-metrics 2.20.0), Loki 3.7.8 (chart 18.13.7), Tempo 3.1.0 (chart
3.1.0), Grafana 13.2.3 (chart 13.2.7) - the Grafana, Loki and Tempo charts come
from the community-maintained `grafana-community` repository, where they moved
in 2026. metrics-server chart 3.14.0. Every Python dependency, direct and
transitive, is pinned in `src/requirements.txt`.

## Where it was verified

[verification.md](verification.md) lists
every check run, on what, and with what result - on a Kind cluster shaped
like the lab, then on the AWS lab itself.

## Limitations, honestly

- Single-instance data and telemetry stores; no backups; local volumes share
  the worker's root disk. This is a lab profile.
- PostgreSQL keeps every order - nothing prunes it (~200 MiB a day at the
  default rate, [README section 9](../README.md#9-scaling-and-capacity)). Telemetry stores are bounded by retention.
- The numbers quoted in the walkthroughs come from one run each on a
  lab-shaped Kind cluster; the AWS lab showed the same evidence with healthy
  latencies 3-6x higher, and two differences in kind, both described where
  they occur (exercise 6 never recovers during the surge; blind B1's queue
  grows). Expect your numbers to differ, not the shape of the evidence.
- Faults are simulated in the application (delays, errors, timeouts, held
  connections, a wrong variant), not real infrastructure failures. Their
  *effects* are real and measured the same way real ones would be.
- The SLI comes from one synthetic client with a 0.4 s polling interval, so
  measured completion times are upper bounds within 0.4 s; journeys in flight
  when the load generator restarts are never evaluated.
- The queue-wait span is reconstructed from the publish timestamp, so it
  depends on node clocks being in sync (they are, on EC2).
- `lab-control` is unauthenticated (reachable only by port-forward and by the
  webshop's own pods) and in-memory by design.
- Anonymous Grafana Editor access is a lab convenience.
