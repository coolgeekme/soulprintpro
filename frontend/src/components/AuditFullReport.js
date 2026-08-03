import React from "react";
import { Wand2, ExternalLink, Zap, Rocket, ShieldAlert, Gauge, ShieldCheck, Smartphone, Monitor, Tablet, MapPin, Layers, Megaphone, Briefcase, Heart, Settings2, LineChart, Users } from "lucide-react";
import CollapsibleSection from "./CollapsibleSection";

const BACKEND = process.env.REACT_APP_BACKEND_URL || "";

// Pretty-print "2400 ms" as "2.4s" once a value crosses 1s — what users
// actually read at a glance. CLS is a unitless float so it gets its own
// formatter that keeps two decimals.
const fmtMs = (n) => {
  if (n == null) return "—";
  if (n >= 1000) return `${(n / 1000).toFixed(1)}s`;
  return `${Math.round(n)} ms`;
};
const fmtCls = (n) => (n == null ? "—" : Number(n).toFixed(3));
// Score colours match Google's own Lighthouse bands so the UI feels
// familiar to anyone who's run PageSpeed before.
const scoreColor = (n) => {
  if (n == null) return "text-white/40";
  if (n >= 90) return "text-emerald-300";
  if (n >= 50) return "text-amber-300";
  return "text-red-300";
};

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
      {/* Industry classification pill — 8 words that tell the reader
          "we know what you are" without adding any prose. Hidden
          gracefully on legacy audits where the field doesn't exist. */}
      {audit.industry_context && (audit.industry_context.industry || audit.industry_context.geography) && (
        <div className="flex flex-wrap items-center gap-2 -mt-2" data-testid="audit-industry-pill">
          <Pill icon={Layers}  label={audit.industry_context.industry} />
          <Pill icon={MapPin}  label={audit.industry_context.geography} />
          <Pill icon={Users}   label={audit.industry_context.size_hint} />
        </div>
      )}

      {/* Honest disclosure banner — when the prospect's site sits behind
          Cloudflare / DataDome / Akamai-style anti-bot protection, our
          headless browser is served the challenge page instead of the
          real homepage. We blank the on-page text in that case and lean
          on Perplexity + Brave for the audit body; the banner tells the
          reader where the analysis came from so the audit reads as
          credible, not magical. */}
      {audit.website_fetch_blocked && (
        <div
          className="rounded-xl border border-amber-500/30 bg-amber-500/5 p-4 flex items-start gap-3"
          data-testid="audit-fetch-blocked-banner"
        >
          <ShieldAlert className="w-5 h-5 text-amber-300 shrink-0 mt-0.5" />
          <div className="text-sm leading-relaxed">
            <div className="font-medium text-amber-200 mb-1">
              Site behind anti-bot protection
            </div>
            <div className="sp-subtle">
              We couldn't crawl{" "}
              {audit.website_url && (
                <span className="text-amber-100/90">{audit.website_url}</span>
              )}{" "}
              directly — it's protected by anti-bot software (Cloudflare,
              DataDome, or similar). This audit is built from live web
              research (Perplexity), brand mentions, and review signals
              instead of on-page copy. Findings that would normally cite
              your hero text or CTAs draw on those external sources here.
            </div>
          </div>
        </div>
      )}
      {/* Recommendations — each one carries a "how Foundry helps you
          act on this" hook so the prospect sees the natural next
          step without us overpromising autonomous execution. */}
      <section>
        <SectionHeader icon={Wand2} title="Prioritized recommendations" subtitle="The highest-leverage fixes — and how Foundry can help you act on each one." />
        <ol className="space-y-3 mt-3">
          {(audit.recommendations || []).map((r, i) => (
            <li key={i} className="sp-glass rounded-xl p-5">
              <div className="flex items-baseline justify-between gap-3 mb-2 flex-wrap">
                <div className="font-medium text-lg">{i + 1}. {r.title}</div>
                <span className={`text-[10px] uppercase tracking-widest px-2 py-0.5 rounded-full border ${
                  r.priority === "high" ? "bg-red-500/15 text-red-200 border-red-500/30"
                  : r.priority === "medium" ? "bg-amber-500/15 text-amber-200 border-amber-500/30"
                  : "bg-white/5 text-white/60 border-white/10"
                }`}>{r.priority || "—"}</span>
              </div>
              <div className="text-base leading-relaxed">{r.detail}</div>
              {r.estimated_impact && (
                <div className="text-sm sp-subtle italic mt-2">Expected impact: {r.estimated_impact}</div>
              )}
              {r.soulprint_execution && (
                <div className="mt-3 rounded-lg border border-orange-500/30 bg-orange-500/5 px-3 py-3 flex items-start gap-2">
                  <Zap className="w-4 h-4 text-orange-300 shrink-0 mt-0.5" />
                  <div className="text-sm leading-relaxed">
                    <span className="text-orange-300/90 uppercase tracking-widest text-[10px] font-medium block mb-1">
                      How Foundry helps you act on this
                    </span>
                    {r.soulprint_execution}
                  </div>
                </div>
              )}
            </li>
          ))}
        </ol>
      </section>

      {/* Strengths + Issues — only render here when the parent didn't
          render its own teaser cards (admin uses this; public renders
          its own teaser-then-unlock pair above the form). */}
      {audit._show_strengths_issues && (
        <div className="grid sm:grid-cols-2 gap-3">
          <FullList title="Top strengths" items={audit.strengths || []} accent="emerald" />
          <FullList title="Critical issues" items={audit.issues || []} accent="orange" />
        </div>
      )}

      {/* Everything below is real signal, but it's a lot to take in at
          once — these sections default to collapsed (one click to open)
          so the report reads as a summary first, deep-dive second. Each
          user's open/closed choice persists across audits via localStorage. */}

      {/* Competitors */}
      <CollapsibleSection
        title="Direct competitors"
        subtitle="Three companies playing the same game — and where they're ahead."
        storageKey="audit-competitors"
        testId="audit-competitors-section"
      >
        <div className="grid sm:grid-cols-3 gap-3">
          {(audit.competitors || []).map((c, i) => (
            <div key={i} className="sp-glass rounded-xl p-5">
              <div className="font-medium text-base">{c.name}</div>
              {c.url && (
                <a href={c.url.startsWith("http") ? c.url : `https://${c.url}`} target="_blank" rel="noopener noreferrer"
                   className="text-xs sp-subtle hover:text-orange-300 inline-flex items-center gap-1 mb-2">
                  {c.url} <ExternalLink className="w-3 h-3" />
                </a>
              )}
              <div className="text-sm leading-relaxed mt-2">{c.edge}</div>
            </div>
          ))}
        </div>
      </CollapsibleSection>

      {/* Brand voice */}
      {audit.brand_voice && (audit.brand_voice.stated_mission || audit.brand_voice.actual_voice || audit.brand_voice.gap) && (
        <CollapsibleSection
          title="Brand voice read"
          subtitle="Stated mission vs. how the market hears it."
          storageKey="audit-brand-voice"
          testId="audit-brand-voice-section"
        >
          <div className="sp-glass rounded-xl p-5 space-y-3 text-base leading-relaxed">
            <div><b>Stated mission:</b> {audit.brand_voice.stated_mission}</div>
            <div><b>Actual voice:</b> {audit.brand_voice.actual_voice}</div>
            <div><b>The gap:</b> {audit.brand_voice.gap}</div>
          </div>
        </CollapsibleSection>
      )}

      {/* Section paragraphs */}
      <CollapsibleSection
        title="Website, social & competitive analysis"
        subtitle="The longer-form write-up behind the scores above."
        storageKey="audit-section-paragraphs"
        testId="audit-section-paragraphs-section"
      >
        <div className="grid sm:grid-cols-2 gap-4">
          <SectionParagraph title="Website analysis" body={sections.website_analysis} />
          <SectionParagraph title="Social presence" body={sections.social_presence} />
          <SectionParagraph title="Reviews summary" body={sections.reviews_summary} />
          <SectionParagraph title="Competitive context" body={sections.competitive_context} />
        </div>
      </CollapsibleSection>

      {/* Cross-functional opportunities — 6-card grid spanning Marketing,
          Sales, CX, Ops, Finance, Talent. Shows the platform's breadth
          without bloating the audit. Hidden gracefully on legacy audits
          where the field doesn't exist. */}
      {Array.isArray(audit.cross_functional_opportunities)
        && audit.cross_functional_opportunities.length > 0
        && (
          <CollapsibleSection
            title="Beyond the website — cross-functional opportunities"
            subtitle="Foundry's agents and missions span the whole business, not just the digital front door."
            storageKey="audit-cross-functional"
            testId="audit-cross-functional-section"
          >
            <CrossFunctionalOpportunities items={audit.cross_functional_opportunities} />
          </CollapsibleSection>
        )}

      {/* Technical signals — Core Web Vitals, viewport screenshots,
          tracking-pixel inventory, domain reputation. These are the
          hard-data signals no chat agent can produce; they're what
          turn the audit from "smart LLM report" into "investigation-
          grade prospect dossier". Each sub-section hides gracefully
          when its signal is unavailable, so older audits keep
          rendering. */}
      {audit.tech_signals && hasTechnicalSignals(audit.tech_signals) && (
        <CollapsibleSection
          title="Technical signals"
          subtitle="Hard data from our crawlers — performance numbers, tracking inventory, email security, and how your site renders across devices."
          storageKey="audit-tech-signals"
          testId="audit-tech-signals-section"
        >
          <TechnicalSignals signals={audit.tech_signals} />
        </CollapsibleSection>
      )}

      {/* Closing pitch — ties the audit's findings to how Foundry can
          help them act on the recommendations. The single highest-value
          section for converting an audit reader into a paying customer.
          Hidden when empty (legacy audits or LLM omitted the field). */}
      {audit.how_soulprint_helps && (
        <section
          className="rounded-2xl p-6 sm:p-8 bg-gradient-to-br from-orange-500/15 via-orange-500/5 to-transparent border border-orange-500/30 mt-2"
          data-testid="audit-spe-cta"
        >
          <div className="flex items-center gap-2 mb-3">
            <Rocket className="w-4 h-4 text-orange-300" />
            <div className="text-[11px] uppercase tracking-widest text-orange-300/90 font-medium">
              Tackle these with Foundry
            </div>
          </div>
          <p className="text-base sm:text-lg leading-relaxed mb-4">
            {audit.how_soulprint_helps}
          </p>
          <a
            href="https://foundryagents.ai/auth"
            target="_blank" rel="noopener noreferrer"
            className="sp-btn sp-btn-primary text-base"
            data-testid="audit-spe-cta-button"
          >
            <Rocket className="w-4 h-4" /> Start with Foundry
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
      <div className="text-sm sp-subtle leading-relaxed">{subtitle}</div>
    </div>
  );
}


