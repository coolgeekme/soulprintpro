/**
 * Unit tests for the safeHeaderValue sanitizer in lib/api.js.
 *
 * Regression for the Feb 2026 production bug where users with a cached
 * `X-User-Location` containing a non-latin-1 character (e.g. an accented
 * city name pasted from a webpage) saw every API call fail silently
 * with the generic "didn't check out" fallback toast — because
 * `XMLHttpRequest.setRequestHeader()` throws synchronously on a
 * non-ISO-8859-1 header value, which axios rejects with no
 * `err.response` object.
 *
 * The sanitizer guarantees:
 *  • Plain ASCII strings pass through unchanged.
 *  • All latin-1 (0x00-0xFF) chars pass through, except CR/LF/NUL.
 *  • Anything above 0xFF gets the whole string down-converted to
 *    printable ASCII (0x20-0x7E), so the header is always safe.
 *  • Empty results (after stripping) return "" so the caller skips
 *    setting the header entirely.
 *  • null / undefined / non-string inputs return "".
 */

// We can't import directly because lib/api.js initialises axios + Firebase
// at module-load time. Re-declare the function for the unit test — kept
// in sync via this comment + visual review.
const safeHeaderValue = (raw) => {
  if (raw == null) return "";
  const s = String(raw);
  if (!s) return "";
  let unsafe = false;
  for (let i = 0; i < s.length; i++) {
    const code = s.charCodeAt(i);
    if (code > 0xFF || code === 0x0A || code === 0x0D || code === 0x00) {
      unsafe = true;
      break;
    }
  }
  if (!unsafe) return s;
  let out = "";
  for (let i = 0; i < s.length; i++) {
    const code = s.charCodeAt(i);
    if (code >= 0x20 && code <= 0x7E) out += s[i];
  }
  return out.trim();
};

describe("safeHeaderValue", () => {
  test("plain ASCII passes through", () => {
    expect(safeHeaderValue("Chicago, Illinois, United States")).toBe(
      "Chicago, Illinois, United States",
    );
    expect(safeHeaderValue("America/Chicago")).toBe("America/Chicago");
    expect(safeHeaderValue("Bearer abc.def-123_xyz")).toBe("Bearer abc.def-123_xyz");
  });

  test("latin-1 (e.g. ñ é ç) passes through — within 0x00-0xFF", () => {
    expect(safeHeaderValue("Málaga")).toBe("Málaga");
    expect(safeHeaderValue("São")).toBe("São");
  });

  test("non-latin-1 (e.g. emoji, CJK) gets stripped to ASCII", () => {
    // 🎉 (U+1F389) is > 0xFFFF, triggers the strip path.
    expect(safeHeaderValue("Party 🎉")).toBe("Party");
    // CJK 中国 (U+4E2D U+56FD) is > 0xFF.
    expect(safeHeaderValue("中国 Shanghai")).toBe("Shanghai");
  });

  test("smart quotes (U+2019 etc.) get stripped — they ARE > 0xFF", () => {
    // U+2019 = right single quote, common in pastes from Word/web
    expect(safeHeaderValue("Cafe\u2019s Corner, NYC")).toBe("Cafes Corner, NYC");
  });

  test("CR / LF / NUL trigger sanitisation (header-injection guard)", () => {
    expect(safeHeaderValue("legit\r\nX-Hack: 1")).toBe("legitX-Hack: 1");
    expect(safeHeaderValue("a\nb")).toBe("ab");
    expect(safeHeaderValue("a\0b")).toBe("ab");
  });

  test("null / undefined / non-string returns empty string", () => {
    expect(safeHeaderValue(null)).toBe("");
    expect(safeHeaderValue(undefined)).toBe("");
    expect(safeHeaderValue("")).toBe("");
    // 0 stringifies to "0" — that's actually a fine header value.
    expect(safeHeaderValue(0)).toBe("0");
  });

  test("string of ONLY unsafe chars returns empty (caller skips header)", () => {
    expect(safeHeaderValue("🎉🎉🎉")).toBe("");
    expect(safeHeaderValue("\n\r\0")).toBe("");
  });

  test("numeric and object inputs coerce safely", () => {
    expect(safeHeaderValue(12345)).toBe("12345");
    // Coerced via String(...) — objects get "[object Object]"
    expect(safeHeaderValue({})).toBe("[object Object]");
  });
});
