import type { Span, SpanKind } from "../types";
import { ms, num, pctPlain, usd } from "../lib/format";

const KIND_DESC: Record<SpanKind, string> = {
  llm: "LLM agent — Groq-backed reasoning node",
  deterministic: "Deterministic rules engine — no LLM call",
  support: "Support enrichment — news / sentiment / prediction",
};

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="border-b border-line/60 py-2">
      <div className="text-2xs uppercase tracking-wider text-muted">{label}</div>
      <div className="mt-1 text-xs text-ink">{children}</div>
    </div>
  );
}

// The audit view for a single span: prompt-level decision, rationale, exact tokens/cost/latency,
// and the raw detail payload. Slides in from the right; Esc (handled globally) closes it.
export function DetailDrawer({ span, onClose }: { span: Span | null; onClose: () => void }) {
  if (!span) return null;
  return (
    <div className="fixed inset-0 z-40 flex justify-end" role="dialog" aria-modal="true">
      <div
        className="absolute inset-0 bg-black/50 backdrop-blur-[1px]"
        onClick={onClose}
        aria-hidden
      />
      <aside className="relative z-10 flex h-full w-full max-w-md flex-col border-l bg-panel shadow-drawer">
        <header className="flex items-center justify-between border-b bg-elevated/50 px-4 py-3">
          <div>
            <div className="text-2xs uppercase tracking-wider text-amber">Span Detail</div>
            <h3 className="text-sm font-medium text-ink">{span.label}</h3>
          </div>
          <button
            onClick={onClose}
            aria-label="Close detail"
            className="rounded-sm border px-2 py-1 text-2xs text-muted hover:bg-elevated hover:text-ink"
          >
            Esc ✕
          </button>
        </header>

        <div className="min-h-0 flex-1 overflow-auto px-4">
          <Field label="Node">
            <span className="font-mono">{span.name}</span>
            <span className="ml-2 text-2xs text-muted">{KIND_DESC[span.kind]}</span>
          </Field>

          <Field label="Decision">
            {span.decision || "—"}
            {span.confidence != null && (
              <span className="ml-2 text-amber">confidence {pctPlain(span.confidence)}</span>
            )}
          </Field>

          <Field label="Reasoning / Rationale">
            <p className="whitespace-pre-wrap font-sans leading-relaxed text-ink/90">
              {span.reasoning || "No reasoning recorded."}
            </p>
          </Field>

          <div className="grid grid-cols-3 gap-3 py-3">
            <div className="rounded-sm border bg-canvas px-2 py-2">
              <div className="text-2xs uppercase text-muted">Tokens</div>
              <div className="tnum mt-1 text-sm text-cost">
                {span.kind === "deterministic" ? "—" : num(span.tokens)}
              </div>
              <div className="tnum text-2xs text-muted">
                in {num(span.inputTokens)} · out {num(span.outputTokens)}
              </div>
            </div>
            <div className="rounded-sm border bg-canvas px-2 py-2">
              <div className="text-2xs uppercase text-muted">Cost</div>
              <div className="tnum mt-1 text-sm text-cost">
                {span.kind === "deterministic" ? "$0" : usd(span.costUsd, 6)}
              </div>
              <div className="text-2xs text-muted">paid-tier eq.</div>
            </div>
            <div className="rounded-sm border bg-canvas px-2 py-2">
              <div className="text-2xs uppercase text-muted">Latency</div>
              <div className="tnum mt-1 text-sm text-ink">{ms(span.latencyMs)}</div>
              <div className="text-2xs text-muted">wall</div>
            </div>
          </div>

          <Field label="Raw Detail">
            <pre className="overflow-auto rounded-sm border bg-canvas p-2 text-2xs text-muted">
              {JSON.stringify(span.detail, null, 2)}
            </pre>
          </Field>

          <p className="py-3 text-2xs italic text-muted">
            Deterministic nodes (risk compliance) carry latency but zero tokens/cost — an honest
            signal that no LLM was called.
          </p>
        </div>
      </aside>
    </div>
  );
}
