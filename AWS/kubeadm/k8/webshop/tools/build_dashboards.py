#!/usr/bin/env python3
"""Generate the Grafana dashboards in observability/grafana/dashboards/.

The dashboards are code: this file is the source, the JSON is the build
output (committed, so the chart installs without running anything). Edit
here, then:

    python tools/build_dashboards.py
    helm dependency build ./chart      # repackages ../observability

Every query uses metric and label names the services actually emit (see
docs/observability.md). Standard library only.
"""

from __future__ import annotations

import json
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "observability" / "grafana" / "dashboards"

PROM = {"type": "prometheus", "uid": "prometheus"}
LOKI = {"type": "loki", "uid": "loki"}
TEMPO = {"type": "tempo", "uid": "tempo"}

# Label filter shared by application panels; matches the dashboard variables.
APP = 'deployment_environment_name=~"$env", k8s_namespace_name=~"$namespace", service_name=~"$service", service_version=~"$version", k8s_pod_name=~"$pod"'
NS = 'namespace=~"$namespace"'
RI = "$__rate_interval"
ORDER_LINK = "/d/webshop-order/webshop-order-investigation?var-order_id=${__value.raw}"


# --------------------------------------------------------------- builders

class Layout:
    """Left-to-right, top-to-bottom placement on Grafana's 24-column grid."""

    def __init__(self) -> None:
        self.x = self.y = self.row_h = 0
        self.next_id = 1

    def place(self, panel: dict, w: int, h: int) -> dict:
        if self.x + w > 24:
            self.x, self.y, self.row_h = 0, self.y + self.row_h, 0
        panel["gridPos"] = {"x": self.x, "y": self.y, "w": w, "h": h}
        panel["id"] = self.next_id
        self.next_id += 1
        self.x += w
        self.row_h = max(self.row_h, h)
        return panel

    def row(self, title: str) -> dict:
        if self.x:
            self.x, self.y, self.row_h = 0, self.y + self.row_h, 0
        panel = {"type": "row", "title": title, "collapsed": False, "panels": [],
                 "gridPos": {"x": 0, "y": self.y, "w": 24, "h": 1}, "id": self.next_id}
        self.next_id += 1
        self.y += 1
        return panel


def prom(expr: str, legend: str = "", ref: str = "A", exemplar: bool = False, instant: bool = False) -> dict:
    t = {"datasource": PROM, "expr": expr, "legendFormat": legend or "__auto", "refId": ref,
         "range": not instant, "instant": instant}
    if exemplar:
        t["exemplar"] = True
    return t


def timeseries(title: str, targets: list[dict], unit: str = "short", *, desc: str = "", stack: bool = False,
               thresholds: list[tuple[float, str]] | None = None, min0: bool = True, max_: float | None = None,
               links: list[dict] | None = None) -> dict:
    custom = {"drawStyle": "line", "lineWidth": 1, "fillOpacity": 25 if stack else 8, "showPoints": "never",
              "spanNulls": False, "stacking": {"mode": "normal" if stack else "none", "group": "A"}}
    defaults: dict = {"unit": unit, "custom": custom}
    if min0:
        defaults["min"] = 0
    if max_ is not None:
        defaults["max"] = max_
    if thresholds:
        defaults["thresholds"] = {"mode": "absolute",
                                  "steps": [{"color": "transparent", "value": None}] +
                                           [{"color": c, "value": v} for v, c in thresholds]}
        custom["thresholdsStyle"] = {"mode": "line+area" if len(thresholds) > 1 else "line"}
    if links:
        defaults["links"] = links
    return {"type": "timeseries", "title": title, "description": desc, "datasource": PROM, "targets": targets,
            "fieldConfig": {"defaults": defaults, "overrides": []},
            "options": {"legend": {"displayMode": "list", "placement": "bottom", "showLegend": True},
                        "tooltip": {"mode": "multi", "sort": "desc"}}}


def stat(title: str, expr: str, unit: str = "short", *, desc: str = "",
         steps: list[tuple[float | None, str]] | None = None, decimals: int | None = None) -> dict:
    defaults: dict = {"unit": unit,
                      "thresholds": {"mode": "absolute",
                                     "steps": [{"color": c, "value": v} for v, c in (steps or [(None, "green")])]}}
    if decimals is not None:
        defaults["decimals"] = decimals
    return {"type": "stat", "title": title, "description": desc, "datasource": PROM,
            "targets": [prom(expr, instant=True)],
            "fieldConfig": {"defaults": defaults, "overrides": []},
            "options": {"reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
                        "colorMode": "background", "graphMode": "none", "textMode": "value",
                        "justifyMode": "center", "orientation": "auto"}}


def table(title: str, expr: str, *, desc: str = "", hide: list[str] | None = None,
          rename: dict[str, str] | None = None) -> dict:
    excludes = {k: True for k in (hide or [])}
    excludes["Time"] = True
    return {"type": "table", "title": title, "description": desc, "datasource": PROM,
            "targets": [{**prom(expr, instant=True), "format": "table"}],
            "transformations": [{"id": "organize", "options": {"excludeByName": excludes,
                                                               "renameByName": rename or {}}}],
            "fieldConfig": {"defaults": {}, "overrides": []}, "options": {"showHeader": True}}


