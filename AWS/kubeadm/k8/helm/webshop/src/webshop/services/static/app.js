// Webshop UI: catalog, checkout, and order tracking. Plain JS, no build step.
"use strict";

const state = { products: [], product: null, sku: null, idempotencyKey: null, pollTimer: null };
const $ = (id) => document.getElementById(id);
const money = (cents) => `$${(cents / 100).toFixed(2)}`;
// crypto.randomUUID needs a secure context (https or localhost); plain-http
// Ingress access falls back to getRandomValues.
const newKey = () => (crypto.randomUUID ? crypto.randomUUID()
  : Array.from(crypto.getRandomValues(new Uint8Array(16)), (b) => b.toString(16).padStart(2, "0")).join(""));

const EXPLAIN = {
  pending: "Checkout is still working on it.",
  accepted: "Accepted: payment is authorized and the order is queued for the warehouse. Not shipped yet.",
  fulfilling: "The warehouse is picking your order.",
  fulfilled: "Shipped and charged.",
  rejected: "Not placed: nothing was charged.",
  failed: "The order could not be fulfilled; the payment hold was released.",
};

async function api(path, options = {}) {
  const resp = await fetch(path, { headers: { "Content-Type": "application/json" }, ...options });
  let body = {};
  try { body = await resp.json(); } catch (_) { /* empty body */ }
  return { status: resp.status, body };
}

async function loadCatalog() {
  const { status, body } = await api("/api/products");
  const el = $("catalog");
  if (status !== 200) {
    el.innerHTML = `<p class="error">Catalog unavailable (HTTP ${status}).</p>`;
    return;
  }
  state.products = body.products;
  el.innerHTML = "";
  for (const product of state.products) {
    const card = document.createElement("article");
    card.className = "card";
    card.innerHTML = `<h3>${product.name}</h3><div class="muted">${product.description}</div>
      <div class="price">${money(product.price_cents)}</div><div class="swatches"></div>`;
    const swatches = card.querySelector(".swatches");
    for (const v of product.variants) {
      const b = document.createElement("button");
      b.type = "button";
      b.className = "swatch";
      b.setAttribute("aria-pressed", "false");
      b.innerHTML = `<span class="dot" style="background:${v.swatch}"></span>${v.color}`;
      b.title = `${v.available.toLocaleString()} available`;
      b.addEventListener("click", () => select(product, v, card, b));
      swatches.appendChild(b);
    }
    el.appendChild(card);
  }
}

function select(product, variant, card, button) {
  document.querySelectorAll(".card").forEach((c) => c.classList.remove("selected"));
  document.querySelectorAll(".swatch").forEach((s) => s.setAttribute("aria-pressed", "false"));
  card.classList.add("selected");
  button.setAttribute("aria-pressed", "true");
  state.product = product;
  state.sku = variant.sku;
  state.idempotencyKey = null; // a new choice is a new order
  $("selection").textContent = `${product.name} — ${variant.color} (${variant.sku})`;
  $("selection").classList.remove("muted");
  $("place").disabled = false;
}

async function placeOrder(event) {
  event.preventDefault();
  if (!state.sku) return;
  // One key per intended order: pressing the button again after an error
  // retries the SAME order, and checkout's idempotency turns it into a replay.
  state.idempotencyKey = state.idempotencyKey || newKey();
  $("place").disabled = true;
  $("checkout-result").textContent = "Placing order…";
  const { status, body } = await api("/api/checkout", {
    method: "POST",
    body: JSON.stringify({
      idempotency_key: state.idempotencyKey,
      sku: state.sku,
      quantity: Number($("quantity").value),
      customer_email: $("email").value,
      card_number: $("card").value.replace(/\s+/g, ""),
    }),
  });
  $("place").disabled = false;
  if (status === 202 || status === 200) {
    $("checkout-result").textContent = status === 200 ? "Already placed (replayed)." : "Order accepted.";
    state.idempotencyKey = null;
    track(body.order_id);
  } else {
    const reason = body.error || (body.detail ? "validation_failed" : `HTTP ${status}`);
    $("checkout-result").innerHTML = `<span class="error">Not placed: ${reason}.</span> ` +
      (status >= 500 ? "Pressing the button again retries the same order." : "");
    if (status < 500) state.idempotencyKey = null;
    if (body.order_id) track(body.order_id);
  }
}

function track(orderId) {
  clearTimeout(state.pollTimer);
  $("order-panel").hidden = false;
  $("order-id").textContent = orderId;
  poll(orderId);
}

async function poll(orderId) {
  const { status, body } = await api(`/api/orders/${encodeURIComponent(orderId)}`);
  if (status !== 200) {
    $("order-status").textContent = status === 404 ? "not found" : `HTTP ${status}`;
    $("order-status").className = "badge failed";
    state.pollTimer = setTimeout(() => poll(orderId), 2000);
    return;
  }
  render(body);
  if (!["fulfilled", "rejected", "failed"].includes(body.status)) {
    state.pollTimer = setTimeout(() => poll(orderId), 1000);
  }
}

function render(order) {
  $("order-status").textContent = order.status;
  $("order-status").className = `badge ${order.status}`;
  $("order-explain").textContent = EXPLAIN[order.status] || "";
  const req = order.requested;
  $("order-requested").textContent = `${req.quantity} × ${req.product_id} — ${req.color} (${req.sku})`;
  const shipped = order.fulfillment;
  if (shipped) {
    const wrong = shipped.shipped_sku !== req.sku;
    $("order-shipped").innerHTML = `${shipped.quantity} × ${shipped.shipped_color} (${shipped.shipped_sku})` +
      (wrong ? ` <span class="mismatch">— not what you ordered</span>` : "");
  } else {
    $("order-shipped").textContent = "—";
  }
  $("order-total").textContent = money(order.amount_cents);
  $("order-events").innerHTML = (order.events || [])
    .map((e) => `<li>${new Date(e.at).toLocaleTimeString()} → ${e.status}${e.reason ? ` (${e.reason})` : ""}</li>`)
    .join("");
}

$("checkout").addEventListener("submit", placeOrder);
$("lookup").addEventListener("submit", (e) => {
  e.preventDefault();
  const id = $("lookup-id").value.trim();
  if (id) track(id);
});
loadCatalog();
