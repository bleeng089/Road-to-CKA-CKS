#!/usr/bin/env python3
"""Operator CLI for the webshop lab: faults, load, blind exercises, verification.

Run from AWS/kubeadm/k8/helm/webshop/ with kubectl pointed at the cluster
(KUBECONFIG / current context, or --context). Standard library only; works
from PowerShell, cmd, macOS and Linux.

    python tools/lab.py status                        what lab-control sees
    python tools/lab.py fault presets                 the known-fault exercises
    python tools/lab.py fault apply slow-inventory    start one (see docs/incidents/)
    python tools/lab.py fault apply policy-drop       the one Kubernetes-level fault: edits a NetworkPolicy
    python tools/lab.py fault list | remove NAME | reset
    python tools/lab.py fault set NAME --kind K --service S [--instance POD] [--param k=v ...]
    python tools/lab.py load set --rate 6 [--duration 600] | load reset
    python tools/lab.py blind start | reveal | stop    (docs/blind-exercises.md)
    python tools/lab.py journey [SKU]                  one synthetic journey, verdict + trace ID
    python tools/lab.py verify                         end-to-end telemetry verification
    python tools/lab.py grafana-password
    python tools/lab.py forward grafana|storefront|prometheus|lab-control

lab-control is reached through `kubectl port-forward` (it is never exposed,
and NetworkPolicy admits only the webshop's own pods). policy-drop is the
exception: it changes the cluster, not lab-control - it saves the
NetworkPolicy it edits to .lab-state/ and `fault reset` puts it back.
"""

from __future__ import annotations

import argparse
import base64
import contextlib
import hashlib
import json
import random
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATE_DIR = ROOT / ".lab-state"
SCENARIOS = ROOT / "docs" / "answer-key" / "blind-scenarios.json"
NETPOL_BACKUP = STATE_DIR / "netpol-backup.json"

# ------------------------------------------------------------- presets
# The known-fault exercises (docs/incidents/NN-*.md). "replica" targets are
# resolved to a pod name when applied.
PRESETS: dict[str, dict] = {
    "slow-inventory": {
        "doc": "docs/incidents/01-slow-inventory.md",
        "faults": [{"kind": "latency", "service": "inventory",
                    "params": {"operation": "reserve", "delay_ms": 1700, "jitter_ms": 500, "rate": 1.0}}],
    },
    "pool-exhaustion": {
        "doc": "docs/incidents/02-pool-exhaustion.md",
        "faults": [{"kind": "pool_hog", "service": "checkout",
                    "params": {"mode": "slow_txn", "rate": 0.3, "hold_ms": 2500}}],
        "load": {"rate": 2.0},
    },
    "retry-amplification": {
        "doc": "docs/incidents/03-retry-amplification.md",
        "faults": [{"kind": "payment_failures", "service": "payment",
                    "params": {"operation": "any", "error_rate": 0.35, "timeout_rate": 0.15, "timeout_delay_ms": 2500}}],
    },
    "bad-replica": {
        "doc": "docs/incidents/04-bad-replica.md",
        "faults": [{"kind": "runtime_override", "service": "checkout", "instance": "replica:0",
                    "params": {"inventory_timeout_ms": 1}}],
    },
    "wrong-fulfillment": {
        "doc": "docs/incidents/05-incorrect-fulfillment.md",
        "faults": [{"kind": "wrong_variant", "service": "fulfillment", "params": {"rate": 0.3}}],
    },
    "traffic-surge": {
        "doc": "docs/incidents/06-insufficient-capacity.md",
        "faults": [],
        "load": {"rate": 30.0},
    },
    "worker-backlog": {
        "doc": "docs/incidents/07-worker-backlog.md",
        "faults": [{"kind": "throughput", "service": "fulfillment", "params": {"concurrency": 1, "extra_delay_ms": 1200}}],
    },
    # Explicit safeguard bypass: fresh idempotency keys on retries. Off unless
    # you apply it; see docs/incidents/08-duplicate-charges.md.
    "duplicate-charges": {
        "doc": "docs/incidents/08-duplicate-charges.md",
        "safeguard_bypass": True,
        "faults": [
            {"kind": "payment_failures", "service": "payment",
             "params": {"operation": "capture", "timeout_rate": 0.4, "timeout_delay_ms": 2000}},
            {"kind": "idempotency_bypass", "service": "fulfillment"},
        ],
    },
    # Not an application fault: removes payment from fulfillment's egress
    # allow-list, the kind of change a policy "tightening" makes by accident.
    "policy-drop": {
        "doc": "docs/incidents/09-policy-drop.md",
        "faults": [],
        "netpol": {"policy": "fulfillment-egress", "drop": "payment"},
    },
}


# ------------------------------------------------------------- plumbing