function SectionParagraph({ title, body }) {
  if (!body) return null;
  return (
    <div className="sp-glass rounded-xl p-5">
      <div className="text-[11px] uppercase tracking-widest sp-subtle mb-2">{title}</div>
      <div className="text-base leading-relaxed whitespace-pre-wrap">{body}</div>
    </div>
  );
}


function FullList({ title, items, accent }) {
  const border = accent === "emerald" ? "border-emerald-500/30" : "border-orange-500/30";
  return (
    <div className={`sp-glass rounded-xl p-5 border ${border}`}>
      <div className="text-[11px] uppercase tracking-widest sp-subtle mb-3">{title}</div>
      <ul className="space-y-3">
        {items.map((it, i) => (
          <li key={i}>
            <div className="font-medium text-base">{it.headline}</div>
            {it.detail && <div className="text-sm sp-subtle mt-1 leading-relaxed">{it.detail}</div>}
          </li>
        ))}
      </ul>
    </div>
  );
}


// ── Technical signals section ────────────────────────────────────────────
// Four sub-cards, each rendering only when its signal is `available` so we
// never show empty "—" stubs. hasTechnicalSignals() decides whether the
// collapsible wrapper itself renders at all (e.g. legacy audits with none).
function hasTechnicalSignals(signals) {
  const wv = signals?.web_vitals || {};
  const sc = signals?.screenshots || {};
  const tp = signals?.tracking_pixels || {};
  const dr = signals?.domain_reputation || {};
  return !!(wv.available || sc.available || tp.available || dr.available);
}

