"""Entrypoint: ``python -m webshop <component>``.

One image serves every component; the chart picks the component per
Deployment through the container arguments.
"""

from __future__ import annotations

import sys

COMPONENTS = {
    "storefront": "webshop.services.storefront",
    "checkout": "webshop.services.checkout",
    "inventory": "webshop.services.inventory",
    "payment": "webshop.services.payment",
    "fulfillment": "webshop.services.fulfillment",
    "loadgen": "webshop.services.loadgen",
    "labcontrol": "webshop.services.labcontrol",
    "migrate": "webshop.migrate",
}


def main(argv: list[str]) -> int:
    if not argv or argv[0] in ("-h", "--help"):
        print("usage: python -m webshop {" + ",".join([*COMPONENTS, "journey"]) + "}")
        return 2
    if argv[0] == "journey":
        from webshop.services.loadgen import journey_cli

        return journey_cli(argv[1:])
    module_name = COMPONENTS.get(argv[0])
    if module_name is None:
        print(f"unknown component {argv[0]!r}", file=sys.stderr)
        return 2
    module = __import__(module_name, fromlist=["run"])
    module.run()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
