#!/usr/bin/env python3
"""Redraw the README's two Kubernetes diagrams in docs/diagrams/.

    helm dependency build ./chart      # once, if chart/charts/ is empty
    python3 tools/draw_diagrams.py

Renders the release from values/values-aws.yaml (`helm template`), draws it
with KubeDiagrams (https://github.com/philippemerle/KubeDiagrams) and lays
the result out left to right with Graphviz, like the README flowchart
(KubeDiagrams itself always draws top to bottom):

    dataflow-aws.png      workloads and Services, with the flowchart's arrows
                          (config docs/diagrams/kube-diagrams-dataflow.yaml)
    kubernetes-aws.png    every object, in the same places
                          (config docs/diagrams/kube-diagrams.yaml)

Needs helm, kube-diagrams and Graphviz's dot on the PATH. Standard library
only.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "docs" / "diagrams"

# (output, KubeDiagrams config, kinds removed before drawing). KubeDiagrams
# 0.9 fails on a hidden ConfigMap or Secret that an environment variable
# references, so the data-flow diagram gets the release without them.
DIAGRAMS = [
    ("dataflow-aws.png", "kube-diagrams-dataflow.yaml", {"ConfigMap", "Secret"}),
    ("kubernetes-aws.png", "kube-diagrams.yaml", set()),
]

KIND = re.compile(r"^kind:\s*(\S+)\s*$", re.MULTILINE)


def without_kinds(manifests: str, kinds: set[str]) -> str:
    """Drop the documents of the given kinds from `helm template` output."""
    kept = []
    for doc in re.split(r"^---\s*$", manifests, flags=re.MULTILINE):
        m = KIND.search(doc)
        if m and m.group(1) in kinds:
            continue
        kept.append(doc)
    return "---".join(kept)


def main() -> None:
    for tool in ("helm", "kube-diagrams", "dot"):
        if shutil.which(tool) is None:
            sys.exit(f"{tool} not found on the PATH")
    manifests = subprocess.run(
        ["helm", "template", "webshop", "./chart", "-n", "webshop",
         "-f", "values/values-aws.yaml", "--skip-tests"],
        cwd=ROOT, check=True, capture_output=True, text=True).stdout
    with tempfile.TemporaryDirectory() as tmp:
        for png, config, drop in DIAGRAMS:
            src = Path(tmp) / f"{png}.yaml"
            src.write_text(without_kinds(manifests, drop))
            base = Path(tmp) / Path(png).stem
            # KubeDiagrams prints a line per hidden or undefined reference;
            # only a failure is worth showing.
            run = subprocess.run(
                ["kube-diagrams", "-c", str(OUT / config), "-f", "dot", "-o", str(base), str(src)],
                capture_output=True, text=True)
            if run.returncode != 0:
                sys.exit(f"kube-diagrams failed for {config}:\n{run.stdout}{run.stderr}")
            dot = base.with_suffix(".dot").read_text()
            # Only the top-level graph: its clusters already say LR.
            dot = dot.replace("rankdir=TB", "rankdir=LR", 1)
            # dot warns about labels wider than their cluster; harmless.
            run = subprocess.run(["dot", "-Tpng", "-o", str(OUT / png)], input=dot, text=True,
                                 capture_output=True)
            if run.returncode != 0:
                sys.exit(f"dot failed for {png}:\n{run.stderr}")
            print(f"docs/diagrams/{png}")


if __name__ == "__main__":
    main()
