// Centralized date/time helpers.
//
// Backend reality: Many MongoDB documents are returned as naive ISO strings
// (e.g. "2026-05-12T14:08:39.771000") because BSON drops tzinfo on read.
// All those timestamps are actually UTC (the backend always writes
// datetime.now(timezone.utc)). The browser, however, parses naive ISO strings
// as LOCAL time, so we must normalize before formatting.
//
// Use parseServerDate() everywhere a date arrives from the API.

export function parseServerDate(input) {
  if (input == null || input === "") return null;
  if (input instanceof Date) return isNaN(input.getTime()) ? null : input;

  // Numeric epoch (seconds or millis)
  if (typeof input === "number") {
    const ms = input < 1e12 ? input * 1000 : input;
    const d = new Date(ms);
    return isNaN(d.getTime()) ? null : d;
  }

  if (typeof input !== "string") return null;
  const s = input.trim();
  if (!s) return null;

  // ISO 8601-ish: if there's no timezone designator, assume UTC.
  // A timezone designator is: trailing "Z" or a "+HH:MM" / "-HH:MM" after the "T".
  const hasTz = /Z$|[+-]\d{2}:?\d{2}$/.test(s);
  const looksIso = /^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}/.test(s);

  let d;
  if (looksIso && !hasTz) {
    // Normalize space to T, append Z so JS treats as UTC.
    d = new Date(s.replace(" ", "T") + "Z");
  } else {
    d = new Date(s);
  }
  return isNaN(d.getTime()) ? null : d;
}

export function formatLocalDateTime(input, opts) {
  const d = parseServerDate(input);
  if (!d) return "";
  try {
    return d.toLocaleString([], opts);
  } catch {
    return d.toLocaleString();
  }
}

export function formatLocalDate(input, opts) {
  const d = parseServerDate(input);
  if (!d) return "";
  try {
    return d.toLocaleDateString([], opts);
  } catch {
    return d.toLocaleDateString();
  }
}

export function formatLocalTime(input, opts) {
  const d = parseServerDate(input);
  if (!d) return "";
  const options = opts || { hour: "2-digit", minute: "2-digit" };
  try {
    return d.toLocaleTimeString([], options);
  } catch {
    return d.toLocaleTimeString();
  }
}

// Smart relative + absolute format: "Just now", "3m ago", "2h ago",
// "Yesterday 4:12 PM", or a local date+time.
export function formatRelativeTime(input) {
  const d = parseServerDate(input);
  if (!d) return "";
  const now = Date.now();
  const diff = now - d.getTime();
  if (diff < 0) return formatLocalDateTime(d);
  const sec = Math.floor(diff / 1000);
  if (sec < 45) return "Just now";
  const min = Math.floor(sec / 60);
  if (min < 60) return `${min}m ago`;
  const hr = Math.floor(min / 60);
  if (hr < 24) return `${hr}h ago`;
  const day = Math.floor(hr / 24);
  if (day < 7) return `${day}d ago`;
  return formatLocalDate(d);
}

// Short "compact" form used inside chat rows / list cells.
export function formatShortDateTime(input) {
  const d = parseServerDate(input);
  if (!d) return "";
  try {
    return d.toLocaleString([], { dateStyle: "medium", timeStyle: "short" });
  } catch {
    return d.toLocaleString();
  }
}
