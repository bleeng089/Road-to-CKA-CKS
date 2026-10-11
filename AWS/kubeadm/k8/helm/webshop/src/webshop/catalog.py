"""The deterministic seed catalog.

Every product exists in black and in one other color, so a fulfillment that
ships the wrong variant is visible at a glance: you ordered Crimson and
received Black.
"""

CATALOG = [
    {
        "id": "trail-runner",
        "name": "Trail Runner Sneaker",
        "description": "Light trail shoe with a grippy outsole.",
        "price_cents": 8900,
        "variants": [
            {"sku": "TRL-BLK", "color": "Black", "swatch": "#1b1b1f"},
            {"sku": "TRL-CRM", "color": "Crimson", "swatch": "#b3122e"},
        ],
    },
    {
        "id": "harbor-hoodie",
        "name": "Harbor Hoodie",
        "description": "Heavyweight cotton hoodie.",
        "price_cents": 5900,
        "variants": [
            {"sku": "HBR-BLK", "color": "Black", "swatch": "#1b1b1f"},
            {"sku": "HBR-GRN", "color": "Forest Green", "swatch": "#2f5d3a"},
        ],
    },
    {
        "id": "night-shift-mug",
        "name": "Night Shift Mug",
        "description": "Stoneware mug, 350 ml.",
        "price_cents": 1400,
        "variants": [
            {"sku": "MUG-BLK", "color": "Black", "swatch": "#1b1b1f"},
            {"sku": "MUG-WHT", "color": "White", "swatch": "#f2f2ee"},
        ],
    },
]

# Large enough that the synthetic client never exhausts stock in a lab session
# (1 journey/s for a week is ~600k units across six SKUs).
INITIAL_STOCK = 1_000_000

# Test cards understood by the payment simulator.
CARD_OK = "4242424242424242"
CARD_DECLINED = "4000000000000002"


def variant_index() -> dict[str, dict]:
    out = {}
    for product in CATALOG:
        for variant in product["variants"]:
            out[variant["sku"]] = {**variant, "product_id": product["id"], "price_cents": product["price_cents"],
                                   "product_name": product["name"]}
    return out