def logs(title: str, expr: str, *, desc: str = "") -> dict:
    return {"type": "logs", "title": title, "description": desc, "datasource": LOKI,
            "targets": [{"datasource": LOKI, "expr": expr, "refId": "A", "queryType": "range"}],
            "options": {"showTime": True, "wrapLogMessage": True, "enableLogDetails": True,
                        "sortOrder": "Descending", "dedupStrategy": "none", "prettifyLogMessage": False}}


def text(content: str, title: str = "") -> dict:
    return {"type": "text", "title": title, "options": {"mode": "markdown", "content": content}}


def var_query(name: str, metric: str, label: str, *, label_text: str = "", all_value: str = ".*") -> dict:
    q = f"label_values({metric}, {label})"
    return {"name": name, "label": label_text or name, "type": "query", "datasource": PROM,
            "query": {"query": q, "refId": f"var-{name}"}, "definition": q, "refresh": 2,
            "includeAll": True, "multi": True, "allValue": all_value, "sort": 1,
            "current": {"selected": True, "text": ["All"], "value": ["$__all"]}}


def app_vars() -> list[dict]:
    m = "http_server_request_duration_seconds_count"
    return [
        var_query("env", m, "deployment_environment_name", label_text="environment"),
        var_query("namespace", m, "k8s_namespace_name"),
        var_query("service", m, "service_name"),
        var_query("version", m, "service_version"),
        var_query("pod", m, "k8s_pod_name"),
    ]


def dashboard(uid: str, title: str, panels: list[dict], *, variables: list[dict] | None = None,
              desc: str = "", time_from: str = "now-30m") -> dict:
    return {
        "uid": uid, "title": title, "description": desc, "tags": ["webshop"], "timezone": "browser",
        "editable": True, "graphTooltip": 1, "schemaVersion": 41, "version": 1, "refresh": "30s",
        "time": {"from": time_from, "to": "now"}, "fiscalYearStartMonth": 0, "liveNow": False,
        "templating": {"list": variables or []},
        "annotations": {"list": []},
        "links": [{"type": "dashboards", "tags": ["webshop"], "asDropdown": True, "title": "Webshop dashboards",
                   "includeVars": False, "keepTime": True}],
        "panels": panels,
    }


def hq(q: float, bucket: str, by: str = "", window: str = RI) -> str:
    by_clause = f"le, {by}" if by else "le"
    return f"histogram_quantile({q}, sum by ({by_clause}) (rate({bucket}[{window}])))"


# ------------------------------------------------------------- dashboards

