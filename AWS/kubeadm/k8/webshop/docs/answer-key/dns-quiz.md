# Answer key: "Fix the FQDNs" (networking track, Module 3)

Do not open before trying - docs/networking.md, "Your turn: fix the FQDNs".

| Value | Answer | Why |
|---|---|---|
| `DNS_1` | `kubernetes.default.svc.cluster.local` | a Service: `<service>.<namespace>.svc.<cluster-domain>` -> its ClusterIP, `10.96.0.1` |
| `DNS_2` | `webshop-checkout-pods.webshop.svc.cluster.local` | a **headless** Service has the same name form; the difference is the answer - one A record per ready pod, not a ClusterIP. The pod IPs never go into the name |
| `DNS_3` | `webshop-rabbitmq-0.webshop-rabbitmq-headless.webshop.svc.cluster.local` | the StatefulSet gives the pod `hostname: webshop-rabbitmq-0` and `subdomain: webshop-rabbitmq-headless`, and a headless Service of that name exists: `<hostname>.<subdomain>.<namespace>.svc.<cluster-domain>`. It follows the pod to whatever IP it gets next |
| `DNS_4` | `1-2-3-4.kube-system.pod.cluster.local` | a pod IP record: the IP with **dashes**, then the namespace, then `pod`. With `pods insecure` CoreDNS answers it without checking that such a pod exists |

Apply them without an editor, then restart - environment variables from a
ConfigMap are read when the container starts:

```text
kubectl -n dns-quiz create configmap dns-quiz --from-literal=DNS_1=kubernetes.default.svc.cluster.local --from-literal=DNS_2=webshop-checkout-pods.webshop.svc.cluster.local --from-literal=DNS_3=webshop-rabbitmq-0.webshop-rabbitmq-headless.webshop.svc.cluster.local --from-literal=DNS_4=1-2-3-4.kube-system.pod.cluster.local --dry-run=client -o yaml | kubectl apply -f -
kubectl -n dns-quiz rollout restart deployment resolver
kubectl -n dns-quiz rollout status deployment resolver --timeout=120s
kubectl -n dns-quiz logs deployment/resolver --tail=5
```

```text
DNS_1=kubernetes.default.svc.cluster.local -> 10.96.0.1
DNS_2=webshop-checkout-pods.webshop.svc.cluster.local -> 192.168.171.104 192.168.171.112
DNS_3=webshop-rabbitmq-0.webshop-rabbitmq-headless.webshop.svc.cluster.local -> 192.168.171.81
DNS_4=1-2-3-4.kube-system.pod.cluster.local -> 1.2.3.4
---
```

Common wrong answers, and what they get:

- `DNS_2` as the pod IPs (`192.168.171.104.webshop-checkout-pods...`): a name
  with dots where the dashes belong - NXDOMAIN.
- `DNS_4` as `1.2.3.4.kube-system.pod.cluster.local`: NXDOMAIN; only the
  dashed form is a pod record.
- The right values but no restart: the resolver keeps logging `todo` - its
  environment was fixed when the container started.
