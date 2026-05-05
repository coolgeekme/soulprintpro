import React from "react";
import { Sparkles, Building2, ExternalLink, Zap, Rocket } from "lucide-react";

/**
 * AuditFullReport — the full rendered audit body.
 *
 * Used in two places:
 *   • PublicAuditPage — after the prospect submits the lead form
 *   • AdminAuditsPanel — admin's "View full report" modal (no lead form
 *     needed, since they already have the row in their dashboard)
 *
 * Pure presentational — takes a fully-loaded audit dict and renders.
 * No fetching, no state, no side effects. The two parents own that.
 */
export default function AuditFullReport({ audit }) {
  const sections = audit.sections || {};
  return (
    <div className="space-y-8" data-testid="audit-full-report">
      {/* Recommendations — each one carries an "execute with SPE Pro"
          execution hook so the prospect sees not just WHAT to fix but
          HOW our platform delivers the fix. This is the audit's primary
          conversion mechanism: every reco is also a soft pitch. */}
      <section>
        <SectionHeader icon={Sparkles} title="Prioritized recommendations" subtitle="The highest-leverage fixes — and exactly how SoulPrint Engine Pro executes each one." />
        <ol className="space-y-3 mt-3">
          {(audit.recommendations || []).map((r, i) => (
            <li key={i} className="sp-glass rounded-xl p-4">
              <div className="flex items-baseline justify-between gap-3 mb-1 flex-wrap">
                <div className="font-medium">{i + 1}. {r.title}</div>
                <span className={`text-[10px] uppercase tracking-widest px-2 py-0.5 rounded-full border ${
                  r.priority === "high" ? "bg-red-500/15 text-red-200 border-red-500/30"
                  : r.priority === "medium" ? "bg-amber-500/15 text-amber-200 border-amber-500/30"
                  : "bg-white/5 text-white/60 border-white/10"
                }`}>{r.priority || "—"}</span>
              </div>
              <div className="text-sm leading-relaxed">{r.detail}</div>
              {r.estimated_impact && (
                <div className="text-xs sp-subtle italic mt-2">Expected impact: {r.estimated_impact}</div>
              )}
              {/* Execution hook — small inset card, orange accent, anchors
                  the recommendation to a specific SPE Pro feature/agent.
                  This is the "and here's how we'd do it for you" beat. */}
              {r.soulprint_execution && (
                <div className="mt-3 rounded-lg border border-orange-500/30 bg-orange-500/5 px-3 py-2.5 flex items-start gap-2">
                  <Zap className="w-3.5 h-3.5 text-orange-300 shrink-0 mt-0.5" />
                  <div className="text-xs leading-relaxed">
                    <span className="text-orange-300/90 uppercase tracking-widest text-[10px] font-medium block mb-0.5">
                      How SoulPrint executes this
                    </span>
                    {r.soulprint_execution}
                  </div>
                </div>
              )}
            </li>
          ))}
        </ol>
      </section>

      {/* Competitors */}
      <section>
        <SectionHeader icon={Building2} title="Direct competitors" subtitle="Three companies playing the same game — and where they're ahead." />
        <div className="grid sm:grid-cols-3 gap-3 mt-3">
          {(audit.competitors || []).map((c, i) => (
            <div key={i} className="sp-glass rounded-xl p-4">
              <div className="font-medium">{c.name}</div>
              {c.url && (
                <a href={c.url.startsWith("http") ? c.url : `https://${c.url}`} target="_blank" rel="noopener noreferrer"
                   className="text-[11px] sp-subtle hover:text-orange-300 inline-flex items-center gap-1 mb-2">
                  {c.url} <ExternalLink className="w-3 h-3" />
                </a>
              )}
              <div className="text-xs leading-relaxed mt-2">{c.edge}</div>
            </div>
          ))}
        </div>
      </section>

      {/* Brand voice */}
      {audit.brand_voice && (audit.brand_voice.stated_mission || audit.brand_voice.actual_voice || audit.brand_voice.gap) && (
        <section>
          <SectionHeader icon={Sparkles} title="Brand voice read" subtitle="Stated mission vs. how the market hears it." />
          <div className="sp-glass rounded-xl p-4 mt-3 space-y-2 text-sm">
            <div><b>Stated mission:</b> {audit.brand_voice.stated_mission}</div>
            <div><b>Actual voice:</b> {audit.brand_voice.actual_voice}</div>
            <div><b>The gap:</b> {audit.brand_voice.gap}</div>
          </div>
        </section>
      )}

      {/* Strengths + Issues — only render here when the parent didn't
          render its own teaser cards (admin uses this; public renders
          its own teaser-then-unlock pair above the form). */}
      {audit._show_strengths_issues && (
        <div className="grid sm:grid-cols-2 gap-3">
          <FullList title="Top strengths" items={audit.strengths || []} accent="emerald" />
          <FullList title="Critical issues" items={audit.issues || []} accent="orange" />
        </div>
      )}

      {/* Section paragraphs */}
      <div className="grid sm:grid-cols-2 gap-4">
        <SectionParagraph title="Website analysis" body={sections.website_analysis} />
        <SectionParagraph title="Social presence" body={sections.social_presence} />
        <SectionParagraph title="Reviews summary" body={sections.reviews_summary} />
        <SectionParagraph title="Competitive context" body={sections.competitive_context} />
      </div>

      {/* Closing pitch — ties the audit's findings to SPE Pro's
          ability to execute them. The single highest-value section
          for converting an audit reader into a paying customer.
          Hidden when empty (legacy audits or LLM omitted the field). */}
      {audit.how_soulprint_helps && (
        <section
          className="rounded-2xl p-6 sm:p-8 bg-gradient-to-br from-orange-500/15 via-orange-500/5 to-transparent border border-orange-500/30 mt-2"
          data-testid="audit-spe-cta"
        >
          <div className="flex items-center gap-2 mb-3">
            <Rocket className="w-4 h-4 text-orange-300" />
            <div className="text-[11px] uppercase tracking-widest text-orange-300/90 font-medium">
              Execute these with SoulPrint Engine Pro
            </div>
          </div>
          <p className="text-sm sm:text-base leading-relaxed mb-4">
            {audit.how_soulprint_helps}
          </p>
          <a
            href="https://pro.soulprintengine.ai/auth"
            target="_blank" rel="noopener noreferrer"
            className="sp-btn sp-btn-primary text-sm"
            data-testid="audit-spe-cta-button"
          >
            <Rocket className="w-3.5 h-3.5" /> Start with SoulPrint Engine Pro
          </a>
        </section>
      )}
    </div>
  );
}