class Lab:
    def __init__(self, args: argparse.Namespace):
        self.ns = args.namespace
        self.release = args.release
        self.context = args.context

    def kubectl(self, *args: str, check: bool = True, input_text: str | None = None) -> str:
        cmd = ["kubectl"] + (["--context", self.context] if self.context else []) + ["-n", self.ns, *args]
        result = subprocess.run(cmd, text=True, capture_output=True, input=input_text)
        if check and result.returncode != 0:
            raise SystemExit(f"kubectl {' '.join(args)} failed:\n{result.stderr.strip()}")
        return result.stdout

    def svc(self, component: str) -> str:
        return f"{self.release}-{component}"

    @contextlib.contextmanager
    def forward(self, service: str, port: int):
        """Port-forward to a Service on a random local port; yields the base URL."""
        cmd = ["kubectl"] + (["--context", self.context] if self.context else []) + \
              ["-n", self.ns, "port-forward", f"svc/{service}", f":{port}"]
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            line = proc.stdout.readline()
            m = re.search(r"127\.0\.0\.1:(\d+)", line)
            if not m:
                raise SystemExit(f"port-forward to {service}:{port} failed: {line}{proc.stderr.read()}")
            yield f"http://127.0.0.1:{m.group(1)}"
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()


def http(method: str, url: str, body=None, headers: dict | None = None, timeout: float = 15.0):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, method=method, data=data,
                                 headers={"Content-Type": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            return resp.status, (json.loads(raw) if raw and raw[:1] in (b"{", b"[") else raw.decode())
    except urllib.error.HTTPError as err:
        raw = err.read()
        try:
            return err.code, json.loads(raw)
        except ValueError:
            return err.code, raw.decode(errors="replace")


def parse_params(pairs: list[str]) -> dict:
    out = {}
    for pair in pairs or []:
        key, _, value = pair.partition("=")
        try:
            out[key] = json.loads(value)
        except ValueError:
            out[key] = value
    return out


def resolve_instance(instances: list[dict], service: str, target: str | None, lab: "Lab | None" = None) -> str | None:
    if not target or not target.startswith("replica:"):
        return target
    # The live pod list, not lab-control's 30 s instance cache: a pod that was
    # just replaced or scaled away must never be picked.
    polling = {i["instance"] for i in instances if i["service"] == service}
    pods = [p for p in ready_pods(lab, service) if p in polling] if lab else sorted(polling)
    index = int(target.split(":", 1)[1])
    if index >= len(pods):
        raise SystemExit(f"{service} has {len(pods)} instance(s) polling lab-control; cannot target replica {index}. "
                         f"Scale it first (e.g. kubectl -n <ns> scale deploy/<release>-{service} --replicas=2).")
    return pods[index]


def apply_faults(base: str, name: str, faults: list[dict], *, duration: float | None, delay: float | None,
                 quiet: bool = False, lab: "Lab | None" = None) -> None:
    _, inst = http("GET", f"{base}/v1/instances")
    instances = inst.get("instances", [])
    for i, fault in enumerate(faults):
        spec = {**fault, "instance": resolve_instance(instances, fault["service"], fault.get("instance"), lab)}
        if duration:
            spec["duration_s"] = duration
        if delay:
            spec["delay_s"] = delay
        fault_name = name if len(faults) == 1 else f"{name}-{i + 1}"
        status, body = http("PUT", f"{base}/v1/faults/{fault_name}", spec)
        if status != 200:
            raise SystemExit(f"lab-control rejected {fault_name}: {body}")
        if not quiet:
            target = spec["instance"] or "all instances"
            print(f"fault {fault_name}: {spec['kind']} on {spec['service']} ({target}) params={body['params']}")


def netpol_drop(lab: Lab, policy: str, drop: str) -> None:
    """Remove one destination from a NetworkPolicy's egress allow-list,
    keeping the original in .lab-state/ for netpol_restore."""
    if NETPOL_BACKUP.exists():
        raise SystemExit("a policy fault is already applied; `fault reset` first")
    obj = json.loads(lab.kubectl("get", "networkpolicy", lab.svc(policy), "-o", "json"))
    for key in ("resourceVersion", "uid", "creationTimestamp", "generation", "managedFields"):
        obj["metadata"].pop(key, None)
    STATE_DIR.mkdir(exist_ok=True)
    NETPOL_BACKUP.write_text(json.dumps(obj, indent=2))
    rules = []
    for rule in obj["spec"].get("egress", []):
        kept = [peer for peer in rule.get("to", [])
                if peer.get("podSelector", {}).get("matchLabels", {}).get("app.kubernetes.io/component") != drop]
        # an egress rule with an empty `to` allows every destination - drop the rule instead
        if kept:
            rules.append({**rule, "to": kept})
    obj["spec"]["egress"] = rules
    lab.kubectl("replace", "-f", "-", input_text=json.dumps(obj))
    print(f"networkpolicy {lab.svc(policy)}: removed egress to {drop} (original saved in {NETPOL_BACKUP.name})")


def netpol_restore(lab: Lab) -> None:
    if not NETPOL_BACKUP.exists():
        return
    saved = NETPOL_BACKUP.read_text()
    name = json.loads(saved)["metadata"]["name"]
    if lab.kubectl("get", "networkpolicy", name, "--ignore-not-found", "-o", "name").strip():
        lab.kubectl("replace", "-f", "-", input_text=saved)
    else:
        lab.kubectl("create", "-f", "-", input_text=saved)
    NETPOL_BACKUP.unlink()
    print(f"networkpolicy {name}: restored")


# ------------------------------------------------------------- commands

def cmd_status(lab: Lab, _: argparse.Namespace) -> None:
    with lab.forward(lab.svc("lab-control"), 80) as base:
        _, inst = http("GET", f"{base}/v1/instances")
        _, faults = http("GET", f"{base}/v1/faults")
        _, load = http("GET", f"{base}/v1/load")
    print("instances polling lab-control:")
    for i in inst["instances"]:
        print(f"  {i['service']:12} {i['instance']:45} version={i['version']}")
    print(f"faults: {len(faults['faults'])} (`fault list` for details)")
    print(f"load override: {load['load'] or 'none (Helm default rate)'}")


def cmd_fault(lab: Lab, args: argparse.Namespace) -> None:
    if args.action == "presets":
        for name, preset in PRESETS.items():
            flag = "  [SAFEGUARD BYPASS]" if preset.get("safeguard_bypass") else ""
            print(f"{name:22} {preset['doc']}{flag}")
        return
    if args.action == "apply" and args.name in PRESETS and PRESETS[args.name].get("netpol"):
        netpol_drop(lab, **PRESETS[args.name]["netpol"])
        print(f"walkthrough: {PRESETS[args.name]['doc']}")
        return
    if args.action == "remove" and PRESETS.get(args.name, {}).get("netpol"):
        netpol_restore(lab)
        return
    with lab.forward(lab.svc("lab-control"), 80) as base:
        if args.action == "list":
            _, body = http("GET", f"{base}/v1/faults")
            if NETPOL_BACKUP.exists():
                name = json.loads(NETPOL_BACKUP.read_text())["metadata"]["name"]
                print(f"{'policy-drop':24} {'networkpolicy':18} {name} edited (original in .lab-state/)")
            elif not body["faults"]:
                print("no faults")
            for f in body["faults"]:
                window = ""
                if f.get("starts_at") and f["starts_at"] > body["now"]:
                    window = f" starts in {int(f['starts_at'] - body['now'])}s"
                if f.get("expires_at"):
                    window += f" expires in {int(f['expires_at'] - body['now'])}s"
                print(f"{f['name']:24} {f['kind']:18} {f['service']:12} {f.get('instance') or 'all':40} {f['params']}{window}")
        elif args.action == "apply":
            preset = PRESETS.get(args.name)
            if preset is None:
                raise SystemExit(f"unknown preset {args.name!r}; try `fault presets`")
            if preset.get("safeguard_bypass") and not args.yes:
                print("This preset DISABLES the idempotency safeguard (fresh keys on every retry) to demonstrate "
                      "duplicate charges. Re-run with --yes to confirm.")
                return
            faults = [dict(f) for f in preset["faults"]]
            for f in faults:
                if args.instance:
                    f["instance"] = args.instance
                elif args.replica is not None:
                    f["instance"] = f"replica:{args.replica}"
                f["params"] = {**f.get("params", {}), **parse_params(args.param)}
            apply_faults(base, args.name, faults, duration=args.duration, delay=None, lab=lab)
            if preset.get("load"):
                load = {**preset["load"], **({"duration_s": args.duration} if args.duration else {})}
                http("PUT", f"{base}/v1/load", load)
                print(f"load: {preset['load']}")
            print(f"walkthrough: {preset['doc']}")
        elif args.action == "set":
            fault = {"kind": args.kind, "service": args.service, "instance": args.instance,
                     "params": parse_params(args.param)}
            apply_faults(base, args.name, [fault], duration=args.duration, delay=args.delay, lab=lab)
        elif args.action == "remove":
            status, body = http("DELETE", f"{base}/v1/faults/{args.name}")
            if status == 404:
                # a multi-fault preset is stored as NAME-1, NAME-2, ...
                _, listing = http("GET", f"{base}/v1/faults")
                names = [f["name"] for f in listing["faults"] if f["name"].startswith(f"{args.name}-")]
                for n in names:
                    http("DELETE", f"{base}/v1/faults/{n}")
                print(f"removed {names or 'nothing'}")
            else:
                print(f"removed {args.name}")
        elif args.action == "reset":
            _, body = http("DELETE", f"{base}/v1/faults")
            print(f"removed {body['deleted']} fault(s); load back to the Helm default")
            netpol_restore(lab)


def cmd_load(lab: Lab, args: argparse.Namespace) -> None:
    with lab.forward(lab.svc("lab-control"), 80) as base:
        if args.action == "set":
            body = {"rate": args.rate}
            if args.duration:
                body["duration_s"] = args.duration
            _, resp = http("PUT", f"{base}/v1/load", body)
            print(f"load: {resp['load']}")
        else:
            http("PUT", f"{base}/v1/load", {})
            print("load back to the Helm default")


def fingerprint(lab: Lab) -> dict:
    """What 'frozen instrumentation' means here: workload images and pod
    templates, plus the telemetry/app configuration ConfigMaps."""
    workloads = json.loads(lab.kubectl("get", "deploy,statefulset", "-o", "json"))
    cms = json.loads(lab.kubectl("get", "configmap", "-o", "json"))
    parts = {}
    for item in workloads["items"]:
        spec = item["spec"]["template"]["spec"]
        parts[f"{item['kind']}/{item['metadata']['name']}"] = {
            "images": [c["image"] for c in spec["containers"]],
            "env": [c.get("env") for c in spec["containers"]],
            "annotations": item["spec"]["template"]["metadata"].get("annotations", {}),
        }
    for item in cms["items"]:
        if item["metadata"]["name"].startswith(lab.release):
            parts[f"ConfigMap/{item['metadata']['name']}"] = hashlib.sha256(
                json.dumps(item.get("data", {}), sort_keys=True).encode()).hexdigest()
    digest = hashlib.sha256(json.dumps(parts, sort_keys=True).encode()).hexdigest()
    return {"digest": digest, "parts": parts}


def pin_replicas(lab: Lab, wanted: dict[str, int]) -> dict:
    """Hold each component at exactly N replicas for the exercise (HPA min =
    max = N, or a plain scale), so a pod targeted at the start is still there
    at the end. Returns what to restore afterwards."""
    restore = {}
    for component, n in wanted.items():
        name = lab.svc(component)
        hpa = lab.kubectl("get", "hpa", name, "-o", "jsonpath={.spec.minReplicas} {.spec.maxReplicas}",
                          check=False).split()
        if hpa:
            restore[component] = {"hpa_min": int(hpa[0]), "hpa_max": int(hpa[1])}
            lab.kubectl("patch", "hpa", name, "--type", "merge", "-p",
                        json.dumps({"spec": {"minReplicas": n, "maxReplicas": n}}))
        else:
            current = int(lab.kubectl("get", "deploy", name, "-o", "jsonpath={.spec.replicas}").strip() or 1)
            restore[component] = {"replicas": current}
            lab.kubectl("scale", f"deploy/{name}", f"--replicas={n}")
    return restore


def restore_replicas(lab: Lab, restore: dict) -> None:
    for component, what in restore.items():
        name = lab.svc(component)
        if "hpa_min" in what:
            lab.kubectl("patch", "hpa", name, "--type", "merge", "-p",
                        json.dumps({"spec": {"minReplicas": what["hpa_min"], "maxReplicas": what["hpa_max"]}}))
        elif "replicas" in what:
            lab.kubectl("scale", f"deploy/{name}", f"--replicas={what['replicas']}")


def ready_pods(lab: Lab, component: str) -> list[str]:
    """Running, Ready, not terminating pods of a component, sorted by name."""
    pods = json.loads(lab.kubectl("get", "pods", "-l",
                                  f"app.kubernetes.io/instance={lab.release},app.kubernetes.io/component={component}",
                                  "-o", "json"))["items"]
    return sorted(p["metadata"]["name"] for p in pods
                  if not p["metadata"].get("deletionTimestamp")
                  and p["status"].get("phase") == "Running"
                  and all(c.get("ready") for c in p["status"].get("containerStatuses", [])))


def wait_for_replicas(lab: Lab, base: str, wanted: dict[str, int], timeout_s: float = 240) -> None:
    """Wait until each component has exactly N ready pods and all of them poll lab-control."""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        _, body = http("GET", f"{base}/v1/instances")
        polling = {i["instance"] for i in body.get("instances", [])}
        if all(len(pods := ready_pods(lab, c)) == n and set(pods) <= polling for c, n in wanted.items()):
            return
        time.sleep(5)
    raise SystemExit(f"replicas did not settle at {wanted} within {timeout_s:.0f}s (check `kubectl get pods`)")


def cmd_blind(lab: Lab, args: argparse.Namespace) -> None:
    state_file = STATE_DIR / "blind.json"
    if args.action == "start":
        scenarios = json.loads(SCENARIOS.read_text())["scenarios"]
        rng = random.Random(args.seed)
        scenario = next((s for s in scenarios if s["id"] == args.scenario), None) if args.scenario \
            else rng.choice(scenarios)
        if scenario is None:
            raise SystemExit("unknown scenario id")
        onset = rng.randint(30, max(30, args.onset_max))
        # Same setup for every scenario (so the setup itself reveals nothing):
        # checkout and inventory run two replicas for the exercise.
        restore = pin_replicas(lab, {"checkout": 2, "inventory": 2})
        with lab.forward(lab.svc("lab-control"), 80) as base:
            http("DELETE", f"{base}/v1/faults")
            wait_for_replicas(lab, base, {"checkout": 2, "inventory": 2})
            apply_faults(base, "blind", scenario["faults"], duration=args.duration, delay=onset, quiet=True,
                         lab=lab)
            if scenario.get("load"):
                http("PUT", f"{base}/v1/load", {**scenario["load"], "duration_s": args.duration + onset})
        STATE_DIR.mkdir(exist_ok=True)
        state_file.write_text(json.dumps({
            "scenario": scenario["id"], "started_at": time.time(), "onset_s": onset,
            "duration_s": args.duration, "restore": restore, "fingerprint": fingerprint(lab)}, indent=1))
        print("Blind exercise armed. Something will start going wrong within the next few minutes and last "
              f"about {int(args.duration / 60)} minutes.")
        print("Instrumentation is now frozen (fingerprint recorded). Work only from Grafana and the telemetry "
              "already being collected; see docs/blind-exercises.md.")
        print("When you have written down your diagnosis: python tools/lab.py blind reveal")
    elif args.action == "reveal":
        if not state_file.exists():
            raise SystemExit("no blind exercise in progress")
        state = json.loads(state_file.read_text())
        now_fp = fingerprint(lab)
        scenarios = {s["id"]: s for s in json.loads(SCENARIOS.read_text())["scenarios"]}
        scenario = scenarios[state["scenario"]]
        started = time.strftime("%H:%M:%S", time.localtime(state["started_at"] + state["onset_s"]))
        print(f"scenario {scenario['id']}: {scenario['title']}")
        print(f"faults began at ~{started} (onset {state['onset_s']} s after arming), lasting {state['duration_s']} s")
        print(f"answer key: docs/answer-key/blind-answers.md#{scenario['id']}")
        if now_fp["digest"] == state["fingerprint"]["digest"]:
            print("instrumentation: UNCHANGED since the exercise started - the diagnosis used existing telemetry only.")
        else:
            changed = sorted(k for k in set(now_fp["parts"]) | set(state["fingerprint"]["parts"])
                             if now_fp["parts"].get(k) != state["fingerprint"]["parts"].get(k))
            print("instrumentation: CHANGED during the exercise -> record this as an instrumentation gap:")
            for k in changed:
                print(f"  {k}")
    elif args.action == "stop":
        with lab.forward(lab.svc("lab-control"), 80) as base:
            http("DELETE", f"{base}/v1/faults")
        if state_file.exists():
            state = json.loads(state_file.read_text())
            restore_replicas(lab, state.get("restore", {}))
            state_file.unlink()
        print("blind exercise stopped; all faults cleared, replica settings restored")


def cmd_journey(lab: Lab, args: argparse.Namespace) -> None:
    extra = [args.sku] if args.sku else []
    out = lab.kubectl("exec", f"deploy/{lab.svc('loadgen')}", "--", "python", "-m", "webshop", "journey", *extra,
                      check=False)
    print("\n".join(line for line in out.splitlines() if not line.startswith("{")))


def cmd_grafana_password(lab: Lab, _: argparse.Namespace) -> None:
    raw = lab.kubectl("get", "secret", lab.svc("grafana-admin"), "-o", "jsonpath={.data.admin-password}")
    print(base64.b64decode(raw).decode())


def cmd_forward(lab: Lab, args: argparse.Namespace) -> None:
    target = {"grafana": ("grafana", 80, 3000), "storefront": ("storefront", 80, 8080),
              "prometheus": ("prometheus-server", 80, 9090), "lab-control": ("lab-control", 80, 8090)}[args.what]
    cmd = ["kubectl"] + (["--context", lab.context] if lab.context else []) + \
          ["-n", lab.ns, "port-forward", f"svc/{lab.svc(target[0])}", f"{target[2]}:{target[1]}"]
    print(f"http://localhost:{target[2]}  (Ctrl-C to stop)")
    subprocess.call(cmd)


# ------------------------------------------------------------- verify

class Report:
    def __init__(self) -> None:
        self.rows: list[tuple[str, str, str]] = []

    def add(self, ok: bool | None, name: str, detail: str) -> None:
        status = "PASS" if ok else ("WARN" if ok is None else "FAIL")
        self.rows.append((status, name, detail))
        print(f"[{status}] {name}: {detail}", flush=True)

    @property
    def failed(self) -> bool:
        return any(r[0] == "FAIL" for r in self.rows)


def promq(base: str, query: str) -> list:
    _, body = http("GET", f"{base}/api/v1/query?" + urllib.parse.urlencode({"query": query}))
    return body.get("data", {}).get("result", []) if isinstance(body, dict) else []


def pod_states(lab: Lab) -> tuple[list[str], list[str], int]:
    pods = json.loads(lab.kubectl("get", "pods", "-o", "json"))["items"]
    not_ready = [p["metadata"]["name"] for p in pods
                 if p["status"].get("phase") == "Running"
                 and not all(c.get("ready") for c in p["status"].get("containerStatuses", []))]
    pending = [p["metadata"]["name"] for p in pods if p["status"].get("phase") == "Pending"]
    running = sum(1 for p in pods if p["status"].get("phase") == "Running")
    return not_ready, pending, running


def cmd_verify(lab: Lab, args: argparse.Namespace) -> None:
    r = Report()
    # Right after an install the HPAs react to start-up CPU and add replicas
    # that are still starting (seen on the AWS lab). Give not-ready and
    # pending pods up to two minutes before judging them.
    deadline = time.time() + 120
    while True:
        not_ready, pending, running = pod_states(lab)
        if (not not_ready and not pending) or time.time() >= deadline:
            break
        time.sleep(5)
    r.add(not not_ready and not pending, "pods", f"{running} running; not ready: {not_ready or 'none'}; pending: {pending or 'none'}")
    netpols = json.loads(lab.kubectl("get", "networkpolicy", "-o", "json"))["items"]
    r.add(len(netpols) > 0, "network policies", f"{len(netpols)} NetworkPolicies enforce the flows above")
    raw = subprocess.run(["kubectl"] + (["--context", lab.context] if lab.context else []) +
                         ["get", "--raw", f"/apis/metrics.k8s.io/v1beta1/namespaces/{lab.ns}/pods"],
                         capture_output=True, text=True)
    r.add(raw.returncode == 0, "resource metrics API (HPA)",
          "metrics.k8s.io answers" if raw.returncode == 0 else "metrics.k8s.io unavailable: install metrics-server")

    out = lab.kubectl("exec", f"deploy/{lab.svc('loadgen')}", "--", "python", "-m", "webshop", "journey", check=False)
    fields = dict(re.findall(r"^(\w+)\s*: (.*)$", out, re.M))
    order_id, trace_id = fields.get("order_id", "").strip(), fields.get("trace_id", "").strip()
    r.add(fields.get("journey", "").startswith("good"), "synthetic journey",
          f"{fields.get('journey', 'no output')}, order {order_id}, trace {trace_id}")
    time.sleep(args.settle)

    with contextlib.ExitStack() as stack:
        prom = stack.enter_context(lab.forward(lab.svc("prometheus-server"), 80))
        loki = stack.enter_context(lab.forward(lab.svc("loki"), 3100))
        tempo = stack.enter_context(lab.forward(lab.svc("tempo"), 3200))
        grafana = stack.enter_context(lab.forward(lab.svc("grafana"), 80))

        # -- metrics
        _, targets = http("GET", f"{prom}/api/v1/targets?state=active")
        active = targets["data"]["activeTargets"]
        down = [f"{t['labels'].get('job')}/{t['labels'].get('pod', t['labels'].get('instance'))}" for t in active
                if t["health"] != "up"]
        r.add(not down, "prometheus targets", f"{len(active)} active, down: {down or 'none'}")
        services = {x["metric"].get("service_name") for x in promq(prom, "count by (service_name) (http_server_request_duration_seconds_count)")}
        services |= {x["metric"].get("service_name") for x in promq(prom, "count by (service_name) (webshop_fulfillment_jobs_total)")}
        services |= {x["metric"].get("service_name") for x in promq(prom, "count by (service_name) (webshop_synthetic_journeys_total)")}
        want = {"storefront", "checkout", "inventory", "payment", "fulfillment-worker", "loadgen"}
        r.add(want <= services, "app metrics per service", f"seen: {sorted(s for s in services if s)}")
        instances = promq(prom, 'count by (service_name, service_instance_id) (otel_sdk_span_started_total{service_namespace="webshop", service_name!="migrate"})')
        per_service: dict[str, int] = {}
        for x in instances:
            per_service[x["metric"].get("service_name", "?")] = per_service.get(x["metric"].get("service_name", "?"), 0) + 1
        r.add(bool(per_service), "instance identity (one series set per pod)", json.dumps(per_service, sort_keys=True))
        now = time.time()
        _, ex = http("GET", f"{prom}/api/v1/query_exemplars?" + urllib.parse.urlencode(
            {"query": "webshop_synthetic_journey_duration_seconds_bucket", "start": now - 900, "end": now}))
        exemplar_traces = [e["labels"].get("trace_id") for s in ex.get("data", []) for e in s.get("exemplars", [])]
        r.add(bool(exemplar_traces), "exemplars on journey histogram", f"{len(exemplar_traces)} exemplars with trace_id")
        if exemplar_traces:
            status, _ = http("GET", f"{tempo}/api/v2/traces/{exemplar_traces[-1]}")
            r.add(status == 200, "exemplar -> Tempo trace", f"trace {exemplar_traces[-1]} HTTP {status}")
        _, rules = http("GET", f"{prom}/api/v1/rules")
        groups = rules["data"]["groups"]
        firing = sorted({a["labels"]["alertname"] for g in groups for rule in g["rules"]
                         for a in rule.get("alerts", []) if a.get("state") == "firing"})
        r.add(len(groups) >= 5, "alert rules loaded", f"{sum(len(g['rules']) for g in groups)} rules; firing now: {firing or 'none'}")
        failed = promq(prom, "sum(rate(otelcol_exporter_send_failed_spans_total[5m])) + sum(rate(otelcol_exporter_send_failed_log_records_total[5m]))")
        value = float(failed[0]["value"][1]) if failed else 0.0
        r.add(value == 0, "collector export failures", f"{value:.3f}/s")

        # -- traces
        if trace_id:
            status, trace = http("GET", f"{tempo}/api/v2/traces/{trace_id}")
            spans, svcs = {}, set()
            if status == 200:
                for rs in trace["trace"]["resourceSpans"]:
                    svc = next(a["value"]["stringValue"] for a in rs["resource"]["attributes"] if a["key"] == "service.name")
                    svcs.add(svc)
                    for ss in rs["scopeSpans"]:
                        for s in ss["spans"]:
                            spans[s["spanId"]] = (s["name"], s.get("parentSpanId"), svc)
            names = {v[0] for v in spans.values()}
            r.add(status == 200 and want <= svcs, "journey trace spans every service",
                  f"{len(spans)} spans from {sorted(svcs)}")
            by_name = {}
            for sid, (name, parent, _) in spans.items():
                by_name.setdefault(name, []).append((sid, parent))
            ok_queue = all(n in names for n in ("order.publish", "queue.wait", "order.fulfill")) and any(
                parent in {sid for sid, _ in by_name.get("order.publish", [])} for _, parent in by_name.get("order.fulfill", []))
            r.add(ok_queue, "context across the queue", "order.fulfill is a child of order.publish (W3C traceparent in AMQP headers)")
            r.add({"db.connection.acquire", "inventory.reserve", "payment.authorize", "payment.capture",
                   "order.persist"} <= names, "business and dependency spans", ", ".join(sorted(n for n in names if "." in n))[:200])
            hits: list[str] = []
            for _attempt in range(12):   # a new trace becomes searchable within ~30 s
                _, found = http("GET", f"{tempo}/api/search?" + urllib.parse.urlencode(
                    {"q": f'{{ span.webshop.order.id = "{order_id}" }}', "start": int(now - 3600), "end": int(time.time() + 60)}))
                hits = [t["traceID"] for t in found.get("traces", [])] if isinstance(found, dict) else []
                if hits:
                    break
                time.sleep(5)
            r.add(trace_id in hits or trace_id.lstrip("0") in [h.lstrip("0") for h in hits],
                  "Tempo search by order ID", f"TraceQL span.webshop.order.id -> {hits[:3]}")

        # -- logs
        if order_id:
            q = f'{{service_namespace="webshop"}} | webshop_order_id="{order_id}"'
            _, logs = http("GET", f"{loki}/loki/api/v1/query_range?" + urllib.parse.urlencode(
                {"query": q, "start": int((now - 900) * 1e9), "end": int((now + 60) * 1e9), "limit": 500}))
            streams = logs.get("data", {}).get("result", []) if isinstance(logs, dict) else []
            log_svcs = {s["stream"].get("service_name") for s in streams}
            log_traces = {s["stream"].get("trace_id") for s in streams if s["stream"].get("trace_id")}
            lines = sum(len(s["values"]) for s in streams)
            r.add(len(log_svcs) >= 4, "Loki logs for the order", f"{lines} records from {sorted(log_svcs)}")
            r.add(trace_id in log_traces, "logs carry the trace ID (Loki -> Tempo, Tempo -> Loki)",
                  f"trace_ids in those records: {sorted(log_traces)[:3]}")
            _, labels = http("GET", f"{loki}/loki/api/v1/labels")
            r.add("webshop_order_id" not in labels.get("data", []), "order ID is structured metadata, not an index label",
                  f"index labels: {labels.get('data')}")
        _, ev = http("GET", f"{loki}/loki/api/v1/query_range?" + urllib.parse.urlencode(
            {"query": '{service_name="kubernetes-events"}', "start": int((now - 3600) * 1e9), "end": int(now * 1e9), "limit": 5}))
        r.add(bool(ev.get("data", {}).get("result")) if isinstance(ev, dict) else False, "Kubernetes events in Loki",
              "k8sobjects receiver -> Loki")

        # -- Grafana
        password = base64.b64decode(lab.kubectl("get", "secret", lab.svc("grafana-admin"), "-o",
                                                "jsonpath={.data.admin-password}")).decode()
        auth = {"Authorization": "Basic " + base64.b64encode(f"admin:{password}".encode()).decode()}
        _, dss = http("GET", f"{grafana}/api/datasources", headers=auth)
        uids = {d["uid"]: d for d in dss} if isinstance(dss, list) else {}
        for uid in ("prometheus", "loki", "tempo"):
            status, health = http("GET", f"{grafana}/api/datasources/uid/{uid}/health", headers=auth)
            r.add(status == 200 and isinstance(health, dict) and health.get("status") == "OK",
                  f"Grafana data source {uid}", str(health.get("message") if isinstance(health, dict) else health)[:120])
        links = []
        if "prometheus" in uids and uids["prometheus"]["jsonData"].get("exemplarTraceIdDestinations"):
            links.append("exemplar->tempo")
        if "tempo" in uids and uids["tempo"]["jsonData"].get("tracesToLogsV2"):
            links.append("trace->logs")
        if "loki" in uids and uids["loki"]["jsonData"].get("derivedFields"):
            links.append("logs->trace")
        r.add(len(links) == 3, "Grafana correlation links provisioned", ", ".join(links))
        _, boards = http("GET", f"{grafana}/api/search?tag=webshop", headers=auth)
        r.add(isinstance(boards, list) and len(boards) >= 6, "Grafana dashboards",
              ", ".join(b["title"] for b in boards) if isinstance(boards, list) else str(boards))

    print()
    print(f"{sum(1 for x in r.rows if x[0] == 'PASS')} passed, {sum(1 for x in r.rows if x[0] == 'FAIL')} failed, "
          f"{sum(1 for x in r.rows if x[0] == 'WARN')} warnings")
    if args.json:
        Path(args.json).write_text(json.dumps([{"status": s, "check": n, "detail": d} for s, n, d in r.rows], indent=1))
    sys.exit(1 if r.failed else 0)


def _dashboard_targets(panels: list[dict]):
    for panel in panels:
        yield from _dashboard_targets(panel.get("panels", []))
        for target in panel.get("targets", []):
            yield panel.get("title", ""), target


def cmd_check_dashboards(lab: Lab, args: argparse.Namespace) -> None:
    """Run every dashboard query (variables set to match-all) against the live
    Prometheus and Loki: catches wrong metric/label names before a reader
    stares at an empty panel."""
    subs = {"$__rate_interval": "1m", "$__range": "30m", "${service:regex}": ".*", "$order_id": args.order_id or "x",
            "$trace_id": "x"}
    for name in ("env", "namespace", "service", "version", "pod", "pool"):
        subs[f"${name}"] = ".*"
    bad = 0
    with contextlib.ExitStack() as stack:
        prom = stack.enter_context(lab.forward(lab.svc("prometheus-server"), 80))
        loki = stack.enter_context(lab.forward(lab.svc("loki"), 3100))
        now = time.time()
        for path in sorted((ROOT / "observability" / "grafana" / "dashboards").glob("*.json")):
            board = json.loads(path.read_text())
            counts = {"ok": 0, "empty": 0, "error": 0}
            for title, target in _dashboard_targets(board["panels"]):
                uid = target.get("datasource", {}).get("uid")
                expr = target.get("expr") or target.get("query") or ""
                for k in sorted(subs, key=len, reverse=True):
                    expr = expr.replace(k, subs[k])
                if uid == "prometheus":
                    status, body = http("GET", f"{prom}/api/v1/query?" + urllib.parse.urlencode({"query": expr}))
                    result = body.get("data", {}).get("result") if isinstance(body, dict) else None
                    err = body.get("error") if isinstance(body, dict) else body
                elif uid == "loki":
                    status, body = http("GET", f"{loki}/loki/api/v1/query_range?" + urllib.parse.urlencode(
                        {"query": expr, "start": int((now - 1800) * 1e9), "end": int(now * 1e9), "limit": 5}))
                    result = body.get("data", {}).get("result") if isinstance(body, dict) else None
                    err = body.get("error") if isinstance(body, dict) else body
                else:
                    continue
                if status != 200:
                    counts["error"] += 1
                    bad += 1
                    print(f"  ERROR {path.name} / {title}: {str(err)[:200]}\n        {expr[:160]}")
                elif not result:
                    counts["empty"] += 1
                    if args.verbose:
                        print(f"  empty {path.name} / {title}: {expr[:120]}")
                else:
                    counts["ok"] += 1
            print(f"{path.name:32} {counts}")
    print("queries with errors:", bad)
    sys.exit(1 if bad else 0)


# ------------------------------------------------------------- main

def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--namespace", "-n", default="webshop")
    parser.add_argument("--release", default="webshop")
    parser.add_argument("--context", default=None, help="kubectl context (default: current)")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("status").set_defaults(fn=cmd_status)

    f = sub.add_parser("fault")
    fsub = f.add_subparsers(dest="action", required=True)
    fsub.add_parser("presets")
    fsub.add_parser("list")
    fa = fsub.add_parser("apply")
    fa.add_argument("name")
    fa.add_argument("--duration", type=float, help="seconds; default: until removed")
    fa.add_argument("--replica", type=int, help="target only the Nth instance (sorted pod names)")
    fa.add_argument("--instance", help="target only this pod")
    fa.add_argument("--param", action="append", help="override a parameter, k=v")
    fa.add_argument("--yes", action="store_true", help="confirm a safeguard-bypass preset")
    fs = fsub.add_parser("set")
    fs.add_argument("name")
    fs.add_argument("--kind", required=True)
    fs.add_argument("--service", required=True)
    fs.add_argument("--instance")
    fs.add_argument("--param", action="append")
    fs.add_argument("--duration", type=float)
    fs.add_argument("--delay", type=float)
    fr = fsub.add_parser("remove")
    fr.add_argument("name")
    fsub.add_parser("reset")
    f.set_defaults(fn=cmd_fault)

    ld = sub.add_parser("load")
    lsub = ld.add_subparsers(dest="action", required=True)
    ls = lsub.add_parser("set")
    ls.add_argument("--rate", type=float, required=True, help="journeys per second")
    ls.add_argument("--duration", type=float)
    lsub.add_parser("reset")
    ld.set_defaults(fn=cmd_load)

    b = sub.add_parser("blind")
    bsub = b.add_subparsers(dest="action", required=True)
    bs = bsub.add_parser("start")
    bs.add_argument("--scenario", help="scenario id (default: random)")
    bs.add_argument("--seed", type=int)
    bs.add_argument("--onset-max", type=int, default=180, help="latest start, seconds after arming")
    bs.add_argument("--duration", type=float, default=900)
    bsub.add_parser("reveal")
    bsub.add_parser("stop")
    b.set_defaults(fn=cmd_blind)

    j = sub.add_parser("journey")
    j.add_argument("sku", nargs="?")
    j.set_defaults(fn=cmd_journey)

    v = sub.add_parser("verify")
    v.add_argument("--settle", type=float, default=20, help="seconds to wait for telemetry after the journey")
    v.add_argument("--json", help="also write results to this file")
    v.set_defaults(fn=cmd_verify)

    cd = sub.add_parser("check-dashboards")
    cd.add_argument("--order-id", help="an order ID, so the order-investigation queries have data")
    cd.add_argument("--verbose", "-v", action="store_true", help="also list queries that returned nothing")
    cd.set_defaults(fn=cmd_check_dashboards)

    sub.add_parser("grafana-password").set_defaults(fn=cmd_grafana_password)
    fw = sub.add_parser("forward")
    fw.add_argument("what", choices=["grafana", "storefront", "prometheus", "lab-control"])
    fw.set_defaults(fn=cmd_forward)

    args = parser.parse_args()
    args.fn(Lab(args), args)


if __name__ == "__main__":
    main()