def user_reliability() -> dict:
    L = Layout()
    good = 'webshop_synthetic_journeys_total{webshop_outcome="good"}'
    total = "webshop_synthetic_journeys_total"
    bad_reason = lambda r: f'sum(increase(webshop_synthetic_journeys_total{{webshop_reason="{r}"}}[$__range])) or vector(0)'
    p = [
        L.row("Is the shop working for its customers?"),
        L.place(text(
            "**Good journey** = the synthetic client got an order ID, the order reached *fulfilled* within "
            "**5 s**, the shipped SKU equals the SKU it *remembers* requesting, and the payment simulator's own "
            "ledger shows **exactly one** successful capture of the right amount. HTTP 2xx alone is not success. "
            "Each journey is judged once, at completion or at the deadline (still pending = bad). "
            "Declined-card and bad-quantity probes are validation checks, outside the SLI. "
            "SLO: 99% (illustrative). Start here, then follow a bad journey's trace."), 24, 3),
        L.place(stat("SLI over the dashboard range", f"sum(increase({good}[$__range])) / sum(increase({total}[$__range]))",
                     "percentunit", decimals=2, steps=[(None, "red"), (0.99, "green")],
                     desc="Good / eligible journeys in the selected time range."), 5, 4),
        L.place(stat("SLI, last 5 minutes", "1 - webshop:journeys_bad:ratio_rate5m", "percentunit", decimals=2,
                     steps=[(None, "red"), (0.99, "green")]), 4, 4),
        L.place(stat("Error budget left (range)",
                     f"1 - ((sum(increase({total}{{webshop_outcome=\"bad\"}}[$__range])) / sum(increase({total}[$__range]))) / 0.01)",
                     "percentunit", decimals=0, steps=[(None, "red"), (0, "orange"), (0.5, "green")],
                     desc="Share of the 1% budget (for this range) not yet spent. Negative = SLO missed."), 4, 4),
        L.place(stat("Burn rate (10 min)", "webshop:journeys_bad:ratio_rate10m / 0.01", "x", decimals=1,
                     steps=[(None, "green"), (1, "orange"), (10, "red")],
                     desc="1x spends the budget exactly at the SLO; the page alert fires at 10x on both 10m and 2m."),
                4, 4),
        L.place(stat("Duplicate charges (range)",
                     f'({bad_reason("duplicate_charge")}) + (sum(increase(webshop_synthetic_payment_audits_total{{webshop_audit_result=~"late_duplicate_charge|charged_after_error"}}[$__range])) or vector(0))',
                     steps=[(None, "green"), (1, "red")],
                     desc="Journeys charged twice, plus late audits: duplicates after evaluation, and charges for orders whose checkout the client saw fail."),
                4, 4),
        L.place(stat("Wrong item (range)", bad_reason("wrong_item"), steps=[(None, "green"), (1, "red")]), 3, 4),
        L.place(stat("Missed deadline (range)", bad_reason("deadline_exceeded"), steps=[(None, "green"), (1, "orange")]), 3, 4),
        L.place(stat("Checkout failed (range)", bad_reason("checkout_failed"), steps=[(None, "green"), (1, "orange")]), 3, 4),
        L.place(stat("Journeys evaluated (range)", f"sum(increase({total}[$__range]))", decimals=0), 3, 4),
        L.place(stat("Validation checks unexpected (range)",
                     'sum(increase(webshop_synthetic_checks_total{webshop_check_result="unexpected"}[$__range])) or vector(0)',
                     steps=[(None, "green"), (1, "red")]), 4, 4),
        L.place(stat("Synthetic rate (target)", "max(webshop_synthetic_target_rate_per_second)", "reqps", decimals=1), 4, 4),
        L.row("What is going wrong, and since when"),
        L.place(timeseries("SLI, 5-minute window", [prom("1 - webshop:journeys_bad:ratio_rate5m", "SLI 5m"),
                                                    prom("1 - webshop:journeys_bad:ratio_rate1m", "SLI 1m", "B")],
                           "percentunit", thresholds=[(0.99, "red")], min0=False, max_=1,
                           desc="The red line is the 99% objective."), 12, 8),
        L.place(timeseries("Bad journeys by reason", [prom(
            f'sum by (webshop_reason) (rate({total}{{webshop_outcome="bad"}}[{RI}]))', "{{webshop_reason}}")],
            "reqps", stack=True, desc="Each bad journey has exactly one reason: the first failing check."), 12, 8),
        L.place(timeseries("Journeys by outcome", [prom(f"sum by (webshop_outcome) (rate({total}[{RI}]))",
                                                         "{{webshop_outcome}}")], "reqps", stack=True), 8, 8),
        L.place(timeseries("Journey duration (submit -> fulfilled or deadline)", [
            prom(hq(0.5, "webshop_synthetic_journey_duration_seconds_bucket"), "p50", exemplar=True),
            prom(hq(0.95, "webshop_synthetic_journey_duration_seconds_bucket"), "p95", "B", exemplar=True),
            prom(hq(0.99, "webshop_synthetic_journey_duration_seconds_bucket"), "p99", "C", exemplar=True)],
            "s", thresholds=[(5, "red")],
            desc="Percentiles are aggregates of many journeys. Each exemplar dot is ONE journey: click it to open "
                 "that journey's trace - a representative, not an explanation of every slow one."), 16, 8),
        L.place(timeseries("Payment audits (30 s after evaluation)", [prom(
            f"sum by (webshop_audit_result) (rate(webshop_synthetic_payment_audits_total[{RI}]))",
            "{{webshop_audit_result}}")], "reqps", stack=True,
            desc="clean = ledger matches; charged_late = charged after the deadline verdict; late_duplicate_charge / "
                 "charged_after_error = money taken wrongly."), 8, 7),
        L.place(timeseries("Validation checks (expected rejections)", [prom(
            f"sum by (webshop_synthetic_check, webshop_check_result) (rate(webshop_synthetic_checks_total[{RI}]))",
            "{{webshop_synthetic_check}} {{webshop_check_result}}")], "reqps", stack=True), 8, 7),
        L.place(timeseries("Synthetic client", [
            prom("max(webshop_synthetic_target_rate_per_second)", "target rate"),
            prom(f"sum(rate({total}[{RI}]))", "evaluated/s", "B"),
            prom("max(webshop_synthetic_in_flight)", "in flight", "C"),
            prom(f"sum(rate(webshop_synthetic_skipped_total[{RI}])) or vector(0)", "skipped/s", "D")],
            desc="If 'skipped' is non-zero the client itself is saturated and the SLI under-samples."), 8, 7),
        L.row("Bad journeys - click a trace link, or an order ID for the full story"),
        L.place(logs("Bad journeys (newest first)",
                     '{service_name="loadgen"} | webshop_outcome="bad"',
                     desc="Expand a line: webshop_order_id, webshop_reason, requested vs shipped SKU, and a link to the trace."),
                24, 9),
    ]
    return dashboard("webshop-reliability", "Webshop / User reliability", p,
                     desc="Journey SLI/SLO, duplicate charges, wrong items, deadline misses, journey latency.")


