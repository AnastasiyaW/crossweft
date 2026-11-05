// Browser client for the shop API.

export const API_VERSION = "2026-09-01";
const VERSION_HEADER = "X-Shop-Api-Version";

export type OrderStatus = "pending" | "paid" | "shipped";

export interface Order {
  id: string;
  items: string[];
  total_cents: number;
  status: OrderStatus;
}

// W3C trace context: one request id from the browser through the API into the logs.
function traceparent(): string {
  const hex = (bytes: number) =>
    Array.from(crypto.getRandomValues(new Uint8Array(bytes)), (b) => b.toString(16).padStart(2, "0")).join("");
  return `00-${hex(16)}-${hex(8)}-01`;
}

function headers(): HeadersInit {
  return { [VERSION_HEADER]: API_VERSION, "Content-Type": "application/json", traceparent: traceparent() };
}

export async function getOrder(id: string): Promise<Order> {
  const res = await fetch(`/v1/orders/${encodeURIComponent(id)}`, { headers: headers() });
  if (!res.ok) throw new Error(`order ${id}: HTTP ${res.status}`);
  return (await res.json()) as Order;
}

export async function createOrder(items: string[]): Promise<void> {
  const res = await fetch("/v1/orders", {
    method: "POST",
    headers: headers(),
    body: JSON.stringify({ items }),
  });
  if (!res.ok) throw new Error(`create order: HTTP ${res.status}`);
}
