import React from "react";
import LegalShell, { Section } from "../components/LegalShell";
import { Server, ExternalLink } from "lucide-react";

/**
 * Public Sub-processors page.
 *
 * Lists every third-party service that processes customer data on
 * Foundry's behalf, what data they see, where they're located, and a
 * link to each provider's data policy. This is the authoritative
 * source — the Privacy Policy points here for the up-to-date list.
 *
 * Required for GDPR Art. 28 transparency, and a standard enterprise
 * procurement checklist item. Keep this updated when you add or
 * remove a provider.
 */
export default function SubProcessorsPage() {
  return (
    <LegalShell
      testid="sub-processors-page"
      eyebrow={<><Server className="w-3 h-3" /> Sub-processors</>}
      title="The third-party services that help us run Foundry."
      intro="To deliver Foundry we use a small set of trusted third-party services ('sub-processors'). Each one signs paid-tier agreements with us under terms that protect your data. None of these providers use your data to train their AI models."
      lastReviewed={LAST_REVIEWED}
    >
      <Section id="overview" title="At a glance">
        <p>
          On paid API tiers, every AI provider listed below contractually
          commits that:
        </p>
        <ul className="list-disc pl-5 space-y-1">
          <li>Your data is <b>not</b> used to train their models.</li>
          <li>Inputs and outputs are retained for a limited window (typically 30 days) for abuse-monitoring, then deleted.</li>
          <li>Data is encrypted in transit and at rest.</li>
        </ul>
        <p>
          For enterprise customers (Refine plan) we can route through
          provider Zero-Data-Retention agreements where available, or
          accept a customer's own API keys so requests stay under the
          customer's contract with the provider. Contact{" "}
          <a href="mailto:legal@foundryagents.ai">legal@foundryagents.ai</a> to scope a custom arrangement.
        </p>
      </Section>

      <Section id="ai-providers" title="AI providers">
        <SubProcessorTable rows={AI_PROVIDERS} />
      </Section>

      <Section id="infrastructure" title="Infrastructure & operations">
        <SubProcessorTable rows={INFRA_PROVIDERS} />
      </Section>

      <Section id="communications" title="Communications & billing">
        <SubProcessorTable rows={COMMS_PROVIDERS} />
      </Section>

      <Section id="changes" title="Changes to this list">
        <p>
          When we add a new sub-processor that handles customer data, we
          will update this page and (for active customers on Temper+
          plans) notify you by email at least 14 days in advance unless
          the addition is required for security or legal reasons.
        </p>
        <p>
          To object to a sub-processor addition, contact{" "}
          <a href="mailto:legal@foundryagents.ai">legal@foundryagents.ai</a>. If we
          cannot accommodate the objection, you may terminate the
          affected subscription with a pro-rated refund of any prepaid
          fees for the period after the change takes effect.
        </p>
      </Section>
    </LegalShell>
  );
}