def service_health() -> dict:
    L = Layout()
    reqs = f"http_server_request_duration_seconds_count{{{APP}}}"
    p = [
        L.row("RED per service"),
        L.place(timeseries("Request rate", [prom(f"sum by (service_name) (rate({reqs}[{RI}]))", "{{service_name}}")],
                           "reqps"), 8, 8),
        L.place(timeseries("5xx ratio", [prom(
            f'(sum by (service_name) (rate(http_server_request_duration_seconds_count{{{APP}, http_response_status_code=~"5.."}}[{RI}])) '
            f"or 0 * sum by (service_name) (rate({reqs}[{RI}]))) / sum by (service_name) (rate({reqs}[{RI}]))",
            "{{service_name}}")], "percentunit", thresholds=[(0.05, "red")]), 8, 8),
        L.place(timeseries("p95 latency by route", [prom(
            hq(0.95, f"http_server_request_duration_seconds_bucket{{{APP}}}", "service_name, http_route"),
            "{{service_name}} {{http_route}}", exemplar=True)], "s",
            desc="Exemplars link to one request's trace."), 8, 8),
        L.row("Per replica - does one instance behave differently?"),
        L.place(timeseries("Requests by pod", [prom(
            f"sum by (k8s_pod_name, service_version) (rate({reqs}[{RI}]))", "{{k8s_pod_name}} ({{service_version}})")],
            "reqps"), 8, 8),
        L.place(timeseries("5xx ratio by pod", [prom(
            f'(sum by (k8s_pod_name) (rate(http_server_request_duration_seconds_count{{{APP}, http_response_status_code=~"5.."}}[{RI}])) '
            f"or 0 * sum by (k8s_pod_name) (rate({reqs}[{RI}]))) / sum by (k8s_pod_name) (rate({reqs}[{RI}]))",
            "{{k8s_pod_name}}")], "percentunit"), 8, 8),
        L.place(timeseries("p95 latency by pod", [prom(
            hq(0.95, f"http_server_request_duration_seconds_bucket{{{APP}}}", "k8s_pod_name"),
            "{{k8s_pod_name}}", exemplar=True)], "s"), 8, 8),
        L.place(table("Running instances: version and configuration revision",
                      f"count by (service_name, k8s_pod_name, service_version, webshop_config_revision, k8s_node_name) (target_info{{{APP}}})",
                      hide=["Value"],
                      desc="webshop.config.revision is a hash of each pod's deployed configuration."), 24, 6),
        L.row("Dependencies, retries, timeouts"),
        L.place(timeseries("Dependency attempts by outcome", [prom(
            f"sum by (service_name, webshop_operation, webshop_outcome) (rate(webshop_dependency_attempts_total{{{APP}}}[{RI}]))",
            "{{service_name}} {{webshop_operation}} {{webshop_outcome}}")], "reqps"), 8, 8),
        L.place(timeseries("Retry amplification (attempts per operation)", [prom(
            f"sum by (webshop_operation) (rate(webshop_dependency_attempts_total{{{APP}}}[{RI}])) "
            f"/ sum by (webshop_operation) (rate(webshop_dependency_duration_seconds_count{{{APP}}}[{RI}]))",
            "{{webshop_operation}}")], "short", thresholds=[(1.5, "orange")], min0=False,
            desc="1.0 = no retries. 2.0 = every operation costs the dependency twice the traffic."), 8, 8),
        L.place(timeseries("Retries by reason / timeouts", [
            prom(f"sum by (webshop_operation, webshop_reason) (rate(webshop_dependency_retries_total{{{APP}}}[{RI}]))",
                 "retry {{webshop_operation}} {{webshop_reason}}"),
            prom(f"sum by (webshop_operation) (rate(webshop_dependency_timeouts_total{{{APP}}}[{RI}]))",
                 "timeout {{webshop_operation}}", "B")], "reqps"), 8, 8),
        L.place(timeseries("Dependency operation p95 (incl. retries) vs single attempt p95", [
            prom(hq(0.95, f"webshop_dependency_duration_seconds_bucket{{{APP}}}", "webshop_operation"),
                 "operation {{webshop_operation}}", exemplar=True),
            prom(hq(0.95, f"http_client_request_duration_seconds_bucket{{{APP}}}", "server_address"),
                 "attempt -> {{server_address}}", "B")], "s"), 12, 8),
        L.place(timeseries("In flight", [
            prom(f"sum by (service_name) (http_server_active_requests{{{APP}}})", "server {{service_name}}"),
            prom(f"sum by (service_name, webshop_dependency) (webshop_dependency_in_flight{{{APP}}})",
                 "client {{service_name}}->{{webshop_dependency}}", "B")], "short"), 12, 8),
        L.row("Saturation and business outcomes"),
        L.place(timeseries("CPU used / CPU limit by pod", [prom(
            f'sum by (pod) (rate(container_cpu_usage_seconds_total{{{NS}, container!="", container!="POD"}}[{RI}])) '
            f'/ sum by (pod) (container_spec_cpu_quota{{{NS}, container!="", container!="POD"}} / container_spec_cpu_period{{{NS}, container!="", container!="POD"}})',
            "{{pod}}")], "percentunit", thresholds=[(0.8, "orange")]), 8, 8),
        L.place(timeseries("CPU throttled periods by pod", [prom(
            f'sum by (pod) (rate(container_cpu_cfs_throttled_periods_total{{{NS}, container!=""}}[{RI}])) '
            f'/ sum by (pod) (rate(container_cpu_cfs_periods_total{{{NS}, container!=""}}[{RI}]))', "{{pod}}")],
            "percentunit", desc="Share of 100 ms CFS periods in which the container hit its CPU limit."), 8, 8),
        L.place(timeseries("Checkout results and payments", [
            prom(f"sum by (webshop_outcome, webshop_reason) (rate(webshop_orders_total{{{APP}}}[{RI}]))",
                 "order {{webshop_outcome}} {{webshop_reason}}"),
            prom(f"sum by (webshop_operation, webshop_outcome) (rate(webshop_payments_total{{{APP}}}[{RI}]))",
                 "payment {{webshop_operation}} {{webshop_outcome}}", "B")], "reqps"), 8, 8),
        L.place(logs("Warnings and errors (selected services)",
                     '{service_namespace="webshop", service_name=~"${service:regex}"} | severity_text=~"WARN|ERROR|FATAL"'),
                24, 9),
    ]
    return dashboard("webshop-services", "Webshop / Service health", p, variables=app_vars(),
                     desc="Rate, errors, latency, per-replica comparison, dependencies, retries, saturation.")


