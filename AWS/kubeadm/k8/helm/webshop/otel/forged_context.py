"""Incoming context from someone you do not trust (OpenTelemetry track, Module 3).

Run it in the playground pod:

    kubectl -n webshop exec -i otel-playground -- python - < otel/forged_context.py

No OpenTelemetry SDK in this script, on purpose: it is "any client on the
internet", and a trace header is just text anyone can type. It sends the
storefront three kinds of made-up traceparent headers:

A. the same invented trace ID on two unrelated requests;
B. a valid header whose trace-flags say "not sampled" (00);
C. a malformed header.

Then look at what the storefront - which trusts incoming context, as most
services do by default - made of each one.
"""

import secrets
import time
import urllib.request

STOREFRONT = "http://webshop-storefront.webshop.svc.cluster.local/api/products"


def send(traceparent: str, user_agent: str) -> str:
    request = urllib.request.Request(STOREFRONT, headers={"traceparent": traceparent, "User-Agent": user_agent})
    with urllib.request.urlopen(request, timeout=5) as response:
        return f"HTTP {response.status} from {response.headers['X-Served-By']}"


forged = secrets.token_hex(16)
print(f"A. trace ID {forged} on two unrelated requests:")
for n in (1, 2):
    print("  ", send(f"00-{forged}-{secrets.token_hex(8)}-01", f"otel-lesson/forged-A{n}"))
    time.sleep(2)

quiet = secrets.token_hex(16)
print(f"B. trace ID {quiet}, trace-flags 00 (not sampled):")
print("  ", send(f"00-{quiet}-{secrets.token_hex(8)}-00", "otel-lesson/forged-B"))

print("C. a malformed traceparent:")
print("  ", send("00-this-is-not-a-trace-id-01", "otel-lesson/forged-C"))

print()
print(f"Tempo, A: paste {forged} - two storefront requests in one trace, parents that never existed")
print(f"Tempo, B: paste {quiet} - not found: the caller switched tracing off for this request")
print('Tempo, C: TraceQL { span.user_agent.original = "otel-lesson/forged-C" } - a new trace; the header was ignored')