function TechnicalSignals({ signals }) {
  const wv = signals?.web_vitals || {};
  const sc = signals?.screenshots || {};
  const tp = signals?.tracking_pixels || {};
  const dr = signals?.domain_reputation || {};
  if (!hasTechnicalSignals(signals)) return null;

  return (
    <div className="grid lg:grid-cols-2 gap-4" data-testid="audit-tech-signals">
      {wv.available && <WebVitalsCard wv={wv} />}
      {dr.available && <DomainReputationCard dr={dr} />}
      {tp.available && <TrackingPixelsCard tp={tp} />}
      {sc.available && <ViewportShotsCard sc={sc} />}
    </div>
  );
}


function WebVitalsCard({ wv }) {
  const m = wv.mobile || {};
  const d = wv.desktop || {};
  return (
    <div className="sp-glass rounded-xl p-5" data-testid="audit-web-vitals">
      <div className="flex items-center gap-2 mb-3">
        <Gauge className="w-4 h-4 opacity-80" />
        <div className="text-[11px] uppercase tracking-widest sp-subtle">
          Core Web Vitals · Google PageSpeed
        </div>
      </div>
      <div className="grid grid-cols-2 gap-3">
        <VitalsBlock label="Mobile" data={m} />
        <VitalsBlock label="Desktop" data={d} />
      </div>
      <div className="text-[10px] sp-subtle mt-3 leading-relaxed">
        Lighthouse-equivalent scores. Mobile is Google's primary ranking signal.
      </div>
    </div>
  );
}