def kubernetes() -> dict:
    L = Layout()
    ns = 'namespace=~"$namespace"'
    p = [
        L.row("Resources against requests and limits"),
        L.place(timeseries("CPU usage by pod (cores)", [
            prom(f'sum by (pod) (rate(container_cpu_usage_seconds_total{{{ns}, container!="", container!="POD"}}[{RI}]))', "{{pod}}")],
            "short"), 12, 8),
        L.place(timeseries("Memory working set by pod", [
            prom(f'sum by (pod) (container_memory_working_set_bytes{{{ns}, container!="", container!="POD"}})', "{{pod}}")],
            "bytes"), 12, 8),
        L.place(table("Requests and limits per container", f'sum by (pod, container, resource, unit) (kube_pod_container_resource_requests{{{ns}}})',
                      hide=["unit"], rename={"Value": "request"}), 12, 8),
        L.place(timeseries("CPU throttling by container", [prom(
            f'sum by (pod, container) (rate(container_cpu_cfs_throttled_periods_total{{{ns}, container!=""}}[{RI}])) '
            f'/ sum by (pod, container) (rate(container_cpu_cfs_periods_total{{{ns}, container!=""}}[{RI}]))',
            "{{pod}}/{{container}}")], "percentunit"), 12, 8),
        L.row("Health: restarts, readiness, terminations"),
        L.place(timeseries("Container restarts (increase over 5m)", [prom(
            f"sum by (pod, container) (increase(kube_pod_container_status_restarts_total{{{ns}}}[5m]))",
            "{{pod}}/{{container}}")], "short"), 8, 8),
        L.place(timeseries("Pods not ready", [prom(
            f'sum by (pod) (kube_pod_status_ready{{{ns}, condition="false"}}) > 0', "{{pod}}")], "short",
            desc="A pod listed here is out of its Service's endpoints."), 8, 8),
        L.place(table("Last termination reason (any restart since the pod started)",
                      f"max by (pod, container, reason) (kube_pod_container_status_last_terminated_reason{{{ns}}}) == 1",
                      hide=["Value"], desc="OOMKilled = memory limit; Error = the process exited non-zero."), 8, 8),
        L.row("Replicas and autoscaling"),
        L.place(timeseries("HPA: desired vs current replicas", [
            prom(f"max by (horizontalpodautoscaler) (kube_horizontalpodautoscaler_status_desired_replicas{{{ns}}})",
                 "desired {{horizontalpodautoscaler}}"),
            prom(f"max by (horizontalpodautoscaler) (kube_horizontalpodautoscaler_status_current_replicas{{{ns}}})",
                 "current {{horizontalpodautoscaler}}", "B"),
            prom(f"max by (horizontalpodautoscaler) (kube_horizontalpodautoscaler_spec_max_replicas{{{ns}}})",
                 "max {{horizontalpodautoscaler}}", "C")], "short",
            desc="desired > current for long = the scheduler cannot place the new pods (see Pending pods)."), 12, 8),
        L.place(timeseries("Deployment replicas: available vs desired", [
            prom(f"max by (deployment) (kube_deployment_status_replicas_available{{{ns}}})", "available {{deployment}}"),
            prom(f"max by (deployment) (kube_deployment_spec_replicas{{{ns}}})", "desired {{deployment}}", "B")],
            "short"), 12, 8),
        L.place(timeseries("Pending pods", [prom(f'sum by (pod) (kube_pod_status_phase{{{ns}, phase="Pending"}}) > 0', "{{pod}}")],
                           "short"), 8, 7),
        L.place(timeseries("HPA observed CPU utilisation (% of request)", [prom(
            f'max by (horizontalpodautoscaler) (kube_horizontalpodautoscaler_status_target_metric{{{ns}, metric_name="cpu", metric_target_type="utilization"}})',
            "{{horizontalpodautoscaler}}")], "percent"), 8, 7),
        L.place(timeseries("Node CPU used / allocatable", [prom(
            f"sum by (node) (rate(node_cpu_usage_seconds_total[{RI}])) / sum by (node) (kube_node_status_allocatable{{resource=\"cpu\"}})",
            "{{node}}")], "percentunit", thresholds=[(0.85, "red")],
            desc="All pods on the node, every namespace: is the node itself out of CPU?"), 8, 7),
        L.place(logs("Kubernetes events (this namespace)", '{service_name="kubernetes-events"}',
                     desc="Scheduling failures, probe failures, OOM kills, HPA rescales, image pulls."), 24, 9),
    ]
    variables = [var_query("namespace", "kube_pod_info", "namespace")]
    return dashboard("webshop-kubernetes", "Webshop / Kubernetes and scaling", p, variables=variables,
                     desc="CPU, memory, throttling, restarts, readiness, replicas, HPA, events.")


