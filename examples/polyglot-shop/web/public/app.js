// Shop admin page. A button names its action in data-action; ACTIONS runs it.

const API_VERSION = "2026-09-01";
const VERSION_HEADER = "X-Shop-Api-Version";

const ACTIONS = {
  "load-order": loadOrder,
  "place-order": placeOrder,
};

async function loadOrder() {
  const id = document.getElementById("order-id").value;
  const res = await fetch(`/v1/orders/${encodeURIComponent(id)}`, { headers: versioned() });
  show(`HTTP ${res.status}: ${await res.text()}`);
}

async function placeOrder() {
  const res = await fetch("/v1/orders", {
    method: "POST",
    headers: versioned(),
    body: JSON.stringify({ items: ["sku-1"] }),
  });
  show(`HTTP ${res.status}`);
}

function versioned() {
  return { [VERSION_HEADER]: API_VERSION, "Content-Type": "application/json" };
}

function show(text) {
  document.getElementById("out").textContent = text;
}

document.addEventListener("click", (event) => {
  const button = event.target.closest("[data-action]");
  if (!button) return;
  const run = ACTIONS[button.dataset.action];
  if (!run) {
    console.error(`no handler for action ${button.dataset.action}`);
    return;
  }
  run().catch((error) => console.error(error));
});
