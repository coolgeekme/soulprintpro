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
    // Real browsers: outerHeight = innerHeight + ~74px (browser chrome).
    // Headless reports 0 / 0 which is a huge tell. Cap to plausible
    // values without breaking sites that read the deltas.
    Object.defineProperty(window, 'outerHeight', {
      get: _nativeWrap(() => window.innerHeight + 74, 'get outerHeight'),
      configurable: true,
    });
    Object.defineProperty(window, 'outerWidth', {
      get: _nativeWrap(() => window.innerWidth, 'get outerWidth'),
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

  // 14. Canvas fingerprint noise — anti-bot systems hash the pixel
  // output of `toDataURL` / `getImageData`. Real GPUs produce minor
  // sub-pixel variance (anti-aliasing, driver quirks); headless
  // produces identical pixels every call, which is itself a tell.
  // We inject 1-bit-of-LSB noise into the alpha channel only — invisible
  // to the user, but enough to make the canvas hash different on every
  // call AND different from any cached "headless Chrome canvas hash"
  // signature the detector has on file.
  try {
    const _origToDataURL = HTMLCanvasElement.prototype.toDataURL;
    const _patchedToDataURL = function (...args) {
      try {
        const ctx = this.getContext('2d');
        if (ctx && this.width > 0 && this.height > 0) {
          // Only nudge a single random pixel's alpha — invisible but
          // changes every byte downstream of it in a JPEG/PNG encoder.
          const x = Math.floor(Math.random() * this.width);
          const y = Math.floor(Math.random() * this.height);
          const px = ctx.getImageData(x, y, 1, 1);
          // Bump alpha by a single LSB tick (clamp at 254 so we don't
          // overflow). The pixel still renders identically to humans.
          px.data[3] = px.data[3] === 0 ? 0 : Math.max(1, px.data[3] - 1);
          ctx.putImageData(px, x, y);
        }
      } catch (_) { /* ignore */ }
      return _origToDataURL.apply(this, args);
    };
    _nativeWrap(_patchedToDataURL, 'toDataURL');
    HTMLCanvasElement.prototype.toDataURL = _patchedToDataURL;

    // Same for getImageData (used directly by FingerprintJS canvas test).
    const _origGetImageData = CanvasRenderingContext2D.prototype.getImageData;
    const _patchedGetImageData = function (...args) {
      const data = _origGetImageData.apply(this, args);
      try {
        // Nudge one random pixel's R channel by ±1. Imperceptible.
        if (data && data.data && data.data.length >= 4) {
          const idx = (Math.floor(Math.random() * (data.data.length / 4))) * 4;
          data.data[idx] = (data.data[idx] + 1) & 0xff;
        }
      } catch (_) { /* ignore */ }
      return data;
    };
    _nativeWrap(_patchedGetImageData, 'getImageData');
    CanvasRenderingContext2D.prototype.getImageData = _patchedGetImageData;
  } catch (_) { /* ignore */ }

  // 15. AudioContext fingerprint noise — same idea as canvas. Anti-bot
  // systems run a known sine-wave through `OfflineAudioContext` and
  // hash the output samples. Headless produces identical samples every
  // run (deterministic). We add inaudible LSB noise so the hash varies
  // per call AND doesn't match the detector's "headless Chrome audio
  // hash" signature.
  try {
    const _origGetChannelData = AudioBuffer.prototype.getChannelData;
    const _patchedGetChannelData = function (channel) {
      const data = _origGetChannelData.call(this, channel);
      try {
        if (data && data.length > 0) {
          // Add 1e-7 noise to a few samples — way below human hearing
          // threshold (~1e-4) but enough to change the hash.
          for (let i = 0; i < 5; i++) {
            const idx = Math.floor(Math.random() * data.length);
            data[idx] = data[idx] + (Math.random() * 2 - 1) * 1e-7;
          }
        }
      } catch (_) { /* ignore */ }
      return data;
    };
    _nativeWrap(_patchedGetChannelData, 'getChannelData');
    AudioBuffer.prototype.getChannelData = _patchedGetChannelData;
  } catch (_) { /* ignore */ }

  // 16. speechSynthesis.getVoices — headless returns 0 voices, real
  // browsers report 10-30. We fake the canonical macOS Chrome voice
  // list so detectors checking voice count get a plausible number.
  try {
    if (window.speechSynthesis) {
      const fakeVoices = [
        { name: 'Alex',            lang: 'en-US', voiceURI: 'Alex',            localService: true,  default: true  },
        { name: 'Samantha',        lang: 'en-US', voiceURI: 'Samantha',        localService: true,  default: false },
        { name: 'Daniel',          lang: 'en-GB', voiceURI: 'Daniel',          localService: true,  default: false },
        { name: 'Karen',           lang: 'en-AU', voiceURI: 'Karen',           localService: true,  default: false },
        { name: 'Moira',           lang: 'en-IE', voiceURI: 'Moira',           localService: true,  default: false },
        { name: 'Tessa',           lang: 'en-ZA', voiceURI: 'Tessa',           localService: true,  default: false },
        { name: 'Veena',           lang: 'en-IN', voiceURI: 'Veena',           localService: true,  default: false },
        { name: 'Google US English', lang: 'en-US', voiceURI: 'Google US English', localService: false, default: false },
      ];
      const _origGetVoices = window.speechSynthesis.getVoices.bind(window.speechSynthesis);
      const _patchedGetVoices = function () {
        const real = _origGetVoices();
        return (real && real.length > 0) ? real : fakeVoices;
      };
      _nativeWrap(_patchedGetVoices, 'getVoices');
      window.speechSynthesis.getVoices = _patchedGetVoices;
    }
  } catch (_) { /* ignore */ }

  // 17. enumerateDevices — fake a normal Mac with a webcam + mic.
  // Headless reports 0 devices; real users have at least 1 audioinput,
  // 1 audiooutput, 1 videoinput.
  try {
    if (navigator.mediaDevices && navigator.mediaDevices.enumerateDevices) {
      const _origEnum = navigator.mediaDevices.enumerateDevices.bind(navigator.mediaDevices);
      const _patchedEnum = async function () {
        const real = await _origEnum();
        if (real && real.length > 0) return real;
        return [
          { deviceId: 'default',     groupId: 'default-grp', kind: 'audioinput',  label: '' },
          { deviceId: 'communications', groupId: 'comms-grp', kind: 'audioinput', label: '' },
          { deviceId: 'default',     groupId: 'default-grp', kind: 'audiooutput', label: '' },
          { deviceId: 'cam',         groupId: 'cam-grp',     kind: 'videoinput',  label: '' },
        ];
      };
      _nativeWrap(_patchedEnum, 'enumerateDevices');
      navigator.mediaDevices.enumerateDevices = _patchedEnum;
    }
  } catch (_) { /* ignore */ }

  // 18. navigator.userAgentData — Client Hints API. Real Chrome 132
  // reports brand=Chromium 132 / Google Chrome 132 / Not A(Brand 8.
  // Headless leaves this with the headless brand, which is detected.
  try {
    if (navigator.userAgentData) {
      const fakeBrands = [
        { brand: 'Not A(Brand', version: '8' },
        { brand: 'Chromium',    version: '132' },
        { brand: 'Google Chrome', version: '132' },
      ];
      Object.defineProperty(navigator.userAgentData, 'brands', {
        get: _nativeWrap(() => fakeBrands, 'get brands'),
        configurable: true,
      });
      Object.defineProperty(navigator.userAgentData, 'mobile', {
        get: _nativeWrap(() => false, 'get mobile'),
        configurable: true,
      });
      Object.defineProperty(navigator.userAgentData, 'platform', {
        get: _nativeWrap(() => 'macOS', 'get platform'),
        configurable: true,
      });
    }
  } catch (_) { /* ignore */ }
})();
"""

__all__ = ["STEALTH_INIT_SCRIPT"]
