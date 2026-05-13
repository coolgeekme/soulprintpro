import React from "react";
import LegalShell, { Section } from "../components/LegalShell";
import { FileText } from "lucide-react";

/**
 * Public Terms & Conditions page.
 *
 * Standard SaaS terms covering account use, payment, acceptable-use,
 * IP, AI output ownership, warranty disclaimers, and liability cap.
 * Drafted to be reasonable for a v1 launch but should be reviewed by
 * counsel before any enterprise procurement cycle.
 */
export default function TermsPage() {
  return (
    <LegalShell
      testid="terms-page"
      eyebrow={<><FileText className="w-3 h-3" /> Terms & Conditions</>}
      title="The agreement between you and ArcheForge."
      intro="These Terms govern your access to and use of Foundry. By creating an account, you agree to these Terms. If you're using Foundry on behalf of a company, you represent that you're authorised to bind that company."
      lastReviewed={LAST_REVIEWED}
    >
      <Section id="parties" title="1. Parties">
        <p>
          These Terms are between you (or the company you represent) and{" "}
          <b>ArcheForge, LLC</b>, a limited liability company organised in
          the United States ("ArcheForge", "we", "us"). The Foundry
          platform and all related services are provided by ArcheForge.
        </p>
      </Section>

      <Section id="account" title="2. Your account">
        <p>
          To use Foundry, you must register and provide accurate
          information. You are responsible for all activity that happens
          under your account, including any team members you invite.
          Keep your login credentials secure. Notify us immediately if
          you suspect unauthorised access.
        </p>
        <p>
          You must be at least 16 years old to use Foundry. Foundry is
          intended for business use; consumer use is not supported.
        </p>
      </Section>

      <Section id="plans" title="3. Plans, billing, and renewal">
        <p>
          Foundry offers paid subscription plans (Spark, Temper, Refine)
          plus optional add-ons. Subscription fees are billed in advance
          on a monthly or annual cadence depending on the plan you choose.
          Add-on packs are billed at the time of purchase.
        </p>
        <p>
          All subscriptions renew automatically at the end of each billing
          period unless cancelled before renewal. You can cancel anytime
          from your account settings; cancellation takes effect at the end
          of the current billing period. We do not provide pro-rated
          refunds for partial periods, except where required by law.
        </p>
        <p>
          Prices are exclusive of any applicable taxes. Payments are
          processed by Stripe; we do not see or store full card numbers.
        </p>
      </Section>

      <Section id="acceptable-use" title="4. Acceptable use">
        <p>You agree not to use Foundry to:</p>
        <ul className="list-disc pl-5 space-y-1">
          <li>Violate any law or third-party right (including intellectual property and privacy rights).</li>
          <li>Generate or distribute content that is defamatory, harassing, unlawful, or that targets a specific individual to harm them.</li>
          <li>Generate or distribute sexual content involving minors, content that incites violence, or content designed to spread disinformation campaigns.</li>
          <li>Run security probes, vulnerability scans, or load tests against Foundry without our written permission.</li>
          <li>Scrape or reverse-engineer Foundry, or attempt to extract our prompts, agent definitions, or model weights.</li>
          <li>Resell Foundry seats or audits as a standalone product without an authorised reseller agreement. (Embedding audits inside your own client deliverables under the white-label feature IS permitted on Temper+ plans.)</li>
          <li>Use Foundry to send unsolicited marketing emails or SMS, or to perform any other action that violates the CAN-SPAM Act, TCPA, GDPR, or comparable laws.</li>
        </ul>
        <p>
          We may suspend or terminate accounts that violate these rules.
          For serious violations we may do so without prior notice.
        </p>
      </Section>

      <Section id="ai-output" title="5. AI-generated output">
        <p>
          Foundry uses large language models and other AI systems to
          generate text, images, audits, and other outputs. By design,
          AI output:
        </p>
        <ul className="list-disc pl-5 space-y-1">
          <li>May be inaccurate, incomplete, or biased. <b>You are responsible for reviewing AI output before publishing, sending, or acting on it.</b></li>
          <li>Is generated probabilistically; identical prompts can produce different outputs.</li>
          <li>May resemble content produced for other Foundry users, because models generate from learned patterns. This is not plagiarism on our part.</li>
        </ul>
        <p>
          Subject to your full payment of fees, you own all output that
          Foundry generates specifically in response to your prompts ("Your
          Output"). You grant ArcheForge a limited, non-exclusive licence
          to host, transmit, and process Your Output as needed to provide
          the service. We do not claim copyright in Your Output.
        </p>
        <p>
          You are responsible for ensuring you have the rights to use any
          input (logos, copy, customer data) you submit to Foundry.
        </p>
      </Section>

      <Section id="ip" title="6. Our intellectual property">
        <p>
          Foundry, including its software, agent prompts, audit engine,
          UI, documentation, and the Foundry and ArcheForge marks, is and
          remains the property of ArcheForge, LLC. We grant you a limited,
          revocable, non-exclusive licence to access and use Foundry per
          these Terms. Nothing in these Terms transfers ownership of
          Foundry or any part of it to you.
        </p>
      </Section>

      <Section id="confidentiality" title="7. Confidentiality">
        <p>
          Each party may receive non-public information from the other
          ("Confidential Information"). The receiving party will use the
          same care it uses for its own confidential information (and at
          minimum reasonable care), use it only to perform under these
          Terms, and not disclose it to third parties except authorised
          sub-processors. This obligation survives for three years after
          termination.
        </p>
      </Section>

      <Section id="privacy" title="8. Privacy and data processing">
        <p>
          Our handling of personal data is governed by the{" "}
          <a href="/legal/privacy"><b>Privacy Policy</b></a> and the list of{" "}
          <a href="/legal/sub-processors"><b>Sub-processors</b></a> we use.
          Where required by law, we will execute a Data Processing
          Addendum with enterprise customers.
        </p>
      </Section>

      <Section id="third-party" title="9. Third-party services">
        <p>
          Foundry integrates with third-party services (OpenAI, Anthropic,
          Google, Perplexity, Brave, Stripe, etc.). Those services are
          governed by their own terms. We're not responsible for outages
          or changes in those services, though we'll work to minimise
          disruption.
        </p>
      </Section>

      <Section id="warranty" title="10. Warranty disclaimer">
        <p>
          Foundry is provided <b>"as is"</b> and "as available." To the
          maximum extent permitted by law, ArcheForge disclaims all
          warranties, express or implied, including warranties of
          merchantability, fitness for a particular purpose, accuracy,
          non-infringement, and uninterrupted operation. We do not
          guarantee that AI-generated content will be accurate, up-to-date,
          or suitable for any particular purpose.
        </p>
      </Section>

      <Section id="liability" title="11. Limitation of liability">
        <p>
          To the maximum extent permitted by law, in no event will
          ArcheForge be liable for any indirect, incidental, special,
          consequential, exemplary, or punitive damages — including lost
          profits, lost revenue, lost data, or business interruption —
          arising out of or related to your use of Foundry, even if we
          have been advised of the possibility of those damages.
        </p>
        <p>
          Our total aggregate liability under these Terms will not
          exceed the greater of (a) the fees you paid to ArcheForge in
          the 12 months preceding the claim or (b) USD 100.
        </p>
      </Section>

      <Section id="indemnity" title="12. Indemnity">
        <p>
          You will indemnify and hold ArcheForge harmless from claims,
          damages, and costs arising from your misuse of Foundry, your
          violation of these Terms, or your violation of third-party
          rights — including any claim that content you submitted to
          Foundry violated another person's intellectual property or
          privacy rights.
        </p>
      </Section>

      <Section id="termination" title="13. Termination">
        <p>
          You can cancel your subscription anytime from your account
          settings. We can suspend or terminate your access for any
          violation of these Terms or for non-payment of fees. On
          termination, your workspace content is retained for 30 days so
          you can export or reactivate, then permanently deleted (subject
          to records we are required by law to keep).
        </p>
      </Section>

      <Section id="changes" title="14. Changes to these Terms">
        <p>
          We may update these Terms from time to time. Material changes
          will be announced via email and on the platform at least 14
          days before they take effect. Continued use after the effective
          date constitutes acceptance.
        </p>
      </Section>

      <Section id="governing-law" title="15. Governing law and disputes">
        <p>
          These Terms are governed by the laws of the State of Delaware,
          United States, without regard to its conflict-of-law principles.
          Any dispute that the parties cannot resolve informally will be
          resolved by binding arbitration in Delaware, individually (no
          class actions), under the rules of the American Arbitration
          Association. Either party may seek injunctive relief in a court
          of competent jurisdiction to protect its intellectual property.
        </p>
      </Section>

      <Section id="misc" title="16. Miscellaneous">
        <p>
          These Terms (together with the Privacy Policy and any executed
          order form / DPA) constitute the entire agreement between you
          and ArcheForge regarding Foundry. If any provision is
          unenforceable, the rest remains in effect. We may assign these
          Terms in connection with a merger, acquisition, or sale of
          assets. You may not assign these Terms without our written
          consent.
        </p>
      </Section>

      <Section id="contact" title="17. Contact">
        <p>
          ArcheForge, LLC<br />
          Attn: Legal<br />
          <a href="mailto:legal@foundryagents.ai">legal@foundryagents.ai</a>
        </p>
      </Section>
    </LegalShell>
  );
}

const LAST_REVIEWED = "May 13, 2026";
