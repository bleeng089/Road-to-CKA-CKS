# OpenTelemetry track: the concepts, running on the webshop

> **Step 7 of 8: OpenTelemetry** · Run from `AWS/kubeadm/k8/webshop/` · Needs: the webshop (step 4)\
> [All steps](../../../../../README.md#learning-path) · Previous: step 6, [Networking on the webshop](networking.md) · Next: step 8, [Tear down](../../../README.md#10-destroy)

The webshop is instrumented with OpenTelemetry from end to end -
[observability.md](observability.md) lists what it records and why. This
track goes the other way round. It walks through the OpenTelemetry concept
pages - the observability primer, context propagation, and the signals:
traces, metrics, logs, baggage, profiles - heading by heading, and for each
idea shows three things:

1. **In the webshop**: the code that does it, with file and line.
2. **Run it**: a short script in [`otel/`](../otel) that does that one thing
   and nothing else. It runs inside the cluster, so you can read it, talk it
   through, run it, change a line and run it again.
3. **See it**: where the result shows up in Grafana, and what it tells you.

Every output below was observed on the AWS lab on 2026-11-22: kubeadm 1.33,
the webshop chart 0.2.0, OpenTelemetry Python SDK 1.45 with instrumentation
0.66b0, Collector 0.161, Tempo 3.1, Loki 3.7, Prometheus 3.15, Grafana 13.2.
Trace IDs, pod names and counts are different on every run; the shapes are
not.

**The OpenTelemetry pages, and where each heading is.** The modules follow
the pages in this order, so you can read a page and then run its module:

| OpenTelemetry page: headings | Module |
|---|---|
| [Observability primer](https://opentelemetry.io/docs/concepts/observability-primer/): What is Observability? · Reliability and metrics (telemetry, reliability, metrics, SLI, SLO) | [1](#module-1---observability-primer-instrumented-reliable-measured) |
| Observability primer: Understanding distributed tracing (logs, spans, span attributes, distributed traces) | [2](#module-2---understanding-distributed-tracing-logs-spans-traces) |
| [Context propagation](https://opentelemetry.io/docs/concepts/context-propagation/): context, propagation, the example (traces, logs, metrics), custom propagation, security, support in language SDKs | [3](#module-3---context-propagation) |
| [Traces](https://opentelemetry.io/docs/concepts/signals/traces/): Tracer Provider, Tracer, Trace Exporters | [4](#module-4---traces-tracer-provider-tracer-trace-exporters) |
| [Metrics](https://opentelemetry.io/docs/concepts/signals/metrics/): Meter Provider, Meter, Metric Exporter | [5](#module-5---metrics-meter-provider-meter-metric-exporter) |
| [Logs](https://opentelemetry.io/docs/concepts/signals/logs/): logs for applications, logging components, Log Record, logs in the Collector | [6](#module-6---logs-the-bridge-the-log-record-the-collector) |
| [Baggage](https://opentelemetry.io/docs/concepts/signals/baggage/): example, what to use it for, security, not the same as attributes | [7](#module-7---baggage) |
| [Profiles](https://opentelemetry.io/docs/concepts/signals/profiles/): how profiles complement the other signals | [8](#module-8---profiles-the-signal-this-lab-does-not-have) |

**Conventions.** As in the [networking track](networking.md): commands run
from `AWS/kubeadm/k8/webshop/`, in one terminal where `KUBECONFIG`
points at the lab (README quick start, step 1); `kubectl` is always written
out, with the namespace right after it; **[on the worker]** marks commands
for a shell on the worker node. Grafana is opened as in quick start step 6.
"Explore (Tempo)" means Grafana's Explore view with the data source Tempo -
the same for Loki and Prometheus. **To open a trace by its ID:** Explore ->
data source **Tempo** (the default one is Prometheus) -> query type
**TraceQL** -> paste the 32-character ID -> **Run query**. Grafana looks for
the ID only inside the time range at the top right, so keep that at *Last 1
hour* or wider: an older trace comes back as *not found in the selected
time range*. A trace or log record is searchable a few seconds after a
script ends; a metric needs up to half a minute (a 5 s export, then
Prometheus's 15 s scrape), and its series disappears 5 minutes after the
script ends (a query for "now" then comes back empty, the graph still
shows it). **The shop itself sends about 70 spans and 9 log records a
second**; every lesson script names itself `lesson-<something>`, so filter
on that: `{ resource.service.name =~ "lesson-.*" }` in Tempo,
`{service_name=~"lesson-.*"}` in Loki, `{__name__=~"lesson_.*"}` in
Prometheus.

**Running a script.** Every script runs inside the playground pod from
Module 0:

```text
kubectl -n webshop exec -i otel-playground -- python - < otel/hello.py
```

`python -` reads the program from standard input, and `-i` passes your
terminal's input on to the pod: the file stays on your laptop. Edit it, run
the same line again. PowerShell has no `<` - there it is
`Get-Content -Raw otel\hello.py | kubectl -n webshop exec -i otel-playground -- python -`.

**Modules are self-contained.** Each opens with its prerequisites; the base
for all of them is the webshop installed with the README's quick start, plus
the playground (Module 0) wherever a script runs. The scripts change nothing
in the shop: they send telemetry of their own and, in Modules 2, 3 and 7,
ordinary requests for the product list. Modules 1 and 8 start one fault
exercise each, for a few minutes, and end it.

---

**Contents**

0. [The playground: OpenTelemetry code inside the cluster](#module-0---the-playground-opentelemetry-code-inside-the-cluster)
1. [Observability primer: instrumented, reliable, measured](#module-1---observability-primer-instrumented-reliable-measured)
2. [Understanding distributed tracing: logs, spans, traces](#module-2---understanding-distributed-tracing-logs-spans-traces)
3. [Context propagation](#module-3---context-propagation)
4. [Traces: Tracer Provider, Tracer, Trace Exporters](#module-4---traces-tracer-provider-tracer-trace-exporters)
5. [Metrics: Meter Provider, Meter, Metric Exporter](#module-5---metrics-meter-provider-meter-metric-exporter)
6. [Logs: the bridge, the Log Record, the Collector](#module-6---logs-the-bridge-the-log-record-the-collector)
7. [Baggage](#module-7---baggage)
8. [Profiles: the signal this lab does not have](#module-8---profiles-the-signal-this-lab-does-not-have)
9. [Cleanup](#module-9---cleanup)

---

## Module 0 - The playground: OpenTelemetry code inside the cluster

**Prerequisites:** the webshop installed (README quick start).

The webshop namespace is default-deny: a pod nobody planned for can resolve
names but reach nothing (networking Module 1).
[`otel/playground.yaml`](../otel/playground.yaml) holds a pod that runs the
webshop's own image - so every script uses exactly the SDK, the exporters
and the instrumentation libraries the services use - and four
NetworkPolicies that let it reach the Collector, the storefront, inventory
and (for Module 4) Tempo. Like the networking tester's, they only add to the
chart's policies.

```text
kubectl apply -f otel/playground.yaml
kubectl -n webshop wait --for=condition=Ready pod/otel-playground --timeout=120s
kubectl -n webshop exec -i otel-playground -- python - < otel/hello.py
```

```text
pod/otel-playground created
networkpolicy.networking.k8s.io/otel-playground-egress created
networkpolicy.networking.k8s.io/otel-playground-to-collector created
networkpolicy.networking.k8s.io/otel-playground-to-apps created
networkpolicy.networking.k8s.io/otel-playground-to-tempo created
pod/otel-playground condition met
trace ID: d7b75a97e64fd303f0fde8cd04ace2f9
Tempo     : Explore -> Tempo -> TraceQL, paste the trace ID
Loki      : {service_name="lesson-hello"}
Prometheus: lesson_hello_runs_total
```

[`otel/hello.py`](../otel/hello.py) is the smallest program that sends all
three signals: one span, one counter increment, one log record. Find each
one:

- **Explore (Tempo)**, the trace ID: one span, `say hello`.
- **Explore (Loki)**, `{service_name="lesson-hello"}`: one line,
  `hello from the playground`. Expand it - its `trace_id` is the trace you
  just opened.
- **Explore (Prometheus)**, `lesson_hello_runs_total`: `1`, with
  `job="webshop/lesson-hello"`.

That is the path every webshop service uses: SDK -> OTLP/HTTP -> Collector
-> Tempo, Prometheus and Loki. Nothing in the script names the Collector:
the OTLP exporters read `OTEL_EXPORTER_OTLP_ENDPOINT` from the environment,
which the playground sets in its pod spec and the services get from the
`webshop-config` ConfigMap:

```text
kubectl -n webshop exec otel-playground -- printenv OTEL_EXPORTER_OTLP_ENDPOINT OTEL_RESOURCE_ATTRIBUTES
kubectl -n webshop get configmap webshop-config -o jsonpath='{.data.OTEL_EXPORTER_OTLP_ENDPOINT}{"\n"}'
```

```text
http://webshop-collector.webshop.svc.cluster.local:4318
k8s.pod.name=otel-playground,k8s.pod.uid=89e1e5a5-34d0-4a6f-9da9-64c9c9f66578,k8s.namespace.name=webshop,k8s.node.name=worker
http://webshop-collector.webshop.svc.cluster.local:4318
```

Now open the span's **Resource** attributes in Tempo - *who* sent it:
`service.name` and `service.namespace` come from the script;
`k8s.pod.name`, `k8s.pod.uid`, `k8s.namespace.name` and `k8s.node.name`
from `OTEL_RESOURCE_ATTRIBUTES` (filled in by the Downward API, exactly as
the chart does for the services); `telemetry.sdk.*` and a random
`service.instance.id` from the SDK; and `k8s.cluster.name` and
`k8s.pod.start_time` were added on the way, by the Collector (Module 4 shows
how).

Every lesson script calls itself `lesson-<something>` and sets
`service.namespace` to `webshop`: Grafana's provisioned links search that
namespace, so they work for the lesson's telemetry too.

---

## Module 1 - Observability primer: instrumented, reliable, measured

**Prerequisites:** the webshop installed; Module 0 for the SLI script.

### What is observability: a question nobody planned for

The primer's definition of a properly instrumented application - developers
don't need to add more instrumentation to troubleshoot an issue, because
they have all the information they need - is the sentence this lab is built
around (the first lines of the README). The blind exercises enforce it:
`blind reveal` compares a fingerprint of every workload template and
telemetry ConfigMap with the one taken at `blind start`, so a log line added
to find the cause shows up as an instrumentation gap (README section 6).

A dashboard answers the questions someone thought of in advance.
Observability is answering the next one - the "unknown unknown" - from what
is already collected. Three questions no dashboard in the Webshop folder
answers:

**Which variant sells best?** Nobody built a sales panel, but every
`order accepted` log record carries the SKU. Explore (Loki):

```logql
sum by (webshop_order_requested_sku) (count_over_time({service_name="checkout"} |= "order accepted" [15m]))
```

Observed: six counts for 15 minutes, from 131 (`MUG-BLK`) to 167
(`HBR-BLK`). (Query type *Instant* gives one number per SKU instead of a
graph.)

**Show me the orders for the crimson sneakers.** Explore (Tempo), TraceQL:

```traceql
{ span.webshop.order.requested_sku = "TRL-CRM" }
```

**Did any checkout take longer than 5 seconds in the last hour?**

```traceql
{ resource.service.name = "storefront" && name = "POST /api/checkout" && duration > 5s }
```

On a quiet lab: nothing. On ours: three traces, all from the first minute
after the stopped nodes had been started again - one of them is the story
under *Reliability* below.

The answers come from attributes the code put on spans and log records - an
SKU, a route, a duration - detail that metrics deliberately do not keep
(Module 5).

### Telemetry: three signals, one pipeline

Telemetry is the data a system emits about its behaviour. The Collector
counts what it receives, per signal - Explore (Prometheus), one query at a
time:

```promql
sum(rate(otelcol_receiver_accepted_spans_total[5m]))
sum(rate(otelcol_receiver_accepted_metric_points_total[5m]))
sum(rate(otelcol_receiver_accepted_log_records_total[5m]))
```

Observed: about 69 spans, 49 metric points and 9 log records per second, at
the default load of about one synthetic journey a second. The **Webshop /
Telemetry health** dashboard is built on these counters: telemetry is data
that can get lost on its way too, and the other dashboards are only as
trustworthy as the pipeline under them (observability.md section 7).

### Reliability: the black pair of shoes

The primer's example of an unreliable system: it is up 100% of the time,
but when a customer adds a black pair of shoes to the cart, it doesn't
always add black shoes. The webshop sells a black *Trail Runner Sneaker*
(`TRL-BLK`) and a crimson one (`TRL-CRM`), and exercise 5 makes exactly
that happen: 30% of orders ship the other color. Start it, with an automatic
end after 5 minutes:

```text
python3 tools/lab.py fault apply wrong-fulfillment --duration 300
```

Wait three minutes. Then ask "is the shop working?" in two ways, in Explore
(Prometheus). The servers' view - the share of HTTP requests that failed:

```promql
(sum(rate(http_server_request_duration_seconds_count{service_namespace="webshop", http_response_status_code=~"5.."}[3m])) or vector(0)) / sum(rate(http_server_request_duration_seconds_count{service_namespace="webshop"}[3m]))
```

and the customers' view - the share of journeys that went right:

```promql
sum(increase(webshop_synthetic_journeys_total{webshop_outcome="good"}[3m])) / sum(increase(webshop_synthetic_journeys_total[3m]))
```

Observed after three minutes: **0** for the first - not a single 5xx -
and **0.62** for the second. **Webshop / User reliability** showed one
reason in *Bad journeys by reason*, `wrong_item` (56 of 147 journeys); the
fulfillment worker reported 130 orders `fulfilled` and none `failed`; and
nothing on **Webshop / Service health** turns red - no errors, no latency
change, no retries (incident 5). Of the alerts,
`WebshopWrongItemShipped` fired 40 seconds after the start, while
`WebshopHighErrorRatio`, the service-side one, stayed silent. (`or
vector(0)`: without any 5xx series the numerator would be empty, not 0, and
so would the whole result.) [Incident 5](incidents/05-incorrect-fulfillment.md)
follows the evidence to the span where the wrong color is picked. End it:

```text
python3 tools/lab.py fault reset
```

**It also happened by itself.** Right after this lab's nodes were started
again, the synthetic client's payment audit found three customers *charged
for an order they saw fail* (`charged_after_error`), and
`WebshopDuplicateCharge` fired. One of those journeys in Tempo, trimmed to
the main spans (start relative to the root, duration):

```text
     0.0 ms   5010 ms  loadgen             synthetic.journey     ERROR  webshop.reason=checkout_failed
     1.0 ms   5008 ms  loadgen             POST                  ERROR  error.type=ReadTimeout
     5.8 ms   7277 ms  storefront          POST /api/checkout           http.response.status_code=202
     9.0 ms   7272 ms  storefront          POST                         http.response.status_code=202
  7184.3 ms     97 ms  checkout            POST /orders                 http.response.status_code=202
  7214.1 ms     16 ms  checkout            payment.authorize            webshop.outcome=success
  7468.1 ms     33 ms  fulfillment-worker  payment.capture              webshop.outcome=success
```

Every service answered with success. The storefront's call took 7.2 seconds
to *reach* checkout - no span covers that time; it went by between the two
processes, right after the boot. The synthetic client gave up after its
5 seconds and recorded the checkout as failed; the storefront, whose own
timeout for checkout is 8 seconds (`WEBSHOP_CHECKOUT_TIMEOUT_S` in the
`webshop-config` ConfigMap), kept waiting, and the order went through and
was charged. Up, every request a 2xx, and still not what the customer
expects: that is the primer's "unreliable".

### Metrics

"Metrics are aggregations over a period of time of numeric data about your
infrastructure or application" - the primer's examples, on the webshop.
Request rate per service:

```promql
sum by (service_name) (rate(http_server_request_duration_seconds_count{service_namespace="webshop"}[5m]))
```

CPU per pod (from the kubelets' cAdvisor, observability.md section 6):

```promql
sum by (pod) (rate(container_cpu_usage_seconds_total{namespace="webshop", container!=""}[5m]))
```

and the error rate is the first query of *Reliability* above. Each is
thousands of individual events reduced to one number per interval: cheap to
keep, fast to alert on - and the individual request is gone. Module 5 builds
metrics from the SDK side.

### SLI and SLO

An SLI measures the service's behaviour, and a good one measures it from the
users' side. The webshop's SLI is code:
[`src/webshop/sli.py`](../src/webshop/sli.py), `evaluate()` (line 70) - the
function the synthetic client runs on every journey it completes.
[`otel/sli_check.py`](../otel/sli_check.py) feeds it five journeys a customer
could have had:

```text
kubectl -n webshop exec -i otel-playground -- python - < otel/sli_check.py
```

```text
journey                                  HTTP status codes said         SLI verdict
everything went right                    202, then 200s                 good (none)
shipped Crimson instead of Black         202, then 200s                 bad (wrong_item)
charged twice                            202, then 200s                 bad (duplicate_charge)
fulfilled, but after 7 s                 202, then 200s                 bad (deadline_exceeded)
client timed out, order went through     no response (client gave up)   bad (checkout_failed)
```

Four of the five got nothing but 2xx answers; the SLI calls four of them
bad. It checks what the customer cares about - an order ID, fulfilled within
5 s, the right item, charged exactly once, the right amount - using the
client's own memory of what it ordered and the payment simulator's ledger,
never the order record the system wrote about itself. The last line is the
restart story above.

The SLO "attaches SLIs to business value": **99% of journeys good**
(`alerts.sloTarget` in `observability/values.yaml`). On **Webshop / User
reliability**: *SLI over the dashboard range*; *Error budget left* - the 1%
that may go wrong, and how much of it is spent; *Burn rate (10 min)* - how
fast it is being spent, where 1x would use it up exactly at the SLO. The
page alert `WebshopJourneySLOBurnRateHigh` fires only when a 10-minute *and*
a 2-minute window both burn at 10x: quick to catch a real outage, deaf to a
single bad journey. (The windows are sized for a lab session; the values
file notes that production would use the SRE workbook's 1h/5m and 6h/30m
pairs.)

**To talk through**

- Which of the two *Reliability* queries would you page someone on? Why do
  rate/errors/duration dashboards miss the black shoes?
- This SLI comes from a synthetic client, not from real customers. What can
  it not see? (observability.md section 9.)

---

## Module 2 - Understanding distributed tracing: logs, spans, traces

**Prerequisites:** the webshop installed, Module 0.

### Logs

The primer's sample log line - a timestamp, a level, a message - "isn't
enough for tracking code execution": nothing in it says which request it
belonged to. Logs "become far more useful when they are included as part of
a span, or when they are correlated with a trace and a span".
[`otel/log_in_context.py`](../otel/log_in_context.py) records the same
event, customer c-1042 looking at their cart, in four ways:

```text
kubectl -n webshop exec -i otel-playground -- python - < otel/log_in_context.py
```

```text
I, [2026-11-22T23:05:49.827771]  INFO -- : cart viewed by customer c-1042, 3 items
trace ID: 7ad636decdc4f80ec5b590e6a5acdc4e   span ID: 36dacda27f2f79c4
Loki : {service_name="lesson-log-context"}   (two records: only one has a trace_id)
Tempo: paste the trace ID - the span GET /cart, its event cart.loaded, and 'Logs for this span'
```

1. **The classic line** went to your terminal and nowhere else. No pipeline
   collects it: the Collector reads no files (Module 6).
2. **Explore (Loki)**, `{service_name="lesson-log-context"}`: two
   `cart viewed` records, both with the fields `webshop_customer_id` and
   `webshop_cart_items`. Only the one written inside the span has
   `trace_id` and `span_id` - and with them an *Open trace* link.
3. **Explore (Tempo)**, the trace ID: the span `GET /cart`, and in its
   **Events** `cart.loaded` - a structured message that is part of the span,
   stamped with the moment it happened. The span's *Logs for this span*
   link finds the record from step 2 (the provisioned query is
   `{service_namespace="webshop"} | trace_id="..."`).

**In the webshop.** Every service logs with Python's `logging`, and
[`src/webshop/telemetry.py`](../src/webshop/telemetry.py) `configure_logging()`
(line 218) sends each record two ways: over OTLP into Loki, and as JSON on
stdout so `kubectl logs` stays useful - both with the trace ID (`grep` and
`tail` are bash/zsh):

```text
kubectl -n webshop logs deploy/webshop-checkout --tail=200 | grep '"order accepted"' | tail -1
```

```text
{"ts": "2026-11-22T23:23:33.170+00:00", "level": "INFO", "service": "checkout", "logger": "webshop.checkout", "msg": "order accepted", "trace_id": "60628be8397755b38af65b82202b0108", "span_id": "3d8f97e83fe86571", "webshop.order.id": "ord-dd3582d4317842e7", "webshop.order.requested_sku": "MUG-BLK", ...}
```

And span events: every retry of a call to another service becomes an event
`retry` on that operation's span, with attempt, reason and backoff, at the
point in the timeline where it happened
([`src/webshop/httpclient.py`](../src/webshop/httpclient.py) line 182).

### Spans

A span is one unit of work: a name, a start and an end time, attributes,
events, a status - and a parent, unless it is the first.
[`otel/span_anatomy.py`](../otel/span_anatomy.py) sets each of those by
hand, then makes one real HTTP request through an *instrumented* client:

```text
kubectl -n webshop exec -i otel-playground -- python - < otel/span_anatomy.py
```

```text
storefront answered 200, served by pod webshop-storefront-67d6b7456-45k5z
trace ID: f9f4d51a52f84e422853057ef5baeb57
Tempo: paste the trace ID; open cart.add, coupon.apply, then the storefront span GET /api/products
```

In Tempo, open the spans one by one:

| Span (service) | Look at |
|---|---|
| `cart.add` (lesson-span) | attributes `webshop.product.sku=TRL-BLK`, `webshop.cart.items=3` - set with `set_attribute()` |
| `price.lookup` | the event `price found` (`add_event()`) |
| `coupon.apply` | status **error**, `coupon rejected`; an event `exception` with `exception.type`, `exception.message`, `exception.stacktrace` (`record_exception()`) |
| `GET` (lesson-span, kind client) | made by the httpx instrumentation - the script set nothing on it |
| `GET /api/products` (storefront, kind server) | made by the FastAPI instrumentation, inside the storefront |
| `catalog.list` -> `GET` -> `GET /products` (inventory) -> `SELECT inventory.products` | the storefront's own call on to inventory, and inventory's SQL |

### Span attributes

The primer's attribute table, next to the storefront's server span
`GET /api/products` from that run:

| Key | The primer's example | The storefront's span |
|---|---|---|
| `http.request.method` | `"GET"` | `GET` |
| `network.protocol.version` | `"1.1"` | `1.1` |
| `url.path` | `"/webshop/articles/4"` | `/api/products` |
| `url.query` | `"?s=1"` | `s=1` |
| `server.address` | `"example.com"` | `192.168.171.103:8080` |
| `server.port` | `8080` | `8080` |
| `url.scheme` | `"https"` | `http` |
| `http.route` | `"/webshop/articles/:article_id"` | `/api/products` |
| `http.response.status_code` | `200` | `200` |
| `client.address` | `"192.0.2.4"` | `192.168.171.117` (the playground pod) |
| `user_agent.original` | `"Mozilla/5.0 (Windows NT 10.0; ...)"` | `Mozilla/5.0 (X11; Linux x86_64) otel-lesson/span_anatomy` |

(`client.socket.address` only appears when a proxy sits in between.)

- Nobody in the webshop wrote these. The instrumentation library did, with
  the names the HTTP **semantic conventions** define - which is what lets
  Grafana, the Collector's servicegraph and any backend understand them
  without being told.
- `url.path` is the path that was asked for; `http.route` is the template
  (`/api/orders/{order_id}` for every order). Only the route is bounded
  enough to become a metric label (Module 5).
- Instrumentation is not perfect: here `server.address` holds the pod's own
  address and port, as the server socket saw them, not the name the client
  asked for (`webshop-storefront.webshop.svc.cluster.local`).

**The vocabulary has versions.** Now look at the playground's own client
span, `GET` from lesson-span: `http.method`, `http.url`,
`http.status_code` - the *old* names. The storefront's client spans have
`http.request.method`, `url.full`, `http.response.status_code`. Same
library, same version: the webshop opts in to the stable HTTP conventions
([`src/webshop/telemetry.py`](../src/webshop/telemetry.py) line 160,
`OTEL_SEMCONV_STABILITY_OPT_IN=http`), the playground does not. Run the
script again with the switch:

```text
kubectl -n webshop exec -i otel-playground -- env OTEL_SEMCONV_STABILITY_OPT_IN=http python - < otel/span_anatomy.py
```

The new trace's `GET` has `url.full`, `http.request.method`,
`http.response.status_code`, `server.address` and `network.peer.address`.
A query written for one set of names finds nothing under the other.

**Span kinds.** `SERVER` handles an incoming request, `CLIENT` makes an
outgoing one, `INTERNAL` is work inside a process; for messages there are
`PRODUCER` and `CONSUMER` (Module 3). The Collector's servicegraph pairs
CLIENT with SERVER spans, and PRODUCER with CONSUMER, to work out who calls
whom.

### Distributed traces

A trace is the spans of one request, across every service it touched: the
root span first, and every other span nested inside its parent.
[`otel/waterfall.py`](../otel/waterfall.py) builds one from fixed sleeps, so
you know its shape before you open it - the picture is at the top of the
script:

```text
kubectl -n webshop exec -i otel-playground -- python - < otel/waterfall.py
```

Tempo showed (start relative to the root, duration):

```text
    0.0 ms   653.0 ms  place order
    0.1 ms   100.3 ms    reserve stock
  100.6 ms   201.0 ms    authorize payment
  131.0 ms   150.3 ms      card network call
  301.9 ms   120.4 ms    send confirmation email
  302.0 ms   200.6 ms    update analytics
```

How to read a waterfall: a child lies inside its parent; siblings one after
the other took turns; siblings that overlap (the email and the analytics)
ran at the same time; and the last 150 ms of `place order`, which no child
covers, is time the parent spent itself - or waiting on something nobody
instrumented.

Then a real one, as networking Module 4 does:

```text
python3 tools/lab.py journey
```

```text
journey  : good (none)
order_id : ord-495149884bab431f
status   : fulfilled  shipped=TRL-BLK  successful_captures=1
trace_id : a3c92a522e544c115010bae609241e9c
```

Paste the trace ID: 70 spans from six services. Trimmed to the main ones:

```text
    0.0 ms   502.6 ms  loadgen             synthetic.journey
   25.8 ms    46.2 ms  storefront          POST /api/checkout
   30.9 ms    38.3 ms  checkout            POST /orders
   36.5 ms    12.9 ms  checkout              inventory.reserve   -> inventory POST /reservations
   49.7 ms    11.1 ms  checkout              payment.authorize   -> payment POST /authorizations
   61.0 ms     5.2 ms  checkout              order.persist       (order + outbox row, one transaction)
  140.2 ms     3.1 ms  fulfillment-worker  queue.wait
  141.1 ms     2.7 ms  checkout            order.publish        PRODUCER
  143.6 ms   139.0 ms  fulfillment-worker  order.fulfill        CONSUMER
  149.2 ms   102.4 ms  fulfillment-worker    fulfillment.pick
  251.9 ms    12.5 ms  fulfillment-worker    payment.capture
  264.6 ms    12.8 ms  fulfillment-worker    inventory.commit
  476.0 ms    14.9 ms  loadgen             GET (order status) -> storefront -> checkout
  492.8 ms     7.9 ms  loadgen             GET (payment ledger) -> payment
```

What to point out:

- The root `synthetic.journey` lasts until the client has *checked* the
  order - status and payment ledger - not just placed it.
- Between `order.persist` (done at ~66 ms) and `order.publish` (~141 ms) no
  code runs: the message waits as a row in the outbox table until the relay
  polls it (Module 3).
- `queue.wait` is reconstructed. Nobody "performs" waiting in a queue, so
  no SDK creates a span for it; the worker builds one from the publish
  timestamp in the message
  ([`src/webshop/services/fulfillment.py`](../src/webshop/services/fulfillment.py)
  line 191).
- The restart trace in Module 1 is read the same way: between the start of
  a CLIENT span and the start of its SERVER span lies time outside both
  services - the network, DNS, a connection being set up.

---

## Module 3 - Context propagation

**Prerequisites:** the webshop installed, Module 0.

Distributed tracing works only if every service knows which trace an
incoming request belongs to. **Context** is that knowledge inside a
process: the current trace ID and span ID, plus any baggage.
**Propagation** moves it between processes: serialized into a carrier (an
HTTP header, say) on one side, deserialized on the other.

### Context and propagation, by hand

[`otel/propagation.py`](../otel/propagation.py) is the page's example: a
`Frontend` that, while handling `POST /cart/add`, calls the product catalog
twice - here the webshop's inventory, `GET /products`. It uses no HTTP
instrumentation: it injects the context into a plain dict and copies it into
the request headers itself.

```text
kubectl -n webshop exec -i otel-playground -- python - < otel/propagation.py
```

```text
1. before any span, the current context holds an invalid span:
   is_valid=False
2. inside POST /cart/add, the context holds its span:
   trace 9953225c7d089b0e54e81d5b93aed386  span e0764e6951fab14c
3. propagate.inject() wrote into an empty dict:
   {'traceparent': '00-9953225c7d089b0e54e81d5b93aed386-e0764e6951fab14c-03'}
   version=00  trace-id=9953225c7d089b0e54e81d5b93aed386  parent-id=e0764e6951fab14c  trace-flags=03
   trace-flags 03 = sampled (0x01): True, random trace ID (0x02): True
4. propagate.extract() on the receiving side gives back:
   trace 9953225c7d089b0e54e81d5b93aed386  span e0764e6951fab14c  is_remote=True
5. two calls to the product catalog, each with its own traceparent:
   call 1: traceparent 00-9953225c7d089b0e54e81d5b93aed386-410f20aee8831fd2-03 -> HTTP 200 from webshop-inventory-7c6f4bcb78-nb6qq
   call 2: traceparent 00-9953225c7d089b0e54e81d5b93aed386-fc91986a7cc16239-03 -> HTTP 200 from webshop-inventory-7c6f4bcb78-nb6qq
trace ID: 9953225c7d089b0e54e81d5b93aed386
Tempo: paste it - lesson-frontend's POST /cart/add with two GET calls, each continued by inventory
```

1. Before any span exists, the context holds an *invalid* span: there is
   nothing to propagate.
2. Inside `POST /cart/add` it holds that span's trace and span ID.
3. `propagate.inject()` wrote one entry, `traceparent`, in the W3C Trace
   Context format `<version>-<trace-id>-<parent-id>-<trace-flags>`.
4. `propagate.extract()` - what the receiving side runs - turns the text
   back into a span context marked `is_remote=True`: "my parent span lives
   in another process".
5. Each call injected again, from inside its own CLIENT span, so the
   `parent-id` differs per call: inventory's span becomes the child of the
   client span that made the call.

**`03`, not `01`.** The page's example header ends in `-01`: sampled. This
SDK writes `-03`. Bit 0x01 is *sampled*; bit 0x02 is the *random trace ID*
flag of W3C Trace Context Level 2 - the trace ID was generated randomly, so
anyone downstream may sample on its bits. Both mean "sampled". (The SDK
source, `opentelemetry/trace/span.py`, class `TraceFlags`, lists both.)

In Tempo, one trace and two services (inventory's `db.connection.acquire`
spans left out):

```text
POST /cart/add                    lesson-frontend  SERVER
  GET                             lesson-frontend  CLIENT
    GET /products                 inventory        SERVER
      SELECT inventory.products   inventory        CLIENT
  GET                             lesson-frontend  CLIENT
    GET /products                 inventory        SERVER
      SELECT inventory.products   inventory        CLIENT
```

The inventory pods continued *our* trace because of one header - what the
networking track showed with wget (networking Module 4).

**In the webshop** nobody writes this code. The httpx instrumentation
injects on every outgoing call
([`src/webshop/httpclient.py`](../src/webshop/httpclient.py) line 113), the
FastAPI instrumentation extracts on every incoming one
([`src/webshop/runtime.py`](../src/webshop/runtime.py) line 151):
"propagation is usually handled by instrumentation libraries and is
transparent to the user".

### Traces, logs and metrics, in context

The page's three examples, on one real order. Place it, and let the shell
keep its trace ID:

```text
TRACE=$(python3 tools/lab.py journey | awk '/^trace_id/{print $3}')
echo "trace: ${TRACE}"
echo "logs : {service_namespace=\"webshop\"} | trace_id=\"${TRACE}\""
```

**Traces.** Paste the trace ID in Explore (Tempo): about 70 spans, six
services (Module 2).

**Logs.** Every log record written inside one of those spans carries the
trace ID. Paste the printed `logs` query in Explore (Loki). Observed: 10
records from five services, in order - checkout `order created`, inventory
`stock reserved`, payment `authorization authorized`, checkout
`order accepted` and `order message published`, fulfillment-worker
`fulfillment started`, payment `capture succeeded`, inventory
`reservation committed`, fulfillment-worker `order fulfilled`, loadgen
`synthetic journey evaluated: good (none)`. From any span in Tempo,
*Logs for this span* runs the same query.

**Metrics.** "Instead of only looking at the response time of all the
`GET /product` requests, you can also get metrics for combinations": the
Collector's servicegraph connector builds those from the spans themselves.
It pairs each CLIENT span with the SERVER span it caused and counts the
edge, *caller > callee*. The page's table, for the webshop's inventory -
Explore (Prometheus), one query each:

```promql
sum by (client) (rate(traces_service_graph_request_total{server="inventory"}[5m]))
sum by (client) (rate(traces_service_graph_request_server_seconds_sum{server="inventory"}[5m])) / sum by (client) (rate(traces_service_graph_request_server_seconds_count{server="inventory"}[5m]))
```

| Name | Calls per second | Average response time |
|---|---|---|
| `* > inventory` | 2.07 | 7.6 ms |
| `checkout > inventory` | 1.04 | 6.9 ms |
| `fulfillment-worker > inventory` | 1.03 | 8.3 ms |
| `storefront > inventory` | 0.007 | 3.4 ms |

(Drop `by (client)` for the `*` row.) Explore (Tempo) -> query type
**Service Graph** draws the same metrics as a map. The webshop's own
dependency metrics go one step further and name the operation -
`checkout > inventory.reserve`, `fulfillment-worker > inventory.commit`,
`storefront > catalog.list`:

```promql
sum by (service_name, webshop_operation) (rate(webshop_dependency_duration_seconds_count{webshop_dependency="inventory"}[5m]))
```

Metrics also carry context back to traces: a histogram measurement taken
inside a span keeps that span's trace ID as an **exemplar** (Module 5). The
worker even records its queue-wait histogram with the *remote* context from
the message ([`src/webshop/services/fulfillment.py`](../src/webshop/services/fulfillment.py)
line 186), so a long wait links to the trace of the order that waited.

### Custom context propagation

Where no instrumentation library covers a protocol, you call the Propagators
API yourself: **inject** into the carrier on the sending side, **extract**
on the receiving side. And: "it is possible to propagate context in
protocols that do not have a dedicated field for metadata, but you have to
make sure that on the receiving side they are extracted and removed before
the data is processed". [`otel/custom_carrier.py`](../otel/custom_carrier.py)
does exactly that for a label printer that reads plain text lines from a
file:

```text
kubectl -n webshop exec -i otel-playground -- python - < otel/custom_carrier.py
```

```text
producer wrote : tp=00-e1fb397837adcea086243711a6891762-f15885b9aabe38c5-03 ORDER ord-7001 TRL-BLK 1
producer wrote : tp=00-e1fb397837adcea086243711a6891762-e662038ad6a62516-03 ORDER ord-7002 MUG-WHT 1
consumer parsed: 'ORDER ord-7001 TRL-BLK 1' -> label for 1 x TRL-BLK, span under the producer's trace
consumer parsed: 'ORDER ord-7002 MUG-WHT 1' -> label for 1 x MUG-WHT, span under the producer's trace
old reader     : rejected: tp=00-e1fb397837adcea086... is not an order line
trace ID: e1fb397837adcea086243711a6891762
Tempo: paste it - lesson-producer and lesson-consumer in one trace, connected by a text file
```

In Tempo, `labels.batch` and two `label.send` (PRODUCER) spans from
lesson-producer, and under them two `label.print` (CONSUMER) spans from
lesson-consumer: one trace, joined by nothing but a text file. The old
reader is the page's warning: context written into the data itself breaks
every reader that does not take it out again. (Two tracer providers in one
script stand in for two programs - each has its own Resource, so Tempo
shows two services.)

**The webshop has two custom carriers.**

*A database row.* checkout accepts an order and writes its outbox row in
one transaction, and the row stores the current `traceparent`
([`src/webshop/services/checkout.py`](../src/webshop/services/checkout.py)
lines 317-331). The relay - a background task with no request context, maybe
in another replica, a moment later - reads the row, extracts the stored
context and starts `order.publish` as its child (lines 371-379). The rows:

```text
kubectl -n webshop exec webshop-postgresql-0 -c postgresql -- psql -U webshop -d webshop -c "SELECT id, order_id, event_type, traceparent, published_at FROM checkout.outbox ORDER BY id DESC LIMIT 3"
```

```text
  id   |       order_id       |   event_type   |                       traceparent                       |         published_at
-------+----------------------+----------------+---------------------------------------------------------+-------------------------------
 10319 | ord-73891b183c114d0b | order.accepted | 00-b517dafa67ed2934f481f7f5298d50e0-f47ba96a003bbe59-03 | 2026-11-22 23:18:15.234202+00
 10318 | ord-6d8e3061d0064d36 | order.accepted | 00-7408f392563591b8b7eb67a27be860a8-ceebef5a219143ce-03 | 2026-11-22 23:18:14.97816+00
 10317 | ord-592951ae922044aa | order.accepted | 00-9a37d48f59627e962abc3e0c927da5f6-0993031a91d8f44c-03 | 2026-11-22 23:18:14.721402+00
```

Paste one of those trace IDs in Tempo: it is that order's journey.

*AMQP message headers.* The relay injects the context into the message's
headers ([`src/webshop/messaging.py`](../src/webshop/messaging.py) lines
29-37, used in checkout.py line 388); the worker extracts it (fulfillment.py
line 173) and makes `order.fulfill` a CONSUMER span under `order.publish`
(lines 197-201). `python3 tools/lab.py verify` checks exactly that:
`context across the queue: order.fulfill is a child of order.publish`.

### Security: context from outside

"Malicious actors could send forged trace headers to manipulate your
tracing data." The storefront is the shop's front door, and - like most
services by default - it trusts any `traceparent` it receives.
[`otel/forged_context.py`](../otel/forged_context.py) plays an outside client
with no OpenTelemetry at all: a trace header is just text anyone can type.

```text
kubectl -n webshop exec -i otel-playground -- python - < otel/forged_context.py
```

```text
A. trace ID 9cd354834982ac71b37eadcc950913e5 on two unrelated requests:
   HTTP 200 from webshop-storefront-67d6b7456-45k5z
   HTTP 200 from webshop-storefront-67d6b7456-45k5z
B. trace ID 2fbc1f5a146ea5f2941ac9cfd7a45666, trace-flags 00 (not sampled):
   HTTP 200 from webshop-storefront-67d6b7456-45k5z
C. a malformed traceparent:
   HTTP 200 from webshop-storefront-67d6b7456-45k5z

Tempo, A: paste 9cd354834982ac71b37eadcc950913e5 - two storefront requests in one trace, parents that never existed
Tempo, B: paste 2fbc1f5a146ea5f2941ac9cfd7a45666 - not found: the caller switched tracing off for this request
Tempo, C: TraceQL { span.user_agent.original = "otel-lesson/forged-C" } - a new trace; the header was ignored
```

What the storefront made of it:

- **A.** One trace holding both requests, two seconds apart; in Tempo's
  search list its root shows as `<root span not yet received>` - the
  parents named in the headers never existed. Whoever knows a trace ID can
  add requests to that trace.
- **B.** *Trace not found.* The webshop's sampler is
  `ParentBased(ALWAYS_ON)`, "follow the caller's decision", so a `-00` from
  outside switches tracing off for that request and everything it calls
  (the storefront passed `-00`
  on to inventory, which recorded nothing either). The request still
  counted in the metrics - in a test, five such requests raised
  `sum(http_server_request_duration_seconds_count{service_name="storefront", http_route="/api/products"})`
  by exactly 5 - only its trace is gone.
- **C.** A new trace, started by the storefront: a malformed header is
  ignored.

**Outgoing context.** "Be mindful of what you propagate to external
services." checkout sends `traceparent` to payment - in this lab a
simulator, in real life a card processor you do not own. Open any journey
trace: payment's spans hang under checkout's. Internal trace IDs, and any
baggage (Module 7), would leave the company with every call.

What production systems do about it - this lab deliberately does not
(observability.md section 12): drop or replace trace headers at the edge
(the ingress controller or gateway); start a new trace for requests from
outside and keep the incoming context only as a span *link*, so it is
recorded but not trusted; and keep trace headers off calls to third
parties, for instance with an uninstrumented HTTP client for that one
dependency or an egress proxy that removes them.

### Support in the language SDKs: Python

The webshop's language. Its global propagator is set by `OTEL_PROPAGATORS`
(default `tracecontext,baggage`) - ask the SDK which headers it reads and
writes:

```text
kubectl -n webshop exec otel-playground -- python -c "from opentelemetry import propagate; print(propagate.get_global_textmap().fields)"
```

```text
{'tracestate', 'baggage', 'traceparent'}
```

(in any order). `tracestate` carries vendor-specific values next to
`traceparent`; `baggage` is Module 7.

---

## Module 4 - Traces: Tracer Provider, Tracer, Trace Exporters

**Prerequisites:** the webshop installed, Module 0.

The three components, in the order a program meets them: a **Tracer
Provider** - created once, at start-up, holding the Resource, the sampler,
and the span processors with their exporters; **Tracers** - one per piece of
instrumented code, made by the provider; and **Trace Exporters** - they send
finished spans to a consumer: standard output, the Collector, or a backend.
[`otel/trace_pipeline.py`](../otel/trace_pipeline.py) builds them one at a
time:

```text
kubectl -n webshop exec -i otel-playground -- python - < otel/trace_pipeline.py
```

```text
1. API only: opentelemetry.trace.ProxyTracer -> opentelemetry.trace.span.NonRecordingSpan, recording=False, trace_id=0
3. stdout exporter, one line per finished span:
   {"name": "via the Collector", "context": {"trace_id": "0x5cd7d5703e027e7fb40a3e9fb62a9613", "span_id": "0xe560ff74c5483310", "trace_state": "[]"}, "kind": "SpanKind.INTERNAL", "parent_id": null, "start_time": "2026-11-22T23:10:38.413555Z", "end_time": "2026-11-22T23:10:38.413624Z", "status": {"status_code": "UNSET"}, "attributes": {"webshop.cart.items": 3}, "events": [{"name": "quote computed", "timestamp": "2026-11-22T23:10:38.413597Z", "attributes": {"webshop.amount_cents": 26700}}], "links": []}
   the same tracer object, after set_tracer_provider: opentelemetry.trace.ProxyTracer -> opentelemetry.sdk.trace._Span, recording=True
5. TraceIdRatioBased(0.25) kept 230 of 1000 root spans
trace via the Collector: 5cd7d5703e027e7fb40a3e9fb62a9613
trace straight to Tempo: 0a101ba06981653b640c733a09d0a944
Tempo: open both and compare their Resource attributes - which ones did the Collector add?
```

1. **The API without an SDK.** `trace.get_tracer()` before any provider is
   set returns a `ProxyTracer`, and its spans are `NonRecordingSpan`s with
   trace ID 0. Instrumentation libraries depend only on the API: in a
   program without an SDK they cost next to nothing and record nothing.
2. **A Tracer Provider** (the script's step 2 prints nothing): a Resource,
   the sampler, and two span processors - a `SimpleSpanProcessor` feeding
   the **stdout** exporter, a `BatchSpanProcessor` feeding the
   **Collector**.
3. **The same tracer object now records.** The proxy forwards to whatever
   provider is set later - that is how libraries can ask for a tracer when
   they are imported, before the application has configured anything. The
   stdout line is the span's whole data model: name, context (trace ID,
   span ID, trace state), kind, parent (`null`: a root span), start and end
   time, status (`UNSET` - nobody said OK or error), attributes, events,
   links. "Standard output for debugging and development-time": the Simple
   processor exports synchronously at every span end, fine for that, too
   slow for production.
4. **Straight to a backend.** A second provider exports to Tempo's own OTLP
   receiver, with no Collector in between. Compare the two traces'
   **Resource** in Tempo:

   | Resource attribute | via the Collector | straight to Tempo |
   |---|---|---|
   | `service.name`, `service.namespace`, `service.version`, `service.instance.id` | yes | yes |
   | `k8s.pod.name`, `k8s.pod.uid`, `k8s.namespace.name`, `k8s.node.name` | yes | yes |
   | `telemetry.sdk.language`, `.name`, `.version` | yes | yes |
   | `k8s.cluster.name` = `kubeadm-aws-lab` | **yes** | no |
   | `k8s.pod.start_time` | **yes** | no |

   The Collector's `k8sattributes` processor (pod details from the
   Kubernetes API) and its `resource/cluster` processor added those two
   ([`observability/collector/config.yaml`](../observability/collector/config.yaml)
   lines 54 and 72). Exporting straight to a backend works - but then every
   application must know the backend's address (and, outside a lab, its
   credentials), and none of them gets the shared processing: enrichment,
   batching, retries, memory limits, one place to change backends.
5. **The sampler**, the provider's other decision: `TraceIdRatioBased(0.25)`
   kept 230 of 1000 root spans (it varies around 250). The webshop keeps
   every trace with `ParentBased(ALWAYS_ON)`; Module 3, B showed the
   "parent-based" half - the caller's flag wins.

**In the webshop** all of it is in
[`src/webshop/telemetry.py`](../src/webshop/telemetry.py) `setup()` (line
142), called once per process:

| Component | Where | There |
|---|---|---|
| Resource | `build_resource()`, line 124 | service name, namespace and version; `service.instance.id` = the pod name (so it matches `kubectl`); environment; config revision; `OTEL_RESOURCE_ATTRIBUTES` merged in |
| Tracer Provider + sampler | line 186 | `ParentBased(ALWAYS_ON)` |
| Span processor + exporter | line 189 | `BatchSpanProcessor` (queue 8192, a batch every second, at most 512 spans each) -> `OTLPSpanExporter` |
| Global provider | line 196 | `trace.set_tracer_provider()` |
| Tracer | `trace.get_tracer("webshop")` | the instrumentation scope `webshop` in Tempo; the libraries' own tracers show as `opentelemetry.instrumentation.fastapi` and `...httpx` |
| Shutdown | `Telemetry.shutdown()`, line 114 | runs on SIGTERM and flushes the queue, so a terminating pod's last spans still arrive |

"In most applications, a Tracer Provider is initialized once and its
lifecycle matches the application's lifecycle." This lab learned why the
hard way: FastAPI 0.14x ships its own OpenTelemetry layer, which at start-up
added a *second* set of OTLP exporters from the `OTEL_*` environment - every
span left each process twice, and Tempo showed every span twice.
[`src/webshop/runtime.py`](../src/webshop/runtime.py) line 55 switches that
layer off (observability.md section 12).

---

## Module 5 - Metrics: Meter Provider, Meter, Metric Exporter

**Prerequisites:** the webshop installed, Module 0.

The same shape as traces: a **Meter Provider** (once per process: a Resource
and metric readers, each with an exporter), **Meters** (one per piece of
instrumented code), and the **instruments** a meter creates - counters,
up-down counters, histograms, gauges. What differs is *when* data leaves. A
measurement only updates an aggregate in memory; a **reader** collects the
aggregates on its own schedule and hands them to its exporter.

[`otel/metric_pipeline.py`](../otel/metric_pipeline.py) runs a fake checkout
four times a second for a minute, measured by four instruments and read by
two readers - stdout every 30 s, the Collector every 5 s. It takes 65
seconds (the 60-second block below is trimmed):

```text
kubectl -n webshop exec -i otel-playground -- python - < otel/metric_pipeline.py
```

```text
recording for 65 s ...
--- stdout exporter, 30 s after start (cumulative since start):
    lesson.cart.adds [{item}] {'webshop.product.color': 'Black'} = 193
    lesson.cart.adds [{item}] {'webshop.product.color': 'Crimson'} = 64
    lesson.carts.active [{cart}]  = 13
    lesson.checkout.duration [s] count=120 sum=26.42 buckets <=0.1:22 <=0.15:16 <=0.2:12 <=0.25:24 <=0.3:12 <=0.35:16 <=0.4:18
        exemplar: value=0.092 trace_id=d1841d831bd2467a63862f0d1f0a8328
    lesson.checkout.duration_default [s] count=120 sum=26.42 buckets <=5:120
        exemplar: value=0.092 trace_id=d1841d831bd2467a63862f0d1f0a8328
    lesson.queue.depth [{message}]  = 8
--- stdout exporter, 60 s after start (cumulative since start):
    lesson.cart.adds [{item}] {'webshop.product.color': 'Black'} = 361
    lesson.cart.adds [{item}] {'webshop.product.color': 'Crimson'} = 127
    ...
--- stdout exporter, 65 s after start (cumulative since start):
    lesson.cart.adds [{item}] {'webshop.product.color': 'Black'} = 389
    lesson.cart.adds [{item}] {'webshop.product.color': 'Crimson'} = 134
    lesson.carts.active [{cart}]  = 23
    lesson.checkout.duration [s] count=259 sum=56.49 buckets <=0.1:48 <=0.15:37 <=0.2:28 <=0.25:44 <=0.3:29 <=0.35:37 <=0.4:36
        exemplar: value=0.074 trace_id=3f4ad9b6981a20fb6de2c1d3514a60fb
    lesson.checkout.duration_default [s] count=259 sum=56.49 buckets <=5:259
        exemplar: value=0.074 trace_id=3f4ad9b6981a20fb6de2c1d3514a60fb
    lesson.queue.depth [{message}]  = 1
Prometheus (Explore): lesson_cart_adds_total, lesson_carts_active, lesson_queue_depth,
  histogram_quantile(0.95, sum by (le) (rate(lesson_checkout_duration_seconds_bucket[1m])))
  histogram_quantile(0.95, sum by (le) (rate(lesson_checkout_duration_default_seconds_bucket[1m])))
```

What to point out in those lines:

- **Aggregation.** 259 checkouts became one histogram data point: a count,
  a sum and one counter per bucket - the primer's "aggregations over a
  period of time", and the reason a metric cannot tell you *which* checkout
  was slow.
- **Cumulative.** Every export shows the total since the process started
  (Black: 193, 361, 389). The reader exports *cumulative* values - the
  default aggregation temporality - and Prometheus's `rate()` turns them back
  into per-second figures.
- **Bounded attributes.** `webshop.product.color` has two possible values,
  so two series. Put an order ID there and every order becomes a series of
  its own. The webshop's rule: "high-cardinality values (order IDs,
  idempotency keys, SKUs per order) are only ever placed on spans and log
  records" ([`src/webshop/semconv.py`](../src/webshop/semconv.py) line 7).
- **Exemplars.** Each histogram point carries an exemplar: one real
  measurement, with the trace ID of the span it was taken in.
- **Default buckets.** `lesson.checkout.duration_default` records the same
  measurements with the SDK's default boundaries (0, 5, 10, 25, ... 10000 -
  shaped for milliseconds): every one of them lands in a single bucket,
  `<=5` seconds.

Now Prometheus - within half a minute of the end, and the series disappear
5 minutes after it (the Collector's `metric_expiration`; history stays). The
names, as the Collector's Prometheus exporter writes them:

| Instrument | Unit | In Prometheus |
|---|---|---|
| counter `lesson.cart.adds` | `{item}` | `lesson_cart_adds_total` |
| up-down counter `lesson.carts.active` | `{cart}` | `lesson_carts_active` (a gauge) |
| histogram `lesson.checkout.duration` | `s` | `lesson_checkout_duration_seconds_bucket`, `_sum`, `_count` |
| histogram `lesson.checkout.duration_default` | `s` | `lesson_checkout_duration_default_seconds_bucket`, `_sum`, `_count` |
| observable gauge `lesson.queue.depth` | `{message}` | `lesson_queue_depth` |

Dots become underscores; the unit becomes a suffix, unless it is an
annotation in braces; monotonic counters get `_total`. Attributes become
labels (`webshop_product_color`), and the Resource becomes
`job="webshop/lesson-metrics"` (`service.namespace/service.name`),
`instance` (`service.instance.id`) and a few promoted labels
(observability.md section 1). Each run of a script gets a new random
`service.instance.id`, so a new set of series; the webshop's services use
their pod name instead.

Then the same percentile, from each histogram:

```promql
histogram_quantile(0.95, sum by (le) (rate(lesson_checkout_duration_seconds_bucket[1m])))
histogram_quantile(0.95, sum by (le) (rate(lesson_checkout_duration_default_seconds_bucket[1m])))
```

Observed: **0.38 s** and **4.75 s**. Every checkout took 0.05-0.40 s, but
with one bucket from 0 to 5 s Prometheus can only interpolate inside it
(0.95 x 5 = 4.75). The webshop had exactly this: its first percentiles were
useless until [`src/webshop/telemetry.py`](../src/webshop/telemetry.py) got
bucket boundaries in seconds (line 49, with the comment why).

Turn on **Exemplars** in the options of the first query: dots appear on the
graph, and each one opens the trace of one checkout - the link from an
aggregate back to a request. (Prometheus kept 33 of them for that run.) An
exemplar is one representative measurement, not an explanation of the line
next to it.

**In the webshop.** [`src/webshop/telemetry.py`](../src/webshop/telemetry.py):
a `PeriodicExportingMetricReader` every 10 s (line 167,
`OTEL_METRIC_EXPORT_INTERVAL`), and a `MeterProvider` with **views** (line
172). A view changes what an instrument produces without touching the code
that records it; here four body-size histograms of the HTTP instrumentation
are dropped (`DropAggregation`) - about 40 series per pod that answered no
question. The webshop's own instruments for every call to another service
are in [`src/webshop/httpclient.py`](../src/webshop/httpclient.py) lines
60-68: a histogram with explicit buckets, three counters and an up-down
counter, each with bounded attributes only. Prometheus then scrapes the
Collector's exporter once per Collector pod (observability.md section 4).

---

## Module 6 - Logs: the bridge, the Log Record, the Collector

**Prerequisites:** the webshop installed, Module 0.

Logs are the signal with the longest history: every language already has a
logging library, so OpenTelemetry does not replace it - it hooks into it.

### OpenTelemetry logs for applications

The page's components, as [`otel/log_pipeline.py`](../otel/log_pipeline.py)
wires them:

- **Log appender / bridge**: `LoggingHandler`, an ordinary Python
  `logging.Handler`. "As an application developer, the Logs Bridge API
  should not be called by you directly": the script's application code
  calls only `logging` - `log.info(...)`, `log.exception(...)`.
- **Logger Provider**: a Resource and processors with exporters - the same
  shape as traces and metrics.
- **Logger**: the bridge asks the provider for one per Python logger name
  (`lesson.shipping` becomes the instrumentation scope). You never touch it.
- **Log Record Exporters**: standard output (`ConsoleLogRecordExporter`) and
  the Collector (`OTLPLogExporter`).

```text
kubectl -n webshop exec -i otel-playground -- python - < otel/log_pipeline.py
```

```text
--- stdout exporter, the first record in full:
{
  "body": "label printed",
  "severity_number": 9,
  "severity_text": "INFO",
  "attributes": {
    "webshop.order.id": "ord-7001",
    "webshop.carrier": "acme"
  },
  "dropped_attributes": 0,
  "timestamp": "2026-11-22T23:28:50.535810Z",
  "observed_timestamp": "2026-11-22T23:28:50.535907Z",
  "trace_id": "0xae387633c695fe93cc7238145b665a11",
  "span_id": "0x5b581642d605c853",
  "trace_flags": 3,
  "resource": {
    "telemetry.sdk.language": "python",
    "telemetry.sdk.name": "opentelemetry",
    "telemetry.sdk.version": "1.45.0",
    "service.instance.id": "8e8762c3-7a56-4919-9910-e551fc7f2a3e",
    "k8s.pod.name": "otel-playground",
    "k8s.pod.uid": "89e1e5a5-34d0-4a6f-9da9-64c9c9f66578",
    "k8s.namespace.name": "webshop",
    "k8s.node.name": "worker",
    "service.name": "lesson-logs",
    "service.namespace": "webshop"
  },
  "event_name": ""
}
--- stdout exporter: ERROR 'label printer jammed' trace_id=0xae387633c695fe93cc7238145b665a11 attributes=['exception.message', 'exception.stacktrace', 'exception.type', 'webshop.order.id']
--- stdout exporter: WARN 'carrier pickup late' trace_id=0 (no span) attributes=['webshop.carrier', 'webshop.delay_ms']
trace ID: ae387633c695fe93cc7238145b665a11
Loki: {service_name="lesson-logs"}   then add  | severity_text="ERROR"  or  | webshop_carrier="acme"
```

**The Log Record**: "named top-level fields of specific type and meaning",
and "resource and attributes fields of arbitrary value and type". The named
fields: `body`; `severity_number` (9 = INFO, 13 = WARN, 17 = ERROR) and
`severity_text`; `timestamp` (when it happened) and `observed_timestamp`
(when OpenTelemetry saw it - they drift apart when logs are collected later,
from a file for instance); `trace_id`, `span_id`, `trace_flags` (from the
active span, all zeros outside one); `event_name` (set for *events*, a kind
of log record). Then `attributes` (what the code passed in `extra=`) and the
`resource` (who logged).

Three records, not four: `log.debug(...)` was below the logger's level and
never left the process. "OpenTelemetry automatically correlates your logs
and traces": the INFO and ERROR records carry the trace ID of `order.ship`;
the WARN record, written outside any span, does not. In Explore (Loki):

```logql
{service_name="lesson-logs"}
{service_name="lesson-logs"} | severity_text="ERROR"
{service_name="lesson-logs"} | webshop_carrier="acme"
```

Expand the ERROR record: `exception_type=TimeoutError`, `exception_message`,
`exception_stacktrace` - what `log.exception()` added. In Loki the attribute
dots become underscores, and only four resource attributes are index labels
(`service_name`, `service_namespace`, `deployment_environment_name`,
`k8s_namespace_name`). Everything else is *structured metadata* -
filterable as above, but not part of the index, so an order ID cannot blow
it up (observability.md section 5).

**In the webshop**: [`src/webshop/telemetry.py`](../src/webshop/telemetry.py)
`configure_logging()` (line 218) puts the same `LoggingHandler` on the root
logger (line 228), with two filters - uvicorn's colored duplicate of each
message is stripped, and the SDK's own complaints (`failed to export ...`)
are kept out of the pipeline they complain about (line 97) - plus the JSON
formatter for stdout (Module 2). `uvicorn.access` is switched off (line 235):
an access log line per request would only repeat the server span.

### OpenTelemetry logs in the Collector

The page lists what the Collector does with logs. The webshop's Collector
uses each part - its pipelines (bash/zsh; or read
[`observability/collector/config.yaml`](../observability/collector/config.yaml)):

```text
kubectl -n webshop get configmap webshop-collector-config -o jsonpath='{.data.relay}' | grep -A 16 'pipelines:'
```

```text
  pipelines:
    traces:
      receivers: [otlp]
      processors: [memory_limiter, k8sattributes, resource/cluster, batch]
      exporters: [otlp/traces, servicegraph]
    metrics:
      receivers: [otlp, servicegraph]
      processors: [memory_limiter, k8sattributes, resource/cluster, transform/metric-resources, batch]
      exporters: [prometheus]
    logs:
      receivers: [otlp]
      processors: [memory_limiter, k8sattributes, resource/cluster, batch]
      exporters: [otlphttp/logs]
    logs/kubernetes-events:
      receivers: [k8sobjects]
      processors: [memory_limiter, transform/events, resource/events, batch]
      exporters: [otlphttp/logs]
```

- **Receivers** "which parse logs from specific, known sources": next to
  `otlp` for the applications, `k8sobjects` watches the Kubernetes API for
  the namespace's events.
- **The `filelogreceiver`** - deliberately absent. "The first step in
  adopting OpenTelemetry frequently involves deploying a Collector as a
  general-purpose logging agent" that tails container log files; here the
  applications already send OTLP, so tailing their stdout as well would put
  every record into Loki twice. Add one later and you must exclude these
  containers.
- **The `transformprocessor`.** `transform/events` (config.yaml line 89)
  turns each raw Kubernetes event - a nested object - into a readable record
  with flat attributes and a severity, in OTTL, the Collector's transform
  language:

  ```yaml
  - set(attributes["k8s.event.reason"], body["object"]["reason"])
  - set(attributes["k8s.object.kind"], body["object"]["regarding"]["kind"])
  - set(attributes["k8s.object.name"], body["object"]["regarding"]["name"])
  - set(severity_text, "WARN") where body["object"]["type"] == "Warning"
  - set(body, Concat([body["object"]["reason"], body["object"]["regarding"]["kind"], body["object"]["regarding"]["name"], body["object"]["note"]], " "))
  ```

  Creating the playground in Module 0 made such events. Explore (Loki):

  ```logql
  {service_name="kubernetes-events"} | k8s_object_name="otel-playground"
  ```

  ```text
  Scheduled Pod otel-playground Successfully assigned webshop/otel-playground to worker
  Pulled Pod otel-playground Container image "docker.io/bleeng089/webshop:0.2.0" already present on machine
  Created Pod otel-playground Created container: playground
  Started Pod otel-playground Started container playground
  ```

  each with `k8s_event_reason`, `k8s_event_type=Normal`, `k8s_object_kind=Pod`
  and `severity_text=INFO` - plus `Killing Pod otel-playground ...` if an
  earlier playground was deleted within the time range.
- **Exporters.** `otlphttp/logs` sends both pipelines to Loki's native OTLP
  endpoint (`/otlp`). Loki understands OTLP itself, so no exporter has to
  convert the records into another format.

---

## Module 7 - Baggage

**Prerequisites:** the webshop installed, Module 0. The capture at the end
needs a shell on the worker.

Baggage is a key-value store that travels next to the context: set at the
start of a request, readable in every service the request reaches -
"information typically available only at the start of a request", such as a
customer ID, an account, a campaign. The webshop does not use baggage. It
forwards it anyway, as you will see.

[`otel/baggage.py`](../otel/baggage.py): `lesson-frontend` puts customer
c-1042's ID, tier and campaign into baggage and calls `lesson-catalog` - a
tiny HTTP server in the same pod - through the httpx instrumentation the
webshop uses:

```text
kubectl -n webshop exec -i otel-playground -- python - < otel/baggage.py
```

```text
0. frontend baggage: {'webshop.customer.id': 'c-1042', 'webshop.customer.tier': 'gold', 'webshop.campaign': 'spring sale'}
   on the frontend's own span: nothing - baggage is not attributes
1. frontend -> catalog (instrumented client):
     catalog received traceparent: 00-7d6aca611da1fe258dc86a7f288779d8-ea59adef734c1688-03
     catalog received baggage: webshop.customer.id=c-1042,webshop.customer.tier=gold,webshop.campaign=spring+sale
     catalog extracted baggage: {'webshop.customer.id': 'c-1042', 'webshop.customer.tier': 'gold', 'webshop.campaign': 'spring sale'}
     baggage on the catalog's span: none
2. the same again, with the baggage span processor on in the catalog:
     catalog received traceparent: 00-7d6aca611da1fe258dc86a7f288779d8-7955270b0b920d3f-03
     catalog received baggage: webshop.customer.id=c-1042,webshop.customer.tier=gold,webshop.campaign=spring+sale
     catalog extracted baggage: {'webshop.customer.id': 'c-1042', 'webshop.customer.tier': 'gold', 'webshop.campaign': 'spring sale'}
     baggage on the catalog's span: {'webshop.customer.id': 'c-1042', 'webshop.customer.tier': 'gold', 'webshop.campaign': 'spring sale'}
3. an outsider, with a baggage header of its own:
     catalog received traceparent: (none)
     catalog received baggage: webshop.customer.tier=platinum
     catalog extracted baggage: {'webshop.customer.tier': 'platinum'}
     baggage on the catalog's span: {'webshop.customer.tier': 'platinum'}
4. frontend -> the real storefront, same client:
     storefront answered 200; the request carried the same baggage header
trace ID: 7d6aca611da1fe258dc86a7f288779d8
Tempo: paste it - compare the attributes of the two lesson-catalog spans GET /product
```

0. The frontend's baggage is not on the frontend's own span: **baggage is
   not the same as attributes** - it is a separate store.
1. The instrumentation added a `baggage` header next to `traceparent`
   without being asked: the global propagator is `tracecontext,baggage`
   (Module 3). The value is URL-encoded (`spring+sale`). The catalog
   extracted all three entries - and its span still has none of them.
2. To get baggage onto spans, code has to copy it, explicitly. The script's
   `CopyBaggage` is a *baggage span processor* in six lines: at every span
   start, copy each baggage entry into an attribute. Several SDKs ship one.
   With it, "which customers see the slowest catalog?" becomes a span search.
3. "There are no built-in integrity checks": an outsider sent
   `baggage: webshop.customer.tier=platinum`, the catalog believed it, and
   with the processor on wrote it on its span. (It sent no `traceparent`,
   so this request is a separate trace.)
4. The same client called the real storefront - next, where that baggage
   went.

In Tempo, open the trace and compare the two `lesson-catalog` spans
`GET /product`: the first without the `webshop.customer.*` attributes, the
second with them.

**Where the baggage went next. [on the worker]** Capture the HTTP request
headers that reach the pods' port 8080 while the script runs again. Open a
shell on the worker (from `AWS/kubeadm`: the worker's entry of
`terraform output session_manager_commands`), start the capture, and within
60 seconds run the script once more in your other terminal:

```text
sudo timeout 60 tcpdump -i any -A -s 0 -l 'tcp dst port 8080' 2>/dev/null | grep -a -E '^Host: |^baggage: ' | grep -a -B1 '^baggage: '
```

Observed (only requests that carry baggage are printed, each with its
`Host` line):

```text
Host: webshop-storefront.webshop.svc.cluster.local
baggage: webshop.customer.id=c-1042,webshop.customer.tier=gold,webshop.campaign=spring+sale
Host: webshop-inventory.webshop.svc.cluster.local
baggage: webshop.customer.id=c-1042,webshop.customer.tier=gold,webshop.campaign=spring+sale
```

The first pair is what the playground sent the storefront. The second is the
storefront's own call on to inventory, carrying the same baggage: the
storefront never reads baggage, but its server instrumentation extracted it
and its client instrumentation sent it on. The page's security notes, live:
"automatic instrumentation includes Baggage in most of your service's
network requests"; it is "visible to anyone inspecting your network
traffic" (the calls between the webshop's pods are plain HTTP); and
"downstream services could propagate Baggage outside your network" - on a
checkout, checkout would pass it to payment, the stand-in for a card
processor. Hence: no credentials, API keys or personal data in baggage, and
treat whatever arrives as a claim, not a fact.

---

## Module 8 - Profiles: the signal this lab does not have

**Prerequisites:** nothing to read; the webshop installed for the exercise.

Profiles are the fourth signal, still alpha: samples of *where in the code*
resources go - on-CPU, off-CPU (waiting), heap, allocations - usually
collected by a sampling profiler; on Linux, OpenTelemetry's eBPF profiler
does it for every process on a node without code changes. This lab collects
none (observability.md section 12: "the telemetry can locate slow code (a
span), not explain it"). The page's table, with what the lab has:

| Signal | Question it answers | In this lab |
|---|---|---|
| Logs | What discrete events occurred? | Loki |
| Metrics | What is happening at the system level? | Prometheus |
| Traces | How does a request travel through a distributed system? | Tempo |
| Profiles | Which code is responsible for consuming resources? | - |

Where a profile would have helped - exercise 1, slow inventory. Start it for
two minutes:

```text
python3 tools/lab.py fault apply slow-inventory --duration 120
```

After a minute, Explore (Tempo), TraceQL:

```traceql
{ name = "stock.reserve" && duration > 1s }
```

Observed in one of the traces (trimmed, children under their parents):

```text
   15.9 ms   4144.6 ms  checkout   inventory.reserve          ERROR  webshop.outcome=timeout, webshop.retry.count=1
   16.4 ms   2007.9 ms  checkout     POST                     ERROR  error.type=ReadTimeout
   21.8 ms   2140.9 ms  inventory      stock.reserve                 webshop.outcome=reserved
 2159.8 ms      0.4 ms  inventory        INSERT inventory.reservations
 2160.4 ms      0.3 ms  inventory        UPDATE inventory.stock
 2152.5 ms   2007.0 ms  checkout     POST                     ERROR  error.type=ReadTimeout
 2157.7 ms   2177.1 ms  inventory      stock.reserve                 webshop.outcome=already_released
 4329.7 ms      0.5 ms  inventory        INSERT inventory.reservations
 4330.5 ms      0.8 ms  inventory        SELECT inventory.reservations
```

`stock.reserve` takes about 2 seconds; the SQL inside it takes well under a
millisecond, at the very end. (Read the tree, not the clock: by their
times, the first two SQL statements also fall inside the second
`stock.reserve` - their parent ID says they belong to the first. Tempo draws
the waterfall from the parent IDs.) The trace proves *where* the time goes - inside inventory's
reservation handler, outside the database - and nothing more: a lock, a
computation and a sleep look the same from here
([incident 1](incidents/01-slow-inventory.md) is careful not to claim one).
The page's "traces to profiles: from a slow span see the corresponding
profile" is exactly the missing step, and the span is the join key: a
profiler that tags its samples with the active trace and span ID (the
page's "request context correlation") would put the hot code right under
this `stock.reserve`. (The trace shows something else worth a discussion:
inventory finished the first attempt, `reserved`, after checkout had already
given up on it at its 2-second timeout - [incident 3](incidents/03-retry-amplification.md)
is about that.)

End it:

```text
python3 tools/lab.py fault reset
```

---

## Module 9 - Cleanup

```text
python3 tools/lab.py fault reset
kubectl delete -f otel/playground.yaml
```

```text
removed 0 fault(s); load back to the Helm default
pod "otel-playground" deleted
networkpolicy.networking.k8s.io "otel-playground-egress" deleted
networkpolicy.networking.k8s.io "otel-playground-to-collector" deleted
networkpolicy.networking.k8s.io "otel-playground-to-apps" deleted
networkpolicy.networking.k8s.io "otel-playground-to-tempo" deleted
```

The lesson's telemetry ages out by itself: its Prometheus series stop 5
minutes after a script ends and their history goes after 3 days, Tempo
keeps traces for 48 hours and Loki keeps records for 72 hours. Nothing else
in the shop was changed.

---

## What each module showed

| Module | The OpenTelemetry idea | Seen on the webshop |
|---|---|---|
| 0 | the pipeline: SDK -> OTLP -> Collector -> backends | one span, one metric and one log record, each through its own provider, processor and exporter |
| 1 | properly instrumented; reliability; SLI/SLO | new questions answered from existing attributes; 0% 5xx and a 62% SLI at once; a real charged-after-error after a restart |
| 2 | logs, spans, attributes, traces | a log with and without a trace ID; the primer's attribute table on a real span; old vs stable semantic conventions; reading gaps in a waterfall |
| 3 | context and propagation | `traceparent` by hand (flags `03`); per-edge metrics from spans; context through a text file, a database row and AMQP; forged, unsampled and malformed headers |
| 4 | Tracer Provider, Tracer, exporters | a no-op tracer that starts recording; stdout vs Collector vs straight to Tempo; what the Collector adds |
| 5 | Meter Provider, Meter, instruments, exporters | aggregation, cumulative temporality, Prometheus names, exemplars, p95 0.38 s vs 4.75 s |
| 6 | the bridge, the Log Record, the Collector | a Log Record field by field; Kubernetes events through `k8sobjects` and OTTL |
| 7 | baggage | forwarded by instrumentation two hops deep, believed without checks, never on spans unless copied |
| 8 | profiles | a 2-second span with sub-millisecond SQL: where, not why |

---

## Troubleshooting

| Symptom | Cause, fix |
|---|---|
| `Error from server (NotFound): pods "otel-playground" not found` | Module 0 not run (or Module 9 already was): `kubectl apply -f otel/playground.yaml` |
| A script prints `Transient error ... encountered while exporting spans batch, retrying in 0.88s.` and ends with `Failed to export spans batch due to timeout, max retries or shutdown.` | the Collector cannot be reached: the playground's NetworkPolicies are missing (apply `otel/playground.yaml` again) or the Collector is down (`kubectl -n webshop get pods -l app.kubernetes.io/name=collector`) |
| Tempo: *trace not found* right after a script | ingestion takes a few seconds - run the query again. In Module 3, B, it is the expected result |
| Tempo: `trace with id ... not found in the selected time range` | the trace is older than the time range at the top right (Grafana searches only inside it): set it to *Last 3 hours*, or run the script again for a fresh trace ID |
| Explore shows nothing at all after pasting a trace ID | the data source at the top left is still Prometheus, Explore's default: switch it to Tempo |
| A TraceQL search finds nothing you know exists | searches cover only the time range at the top right; widen it. Brand-new spans can take up to a minute to become searchable (opening a trace by ID is faster) |
| Prometheus: no `lesson_*` series | wait half a minute after the script; time range "Last 15 minutes"; the series stop 5 minutes after the script ended |
| `histogram_quantile` returns nothing | the `[1m]` window must fall into the minute the script ran; set the time range to cover it |
| PowerShell: `The '<' operator is reserved for future use.` | `Get-Content -Raw otel\<file>.py \| kubectl -n webshop exec -i otel-playground -- python -` |
| `baggage.py`: `OSError: [Errno 98] Address already in use` | an earlier run is still finishing in the pod; run it again a few seconds later |
| The worker capture prints nothing | the script ran before the capture started or after its 60 seconds; start the capture first |
| `WebshopDuplicateCharge` fires right after the nodes were started | the restart itself (Module 1, *It also happened by itself*), not the lesson - it clears 10 minutes later |

---

> [All steps](../../../../../README.md#learning-path) · Previous: step 6, [Networking on the webshop](networking.md) · Next: step 8, [Tear down](../../../README.md#10-destroy)
