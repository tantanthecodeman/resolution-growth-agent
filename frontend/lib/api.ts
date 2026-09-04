const API_BASE = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";

export type Outcome = "approved" | "escalated" | "rejected";

export interface LedgerRow {
  id: number;
  time: string;
  mandate: string;
  action: string;
  outcome: Outcome;
  rule: string;
  reason: string;
  amount: string | null;
  hash: string;
}

export interface Escalation {
  mandate: string;
  title: string;
  detail: string;
  amount: string | null;
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, {
    ...init,
    headers: { "Content-Type": "application/json", ...init?.headers },
    cache: "no-store", // this is live operational data, never statically cached
  });
  if (!res.ok) {
    const body = await res.text().catch(() => "");
    throw new Error(`${path} failed: ${res.status} ${body}`);
  }
  return res.json() as Promise<T>;
}

export function getLedger(outcome?: Outcome, limit = 50): Promise<LedgerRow[]> {
  const params = new URLSearchParams({ limit: String(limit) });
  if (outcome) params.set("outcome", outcome);
  return request<LedgerRow[]>(`/api/ledger?${params.toString()}`);
}

export function getEscalations(): Promise<Escalation[]> {
  return request<Escalation[]>("/api/escalations");
}

export function resolveEscalation(
  mandateId: string,
  decision: "approve" | "deny",
  note: string
): Promise<{ status: string; ledger_row_id: number }> {
  return request("/api/escalations/resolve", {
    method: "POST",
    body: JSON.stringify({ mandate_id: mandateId, decision, note }),
  });
}
