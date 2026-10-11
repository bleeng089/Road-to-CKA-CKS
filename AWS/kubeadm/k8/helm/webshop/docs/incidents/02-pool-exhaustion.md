# Incident 2 - Exhausted database pool

**Start:** `python tools/lab.py fault apply pool-exhaustion`
**Injected (known-fault exercise):** in checkout, 30% of database uses keep
their connection for 2.5 s after their SQL has finished, and synthetic traffic
doubles to 2 journeys/s. Each checkout pod has a pool of 5 connections and
waits at most 2 s for one.

Numbers quoted below were observed on the lab-shaped Kind cluster during the
verification run (2-3 minute windows).
The same exercise on the AWS lab showed the same evidence with somewhat
different counts; healthy latencies there are 3-6x higher on its t3 CPU (AWS
results: [../verification.md](../verification.md)).

## What the user experiences

Checkout becomes slow and erratic: many orders take 5-7 s, some fail, and -
the expensive part - some customers see the checkout "fail" (their browser
gives up) while the order goes through and they are charged anyway.

## Where to begin

**Webshop / User reliability.** The SLI falls to **47.1%**. *Bad journeys by
reason*: **`checkout_failed`** (70 in 2 min) and **`deadline_exceeded`** (37).
Then look at the money stats: *Duplicate charges (range)* rises, and *Payment
audits* shows **`charged_after_error`** - observed **62** in three minutes.

`checkout_failed` here mostly means *the client gave up*: the synthetic
client waits until its 5 s deadline for checkout to answer; checkout's p95 is
**6.31 s**. Checkout mostly does answer - `/api/checkout` returned 207 x 202
against 19 x 503 and 11 x 504 in two minutes - but often after the customer
has left.

## Queries and navigation

1. Where is checkout's time going? Open an exemplar on *Journey duration* or
   search Tempo:

   ```traceql
   { name = "db.connection.acquire" && duration > 500ms }
   ```

   In the trace, `order.create`/`order.persist` start with a long
   `db.connection.acquire` (up to ~2 s) followed by fast SQL. The request is
   **waiting for a connection**, not running a query.

2. Pool pressure vs SQL time - **Database and queue**, pool `checkout`:

   ```promql
   max by (k8s_pod_name) (max_over_time(db_client_connection_count{db_client_connection_pool_name="checkout", db_client_connection_state="used"}[3m]))
   max by (k8s_pod_name) (max_over_time(db_client_connection_pending_requests{db_client_connection_pool_name="checkout"}[2m]))
   histogram_quantile(0.95, sum by (le) (rate(db_client_connection_wait_time_seconds_bucket{db_client_connection_pool_name="checkout"}[2m])))
   histogram_quantile(0.95, sum by (le) (rate(db_client_operation_duration_seconds_bucket{db_client_connection_pool_name="checkout"}[2m])))
   ```

   | Signal | Observed | Normal |
   |---|---|---|
   | connections used (peak) | **5 / 5** on both pods | 0-2 |
   | callers waiting (peak) | **7** | 0 |
   | acquire wait p95 | **1.77 s** | 0.5 ms |
   | **SQL p95** | **4.8 ms** | 4.8 ms |
   | **connection hold p95** | **2.92 s** | 4.8 ms |
   | acquisition timeouts | **36** in 2 min | 0 |

   SQL is as fast as ever; connections are *held* ~600x longer than their SQL
   needs. That gap - hold time vs SQL time - is the signature of connections
   being kept by something other than queries.

3. The database's own view (postgres_exporter):

   ```promql
   sum by (state) (pg_stat_activity_count{datname="webshop"})
   ```

   All **idle** - nothing active, no locks. PostgreSQL is not the bottleneck;
   it is barely being used.

4. The errors that did happen, in Loki:

   ```logql
   {service_name="checkout"} |= "database pool exhausted"
   ```

5. The customers who were charged after an error:

   ```logql
   {service_name="loadgen"} |= "payment audit" | webshop_audit_result="charged_after_error"
   ```

   Each names the order; *Order investigation* shows its checkout finishing
   (`order accepted`) seconds after the client's journey was evaluated as
   `checkout_failed`.

## Evidence and scope

| Claim | Evidence |
|---|---|
| Users affected: SLI 64%; slow and failed checkouts; customers charged after seeing an error | journeys by reason; `charged_after_error` audits |
| Cause class: checkout's connection pool is exhausted | used 5/5, callers waiting, acquire wait p95 1.77 s |
| Not the database | SQL p95 unchanged at 4.7 ms; PostgreSQL connections idle |
| Connections are held while no SQL runs | hold p95 2.92 s vs SQL 4.8 ms |
| Scope: the checkout pool, both replicas; the traffic doubled at the same time | per-pod pool panels; *Synthetic rate (target)* = 2 |
| Load makes it worse | a calculation, not a measurement (Little's law): ~5 checkout database uses per journey x 2 journeys/s x 30% held x 2.5 s = ~7.5 connections held on average, out of 10 in the two pools - saturation with bursts. At 1 journey/s it would be ~3.75. Try `--param rate=0.3` with `python tools/lab.py load set --rate 1` to see the difference |

**What this does not prove:** *what* holds the connections. Nothing in the
telemetry identifies the holder - there is no span for "connection held
without a query". You can say "checkout code paths keep connections for
seconds after their SQL completes"; finding which one needs a code read or a
profiler. An owner tag on pool checkouts would close that gap.

**The second lesson** is the timeout mismatch: the client gives up after 5 s,
checkout keeps working and commits the order. Idempotency prevents a *double*
charge on retry, but nothing tells checkout that nobody is waiting any more.
The late payment audit is what makes this visible.

## Recovery

```text
python tools/lab.py fault reset
```

Observed two minutes later: SLI **100%**, acquire wait p95 **0.5 ms**, hold
p95 **4.8 ms**, checkout p95 **24 ms**, no pending callers, load back to
1 journey/s. The audits keep reporting `charged_after_error` for another
30 s - they re-check journeys evaluated during the incident.