def data_and_queue() -> dict:
    L = Layout()
    db = 'db_client_connection_pool_name=~"$pool"'
    p = [
        L.row("Application side of the database: pool pressure vs SQL time"),
        L.place(timeseries("Connections used vs pool size", [
            prom(f'max by (db_client_connection_pool_name, k8s_pod_name) (db_client_connection_count{{{db}, db_client_connection_state="used"}})',
                 "used {{k8s_pod_name}}"),
            prom(f"max by (db_client_connection_pool_name) (db_client_connection_max{{{db}}})", "max per pod {{db_client_connection_pool_name}}", "B")],
            "short"), 8, 8),
        L.place(timeseries("Callers waiting for a connection", [prom(
            f"max by (k8s_pod_name) (db_client_connection_pending_requests{{{db}}})", "{{k8s_pod_name}}")], "short"), 8, 8),
        L.place(timeseries("Acquisition timeouts", [prom(
            f"sum by (db_client_connection_pool_name) (rate(db_client_connection_timeouts_total{{{db}}}[{RI}]))",
            "{{db_client_connection_pool_name}}")], "reqps"), 8, 8),
        L.place(timeseries("Connection wait (acquire) p95 / p99", [
            prom(hq(0.95, f"db_client_connection_wait_time_seconds_bucket{{{db}}}", "db_client_connection_pool_name"),
                 "p95 {{db_client_connection_pool_name}}", exemplar=True),
            prom(hq(0.99, f"db_client_connection_wait_time_seconds_bucket{{{db}}}", "db_client_connection_pool_name"),
                 "p99 {{db_client_connection_pool_name}}", "B")], "s"), 12, 8),
        L.place(timeseries("SQL execution p95 by statement", [prom(
            hq(0.95, f"db_client_operation_duration_seconds_bucket{{{db}}}", "db_operation_name, db_collection_name"),
            "{{db_operation_name}} {{db_collection_name}}", exemplar=True)], "s",
            desc="Execution only - pool wait is measured separately, on the left."), 12, 8),
        L.place(timeseries("Connection hold time p95 (borrow -> return)", [prom(
            hq(0.95, f"db_client_connection_use_time_seconds_bucket{{{db}}}", "db_client_connection_pool_name"),
            "{{db_client_connection_pool_name}}")], "s",
            desc="Hold time far above SQL time = connections are kept while no SQL runs."), 12, 7),
        L.place(timeseries("PostgreSQL: server connections by state", [prom(
            'sum by (state) (pg_stat_activity_count{datname="webshop"})', "{{state}}")], "short",
            desc="The database's own view (postgres_exporter). Many 'idle' while pools report 'used' = held, not busy."), 12, 7),
        L.row("Queue: depth, age, waiting vs processing"),
        L.place(timeseries("Queue depth (broker)", [
            prom('sum by (queue) (rabbitmq_detailed_queue_messages_ready{queue=~".*fulfillment"})', "ready {{queue}}"),
            prom('sum by (queue) (rabbitmq_detailed_queue_messages_unacked{queue=~".*fulfillment"})', "unacked {{queue}}", "B"),
            prom('sum by (queue) (rabbitmq_detailed_queue_consumers{queue=~".*fulfillment"})', "consumers {{queue}}", "C")],
            "short"), 8, 8),
        L.place(timeseries("Oldest message age (broker)", [prom(
            '(time() - max by (queue) (rabbitmq_detailed_queue_head_message_timestamp{queue=~".*fulfillment"} > 0)) '
            'and max by (queue) (rabbitmq_detailed_queue_messages_ready{queue=~".*fulfillment"}) > 0', "{{queue}}")],
            "s", desc="From the head message's AMQP timestamp (1 s resolution). Empty queue = no value."), 8, 8),
        L.place(timeseries("Waiting vs processing p95 (worker)", [
            prom(hq(0.95, "webshop_queue_wait_duration_seconds_bucket"), "waiting (publish -> start)", exemplar=True),
            prom(hq(0.95, "messaging_process_duration_seconds_bucket"), "processing (start -> ack)", "B", exemplar=True)],
            "s", desc="Waiting grows with backlog; processing grows when each job is slower."), 8, 8),
        L.place(timeseries("Fulfillment jobs by outcome", [prom(
            f"sum by (webshop_outcome) (rate(webshop_fulfillment_jobs_total[{RI}]))", "{{webshop_outcome}}")],
            "reqps", stack=True), 8, 8),
        L.place(timeseries("Worker throughput vs arrivals", [
            prom(f'sum(rate(webshop_fulfillment_jobs_total{{webshop_outcome="fulfilled"}}[{RI}]))', "fulfilled/s"),
            prom(f'sum(rate(webshop_orders_total{{webshop_outcome="accepted"}}[{RI}]))', "accepted/s", "B"),
            prom("sum(webshop_fulfillment_in_progress)", "in progress", "C")], "short"), 8, 8),
        L.place(timeseries("Accepted -> fulfilled (server-side) p95", [prom(
            hq(0.95, "webshop_order_fulfillment_duration_seconds_bucket"), "p95")], "s", thresholds=[(5, "red")]), 8, 8),
        L.row("Outbox: accepted orders on their way to the broker"),
        L.place(timeseries("Outbox backlog", [
            prom("max(webshop_outbox_pending)", "unpublished rows"),
            prom("max(webshop_outbox_oldest_age_seconds)", "oldest (s)", "B")], "short"), 12, 7),
        L.place(timeseries("Outbox lag p95 and publish errors", [
            prom(hq(0.95, "webshop_outbox_lag_seconds_bucket"), "lag p95 (s)"),
            prom(f'sum(rate(messaging_client_sent_messages_total{{error_type!=""}}[{RI}])) or vector(0)', "publish errors/s", "B")],
            "short"), 12, 7),
    ]
    variables = [var_query("pool", "db_client_connection_max", "db_client_connection_pool_name")]
    return dashboard("webshop-data", "Webshop / Database and queue", p, variables=variables,
                     desc="Pool usage and wait vs SQL time; PostgreSQL connections; queue depth, age, waiting vs processing; outbox.")