function VitalsBlock({ label, data }) {
  if (!data || Object.keys(data).length === 0) {
    return (
      <div className="rounded-lg border border-white/10 p-3">
        <div className="text-[10px] uppercase tracking-widest sp-subtle mb-1">{label}</div>
        <div className="text-sm sp-subtle">Not available</div>
      </div>
    );
  }
  return (
    <div className="rounded-lg border border-white/10 p-3">
      <div className="flex items-baseline justify-between mb-2">
        <div className="text-[10px] uppercase tracking-widest sp-subtle">{label}</div>
        <div className={`text-2xl font-semibold tabular-nums ${scoreColor(data.performance)}`}>
          {data.performance ?? "—"}
        </div>
      </div>
      <dl className="space-y-1 text-xs">
        <Row k="LCP" v={fmtMs(data.lcp_ms)} hint="Largest Contentful Paint — when the main content shows" />
        <Row k="INP" v={fmtMs(data.inp_ms)} hint="Interaction-to-Next-Paint — input responsiveness" />
        <Row k="CLS" v={fmtCls(data.cls)} hint="Cumulative Layout Shift — visual stability (lower is better)" />
        <Row k="TTFB" v={fmtMs(data.ttfb_ms)} hint="Time to First Byte — server response latency" />
        <Row k="FCP" v={fmtMs(data.fcp_ms)} hint="First Contentful Paint" />
      </dl>
    </div>
  );
}


function Row({ k, v, hint }) {
  return (
    <div className="flex items-center justify-between gap-2" title={hint}>
      <dt className="sp-subtle">{k}</dt>
      <dd className="tabular-nums">{v}</dd>
    </div>
  );
}


function DomainReputationCard({ dr }) {
  const has = (b) => b ? "text-emerald-300" : "text-red-300";
  const word = (b) => b ? "Present" : "Missing";
  return (
    <div className="sp-glass rounded-xl p-5" data-testid="audit-domain-rep">
      <div className="flex items-center gap-2 mb-3">
        <ShieldCheck className="w-4 h-4 opacity-80" />
        <div className="text-[11px] uppercase tracking-widest sp-subtle">
          Email & domain security
        </div>
      </div>
      <ul className="space-y-2 text-sm">
        <li className="flex justify-between"><span className="sp-subtle">SPF record</span>
          <span className={has(dr?.spf?.present)}>{word(dr?.spf?.present)}</span></li>
        <li className="flex justify-between"><span className="sp-subtle">DMARC policy</span>
          <span className={has(dr?.dmarc?.present)}>
            {dr?.dmarc?.present ? (dr?.dmarc?.policy || "Present") : "Missing"}
          </span></li>
        <li className="flex justify-between"><span className="sp-subtle">DKIM (common selectors)</span>
          <span className={has(dr?.dkim?.verified)}>
            {dr?.dkim?.verified ? (dr?.dkim?.selectors_found || []).join(", ") : "None found"}
          </span></li>
      </ul>
      {Array.isArray(dr.issues) && dr.issues.length > 0 && (
        <div className="mt-3 rounded-lg border border-amber-500/30 bg-amber-500/5 p-3 text-xs leading-relaxed space-y-1">
          {dr.issues.map((m, i) => (
            <div key={i} className="text-amber-200">• {m}</div>
          ))}
        </div>
      )}
      <div className="text-[10px] sp-subtle mt-3 leading-relaxed">
        Missing email auth = your domain is trivially spoofable for phishing.
      </div>
    </div>
  );
}


