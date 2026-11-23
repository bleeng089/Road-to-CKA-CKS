#!/usr/bin/env python3
"""Build (and optionally push) the single webshop image.

    python tools/images.py build --tag 0.2.0
        local image webshop:0.2.0 (for Kind: then `python tools/kind_cluster.py load`)

    python tools/images.py build --registry docker.io/<your-user> --tag 0.2.0 --push
        builds for linux/amd64 (the EC2 nodes) and pushes, so containerd on the
        nodes can pull it. Log in first: `docker login` (or `podman login`).

Building on your workstation does not put the image on the EC2 nodes: they
pull from the registry like any other image. A private repository needs an
imagePullSecret (docs/install.md, "If your registry is private").

Uses docker buildx when available, otherwise podman. Standard library only.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src"


def run(*args: str) -> None:
    print("+", " ".join(args), flush=True)
    subprocess.run(args, check=True)


def pick_tool(requested: str | None) -> str:
    if requested:
        return requested
    for tool in ("docker", "podman"):
        if shutil.which(tool):
            return tool
    sys.exit("neither docker nor podman is on PATH")


def build(args: argparse.Namespace) -> None:
    tool = pick_tool(args.tool)
    ref = f"{args.registry.rstrip('/')}/webshop:{args.tag}" if args.registry else f"webshop:{args.tag}"
    build_args = ["--build-arg", f"WEBSHOP_VERSION={args.tag}", "-t", ref]
    if tool == "docker":
        cmd = ["docker", "buildx", "build", "--platform", args.platform, *build_args]
        cmd.append("--push" if args.push else "--load")
        run(*cmd, str(SRC))
    else:
        run("podman", "build", "--platform", args.platform, *build_args, str(SRC))
        if args.push:
            run("podman", "push", ref)
    print(f"\nimage: {ref}")
    if args.registry:
        registry, _, repository = args.registry.partition("/")
        print("helm flags: --set image.registry={} --set image.repository={}/webshop --set image.tag={}".format(
            registry, repository, args.tag) if repository else
              f"helm flags: --set image.registry={registry} --set image.repository=webshop --set image.tag={args.tag}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build")
    b.add_argument("--registry", default="", help="e.g. docker.io/<user>, ghcr.io/<user>; empty = local only")
    b.add_argument("--tag", default="0.2.0")
    b.add_argument("--platform", default="linux/amd64", help="the lab's EC2 nodes are x86_64")
    b.add_argument("--push", action="store_true")
    b.add_argument("--tool", choices=["docker", "podman"])
    b.set_defaults(fn=build)
    args = parser.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
