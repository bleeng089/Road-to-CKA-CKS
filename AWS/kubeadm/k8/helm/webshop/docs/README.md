# The webshop lessons

> **Steps 5–7 of 8: the exercises, networking and OpenTelemetry** · Run from `AWS/kubeadm/k8/helm/webshop/` · Needs: the webshop installed (step 4)

In this order:

1. [install.md](install.md): what each quick start step does, and the variations (step 4, explained)
2. [incidents/](incidents/01-slow-inventory.md): nine walkthroughs, one fault each (step 5)
3. [blind-exercises.md](blind-exercises.md): unknown faults, diagnosed from telemetry alone (step 5)
4. [networking.md](networking.md): networking on the webshop (step 6), with
   [nat-and-conntrack.md](nat-and-conntrack.md) explaining the NAT rules and conntrack entries it shows
5. [opentelemetry.md](opentelemetry.md): the OpenTelemetry concepts, one script at a time (step 7)

Also here: [observability.md](observability.md), how the webshop is
instrumented and why (background for steps 5–7); [answer-key/](answer-key),
only after you have written your answer; and, for maintainers,
[reference.md](reference.md) and [verification.md](verification.md).

---

> **Next:** [install.md](install.md), what each install step does
