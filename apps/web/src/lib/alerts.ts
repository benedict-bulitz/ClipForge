/**
 * ClipForge's one semantic alert system: tones, their CSS classes and the
 * severity rules shared by every banner, badge and inline note.  The colours
 * live in app/globals.css as theme tokens (light and dark); components never
 * pick raw red/amber/green Tailwind colours for status.
 *
 * error   - an actual failure or something that blocks the action
 * warning - a limitation or risk the user should know about (never blocking by itself)
 * success - something finished as intended
 * info    - neutral context
 */
export type AlertTone = "error" | "warning" | "success" | "info";
export type BadgeTone = AlertTone | "muted";
export type AlertSize = "sm" | "md" | "lg";

export const ALERT_TONES: AlertTone[] = ["error", "warning", "success", "info"];

export function toneClass(tone: BadgeTone): string {
  return `cf-tone-${tone}`;
}

export function textToneClass(tone: AlertTone): string {
  return `cf-text-${tone}`;
}

/** Banner shape + tone colours. */
export function alertClass(tone: AlertTone, size: AlertSize = "md"): string {
  return size === "md" ? `cf-alert ${toneClass(tone)}` : `cf-alert cf-alert-${size} ${toneClass(tone)}`;
}

/** Pill shape + tone colours (status chips). */
export function badgeClass(tone: BadgeTone): string {
  return `cf-badge ${toneClass(tone)}`;
}

/** Errors interrupt (role=alert); everything else is announced politely. */
export function alertRole(tone: AlertTone): "alert" | "status" {
  return tone === "error" ? "alert" : "status";
}

/**
 * Provider restrictions (account cards, the Upload sheet).  A restriction that
 * blocks publishing is an error; a limitation that still lets the user publish
 * (e.g. an unaudited TikTok app: private posts only) is a warning.
 */
export function restrictionTone(restriction: { code?: string; blocks_publishing?: boolean }): AlertTone {
  if (restriction.code === "unaudited_client" || restriction.code === "token_expiring") return "warning";
  return restriction.blocks_publishing ? "error" : "warning";
}

/** Component notice tones (including legacy spellings) -> the semantic tone. */
export function noticeTone(tone: "success" | "error" | "info" | "warn" | "warning" | "ok"): AlertTone {
  if (tone === "error") return "error";
  if (tone === "success" || tone === "ok") return "success";
  if (tone === "info") return "info";
  return "warning";
}
