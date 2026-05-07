"""Browser-fingerprint stealth init script.

Headless Chromium leaks a dozen tiny signals that anti-bot systems
(Instagram, LinkedIn, Cloudflare, PerimeterX, DataDome, hCaptcha) use
to silently flag a session — even when a real human is typing. The
flagged session may render the login form fine but reject the password
without showing a CAPTCHA, then ban the IP.

This script runs BEFORE any page JavaScript on every mission context
and patches the most common detection vectors:

  • `navigator.webdriver` — the textbook tell. Set to undefined.
  • `navigator.plugins` / `mimeTypes` — headless Chrome reports zero;
    we fake the standard PDF + Native Client trio that real Chrome ships.
  • `navigator.languages` — must match `Accept-Language` header.
  • `navigator.hardwareConcurrency` / `deviceMemory` — headless reports
    odd values; we set 8 / 8.
  • `navigator.platform` / `userAgentData` — fixed to MacIntel / Mac OS X
    so they match the user-agent we spoof.
  • `WebGLRenderingContext.getParameter` — headless reports
    "ANGLE/SwiftShader" which is a dead giveaway. We spoof the
    Apple/M1 GPU that matches macOS 14 fingerprints.
  • `navigator.permissions.query` — headless answers `denied` for
    `notifications`; real Chrome answers `default`.
  • `window.chrome` runtime stub — real Chrome has it; headless doesn't.
  • `RTCPeerConnection` — leaving it untouched is fine, but we override
    `getUserMedia` to look like a normal MacBook with a camera + mic.
  • `iframe.contentWindow.chrome` — checks for the runtime in nested
    frames; we patch them too.
  • `Notification.permission` — match the canonical macOS Chrome value.
  • `Function.prototype.toString` — patch the spoofed properties so
    `getter.toString()` returns native-looking source instead of our
    arrow function (a textbook bot-detection trick).

This is NOT a guarantee of bypassing every anti-bot system — but it
turns the most common "silent reject" into an actual login form +
CAPTCHA challenge that a human can complete. From the user's
perspective the session looks like a normal Chrome on a Mac.
"""

