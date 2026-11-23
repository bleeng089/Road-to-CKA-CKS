# Helm, Hands-On: package, sign, publish, verify

> **Step 3 of 8: Helm chart signing** · Run from `AWS/kubeadm/k8/helm/` · Needs: a registry account; the cluster only from section 9\
> [All steps](../../../../README.md#learning-path) · Previous: step 2, [Networking on a three-tier app](../README.md) · Next: step 4, [Install the webshop](../webshop/README.md#quick-start-on-the-aws-lab)

The same three-tier app from [`../README.md`](../README.md), rebuilt as a Helm
chart and pushed to an OCI registry as a **signed** artifact — then pulled back
down and verified before anything is installed.

The networking lesson answers *how does a packet get there*. This one answers
*how do you know the manifests you are about to apply are the ones you
published*.

```text
   ./app  ──helm package --sign──>  app-1.0.1.tgz
                                    app-1.0.1.tgz.prov   (GPG signature +
                                          |               SHA-256 of the .tgz)
                                          | helm push
                                          v
                              OCI registry (Docker Hub, ECR, GHCR, ...)
                                          |
                                          | helm pull --verify
                                          v
                              signature + digest checked, then installed
```

The chart:

```text
app/
├── Chart.yaml                  name, chart version, appVersion
├── values.yaml                 namespace: three-tier
└── templates/
    ├── 00-namespace.yaml       Namespace
    ├── 10-db.yaml              ConfigMap + Deployment + Service (data tier)
    ├── 20-api.yaml             ConfigMap + Deployment + Service (logic tier)
    └── 30-frontend.yaml        ConfigMap + Deployment + Service (web tier)
```

Same three `nginx:1.27-alpine` tiers, same proxy chain, same ports — so you can
compare `helm template` output against the raw YAML in `../app/` and see
exactly what templating bought you.

**Prerequisites:** Helm 3.8+ (OCI support; written against v4.3), `gpg`, and a
registry account you can push to. Installing from the chart needs a cluster —
build one with the Terraform project in [`../../`](../../README.md). Packaging,
signing, pushing, and verifying all work with no cluster at all.

**Run every command below from this `helm/` directory** — the paths are
relative to it (`./app`, `./dist`). **Substitute your own values** for
`<registry-namespace>` (your Docker Hub user or org) and `<key-name>` (the UID
on your GPG key) throughout.

---

**Contents**

1. [Render before you install](#1-render-before-you-install)
2. [Chart version vs. app version](#2-chart-version-vs-app-version)
3. [Prepare the signing keyrings](#3-prepare-the-signing-keyrings)
4. [Package and sign](#4-package-and-sign)
5. [Verify locally, before anything is published](#5-verify-locally-before-anything-is-published)
6. [Push to an OCI registry](#6-push-to-an-oci-registry)
7. [Pull it back and verify remotely](#7-pull-it-back-and-verify-remotely)
8. [Under the hood: what a `.prov` file actually is](#8-under-the-hood-what-a-prov-file-actually-is)
9. [Install and upgrade with verification on](#9-install-and-upgrade-with-verification-on)
10. [Releasing a new version](#10-releasing-a-new-version)

Also: [Known limitation](#known-limitation-namespace-is-only-half-templated) · [Exercises](#exercises) · [What signing does and does not prove](#what-signing-does-and-does-not-prove) · [Cleanup](#cleanup) · [References](#references)

---

## 1. Render before you install

Three checks, cheapest first. None of them touch the cluster except the last.

```text
helm lint ./app
helm template test ./app > rendered.yaml
helm install --dry-run=client test ./app
```

`lint` catches structural problems. `template` renders the manifests locally so
you can read them. `--dry-run=client` runs the full install path but stops
before submitting anything — it tells you what *Helm* would send.

An `[INFO] Chart.yaml: icon is recommended` from `lint` is advisory; the chart
passes.

Once a cluster is up, escalate to the one that actually validates:

```text
helm install test ./app --namespace three-tier --dry-run=server
```

`--dry-run=server` sends the manifests to the API server for validation
(schemas, admission webhooks, CRDs) without persisting them. This is the check
that catches a typo'd `apiVersion` or a field the cluster rejects —
`--dry-run=client` cannot, because it never asks the cluster anything.

Inspect and override values:

```text
helm show values ./app
helm template test ./app --set namespace=other-ns
```

## 2. Chart version vs. app version

```text
helm show chart ./app
```

```text
apiVersion: v2
appVersion: "1.27"
description: A Helm chart for app
name: app
type: application
version: 1.0.1
```

Two versions, two jobs:

| Field | What it versions | Where it shows up |
|---|---|---|
| `version` | the **chart** — templates, values, defaults | package filename (`app-1.0.1.tgz`) and the OCI tag |
| `appVersion` | the **software** the chart deploys (here, nginx 1.27) | metadata only; `helm ls` shows it |

`version` must be SemVer — the OCI tag is derived from it. Bump `version` for
any chart change; bump `appVersion` only when the deployed application changes.
Never republish an existing `version` with different content: consumers who
pinned that tag would silently get something else.

## 3. Prepare the signing keyrings

Helm signs with OpenPGP, but it reads **legacy binary keyrings** — not
GnuPG's newer keybox format. Helm's own default gives this away:

```text
--keyring string   location of a public keyring (default "~/.gnupg/pubring.kbx")
```

That default path is a `.kbx`, which is why every command below passes an
explicit keyring instead: you export your existing key into the format Helm can
actually read.

Find the key:

```text
gpg --list-secret-keys --keyid-format=LONG
```

```text
sec   ed25519/A1B2C3D4E5F60789 2025-09-30 [SC]
      0123456789ABCDEF0123456789ABCDEFA1B2C3D4
uid                 [ultimate] <key-name>
ssb   cv25519/89ABCDEF01234567 2025-09-30 [E]
```

You need the `[SC]` (sign-capable) primary key. Any existing key works; there
is no need to generate a Helm-specific one.

Export both halves:

```text
umask 077
gpg --export-secret-keys <KEY-ID> > ~/.gnupg/helm-signing.gpg
gpg --export             <KEY-ID> > ~/.gnupg/helm-public.gpg
```

```text
~/.gnupg/
├── helm-signing.gpg    private — signs packages, never leaves your machine
└── helm-public.gpg     public  — verifies packages, safe to distribute
```

`umask 077` before the export is the point of that line: `helm-signing.gpg` is
exported **private key material**. It lives outside the repo on purpose. Never
commit it, never bake it into an image, and if you wire this into CI, inject it
as a secret and prefer a dedicated signing subkey over your personal key.

## 4. Package and sign

```text
helm package ./app \
  --sign \
  --key <key-name> \
  --keyring ~/.gnupg/helm-signing.gpg \
  --destination ./dist
```

Helm prompts for the key passphrase, then writes two files:

```text
dist/
├── app-1.0.1.tgz          the chart archive
└── app-1.0.1.tgz.prov     the provenance file
```

| Flag | Purpose |
|---|---|
| `--sign` | produce the `.prov` provenance file alongside the archive |
| `--key` | which key to sign with (UID or key ID) |
| `--keyring` | the exported **private** keyring from step 3 |
| `--destination` | where to write both files (keeps build output out of the chart dir) |
| `--passphrase-file` | read the passphrase from a file or `-` for stdin — for CI, not for your laptop |

`--version 1.0.2` would override the packaged version without editing
`Chart.yaml`. Useful for a one-off, but prefer editing `Chart.yaml` so git and
the registry never disagree about what `1.0.2` contains.

## 5. Verify locally, before anything is published

```text
helm verify ./dist/app-1.0.1.tgz --keyring ~/.gnupg/helm-public.gpg
```

```text
Signed by: <key-name>
Using Key With Fingerprint: 0123456789ABCDEF0123456789ABCDEFA1B2C3D4
Chart Hash Verified: sha256:46d8221459be484ea08a9f864a8a5308cca0ad4e56c6cc7815d29174f6d9b87f
```

Two independent claims, both of which must hold:

- **Signed by / fingerprint** — the `.prov` signature validates against a
  public key you hold. Note what this does *not* say: that the key is
  trustworthy. That is your call, made out of band.
- **Chart Hash Verified** — the archive's actual SHA-256 matches the digest
  recorded inside the signed `.prov`. The bytes have not changed since signing.

`helm verify` needs the `.prov` sitting next to the `.tgz`. Move one without the
other and verification fails for want of a signature, not because anything is
wrong.

## 6. Push to an OCI registry

```text
helm registry login registry-1.docker.io -u <registry-namespace>
helm push ./dist/app-1.0.1.tgz oci://registry-1.docker.io/<registry-namespace>
```

```text
Pushed: registry-1.docker.io/<registry-namespace>/app:1.0.1
Digest: sha256:df8f3489ddbf0c1167d354aaaa058b96c7efd4748e8a1ad98c9a0eeb9226ae89
```

Notes worth knowing before the first push:

- The push target is the **namespace**, not the repository. Helm appends the
  chart name and tag from `Chart.yaml` itself — `oci://.../<ns>` becomes
  `<ns>/app:1.0.1`. Appending `/app` yourself gets you `<ns>/app/app`.
- Docker Hub's registry endpoint is `registry-1.docker.io`. Other registries
  work identically: `<account>.dkr.ecr.<region>.amazonaws.com`, `ghcr.io`.
- Prefer an access token over your account password at the login prompt.
- The adjacent `.prov` is uploaded automatically as a second layer. Delete it
  before pushing and the chart still publishes — unsigned, silently. Verify
  locally first (step 5) so you notice.
- The `Digest:` here is the **OCI manifest** digest. It is not the chart
  archive's SHA-256 from step 5, and the two are not supposed to match.

## 7. Pull it back and verify remotely

Publishing is not proof. Fetch the artifact as a consumer would, into a
directory that shares nothing with your build:

```text
mkdir -p /tmp/helm-verify

helm pull oci://registry-1.docker.io/<registry-namespace>/app \
  --version 1.0.1 \
  --verify \
  --keyring ~/.gnupg/helm-public.gpg \
  --destination /tmp/helm-verify
```

```text
Pulled: registry-1.docker.io/<registry-namespace>/app:1.0.1
Digest: sha256:df8f3489ddbf0c1167d354aaaa058b96c7efd4748e8a1ad98c9a0eeb9226ae89

Signed by: <key-name>
Using Key With Fingerprint: 0123456789ABCDEF0123456789ABCDEFA1B2C3D4
Chart Hash Verified: sha256:46d8221459be484ea08a9f864a8a5308cca0ad4e56c6cc7815d29174f6d9b87f
```

The `Pulled:`/`Digest:` pair may print twice — the chart and its provenance
file are fetched as separate operations against the same manifest. Not an error.

What this round trip proves: the OCI digest matches what the push reported, and
the downloaded bytes still satisfy the signature made on your machine. The
registry did not alter the chart in transit or at rest.

Read what you actually downloaded:

```text
tar -tvf /tmp/helm-verify/app-1.0.1.tgz
helm show values /tmp/helm-verify/app-1.0.1.tgz
tar -xzvf /tmp/helm-verify/app-1.0.1.tgz -C /tmp/helm-verify
```

Comparing `helm show values` from the *archive* against `helm show values
oci://...` is the cheap way to confirm the registry holds the version you think
it does, rather than a stale package under the same tag.

## 8. Under the hood: what a `.prov` file actually is

```text
cat /tmp/helm-verify/app-1.0.1.tgz.prov
```

```text
-----BEGIN PGP SIGNED MESSAGE-----
Hash: SHA512

apiVersion: v2
appVersion: "1.27"
name: app
version: 1.0.1
...
files:
  app-1.0.1.tgz: sha256:46d8221459be484ea08a9f864a8a5308cca0ad4e56c6cc7815d29174f6d9b87f

-----BEGIN PGP SIGNATURE-----
...
-----END PGP SIGNATURE-----
```

It is a clearsigned text document: a copy of `Chart.yaml` plus a `files:` block
holding the archive's SHA-256, all wrapped in a PGP signature.

| Part | Role |
|---|---|
| `Hash: SHA512` | digest algorithm **of the signature** over this document |
| chart metadata | names what was signed, so a renamed archive cannot be passed off |
| `files:` | binds the exact archive bytes to the signature |
| PGP signature | verifiable with the signer's public key |

The two hash algorithms have unrelated jobs — SHA-512 protects the provenance
document, SHA-256 identifies the chart archive. They will never match.

That layering is the whole trick: the signature covers a small text file, and
the text file's digest covers the large archive. Tamper with the archive and the
digest breaks; tamper with the digest and the signature breaks.

## 9. Install and upgrade with verification on

```text
helm install test oci://registry-1.docker.io/<registry-namespace>/app \
  --version 1.0.1 \
  --namespace three-tier \
  --create-namespace \
  --verify \
  --keyring ~/.gnupg/helm-public.gpg
```

For an existing release, swap `install` for `upgrade` and drop
`--create-namespace`:

```text
helm upgrade test oci://registry-1.docker.io/<registry-namespace>/app \
  --version 1.0.1 \
  --namespace three-tier \
  --verify \
  --keyring ~/.gnupg/helm-public.gpg
```

`--verify` makes verification a **gate**: no signature, no install. Without it,
Helm installs whatever the registry hands back, and the `.prov` you carefully
published is decoration.

Check the result:

```text
helm status test -n three-tier
helm history test -n three-tier
kubectl get pods -n three-tier
```

Then walk the chain exactly as in the networking lesson:

```text
kubectl -n three-tier run tester --image=busybox:1.36 --restart=Never -it --rm -- \
  wget -qO- http://frontend/api/db/
```

```text
[db] pod db-... - you reached the data tier, end of the chain
```

Roll back by revision, not by re-pulling an older tag:

```text
helm rollback test 1 -n three-tier
```

## 10. Releasing a new version

Every chart change gets its own version. The loop:

1. Edit templates or `values.yaml`, then bump `version` in `Chart.yaml`
   (and `appVersion` if the deployed software changed).
2. `helm lint` → `helm template` → `--dry-run=server`.
3. `helm package --sign --destination ./dist`.
4. `helm verify ./dist/app-<new>.tgz` — before pushing, while a mistake is
   still private.
5. `helm push ./dist/app-<new>.tgz oci://...`.
6. `helm pull --verify` into a clean directory.
7. `helm upgrade --verify`.
8. Commit the chart source and tag it, so the git tree matches the artifact:

```text
git add app/
git commit -m "Release Helm chart <new>"
git tag chart-v<new>
git push origin main && git push origin chart-v<new>
```

Step 8 is what makes the published artifact auditable later: without a tag you
can push `1.0.2` and lose track of which source tree produced it. (This repo
currently has no chart tags — its history was rewritten, which discarded them.)

## Known limitation: `namespace` is only half-templated

`values.yaml` exposes `namespace: three-tier`, and the ConfigMaps, Deployments,
and Services all honour it. Two things do not:

```text
helm template test ./app --set namespace=other-ns | grep -E 'kind: Namespace' -A3
```

```text
kind: Namespace
metadata:
  name: three-tier          # <-- ignored the override
```

```text
helm template test ./app --set namespace=other-ns | grep proxy_pass
```

```text
proxy_pass http://db.three-tier.svc.cluster.local/;      # <-- hardcoded
proxy_pass http://api.three-tier.svc.cluster.local/;     # <-- hardcoded
```

So `--set namespace=other-ns` renders workloads into `other-ns` while creating a
Namespace called `three-tier`, and the inter-tier proxies still resolve names in
`three-tier`. The install fails or the chain breaks. The override only works at
its default value, which is the same as not being an override.

Left in place deliberately — it is a good first exercise, and a realistic one:
a value that is *almost* wired up is more dangerous than one that was never
offered, because `helm lint` and `helm template` both pass. Fixing it means
templating `00-namespace.yaml` and both `proxy_pass` lines, ideally through a
named helper in `_helpers.tpl` so the FQDN is built in one place.

## Exercises

- Wire `namespace` through properly (above), bump to `1.0.2`, and publish it.
- Template the replica counts, image tag, and resource limits instead of
  hardcoding them. Add a `values.schema.json` so bad input fails at `lint`.
- Add `NOTES.txt` that prints the `wget` command to walk the chain.
- Move the NetworkPolicies from Module 8 into the chart behind
  `networkPolicy.enabled`, and template the Ingress from Module 6.
- Try to install a tampered chart: unpack the `.tgz`, change a template,
  repack it *without* re-signing, and confirm `--verify` rejects it. Then
  re-sign with a different key and watch it fail for a different reason.
- Publish to a second registry (ECR, GHCR) and verify the same `.prov`
  validates from both — the signature is independent of where it is hosted.

## What signing does and does not prove

A valid `.prov` tells you: these chart bytes are the ones the holder of that key
signed, unmodified. That is a narrow claim, and everything below is outside it.

- **It says nothing about the container images.** The chart references
  `nginx:1.27-alpine` by mutable tag. That tag can move to different bytes
  tomorrow, and the chart signature still verifies. Pin images by digest
  (`nginx@sha256:...`) if that matters.
- **It says nothing about whether the key is trustworthy** — only that it
  signed this. Key distribution and trust are your problem; a signature
  verified against an attacker's key verifies perfectly.
- **It says nothing about the chart's contents being safe.** A signed chart can
  still ask for `privileged: true`, mount the host filesystem, or grant
  cluster-admin. Signing is integrity and attribution, not review.
- **`--verify` is opt-in.** Every `install`, `upgrade`, and `pull` without it
  skips the check entirely.

For CKS, that boundary is the examinable idea: supply-chain controls compose —
image digest pinning, image signing, SBOMs, provenance attestations, and an
admission controller that refuses anything lacking the required evidence. Chart
provenance is one link, and the only one this lesson covers.

## Cleanup

```text
helm uninstall test -n three-tier
kubectl delete namespace three-tier
rm -rf ./dist rendered.yaml /tmp/helm-verify
```

Build artifacts (`*.tgz`, `*.tgz.prov`, `dist/`, `rendered.yaml`) are
git-ignored — the chart *source* is what belongs in git, and the signed package
belongs in the registry. Published versions are immutable by convention: to
change something, release a new version rather than deleting and re-pushing.

## References

- Helm registries (OCI): <https://helm.sh/docs/topics/registries/>
- Helm provenance and integrity: <https://helm.sh/docs/topics/provenance/>
- Docker Hub OCI artifacts: <https://docs.docker.com/docker-hub/repos/manage/hub-images/oci-artifacts/>

---

> [All steps](../../../../README.md#learning-path) · Previous: step 2, [Networking on a three-tier app](../README.md) · Next: step 4, [Install the webshop](../webshop/README.md#quick-start-on-the-aws-lab)