def telemetry_health() -> dict:
    L = Layout()
    p = [
        L.row("Is the telemetry itself complete?"),
        L.place(table("Scrape targets", "up", hide=["__name__"], rename={"Value": "up"}), 12, 8),
        L.place(timeseries("Collector: accepted per signal", [
            prom(f"sum(rate(otelcol_receiver_accepted_spans_total[{RI}]))", "spans/s"),
            prom(f"sum(rate(otelcol_receiver_accepted_metric_points_total[{RI}]))", "metric points/s", "B"),
            prom(f"sum(rate(otelcol_receiver_accepted_log_records_total[{RI}]))", "log records/s", "C")], "short"), 12, 8),
        L.place(timeseries("Collector: refused by receivers (back-pressure)", [
            prom(f"sum by (receiver) (rate(otelcol_receiver_refused_spans_total[{RI}])) or vector(0)", "spans {{receiver}}"),
            prom(f"sum by (receiver) (rate(otelcol_receiver_refused_metric_points_total[{RI}])) or vector(0)", "metrics {{receiver}}", "B"),
            prom(f"sum by (receiver) (rate(otelcol_receiver_refused_log_records_total[{RI}])) or vector(0)", "logs {{receiver}}", "C")],
            "short"), 8, 8),
        L.place(timeseries("Collector: export failures", [
            prom(f"sum by (exporter) (rate(otelcol_exporter_send_failed_spans_total[{RI}])) or vector(0)", "spans {{exporter}}"),
            prom(f"sum by (exporter) (rate(otelcol_exporter_send_failed_metric_points_total[{RI}])) or vector(0)", "metrics {{exporter}}", "B"),
            prom(f"sum by (exporter) (rate(otelcol_exporter_send_failed_log_records_total[{RI}])) or vector(0)", "logs {{exporter}}", "C")],
            "short"), 8, 8),
        L.place(timeseries("Collector: export queue fill", [prom(
            "max by (exporter) (otelcol_exporter_queue_size / otelcol_exporter_queue_capacity)", "{{exporter}}")],
            "percentunit", thresholds=[(0.8, "red")], max_=1), 8, 8),
        L.place(timeseries("Applications: SDK drops and export errors", [
            prom(f'sum by (service_name, error_type) (rate(otel_sdk_processor_span_processed_total{{error_type!=""}}[{RI}])) or vector(0)',
                 "span drop {{service_name}} {{error_type}}"),
            prom(f'sum by (service_name, error_type) (rate(otel_sdk_exporter_span_exported_total{{error_type!=""}}[{RI}]))',
                 "span export error {{service_name}} {{error_type}}", "B"),
            prom(f'sum by (service_name, error_type) (rate(otel_sdk_exporter_log_exported_total{{error_type!=""}}[{RI}]))',
                 "log export error {{service_name}} {{error_type}}", "C")], "short",
            desc="Measured inside each app. Note: these ride the same pipeline, so they show partial loss; total loss shows as missing data and down targets."),
                12, 8),
        L.place(timeseries("Applications: spans started vs exported", [
            prom(f"sum by (service_name) (rate(otel_sdk_span_started_total[{RI}]))", "started {{service_name}}"),
            prom(f'sum by (service_name) (rate(otel_sdk_exporter_span_exported_total{{error_type=""}}[{RI}]))',
                 "exported {{service_name}}", "B")], "short"), 12, 8),
        L.row("Backends"),
        L.place(timeseries("Loki: lines received / discarded", [
            prom(f"sum(rate(loki_distributor_lines_received_total[{RI}]))", "received/s"),
            prom(f"sum by (reason) (rate(loki_discarded_samples_total[{RI}])) or vector(0)", "discarded {{reason}}", "B")],
            "short"), 8, 8),
        L.place(timeseries("Tempo: spans received / discarded", [
            prom(f"sum(rate(tempo_distributor_spans_received_total[{RI}]))", "received/s"),
            prom(f"sum by (reason) (rate(tempo_discarded_spans_total[{RI}])) or vector(0)", "discarded {{reason}}", "B")],
            "short"), 8, 8),
        L.place(timeseries("Prometheus: head series and scrape time", [
            prom("prometheus_tsdb_head_series", "head series"),
            prom("max by (job) (scrape_duration_seconds)", "scrape s {{job}}", "B")], "short"), 8, 8),
        L.place(timeseries("Collector process memory", [prom(
            "max by (pod) (otelcol_process_memory_rss_bytes)", "{{pod}}")], "bytes"), 12, 7),
        L.place(timeseries("Synthetic heartbeat: journeys evaluated per second", [prom(
            f"sum(rate(webshop_synthetic_journeys_total[{RI}]))", "journeys/s")], "reqps",
            desc="Zero here means the SLI is blind - not that everything is fine."), 12, 7),
    ]
    return dashboard("webshop-telemetry", "Webshop / Telemetry health", p,
                     desc="Collector accepted/refused/failed, queues, SDK drops, Loki/Tempo/Prometheus ingestion.")


