"use client";

import { useEffect, useState, useCallback } from "react";
import {
  getLedger, getEscalations, resolveEscalation,
  type LedgerRow, type Escalation, type Outcome,
} from "@/lib/api";

const OUTCOME_STYLE: Record<Outcome, { dot: string; label: string }> = {
  approved: { dot: "bg-verified", label: "approved" },
  escalated: { dot: "bg-attention", label: "escalated" },
  rejected: { dot: "bg-danger", label: "rejected" },
};

const FILTERS: ("all" | Outcome)[] = ["all", "approved", "escalated", "rejected"];

export default function ResolutionLedger() {
  const [rows, setRows] = useState<LedgerRow[] | null>(null);
  const [queue, setQueue] = useState<Escalation[] | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [expanded, setExpanded] = useState<number | null>(null);
  const [filter, setFilter] = useState<"all" | Outcome>("all");
  const [search, setSearch] = useState("");
  const [resolving, setResolving] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoadError(null);
    try {
      const [ledgerRows, escalations] = await Promise.all([
        getLedger(filter === "all" ? undefined : filter),
        getEscalations(),
      ]);
      setRows(ledgerRows);
      setQueue(escalations);
    } catch (e) {
      setLoadError(e instanceof Error ? e.message : "Failed to reach the ledger API.");
    }
  }, [filter]);

  useEffect(() => {
    const timer = setTimeout(() => {
      void load();
    }, 0);

    return () => clearTimeout(timer);
  }, [load]);

  async function handleResolve(mandate: string, decision: "approve" | "deny") {
    setResolving(mandate);
    try {
      await resolveEscalation(mandate, decision, `${decision === "approve" ? "Approved" : "Denied"} from dashboard.`);
      await load(); // re-fetch rather than optimistically mutate -- the ledger is the source of truth, not local state
    } catch (e) {
      setLoadError(e instanceof Error ? e.message : "Failed to record the decision.");
    } finally {
      setResolving(null);
    }
  }

  const visibleRows = (rows ?? []).filter(
    (r) => !search || r.mandate.toLowerCase().includes(search.trim().toLowerCase())
  );

  return (
    <div className="min-h-screen bg-paper text-ink font-[family-name:var(--font-serif)] p-8">
      <div className="max-w-4xl mx-auto">
        <Header onVerify={load} entryCount={rows?.length ?? 0} />

        {loadError && (
          <div className="border border-danger bg-danger-bg text-danger text-sm px-4 py-3 mb-6">
            {loadError}. Is the backend running at{" "}
            <code className="font-[family-name:var(--font-mono)]">
              {process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000"}
            </code>?
          </div>
        )}

        {queue === null && !loadError ? (
          <p className="text-sm text-ink-soft mb-6">Loading escalation queue…</p>
        ) : queue && queue.length > 0 ? (
          <EscalationQueue queue={queue} resolving={resolving} onResolve={handleResolve} />
        ) : null}

        <Controls filter={filter} setFilter={setFilter} search={search} setSearch={setSearch} />

        {rows === null && !loadError && (
          <p className="text-sm text-ink-soft py-8">Loading ledger…</p>
        )}
        {rows !== null && visibleRows.length === 0 && (
          <p className="text-sm text-ink-soft py-8">
            {search ? "No entries match that mandate." : "No entries yet — this fills in as the agent resolves requests."}
          </p>
        )}
        {rows !== null && visibleRows.map((r) => (
          <LedgerRowView key={r.id} row={r} isOpen={expanded === r.id}
            onToggle={() => setExpanded(expanded === r.id ? null : r.id)} />
        ))}
      </div>
    </div>
  );
}

function Header({ onVerify, entryCount }: { onVerify: () => void; entryCount: number }) {
  const [verifying, setVerifying] = useState(false);
  const [lastChecked, setLastChecked] = useState<string | null>(null);

  async function handleVerify() {
    setVerifying(true);
    await onVerify();
    setLastChecked(new Date().toLocaleTimeString());
    setVerifying(false);
  }

  return (
    <div className="flex items-baseline justify-between flex-wrap gap-4 mb-7">
      <div>
        <h1 className="text-[22px] font-semibold tracking-tight m-0">Resolution ledger</h1>
        <p className="font-[family-name:var(--font-mono)] text-xs text-ink-soft mt-1">merchant m-1</p>
      </div>
      <div className="flex items-center gap-3">
        <div className="text-right">
          <div className="flex items-center gap-1.5 justify-end">
            <span className={`w-1.5 h-1.5 rounded-full inline-block ${verifying ? "bg-attention" : "bg-verified"}`} />
            <span className="text-sm font-medium">{verifying ? "Checking…" : "Ledger live"}</span>
          </div>
          <p className="font-[family-name:var(--font-mono)] text-[11px] text-ink-soft mt-0.5">
            {entryCount} entries{lastChecked ? ` · refreshed ${lastChecked}` : ""}
          </p>
        </div>
        <button
          onClick={handleVerify}
          disabled={verifying}
          className="border border-rule-strong rounded px-3.5 py-1.5 text-sm disabled:opacity-60"
        >
          Refresh
        </button>
      </div>
    </div>
  );
}

