import React from "react";
import LegalShell, { Section } from "../components/LegalShell";
import { Shield } from "lucide-react";

/**
 * Public Privacy Policy page.
 *
 * Plain-English version of the privacy commitments Foundry actually
 * enforces in code. Drafted to be GDPR / CCPA-compliant enough for a
 * v1 launch, but ArcheForge should still have counsel review before
 * any enterprise procurement cycle. Updates here should also update
 * the sub-processors page when the provider list changes.
 */
export default function PrivacyPage() {
  return (
    <LegalShell
      testid="privacy-page"
      eyebrow={<><Shield className="w-3 h-3" /> Privacy Policy</>}
      title="Your data, treated like it belongs to you."
      intro="This policy explains what data we collect when you use Foundry, why we collect it, who else processes it on our behalf, and the controls you have. Foundry is operated by ArcheForge, LLC ('we', 'us', 'ArcheForge')."
      lastReviewed={LAST_REVIEWED}
    >
      <Section id="who-we-are" title="Who we are">
        <p>
          Foundry is a B2B platform operated by <b>ArcheForge, LLC</b>, a
          limited liability company. Throughout this policy, references to
          "Foundry," "we," "us," or "our" refer to ArcheForge, LLC and the
          Foundry platform it operates.
        </p>
        <p>
          If you have any privacy question or want to exercise a data right
          described below, email <a href="mailto:privacy@foundryagents.ai">privacy@foundryagents.ai</a>.
        </p>
      </Section>

      <Section id="what-we-collect" title="What we collect">
        <p>We collect only what we need to run Foundry for you:</p>
        <ul className="list-disc pl-5 space-y-1">
          <li><b>Account information</b> — your name, email, password hash, profile photo (if you upload one), and the organisations / teams you create or join.</li>
          <li><b>Workspace content</b> — anything you and your team create inside Foundry: conversation messages, documents, brand assets, audit results, mission outputs, scheduled posts.</li>
          <li><b>Customer-supplied API keys</b> — if you connect your own OpenAI, Brave, Perplexity, or Kie.ai keys, we store them encrypted at rest and use them only to fulfil your requests.</li>
          <li><b>Audit lead data</b> — when a visitor submits the public Signal Audit form (name, business name, work email, website URL, and optionally phone + focus area).</li>
          <li><b>Usage logs</b> — request timestamps, the endpoints called, your IP address, and basic device / browser info, retained for security and abuse-prevention.</li>
          <li><b>Payment information</b> — handled entirely by Stripe; we never see or store your full card number.</li>
        </ul>
      </Section>

      <Section id="how-we-use" title="How we use it">
        <p>We use the data above to:</p>
        <ul className="list-disc pl-5 space-y-1">
          <li>Provide the Foundry service you signed up for — running agents, generating audits, sending PDFs, etc.</li>
          <li>Keep your account secure (login alerts, rate limiting, fraud detection).</li>
          <li>Send you transactional emails (welcome, password reset, audit-ready notifications, billing receipts).</li>
          <li>Send you optional product updates and occasional follow-ups <i>only if</i> you opted in. You can unsubscribe from any non-essential email at any time.</li>
          <li>Improve Foundry — aggregated, de-identified usage metrics help us spot bugs and prioritise features. We never use the <i>content</i> of your workspace (conversations, documents, audit details) for product analytics.</li>
        </ul>
        <p>
          We do <b>not</b> sell your data to advertisers or data brokers. Ever.
        </p>
      </Section>

      <Section id="ai-providers" title="AI providers (sub-processors)">
        <p>
          Foundry is an AI platform, which means parts of your workspace
          interactions are passed to third-party AI providers to generate
          responses, audits, and assets. We use these providers under
          paid API tiers, which means:
        </p>
        <ul className="list-disc pl-5 space-y-1">
          <li><b>Your data is NOT used to train their models.</b></li>
          <li>Providers may retain inputs and outputs for a limited window (typically 30 days) for abuse-monitoring, then delete them.</li>
        </ul>
        <p>
          For the complete list of sub-processors we use and their data
          policies, see our <a href="/legal/sub-processors"><b>Sub-processors page</b></a>.
          That list is the authoritative version and is kept up to date
          when we add or remove a provider.
        </p>
      </Section>

      <Section id="where-data-lives" title="Where your data lives">
        <p>
          Workspace content (conversations, documents, brand assets, audit
          history, custom agents) is stored in our managed MongoDB cluster
          and object storage, located in the United States. Backups are
          encrypted at rest. Access is restricted to the small operations
          team needed to run the service, logged, and audited.
        </p>
        <p>
          File uploads (logos, documents, brand assets) <b>stay in our
          storage</b>. They are only sent to an AI provider if you explicitly
          reference them in a request that needs them processed.
        </p>
      </Section>

      <Section id="retention" title="How long we keep your data">
        <ul className="list-disc pl-5 space-y-1">
          <li><b>Active accounts</b> — retained for as long as your account is active.</li>
          <li><b>Cancelled accounts</b> — workspace content is retained for 30 days after cancellation so you can reactivate, then permanently deleted.</li>
          <li><b>Public Signal Audit leads</b> — retained for up to 24 months unless you ask us to delete them sooner.</li>
          <li><b>Security / audit logs</b> — retained for up to 12 months.</li>
          <li><b>Billing records</b> — retained for the period required by U.S. tax / accounting law (typically 7 years).</li>
        </ul>
        <p>
          Enterprise customers on the Refine plan can negotiate custom
          retention windows as part of their build engagement.
        </p>
      </Section>

      <Section id="rights" title="Your rights (GDPR, CCPA, and more)">
        <p>
          Regardless of where you live, you may ask us to:
        </p>
        <ul className="list-disc pl-5 space-y-1">
          <li><b>Access</b> the personal data we hold about you.</li>
          <li><b>Correct</b> data that is inaccurate or incomplete.</li>
          <li><b>Delete</b> your account and the personal data associated with it.</li>
          <li><b>Export</b> your data in a portable format.</li>
          <li><b>Restrict or object</b> to certain processing.</li>
          <li><b>Withdraw consent</b> at any time where we relied on consent.</li>
        </ul>
        <p>
          Email <a href="mailto:privacy@foundryagents.ai">privacy@foundryagents.ai</a> with
          your request. We respond within 30 days. EU/UK residents have the
          additional right to lodge a complaint with their local data
          protection authority. California residents have the rights
          described in the CCPA / CPRA.
        </p>
      </Section>

      <Section id="security" title="Security">
        <p>
          Foundry encrypts all data in transit (TLS 1.2+) and at rest. We
          use per-org authorization on every API endpoint so members of
          one organisation can't see another's data. Our credential vault
          is designed so that even our admins can't read your API keys
          back. See the <a href="/security"><b>Security page</b></a> for the
          current posture — it's updated in lockstep with the running code.
        </p>
      </Section>

      <Section id="children" title="Children">
        <p>
          Foundry is a B2B tool and is not directed at children under 16.
          We do not knowingly collect personal data from anyone under 16.
          If you believe a child has provided us personal data, contact us
          and we will delete it.
        </p>
      </Section>

      <Section id="changes" title="Changes to this policy">
        <p>
          We may update this Privacy Policy from time to time. Material
          changes will be announced via email to account holders and on
          the platform at least 14 days before they take effect. The
          "Last reviewed" date at the top reflects the most recent change.
        </p>
      </Section>

      <Section id="contact" title="Contact">
        <p>
          ArcheForge, LLC<br />
          Attn: Privacy<br />
          <a href="mailto:privacy@foundryagents.ai">privacy@foundryagents.ai</a>
        </p>
      </Section>
    </LegalShell>
  );
}

// Surfaced near the top of the page. Bump this whenever you edit the
// content above so visitors (and procurement teams) see the most
// recent review date.
const LAST_REVIEWED = "May 13, 2026";