def order_investigation() -> dict:
    L = Layout()
    order_var = {"name": "order_id", "label": "Order ID", "type": "textbox", "query": "",
                 "current": {"text": "", "value": ""}, "options": []}
    trace_var = {"name": "trace_id", "label": "Trace ID", "type": "textbox", "query": "",
                 "current": {"text": "", "value": ""}, "options": []}
    p = [
        L.place(text(
            "Paste an order ID (`ord-...`, from the UI, a bad-journey log line, or `tools/lab.py journey`). "
            "The logs below are every record carrying that order ID, from every service, in order; the table "
            "lists the traces that touched it (the synthetic journey trace includes checkout, the queue hop and "
            "fulfilment). Click a trace ID, or paste it into *Trace ID* for the waterfall here."), 24, 3),
        L.place(logs("Order timeline: every log record for this order",
                     '{service_namespace="webshop"} | webshop_order_id="$order_id"'), 24, 11),
        L.place({"type": "table", "title": "Traces that touched this order", "datasource": TEMPO,
                 "targets": [{"datasource": TEMPO, "refId": "A", "queryType": "traceql", "limit": 20,
                              "tableType": "traces", "query": '{ span.webshop.order.id = "$order_id" }'}],
                 "fieldConfig": {"defaults": {}, "overrides": []}, "options": {"showHeader": True}}, 24, 7),
        L.place({"type": "traces", "title": "Trace", "datasource": TEMPO,
                 "targets": [{"datasource": TEMPO, "refId": "A", "queryType": "traceql", "query": "$trace_id"}]},
                24, 14),
    ]
    return dashboard("webshop-order", "Webshop / Order investigation", p, variables=[order_var, trace_var],
                     desc="Everything about one order ID: logs from all services and the traces that touched it.",
                     time_from="now-3h")


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    builders = {
        "01-user-reliability.json": user_reliability,
        "02-service-health.json": service_health,
        "03-kubernetes-scaling.json": kubernetes,
        "04-database-queue.json": data_and_queue,
        "05-telemetry-health.json": telemetry_health,
        "06-order-investigation.json": order_investigation,
    }
    for name, build in builders.items():
        path = OUT / name
        path.write_text(json.dumps(build(), indent=1) + "\n", encoding="utf-8")
        print(f"wrote {path.relative_to(OUT.parent.parent.parent)}")


if __name__ == "__main__":
    main()