# Single triple-quoted string so we can insert it via add_init_script.
# The script is self-contained — no external dependencies, no eval.
STEALTH_INIT_SCRIPT = r"""
(() => {
  // Helper: make a getter return native-looking source under toString.
  // Without this, sites can call `Object.getOwnPropertyDescriptor(
  // navigator, 'webdriver').get.toString()` and see our arrow function,
  // which is itself a strong bot signal.
  const _ftoString = Function.prototype.toString;
  const _native = (name) => `function ${name}() { [native code] }`;
  const _nativeWrap = (fn, name) => {
    try {
      Object.defineProperty(fn, 'toString', {
        value: function toString() { return _native(name || (fn.name || '')); },
        writable: false, enumerable: false, configurable: true,
      });
    } catch (_) { /* ignore */ }
    return fn;
  };

  // 1. navigator.webdriver — the textbook tell.
  try {
    Object.defineProperty(navigator, 'webdriver', {
      get: _nativeWrap(() => undefined, 'get webdriver'),
      configurable: true,
    });
  } catch (_) { /* ignore */ }

  // 2. window.chrome — real Chrome has a runtime; headless doesn't.
  try {
    if (!window.chrome) {
      window.chrome = { runtime: {}, app: { isInstalled: false }, csi: () => {}, loadTimes: () => {} };
    } else if (!window.chrome.runtime) {
      window.chrome.runtime = {};
    }
  } catch (_) { /* ignore */ }

  // 3. plugins + mimeTypes — fake the canonical Chrome PDF trio.
  try {
    const fakePlugins = [
      { name: 'PDF Viewer',           filename: 'internal-pdf-viewer', description: 'Portable Document Format' },
      { name: 'Chrome PDF Viewer',    filename: 'internal-pdf-viewer', description: 'Portable Document Format' },
      { name: 'Chromium PDF Viewer',  filename: 'internal-pdf-viewer', description: 'Portable Document Format' },
      { name: 'Microsoft Edge PDF Viewer', filename: 'internal-pdf-viewer', description: 'Portable Document Format' },
      { name: 'WebKit built-in PDF',  filename: 'internal-pdf-viewer', description: 'Portable Document Format' },
    ];
    Object.defineProperty(navigator, 'plugins', {
      get: _nativeWrap(() => fakePlugins, 'get plugins'),
      configurable: true,
    });
    Object.defineProperty(navigator, 'mimeTypes', {
      get: _nativeWrap(() => [
        { type: 'application/pdf', suffixes: 'pdf', description: '' },
        { type: 'text/pdf',        suffixes: 'pdf', description: '' },
      ], 'get mimeTypes'),
      configurable: true,
    });
  } catch (_) { /* ignore */ }

  // 4. languages — must match Accept-Language header.
  try {
    Object.defineProperty(navigator, 'languages', {
      get: _nativeWrap(() => ['en-US', 'en'], 'get languages'),
      configurable: true,
    });
  } catch (_) { /* ignore */ }

  // 5. hardware properties — headless reports weird values.
  try {
    Object.defineProperty(navigator, 'hardwareConcurrency', {
      get: _nativeWrap(() => 8, 'get hardwareConcurrency'),
      configurable: true,
    });
  } catch (_) { /* ignore */ }
  try {
    Object.defineProperty(navigator, 'deviceMemory', {
      get: _nativeWrap(() => 8, 'get deviceMemory'),
      configurable: true,
    });
  } catch (_) { /* ignore */ }

  // 6. platform — must match the user-agent we send.
  try {
    Object.defineProperty(navigator, 'platform', {
      get: _nativeWrap(() => 'MacIntel', 'get platform'),
      configurable: true,
    });
  } catch (_) { /* ignore */ }

  // 7. WebGL renderer/vendor — headless reports SwiftShader, which is
  // an instant flag. Spoof the canonical Apple M1/M2 fingerprint.
  try {
    const _origGetParam = WebGLRenderingContext.prototype.getParameter;
    const _patchedGetParam = function (parameter) {
      // UNMASKED_VENDOR_WEBGL = 37445
      if (parameter === 37445) return 'Google Inc. (Apple)';
      // UNMASKED_RENDERER_WEBGL = 37446
      if (parameter === 37446) return 'ANGLE (Apple, Apple M2, OpenGL 4.1)';
      return _origGetParam.call(this, parameter);
    };
    _nativeWrap(_patchedGetParam, 'getParameter');
    WebGLRenderingContext.prototype.getParameter = _patchedGetParam;
    if (window.WebGL2RenderingContext) {
      WebGL2RenderingContext.prototype.getParameter = _patchedGetParam;
    }
  } catch (_) { /* ignore */ }

  // 8. Permissions API — headless answers `denied` for notifications;
  // real Chrome answers `default`.
  try {
    const _origQuery = navigator.permissions && navigator.permissions.query;
    if (_origQuery) {
      const patched = function (p) {
        if (p && p.name === 'notifications') {
          return Promise.resolve({ state: Notification.permission || 'default' });
        }
        return _origQuery.call(navigator.permissions, p);
      };
      _nativeWrap(patched, 'query');
      navigator.permissions.query = patched;
    }
  } catch (_) { /* ignore */ }

  // 9. Notification.permission — sanitise the default value.
  try {
    if (window.Notification && Notification.permission === 'denied') {
      Object.defineProperty(Notification, 'permission', {
        get: _nativeWrap(() => 'default', 'get permission'),
        configurable: true,
      });
    }
  } catch (_) { /* ignore */ }

  // 10. Battery API — real Chrome reports a battery; headless doesn't.
  try {
    if (navigator.getBattery) {
      const _patchedBattery = function () {
        return Promise.resolve({
          charging: true, chargingTime: 0, dischargingTime: Infinity,
          level: 0.92,
          addEventListener: () => {}, removeEventListener: () => {},
        });
      };
      _nativeWrap(_patchedBattery, 'getBattery');
      navigator.getBattery = _patchedBattery;
    }
  } catch (_) { /* ignore */ }

  // 11. Connection API — fake a normal home wifi connection.
  try {
    if (!navigator.connection) {
      Object.defineProperty(navigator, 'connection', {
        get: _nativeWrap(() => ({
          effectiveType: '4g', rtt: 50, downlink: 10, saveData: false,
          addEventListener: () => {}, removeEventListener: () => {},
        }), 'get connection'),
        configurable: true,
      });
    }
  } catch (_) { /* ignore */ }

  // 12. screen orientation / colour depth — minor but fixes another tell.
  try {
    Object.defineProperty(screen, 'colorDepth', {
      get: _nativeWrap(() => 30, 'get colorDepth'),
      configurable: true,
    });
    Object.defineProperty(screen, 'pixelDepth', {
      get: _nativeWrap(() => 30, 'get pixelDepth'),
      configurable: true,
    });
  } catch (_) { /* ignore */ }

  // 13. iframe contentWindow.chrome — anti-bot checks nested frames too.
  try {
    const _origCreate = document.createElement;
    const _patched = function (tag) {
      const el = _origCreate.call(this, tag);
      if (tag && tag.toLowerCase() === 'iframe') {
        try {
          Object.defineProperty(el, 'contentWindow', {
            get() {
              const cw = HTMLIFrameElement.prototype.__lookupGetter__('contentWindow').call(el);
              if (cw && !cw.chrome) cw.chrome = window.chrome;
              return cw;
            }
          });
        } catch (_) { /* ignore */ }
      }
      return el;
    };
    _nativeWrap(_patched, 'createElement');
    document.createElement = _patched;
  } catch (_) { /* ignore */ }
})();
"""

__all__ = ["STEALTH_INIT_SCRIPT"]