function EscalationQueue({
  queue, resolving, onResolve,
}: {
  queue: Escalation[];
  resolving: string | null;
  onResolve: (mandate: string, decision: "approve" | "deny") => void;
}) {
  return (
    <div className="mb-7">
      <h2 className="text-[15px] font-semibold mb-2.5">Needs your review ({queue.length})</h2>
      <div className="flex flex-col gap-2">
        {queue.map((item) => (
          <div
            key={item.mandate}
            className="bg-paper-raised border border-dashed border-attention px-3.5 py-3 flex items-center justify-between gap-3 flex-wrap"
          >
            <div className="flex-1 min-w-[280px]">
              <p className="m-0 text-sm font-medium capitalize">{item.title}</p>
              <p className="m-0 mt-0.5 text-[13px] text-ink-soft">{item.detail}</p>
              <p className="font-[family-name:var(--font-mono)] m-0 mt-1 text-[11px] text-ink-soft">
                mandate {item.mandate.slice(0, 8)}{item.amount ? `  ·  ${item.amount}` : ""}
              </p>
            </div>
            <div className="flex gap-2">
              <button
                onClick={() => onResolve(item.mandate, "approve")}
                disabled={resolving === item.mandate}
                className="bg-verified text-paper rounded px-3.5 py-1.5 text-sm disabled:opacity-60"
              >
                Approve
              </button>
              <button
                onClick={() => onResolve(item.mandate, "deny")}
                disabled={resolving === item.mandate}
                className="border border-danger text-danger rounded px-3.5 py-1.5 text-sm disabled:opacity-60"
              >
                Deny
              </button>
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}

function Controls({
  filter, setFilter, search, setSearch,
}: {
  filter: "all" | Outcome;
  setFilter: (f: "all" | Outcome) => void;
  search: string;
  setSearch: (s: string) => void;
}) {
  return (
    <div className="flex items-center justify-between flex-wrap gap-2.5 mb-2.5 border-b border-rule pb-2.5">
      <div className="flex gap-1.5">
        {FILTERS.map((f) => (
          <button
            key={f}
            onClick={() => setFilter(f)}
            className={`rounded px-3 py-1 text-sm border ${
              filter === f ? "bg-ink text-paper border-ink" : "text-ink-soft border-rule"
            }`}
          >
            {f}
          </button>
        ))}
      </div>
      <input
        value={search}
        onChange={(e) => setSearch(e.target.value)}
        placeholder="Search by mandate"
        className="font-[family-name:var(--font-mono)] bg-paper-raised border border-rule rounded px-2.5 py-1.5 text-xs w-44"
      />
    </div>
  );
}

function LedgerRowView({ row, isOpen, onToggle }: { row: LedgerRow; isOpen: boolean; onToggle: () => void }) {
  const style = OUTCOME_STYLE[row.outcome];
  return (
    <div className="border-b border-rule">
      <div
        onClick={onToggle}
        className="grid grid-cols-[84px_96px_1fr_96px_96px] items-center gap-3 py-2.5 px-1.5 cursor-pointer hover:bg-paper-raised transition-colors"
      >
        <span className="font-[family-name:var(--font-mono)] text-xs text-ink-soft">{row.time}</span>
        <span className="font-[family-name:var(--font-mono)] text-xs text-ink-soft">{row.mandate.slice(0, 8)}</span>
        <span className="text-sm">{row.action.replace(/_/g, " ")}</span>
        <span className="flex items-center gap-1.5 text-[13px]">
          <span className={`w-1.5 h-1.5 rounded-full inline-block ${style.dot}`} />
          {style.label}
        </span>
        <span className="font-[family-name:var(--font-mono)] text-[13px] text-right">{row.amount || "—"}</span>
      </div>
      {isOpen && (
        <div className="pb-3.5 px-1.5 pl-24 flex flex-col gap-1.5">
          <p className="text-[13.5px] m-0 max-w-xl">{row.reason}</p>
          <p className="font-[family-name:var(--font-mono)] text-[11px] text-ink-soft m-0">
            rule {row.rule} · hash {row.hash}
          </p>
        </div>
      )}
    </div>
  );
}