function SectionHeader({ icon: Icon, title, subtitle }) {
  return (
    <div>
      <div className="flex items-center gap-2 mb-1">
        <Icon className="w-4 h-4 opacity-80" />
        <div className="text-[11px] uppercase tracking-widest sp-subtle">{title}</div>
      </div>
      <div className="text-xs sp-subtle">{subtitle}</div>
    </div>
  );
}


function SectionParagraph({ title, body }) {
  if (!body) return null;
  return (
    <div className="sp-glass rounded-xl p-4">
      <div className="text-[11px] uppercase tracking-widest sp-subtle mb-2">{title}</div>
      <div className="text-sm leading-relaxed whitespace-pre-wrap">{body}</div>
    </div>
  );
}


function FullList({ title, items, accent }) {
  const border = accent === "emerald" ? "border-emerald-500/30" : "border-orange-500/30";
  return (
    <div className={`sp-glass rounded-xl p-4 border ${border}`}>
      <div className="text-[11px] uppercase tracking-widest sp-subtle mb-2">{title}</div>
      <ul className="space-y-3">
        {items.map((it, i) => (
          <li key={i}>
            <div className="font-medium text-sm">{it.headline}</div>
            {it.detail && <div className="text-xs sp-subtle mt-1 leading-relaxed">{it.detail}</div>}
          </li>
        ))}
      </ul>
    </div>
  );
}
