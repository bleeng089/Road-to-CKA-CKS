# Blind exercises

> **Step 5 of 8: Incident and blind exercises** · The blind exercises, after the nine walkthroughs · Run from `AWS/kubeadm/k8/helm/webshop/` · Needs: the webshop (step 4)

The known-fault walkthroughs teach where to look. A blind exercise tests
whether the system is instrumented well enough that you *can* look: an unknown
combination of at least two faults starts at an unknown moment, and you
diagnose it from telemetry that was already being collected.

**Do not open `docs/answer-key/`, the lab-control API, or the lab-control
pod's logs until you have written your diagnosis.** (`tools/lab.py` reads the
scenario definitions from the answer-key directory; running `blind start` is
fine, reading the file is not.)

## Run one

```text
python3 tools/lab.py blind start
```

This sets every scenario up the same way (checkout and inventory held at
exactly two replicas for the exercise - visible, ordinary deployment context,
and it keeps a targeted pod from being scaled away), picks a scenario at random,
and arms its faults to begin 30 s to 3 min later, for 15 minutes. It also
records a fingerprint of every workload template and telemetry ConfigMap:
from now on, the instrumentation is **frozen**.

Optional: `--seed N` (repeatable choice), `--onset-max 300`, `--duration 1200`.

## Rules

Allowed:

- Grafana: every dashboard, Explore, any PromQL / LogQL / TraceQL you can write,
  the Alerting page, building new panels or saving queries.
- Read-only `kubectl`: `get`, `describe`, `top`, `events` - ordinary
  operational context.
- The storefront UI, as a customer would use it.

Not allowed until your diagnosis is written down:

- adding or changing instrumentation, log levels, sampling, or telemetry
  configuration; redeploying with different code or values;
- restarting, scaling, or deleting pods (it destroys evidence - and in real
  life you would be guessing);
- reading the answer key, lab-control, or `kubectl logs` of lab-control.

If you cannot reach a conclusion without breaking a rule, stop there: that is
the finding. Write down what was missing as an **instrumentation gap** (see
the report template), then continue if you like - `blind reveal` will report
any change you made.

## Method that works

1. **Impact first.** *User reliability*: is the SLI below objective? Which
   bad reasons, how many, since when? Are there duplicate charges or wrong
   items (those are the expensive ones)?
2. **Where.** *Service health*: which services' latency or errors changed, on
   which routes - and **per pod**: one replica or all of them? Which versions
   and configuration revisions are running?
3. **Synchronous or asynchronous?** *Database and queue*: pool pressure vs SQL
   time; queue depth, oldest message, waiting vs processing time; outbox lag.
4. **One example, end to end.** Click an exemplar, or take an order ID from a
   bad journey's log line, and read its trace and its logs (*Order
   investigation*). Find where the time or the wrong decision is.
5. **Platform?** *Kubernetes and scaling*: throttling, restarts, readiness,
   HPA, events - cause or consequence?
6. **Can you trust the data?** *Telemetry health*: refused/failed exports,
   SDK drops. A missing signal is not a healthy signal.
7. **Count your faults.** Every scenario has at least two independent causes.
   When one explanation does not cover all the evidence, keep going.

## Report template

Write this down before `blind reveal`:

```text
Detected at:        (time; and how - which alert or panel)
User impact:        (SLI value; bad reasons and counts; money: duplicate charges / wrong items)
Onset:              (time the first evidence appears, per problem)

Problem 1:
  Where:            (service, operation, pod/version/config revision if specific)
  What:             (what the telemetry shows happening - not the mechanism you imagine behind it)
  Evidence:         (query or panel -> value; one example trace/order ID)
  Scope:            (all instances or one? which operations? which users?)
  Confidence:       (high / medium / low, and why)

Problem 2: ...

Ruled out:          (what you checked and why it is not the cause)
Not determinable:   (what you could not establish from the telemetry)
Instrumentation gaps: (what you would have added; "none" is a valid answer only if true)
Remediation:        (what you would do, in what order)
```

Keep cause and evidence apart: "the `stock.reserve` span on pod X takes 2 s
while its SQL takes 4 ms" is evidence; "the code is waiting for a lock" is a
hypothesis the spans cannot support.

## Reveal and stop

```text
python3 tools/lab.py blind reveal
python3 tools/lab.py blind stop
```

`reveal` prints the scenario, when the faults really began, a pointer into
`docs/answer-key/blind-answers.md`, and whether the instrumentation fingerprint
is unchanged. `stop` clears the faults and restores the replica settings.

## Scoring yourself

| Points | For |
|---|---|
| 2 per problem | correct component **and** scope (one replica vs all; which operation) |
| 1 per problem | onset within a few minutes |
| 2 | user impact quantified from the SLI (reasons, counts, money) |
| 1 per claim | every claim backed by a query or a trace, not by inference |
| -2 | any claim about a mechanism the telemetry cannot show |
| -2 | instrumentation changed before the diagnosis was written (`reveal` tells you) |

A "not determinable" written honestly is worth more than a confident guess.

---

> **Next:** [networking.md](networking.md), step 6: networking on the webshop
