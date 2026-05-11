/**
 * Centralised brand constants.
 *
 * Keep this the single source of truth so future rebrands (or
 * white-label deploys) flip in one file, not 80.
 *
 * `PRODUCT_NAME`  — what users see as the platform
 * `COMPANY_NAME`  — legal/parent entity, shown in fine print + emails
 * `FULL_NAME`     — combined "Product by Company" lockup for hero copy
 * `AGENCY_NAME`   — the done-for-you/services arm
 * `DOMAIN`        — primary marketing domain (used in copy / emails;
 *                   never used for API calls — those go through
 *                   REACT_APP_BACKEND_URL)
 *
 * IMPORTANT: "SoulPrint" alone (without "Engine") is the domain noun
 * for the per-team mission/identity document. NEVER rename that.
 */
export const PRODUCT_NAME = "Foundry";
export const COMPANY_NAME = "SoulPrint Engine";
export const FULL_NAME = "Foundry by SoulPrint Engine";
export const AGENCY_NAME = "ArcheForge Studios";
export const DOMAIN = "foundryagents.ai";

// Convenience for `<title>` / meta / email subject lines.
export const TAGLINE = "Your company's AI crew.";