function SubProcessorTable({ rows }) {
  return (
    <div className="overflow-x-auto rounded-xl border border-white/10">
      <table className="w-full text-xs">
        <thead className="bg-white/[0.04] text-[10px] uppercase tracking-widest sp-subtle">
          <tr>
            <th className="text-left py-2.5 px-3">Provider</th>
            <th className="text-left py-2.5 px-3">What they do</th>
            <th className="text-left py-2.5 px-3">Data they may see</th>
            <th className="text-left py-2.5 px-3">Location</th>
            <th className="text-left py-2.5 px-3">Policy</th>
          </tr>
        </thead>
        <tbody className="divide-y divide-white/5">
          {rows.map((r) => (
            <tr key={r.name} className="align-top">
              <td className="py-3 px-3 font-medium whitespace-nowrap">{r.name}</td>
              <td className="py-3 px-3 sp-subtle leading-relaxed">{r.purpose}</td>
              <td className="py-3 px-3 sp-subtle leading-relaxed">{r.dataSeen}</td>
              <td className="py-3 px-3 sp-subtle whitespace-nowrap">{r.location}</td>
              <td className="py-3 px-3">
                <a
                  href={r.policyUrl}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="inline-flex items-center gap-1 text-orange-300 hover:underline"
                >
                  Policy <ExternalLink className="w-3 h-3" />
                </a>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

const LAST_REVIEWED = "May 13, 2026";

const AI_PROVIDERS = [
  {
    name: "OpenAI",
    purpose: "Text generation, image generation, audio transcription (Whisper). Used for agent chats, audits, image creation, voice features.",
    dataSeen: "Prompts and responses for requests routed to GPT models. Image prompts and outputs. Audio bytes for transcription. Files attached to a prompt by the user.",
    location: "United States",
    policyUrl: "https://openai.com/policies/api-data-usage-policies",
  },
  {
    name: "Anthropic",
    purpose: "Text generation via Claude models. Used for selected agent tasks (legal-style writing, long-context analysis) and select audits.",
    dataSeen: "Prompts and responses for requests routed to Claude. Documents attached to a Claude conversation.",
    location: "United States",
    policyUrl: "https://www.anthropic.com/legal/commercial-terms",
  },
  {
    name: "Google (Gemini)",
    purpose: "Image generation (Nano Banana), select text tasks. Used when users generate brand visuals or run Gemini-routed conversations.",
    dataSeen: "Image generation prompts, reference images, and resulting images. Text prompts for Gemini routed tasks.",
    location: "United States",
    policyUrl: "https://ai.google.dev/gemini-api/terms",
  },
  {
    name: "Perplexity",
    purpose: "Live web research used by the Signal Audit pipeline (Sonar Pro API).",
    dataSeen: "Business name, website URL, and audit hint sent as a research query.",
    location: "United States",
    policyUrl: "https://www.perplexity.ai/hub/legal/perplexity-api-terms-of-service",
  },
  {
    name: "Brave Search",
    purpose: "Web search results powering the Signal Audit and certain agent web-research actions.",
    dataSeen: "Search query strings (typically a business name or topic). No customer-identifying data attached.",
    location: "United States",
    policyUrl: "https://search.brave.com/help/privacy-policy",
  },
  {
    name: "Kie.ai",
    purpose: "Optional generative-media pipeline available to Refine customers who provide their own key.",
    dataSeen: "Only requests routed via a customer's own Kie.ai API key, on that customer's behalf.",
    location: "Singapore",
    policyUrl: "https://kie.ai/privacy",
  },
  {
    name: "Emergent (Universal LLM Key)",
    purpose: "Routing layer for OpenAI / Anthropic / Google when customers use the universal Emergent key instead of bringing their own.",
    dataSeen: "The same prompts and outputs that pass through to the underlying provider — Emergent's infrastructure sees the request before forwarding it.",
    location: "United States",
    policyUrl: "https://emergent.sh/privacy",
  },
];

const INFRA_PROVIDERS = [
  {
    name: "MongoDB Atlas",
    purpose: "Managed primary database. Stores all workspace content: accounts, organisations, conversations, documents, audits, leads.",
    dataSeen: "All persisted application data, encrypted at rest.",
    location: "United States",
    policyUrl: "https://www.mongodb.com/legal/privacy-policy",
  },
  {
    name: "Cloudflare",
    purpose: "DNS, CDN, DDoS protection, TLS termination for foundryagents.ai.",
    dataSeen: "Request metadata (IP, headers, URL paths). Does not see request body content on encrypted endpoints unless TLS terminates at Cloudflare; we use end-to-end TLS for sensitive routes where applicable.",
    location: "Global edge (data primarily processed in user's region)",
    policyUrl: "https://www.cloudflare.com/privacypolicy/",
  },
];

const COMMS_PROVIDERS = [
  {
    name: "Stripe",
    purpose: "Payment processing for subscription and add-on purchases.",
    dataSeen: "Billing name, email, payment method details. We never see your full card number.",
    location: "United States",
    policyUrl: "https://stripe.com/privacy",
  },
  {
    name: "Resend",
    purpose: "Transactional email delivery (login alerts, audit-ready notifications, password resets, billing receipts, optional product updates).",
    dataSeen: "Recipient email address, subject line, email body, attachments such as your audit PDF.",
    location: "United States",
    policyUrl: "https://resend.com/legal/privacy-policy",
  },
];