function TrackingPixelsCard({ tp }) {
  const byCat = (tp.pixels || []).reduce((acc, p) => {
    acc[p.category] = acc[p.category] || [];
    acc[p.category].push(p);
    return acc;
  }, {});
  const cats = Object.keys(byCat);
  return (
    <div className="sp-glass rounded-xl p-5" data-testid="audit-tracking-pixels">
      <div className="flex items-center gap-2 mb-3">
        <Zap className="w-4 h-4 opacity-80" />
        <div className="text-[11px] uppercase tracking-widest sp-subtle">
          Tracking · analytics · ad pixels
        </div>
      </div>
      {tp.count === 0 ? (
        <div className="text-sm sp-subtle italic">
          No analytics, ad, or marketing pixels detected in the rendered HTML.
        </div>
      ) : (
        <div className="space-y-2">
          {cats.map((c) => (
            <div key={c}>
              <div className="text-[10px] uppercase tracking-widest sp-subtle mb-1">{c}</div>
              <div className="flex flex-wrap gap-1.5">
                {byCat[c].map((p, i) => (
                  <span key={i}
                    className="text-[11px] px-2 py-0.5 rounded-full border border-white/15 bg-white/[0.04]"
                    title={p.evidence}
                  >
                    {p.vendor}
                  </span>
                ))}
              </div>
            </div>
          ))}
        </div>
      )}
      {Array.isArray(tp.gaps) && tp.gaps.length > 0 && (
        <div className="mt-3 rounded-lg border border-amber-500/30 bg-amber-500/5 p-3 text-xs leading-relaxed space-y-1">
          {tp.gaps.map((m, i) => (
            <div key={i} className="text-amber-200">• {m}</div>
          ))}
        </div>
      )}
    </div>
  );
}


function ViewportShotsCard({ sc }) {
  const icon = { mobile: Smartphone, tablet: Tablet, desktop: Monitor };
  return (
    <div className="sp-glass rounded-xl p-5 lg:col-span-2" data-testid="audit-viewport-shots">
      <div className="flex items-center gap-2 mb-3">
        <Smartphone className="w-4 h-4 opacity-80" />
        <div className="text-[11px] uppercase tracking-widest sp-subtle">
          How the site renders — mobile, tablet, desktop
        </div>
      </div>
      <div className="grid grid-cols-3 gap-3">
        {(sc.shots || []).map((s) => {
          const Icon = icon[s.viewport] || Monitor;
          const src = s.url?.startsWith("http") ? s.url : `${BACKEND}${s.url}`;
          return (
            <div key={s.viewport} className="space-y-2">
              <div className="flex items-center gap-1.5 text-[10px] uppercase tracking-widest sp-subtle">
                <Icon className="w-3 h-3" /> {s.viewport} · {s.width}px
              </div>
              <a href={src} target="_blank" rel="noopener noreferrer"
                 className="block rounded-lg overflow-hidden border border-white/10 hover:border-orange-400/60 transition">
                <img src={src} alt={`${s.viewport} viewport`} className="w-full h-auto" loading="lazy" />
              </a>
            </div>
          );
        })}
      </div>
    </div>
  );
}


// ── Small pill used in the industry-context strip ────────────────────────
function Pill({ icon: Icon, label }) {
  if (!label) return null;
  return (
    <span className="inline-flex items-center gap-1.5 sp-glass rounded-full px-2.5 py-1 text-[10px] uppercase tracking-widest sp-subtle">
      <Icon className="w-3 h-3" /> {label}
    </span>
  );
}


// ── Cross-functional opportunities ──────────────────────────────────────
// Six cards, one per business function, in a fixed order so the reader
// always sees the same matrix shape. The icon set is intentional: each
// icon matches the function so a skimmer can land on a card by glyph
// alone. We never invent cards — if the LLM returned fewer than six,
// we render whatever it produced (the prompt requires 6, but defence
// in depth in case of a degraded LLM response).
const FUNCTION_ICONS = {
  "Marketing & Brand": Megaphone,
  "Sales & Lead Ops": Briefcase,
  "Customer Experience": Heart,
  "Operations": Settings2,
  "Finance & Reporting": LineChart,
  "Talent & Hiring": Users,
};

function CrossFunctionalOpportunities({ items }) {
  return (
    <div className="grid sm:grid-cols-2 lg:grid-cols-3 gap-3" data-testid="audit-cross-functional">
      {items.map((op, i) => {
          const Icon = FUNCTION_ICONS[op.function] || Layers;
          return (
            <div
              key={i}
              className="sp-glass rounded-xl p-4 flex flex-col gap-2"
              data-testid={`audit-cross-fn-${i}`}
            >
              <div className="flex items-center gap-2 text-[10px] uppercase tracking-widest sp-subtle">
                <Icon className="w-3.5 h-3.5 opacity-80" /> {op.function}
              </div>
              <div className="font-medium text-sm leading-snug">
                {op.headline}
              </div>
              <div className="text-xs sp-subtle leading-relaxed">
                {op.detail}
              </div>
              {op.foundry_feature && (
                <div className="mt-auto pt-2 text-[10px] uppercase tracking-widest text-orange-300/90 flex items-center gap-1">
                  <Wand2 className="w-3 h-3" /> {op.foundry_feature}
                </div>
              )}
            </div>
          );
        })}
    </div>
  );
}

