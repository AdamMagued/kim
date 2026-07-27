(function() {
  'use strict';
  if (window.__CHATGPT_BRIDGE_LOADED__) return;
  window.__CHATGPT_BRIDGE_LOADED__ = true;

  // ═══════════════════════════════════════════════════════════════
  // SHA3-512 (js-sha3 by Chen Yi-Cyuan, MIT license, inlined)
  // ═══════════════════════════════════════════════════════════════
  var sha3_512_digest;
  (function() {
    var PADDING = [6, 1536, 393216, 100663296];
    var SHIFT = [0, 8, 16, 24];
    var RC = [1, 0, 32898, 0, 32906, 2147483648, 2147516416, 2147483648, 32907, 0, 2147483649,
      0, 2147516545, 2147483648, 32777, 2147483648, 138, 0, 136, 0, 2147516425, 0,
      2147483658, 0, 2147516555, 0, 139, 2147483648, 32905, 2147483648, 32771,
      2147483648, 32770, 2147483648, 128, 2147483648, 32778, 0, 2147483658, 2147483648,
      2147516545, 2147483648, 32896, 2147483648, 2147483649, 0, 2147516424, 2147483648];

    function f(s) {
      var h, l, n, c0, c1, c2, c3, c4, c5, c6, c7, c8, c9,
        b0, b1, b2, b3, b4, b5, b6, b7, b8, b9, b10, b11, b12, b13, b14, b15, b16, b17,
        b18, b19, b20, b21, b22, b23, b24, b25, b26, b27, b28, b29, b30, b31, b32, b33,
        b34, b35, b36, b37, b38, b39, b40, b41, b42, b43, b44, b45, b46, b47, b48, b49;
      for (n = 0; n < 48; n += 2) {
        c0 = s[0] ^ s[10] ^ s[20] ^ s[30] ^ s[40];
        c1 = s[1] ^ s[11] ^ s[21] ^ s[31] ^ s[41];
        c2 = s[2] ^ s[12] ^ s[22] ^ s[32] ^ s[42];
        c3 = s[3] ^ s[13] ^ s[23] ^ s[33] ^ s[43];
        c4 = s[4] ^ s[14] ^ s[24] ^ s[34] ^ s[44];
        c5 = s[5] ^ s[15] ^ s[25] ^ s[35] ^ s[45];
        c6 = s[6] ^ s[16] ^ s[26] ^ s[36] ^ s[46];
        c7 = s[7] ^ s[17] ^ s[27] ^ s[37] ^ s[47];
        c8 = s[8] ^ s[18] ^ s[28] ^ s[38] ^ s[48];
        c9 = s[9] ^ s[19] ^ s[29] ^ s[39] ^ s[49];
        h = c8 ^ ((c2 << 1) | (c3 >>> 31)); l = c9 ^ ((c3 << 1) | (c2 >>> 31));
        s[0] ^= h; s[1] ^= l; s[10] ^= h; s[11] ^= l; s[20] ^= h; s[21] ^= l; s[30] ^= h; s[31] ^= l; s[40] ^= h; s[41] ^= l;
        h = c0 ^ ((c4 << 1) | (c5 >>> 31)); l = c1 ^ ((c5 << 1) | (c4 >>> 31));
        s[2] ^= h; s[3] ^= l; s[12] ^= h; s[13] ^= l; s[22] ^= h; s[23] ^= l; s[32] ^= h; s[33] ^= l; s[42] ^= h; s[43] ^= l;
        h = c2 ^ ((c6 << 1) | (c7 >>> 31)); l = c3 ^ ((c7 << 1) | (c6 >>> 31));
        s[4] ^= h; s[5] ^= l; s[14] ^= h; s[15] ^= l; s[24] ^= h; s[25] ^= l; s[34] ^= h; s[35] ^= l; s[44] ^= h; s[45] ^= l;
        h = c4 ^ ((c8 << 1) | (c9 >>> 31)); l = c5 ^ ((c9 << 1) | (c8 >>> 31));
        s[6] ^= h; s[7] ^= l; s[16] ^= h; s[17] ^= l; s[26] ^= h; s[27] ^= l; s[36] ^= h; s[37] ^= l; s[46] ^= h; s[47] ^= l;
        h = c6 ^ ((c0 << 1) | (c1 >>> 31)); l = c7 ^ ((c1 << 1) | (c0 >>> 31));
        s[8] ^= h; s[9] ^= l; s[18] ^= h; s[19] ^= l; s[28] ^= h; s[29] ^= l; s[38] ^= h; s[39] ^= l; s[48] ^= h; s[49] ^= l;
        b0 = s[0]; b1 = s[1];
        b32 = (s[11] << 4) | (s[10] >>> 28); b33 = (s[10] << 4) | (s[11] >>> 28);
        b14 = (s[20] << 3) | (s[21] >>> 29); b15 = (s[21] << 3) | (s[20] >>> 29);
        b46 = (s[31] << 9) | (s[30] >>> 23); b47 = (s[30] << 9) | (s[31] >>> 23);
        b28 = (s[40] << 18) | (s[41] >>> 14); b29 = (s[41] << 18) | (s[40] >>> 14);
        b20 = (s[2] << 1) | (s[3] >>> 31); b21 = (s[3] << 1) | (s[2] >>> 31);
        b2 = (s[13] << 12) | (s[12] >>> 20); b3 = (s[12] << 12) | (s[13] >>> 20);
        b34 = (s[22] << 10) | (s[23] >>> 22); b35 = (s[23] << 10) | (s[22] >>> 22);
        b16 = (s[33] << 13) | (s[32] >>> 19); b17 = (s[32] << 13) | (s[33] >>> 19);
        b48 = (s[42] << 2) | (s[43] >>> 30); b49 = (s[43] << 2) | (s[42] >>> 30);
        b40 = (s[5] << 30) | (s[4] >>> 2); b41 = (s[4] << 30) | (s[5] >>> 2);
        b22 = (s[14] << 6) | (s[15] >>> 26); b23 = (s[15] << 6) | (s[14] >>> 26);
        b4 = (s[25] << 11) | (s[24] >>> 21); b5 = (s[24] << 11) | (s[25] >>> 21);
        b36 = (s[34] << 15) | (s[35] >>> 17); b37 = (s[35] << 15) | (s[34] >>> 17);
        b18 = (s[45] << 29) | (s[44] >>> 3); b19 = (s[44] << 29) | (s[45] >>> 3);
        b10 = (s[6] << 28) | (s[7] >>> 4); b11 = (s[7] << 28) | (s[6] >>> 4);
        b42 = (s[17] << 23) | (s[16] >>> 9); b43 = (s[16] << 23) | (s[17] >>> 9);
        b24 = (s[26] << 25) | (s[27] >>> 7); b25 = (s[27] << 25) | (s[26] >>> 7);
        b6 = (s[36] << 21) | (s[37] >>> 11); b7 = (s[37] << 21) | (s[36] >>> 11);
        b38 = (s[47] << 24) | (s[46] >>> 8); b39 = (s[46] << 24) | (s[47] >>> 8);
        b30 = (s[8] << 27) | (s[9] >>> 5); b31 = (s[9] << 27) | (s[8] >>> 5);
        b12 = (s[18] << 20) | (s[19] >>> 12); b13 = (s[19] << 20) | (s[18] >>> 12);
        b44 = (s[29] << 7) | (s[28] >>> 25); b45 = (s[28] << 7) | (s[29] >>> 25);
        b26 = (s[38] << 8) | (s[39] >>> 24); b27 = (s[39] << 8) | (s[38] >>> 24);
        b8 = (s[48] << 14) | (s[49] >>> 18); b9 = (s[49] << 14) | (s[48] >>> 18);
        s[0] = b0 ^ (~b2 & b4); s[1] = b1 ^ (~b3 & b5);
        s[10] = b10 ^ (~b12 & b14); s[11] = b11 ^ (~b13 & b15);
        s[20] = b20 ^ (~b22 & b24); s[21] = b21 ^ (~b23 & b25);
        s[30] = b30 ^ (~b32 & b34); s[31] = b31 ^ (~b33 & b35);
        s[40] = b40 ^ (~b42 & b44); s[41] = b41 ^ (~b43 & b45);
        s[2] = b2 ^ (~b4 & b6); s[3] = b3 ^ (~b5 & b7);
        s[12] = b12 ^ (~b14 & b16); s[13] = b13 ^ (~b15 & b17);
        s[22] = b22 ^ (~b24 & b26); s[23] = b23 ^ (~b25 & b27);
        s[32] = b32 ^ (~b34 & b36); s[33] = b33 ^ (~b35 & b37);
        s[42] = b42 ^ (~b44 & b46); s[43] = b43 ^ (~b45 & b47);
        s[4] = b4 ^ (~b6 & b8); s[5] = b5 ^ (~b7 & b9);
        s[14] = b14 ^ (~b16 & b18); s[15] = b15 ^ (~b17 & b19);
        s[24] = b24 ^ (~b26 & b28); s[25] = b25 ^ (~b27 & b29);
        s[34] = b34 ^ (~b36 & b38); s[35] = b35 ^ (~b37 & b39);
        s[44] = b44 ^ (~b46 & b48); s[45] = b45 ^ (~b47 & b49);
        s[6] = b6 ^ (~b8 & b0); s[7] = b7 ^ (~b9 & b1);
        s[16] = b16 ^ (~b18 & b10); s[17] = b17 ^ (~b19 & b11);
        s[26] = b26 ^ (~b28 & b20); s[27] = b27 ^ (~b29 & b21);
        s[36] = b36 ^ (~b38 & b30); s[37] = b37 ^ (~b39 & b31);
        s[46] = b46 ^ (~b48 & b40); s[47] = b47 ^ (~b49 & b41);
        s[8] = b8 ^ (~b0 & b2); s[9] = b9 ^ (~b1 & b3);
        s[18] = b18 ^ (~b10 & b12); s[19] = b19 ^ (~b11 & b13);
        s[28] = b28 ^ (~b20 & b22); s[29] = b29 ^ (~b21 & b23);
        s[38] = b38 ^ (~b30 & b32); s[39] = b39 ^ (~b31 & b33);
        s[48] = b48 ^ (~b40 & b42); s[49] = b49 ^ (~b41 & b43);
        s[0] ^= RC[n]; s[1] ^= RC[n + 1];
      }
    }

    function Keccak(bits, padding, outputBits) {
      this.blocks = []; this.s = []; this.padding = padding;
      this.outputBits = outputBits; this.reset = true; this.finalized = false;
      this.block = 0; this.start = 0;
      this.blockCount = (1600 - (bits << 1)) >> 5;
      this.byteCount = this.blockCount << 2;
      this.outputBlocks = outputBits >> 5;
      this.extraBytes = (outputBits & 31) >> 3;
      for (var i = 0; i < 50; ++i) this.s[i] = 0;
    }

    Keccak.prototype.update = function(message) {
      if (this.finalized) throw new Error('finalize already called');
      var isString = typeof message === 'string';
      var blocks = this.blocks, byteCount = this.byteCount, length = message.length,
        blockCount = this.blockCount, index = 0, s = this.s, i, code;
      while (index < length) {
        if (this.reset) {
          this.reset = false; blocks[0] = this.block;
          for (i = 1; i < blockCount + 1; ++i) blocks[i] = 0;
        }
        if (isString) {
          for (i = this.start; index < length && i < byteCount; ++index) {
            code = message.charCodeAt(index);
            if (code < 0x80) {
              blocks[i >> 2] |= code << SHIFT[i++ & 3];
            } else if (code < 0x800) {
              blocks[i >> 2] |= (0xc0 | (code >> 6)) << SHIFT[i++ & 3];
              blocks[i >> 2] |= (0x80 | (code & 0x3f)) << SHIFT[i++ & 3];
            } else if (code < 0xd800 || code >= 0xe000) {
              blocks[i >> 2] |= (0xe0 | (code >> 12)) << SHIFT[i++ & 3];
              blocks[i >> 2] |= (0x80 | ((code >> 6) & 0x3f)) << SHIFT[i++ & 3];
              blocks[i >> 2] |= (0x80 | (code & 0x3f)) << SHIFT[i++ & 3];
            } else {
              code = 0x10000 + (((code & 0x3ff) << 10) | (message.charCodeAt(++index) & 0x3ff));
              blocks[i >> 2] |= (0xf0 | (code >> 18)) << SHIFT[i++ & 3];
              blocks[i >> 2] |= (0x80 | ((code >> 12) & 0x3f)) << SHIFT[i++ & 3];
              blocks[i >> 2] |= (0x80 | ((code >> 6) & 0x3f)) << SHIFT[i++ & 3];
              blocks[i >> 2] |= (0x80 | (code & 0x3f)) << SHIFT[i++ & 3];
            }
          }
        } else {
          for (i = this.start; index < length && i < byteCount; ++index) {
            blocks[i >> 2] |= message[index] << SHIFT[i++ & 3];
          }
        }
        this.lastByteIndex = i;
        if (i >= byteCount) {
          this.start = i - byteCount; this.block = blocks[blockCount];
          for (i = 0; i < blockCount; ++i) s[i] ^= blocks[i];
          f(s); this.reset = true;
        } else { this.start = i; }
      }
      return this;
    };

    Keccak.prototype.finalize = function() {
      if (this.finalized) return;
      this.finalized = true;
      var blocks = this.blocks, i = this.lastByteIndex, blockCount = this.blockCount, s = this.s;
      blocks[i >> 2] |= this.padding[i & 3];
      if (this.lastByteIndex === this.byteCount) {
        blocks[0] = blocks[blockCount];
        for (i = 1; i < blockCount + 1; ++i) blocks[i] = 0;
      }
      blocks[blockCount - 1] |= 0x80000000;
      for (i = 0; i < blockCount; ++i) s[i] ^= blocks[i];
      f(s);
    };

    Keccak.prototype.digest = function() {
      this.finalize();
      var blockCount = this.blockCount, s = this.s, outputBlocks = this.outputBlocks,
        extraBytes = this.extraBytes, i = 0, j = 0;
      var array = [], offset, block;
      while (j < outputBlocks) {
        for (i = 0; i < blockCount && j < outputBlocks; ++i, ++j) {
          offset = j << 2; block = s[i];
          array[offset] = block & 0xFF;
          array[offset + 1] = (block >> 8) & 0xFF;
          array[offset + 2] = (block >> 16) & 0xFF;
          array[offset + 3] = (block >> 24) & 0xFF;
        }
        if (j % blockCount === 0) { s = s.slice(); f(s); }
      }
      if (extraBytes) {
        offset = j << 2; block = s[i];
        array[offset] = block & 0xFF;
        if (extraBytes > 1) array[offset + 1] = (block >> 8) & 0xFF;
        if (extraBytes > 2) array[offset + 2] = (block >> 16) & 0xFF;
      }
      return array;
    };

    sha3_512_digest = function(message) {
      return new Keccak(512, PADDING, 512).update(message).digest();
    };
  })();

  // ═══════════════════════════════════════════════════════════════
  // State
  // ═══════════════════════════════════════════════════════════════
  var authToken = null;
  var accountId = null;
  var deviceId = crypto.randomUUID();
  var cachedScripts = [];
  var cachedDpl = '';
  // Captured before the interceptor below replaces window.fetch. The bridge's
  // own calls go through this so they are never re-intercepted.
  var originalFetch = window.fetch.bind(window);

  // ═══════════════════════════════════════════════════════════════
  // Intercept page fetch to capture auth tokens
  // ═══════════════════════════════════════════════════════════════
  window.fetch = function() {
    var args = arguments;
    var url = typeof args[0] === 'string' ? args[0] : (args[0] && args[0].url) || '';
    var opts = args[1] || {};
    if (url.indexOf('/backend-api/') !== -1) {
      var headers = opts.headers || {};
      if (headers instanceof Headers) {
        var auth = headers.get('Authorization');
        if (auth) authToken = auth.replace('Bearer ', '');
        var acc = headers.get('chatgpt-account-id');
        if (acc) accountId = acc;
        var dev = headers.get('oai-device-id');
        if (dev) deviceId = dev;
        var sentinel = headers.get('openai-sentinel-proof-token');
        if (sentinel) console.log('[Bridge] Captured page proof token:', sentinel.slice(0, 30) + '...');
        var reqToken = headers.get('openai-sentinel-chat-requirements-token');
        if (reqToken) console.log('[Bridge] Captured page requirements token:', reqToken.slice(0, 30) + '...');
      } else if (typeof headers === 'object') {
        if (headers['Authorization']) authToken = headers['Authorization'].replace('Bearer ', '');
        if (headers['authorization']) authToken = headers['authorization'].replace('Bearer ', '');
        if (headers['chatgpt-account-id']) accountId = headers['chatgpt-account-id'];
        if (headers['Chatgpt-Account-Id']) accountId = headers['Chatgpt-Account-Id'];
        if (headers['oai-device-id']) deviceId = headers['oai-device-id'];
        if (headers['Oai-Device-Id']) deviceId = headers['Oai-Device-Id'];
        var pt = headers['openai-sentinel-proof-token'] || headers['Openai-Sentinel-Proof-Token'];
        if (pt) console.log('[Bridge] Captured page proof token:', pt.slice(0, 30) + '...');
        var rt = headers['openai-sentinel-chat-requirements-token'] || headers['Openai-Sentinel-Chat-Requirements-Token'];
        if (rt) console.log('[Bridge] Captured page requirements token:', rt.slice(0, 30) + '...');
      }
    }
    return originalFetch.apply(window, args);
  };

  // ═══════════════════════════════════════════════════════════════
  // Auth & environment helpers
  // ═══════════════════════════════════════════════════════════════
  // Hoisted: solvePoW calls this once per hash attempt (up to 500k times for
  // one turn), and allocating a TextEncoder per call dominated the loop.
  var _textEncoder = new TextEncoder();
  // fromCharCode.apply over a chunk beats appending one character at a time;
  // 8192 stays well under the argument-count limit for a spread call.
  var _B64_CHUNK = 8192;

  function utf8Base64Encode(str) {
    var bytes = _textEncoder.encode(str);
    var binary = '';
    for (var k = 0; k < bytes.length; k += _B64_CHUNK) {
      binary += String.fromCharCode.apply(
        null, bytes.subarray(k, k + _B64_CHUNK)
      );
    }
    return btoa(binary);
  }

  function hexToBytes(hex) {
    var bytes = [];
    for (var i = 0; i < hex.length; i += 2)
      bytes.push(parseInt(hex.substr(i, 2), 16));
    return bytes;
  }

  function bytesLessOrEqual(hash, target, len) {
    for (var j = 0; j < len; j++) {
      if (hash[j] < target[j]) return true;
      if (hash[j] > target[j]) return false;
    }
    return true;
  }

  function refreshPageInfo() {
    var scripts = document.querySelectorAll('script[src*="cdn.oaistatic.com"]');
    cachedScripts = Array.from(scripts).map(function(s) { return s.src; });
    cachedDpl = '';
    for (var i = 0; i < cachedScripts.length; i++) {
      var m = cachedScripts[i].match(/(?:c\/|_next\/static\/)([a-zA-Z0-9_-]+)\//);
      if (m) { cachedDpl = m[1]; break; }
    }
    if (!cachedDpl) {
      var dataBuild = document.querySelector('[data-build]');
      if (dataBuild) cachedDpl = dataBuild.getAttribute('data-build');
    }
  }

  async function ensureAuth() {
    if (!authToken && window.__NEXT_DATA__ && window.__NEXT_DATA__.props && window.__NEXT_DATA__.props.pageProps) {
      var pp = window.__NEXT_DATA__.props.pageProps;
      if (pp.accessToken) authToken = pp.accessToken;
      else if (pp.session && pp.session.accessToken) authToken = pp.session.accessToken;
    }
    if (authToken) return;
    try {
      var resp = await originalFetch('/api/auth/session', { credentials: 'same-origin' });
      if (resp.ok) {
        var data = await resp.json();
        if (data.accessToken) {
          authToken = data.accessToken;
          try {
            var payload = JSON.parse(atob(authToken.split('.')[1]));
            var authInfo = payload['https://api.openai.com/auth'] || {};
            accountId = accountId || authInfo.chatgpt_account_id || '';
          } catch(e) {}
        }
      }
    } catch(e) {
      console.error('[Bridge] Failed to get auth token:', e);
    }
  }

  // ═══════════════════════════════════════════════════════════════
  // PoW Solver — runs in real browser context with authentic APIs
  // ═══════════════════════════════════════════════════════════════
  function getConfig() {
    refreshPageInfo();
    var sep = '−';

    var navProto = Object.getOwnPropertyNames(Object.getPrototypeOf(navigator));
    var navOwn = Object.getOwnPropertyNames(navigator);
    var navKeys = navProto.concat(navOwn);
    var navKey = navKeys[Math.floor(Math.random() * navKeys.length)];
    var navVal;
    try { navVal = String(navigator[navKey]); } catch(e) { navVal = 'undefined'; }

    var docKeys = Object.getOwnPropertyNames(document);
    var docKey = docKeys[Math.floor(Math.random() * docKeys.length)];

    var winKeys = Object.getOwnPropertyNames(window);
    var winKey = winKeys[Math.floor(Math.random() * winKeys.length)];

    return [
      window.screen.width + window.screen.height,
      new Date().toString(),
      4294705152,
      0,
      navigator.userAgent,
      cachedScripts.length > 0 ? cachedScripts[Math.floor(Math.random() * cachedScripts.length)] : '',
      cachedDpl,
      navigator.language || 'en-US',
      (navigator.languages || ['en-US']).join(','),
      0,
      navKey + sep + navVal,
      docKey,
      winKey,
      performance.now() * 1000,
      crypto.randomUUID(),
      '',
      navigator.hardwareConcurrency || 8,
      Date.now() - performance.now(),
    ];
  }

  function solvePoW(seed, difficulty) {
    var diffLen = difficulty.length / 2;
    var target = hexToBytes(difficulty);
    var config = getConfig();

    var part1 = JSON.stringify(config.slice(0, 3));
    var part2 = JSON.stringify(config.slice(4, 9));
    var part3 = JSON.stringify(config.slice(10));

    for (var i = 0; i < 500000; i++) {
      var configJson = part1.slice(0, -1) + ',' + i + ',' + part2.slice(1, -1) + ',' + (i >> 1) + ',' + part3.slice(1);
      var b64 = utf8Base64Encode(configJson);
      var hash = sha3_512_digest(seed + b64);

      if (bytesLessOrEqual(hash, target, diffLen)) {
        return 'gAAAAAB' + b64;
      }
    }
    // Exhausted the search space. The token below will NOT satisfy the
    // server's check — /backend-api/conversation answers 4xx and the turn
    // fails. Say so loudly: this used to be returned silently, so the real
    // cause showed up only as an unexplained HTTP error further down.
    console.error(
      '[Bridge] PoW FAILED: no solution in 500000 attempts for difficulty ' +
      difficulty + '. The next request will likely be rejected by ChatGPT.'
    );
    return 'gAAAAABwQ8Lk5FbGpA2NcR9dShT6gYjU7VxZ4D' + utf8Base64Encode('"' + seed + '"');
  }

  function generateRequirementsToken() {
    var config = getConfig();
    var b64 = utf8Base64Encode(JSON.stringify(config));
    return 'gAAAAAC' + b64;
  }

  // ═══════════════════════════════════════════════════════════════
  // Sentinel Flow + Conversation API
  // ═══════════════════════════════════════════════════════════════
  function baseHeaders() {
    var h = {
      'Content-Type': 'application/json',
      'Oai-Device-Id': deviceId,
      'Oai-Language': navigator.language || 'en-US',
    };
    if (authToken) h['Authorization'] = 'Bearer ' + authToken;
    if (accountId) h['Chatgpt-Account-Id'] = accountId;
    return h;
  }

  function bridgeLog(msg) {
    console.log('[Bridge]', msg);
    try {
      window.postMessage({
        type: 'CHATGPT_BRIDGE_RESPONSE',
        requestId: '__log__',
        event: 'log',
        delta: '[EXT] ' + msg,
      }, '*');
    } catch(e) {}
  }

  async function getChatRequirements() {
    var pToken = generateRequirementsToken();
    var resp = await originalFetch('/backend-api/sentinel/chat-requirements', {
      method: 'POST',
      headers: baseHeaders(),
      body: JSON.stringify({ p: pToken }),
    });
    if (!resp.ok) throw new Error('sentinel/chat-requirements failed: ' + resp.status);
    return resp.json();
  }

    async function sendConversation(messages, model, requestId, conversationId, parentMessageId, attachments) {
    await ensureAuth();
    if (!authToken) throw new Error('No auth token available. Make sure you are logged into ChatGPT.');

    var requirements = await getChatRequirements();
    var sentinelToken = requirements.token || '';

    var proofToken = '';
    var pow = requirements.proofofwork || {};
    if (pow.required && pow.seed && pow.difficulty) {
      var start = performance.now();
      proofToken = solvePoW(pow.seed, pow.difficulty);
      var elapsed = (performance.now() - start).toFixed(0);
      console.log('[Bridge] PoW solved in ' + elapsed + 'ms');
    }

  function b64ToBlob(b64, mime) {
    var cleanB64 = (b64 || '').replace(/^data:[^;]+;base64,/, '').replace(/[\s\r\n]/g, '');
    var byteChars = atob(cleanB64);
    var byteNumbers = new Array(byteChars.length);
    for (var i = 0; i < byteChars.length; i++) {
      byteNumbers[i] = byteChars.charCodeAt(i);
    }
    var byteArray = new Uint8Array(byteNumbers);
    return new Blob([byteArray], { type: mime });
  }

  async function uploadAttachment(att) {
    await ensureAuth();
    var b64 = att.data_base64 || att.data || '';
    var mime = att.mime_type || att.media_type || 'image/png';
    var name = att.name || 'image.png';
    if (!b64) {
      bridgeLog('uploadAttachment: no base64 data found, keys: ' + Object.keys(att).join(','));
      return null;
    }

    var blob = b64ToBlob(b64, mime);
    var size = blob.size;
    bridgeLog('uploadAttachment: Blob created, size=' + size + ' mime=' + mime);

    var headers = baseHeaders();
    headers['Content-Type'] = 'application/json';

    var useCases = ['multimodal', 'ace_upload', 'my_files'];
    var initResp = null;
    var initData = null;
    for (var u = 0; u < useCases.length; u++) {
      try {
        initResp = await originalFetch('/backend-api/files', {
          method: 'POST',
          headers: headers,
          body: JSON.stringify({
            file_name: name,
            file_size: size,
            use_case: useCases[u]
          })
        });
        bridgeLog('/backend-api/files init status (use_case=' + useCases[u] + '): ' + initResp.status);
        if (initResp.ok) {
          initData = await initResp.json();
          break;
        } else {
          var errTxt = await initResp.text();
          bridgeLog('FAIL init file upload use_case=' + useCases[u] + ': ' + errTxt.substring(0, 200));
        }
      } catch(e) {
        console.error('[Bridge] Error calling /backend-api/files:', e);
      }
    }

    if (!initData || !initData.file_id) {
      bridgeLog('FAIL: no file_id from /backend-api/files after all use_cases');
      return null;
    }

    bridgeLog('/backend-api/files OK: initData=' + JSON.stringify(initData));
    var fileId = initData.file_id;
    var uploadUrl = initData.upload_url;

    if (uploadUrl) {
      var urlDomain = '';
      try { urlDomain = new URL(uploadUrl).hostname; } catch(e) { urlDomain = uploadUrl.substring(0, 60); }
      bridgeLog('Uploading to: ' + urlDomain + ' mime=' + mime + ' blobSize=' + blob.size);

      var svParam = '';
      try {
        var uObj = new URL(uploadUrl);
        svParam = uObj.searchParams.get('sv') || '';
      } catch(e) {}
      bridgeLog('Extracted Azure svParam: ' + svParam);

      var headerOptions = [];
      if (uploadUrl.includes('oaiusercontent.com') || uploadUrl.includes('blob.core.windows.net') || uploadUrl.includes('azure')) {
        if (svParam) {
          headerOptions.push({ 'x-ms-blob-type': 'BlockBlob', 'x-ms-version': svParam, 'x-ms-blob-content-type': mime, 'Content-Type': mime });
          headerOptions.push({ 'x-ms-blob-type': 'BlockBlob', 'x-ms-version': svParam });
        }
        headerOptions.push({ 'x-ms-blob-type': 'BlockBlob', 'x-ms-blob-content-type': mime, 'Content-Type': mime });
        headerOptions.push({ 'x-ms-blob-type': 'BlockBlob', 'x-ms-version': '2020-04-08' });
        headerOptions.push({ 'x-ms-blob-type': 'BlockBlob', 'Content-Type': mime });
        headerOptions.push({ 'x-ms-blob-type': 'BlockBlob' });
      } else {
        headerOptions.push({ 'Content-Type': mime });
        headerOptions.push({ 'Content-Type': 'application/octet-stream' });
        headerOptions.push({});
      }

      var uploadSuccess = false;
      for (var hIdx = 0; hIdx < headerOptions.length; hIdx++) {
        try {
          var uploadResp = await window.fetch(uploadUrl, {
            method: 'PUT',
            headers: headerOptions[hIdx],
            body: blob
          });
          bridgeLog('uploadUrl PUT opt#' + hIdx + ' status: ' + uploadResp.status);
          if (uploadResp.ok) {
            uploadSuccess = true;
            break;
          } else {
            var errBody = await uploadResp.text();
            bridgeLog('PUT opt#' + hIdx + ' FAIL body: ' + errBody.substring(0, 200));
          }
        } catch(e) {
          bridgeLog('PUT opt#' + hIdx + ' EXCEPTION: ' + e.message);
        }
      }
      if (!uploadSuccess) {
        bridgeLog('ALL PUT strategies failed for file_id=' + fileId);
        return null;
      }
    }

    try {
      var completeResp = await originalFetch('/backend-api/files/' + fileId + '/uploaded', {
        method: 'POST',
        headers: Object.assign({}, baseHeaders(), { 'Content-Type': 'application/json' }),
        body: JSON.stringify({ file_id: fileId })
      });
      bridgeLog('/backend-api/files/uploaded status: ' + completeResp.status);
    } catch(e) {
      console.warn('[Bridge] Warning on file uploaded completion:', e);
    }

    return {
      file_id: fileId,
      file_name: name,
      file_size: size,
      mime_type: mime
    };
  }

    var uploadedFiles = [];
    if (Array.isArray(attachments) && attachments.length > 0) {
      bridgeLog('Starting upload of ' + attachments.length + ' attachment(s). Keys: ' + Object.keys(attachments[0] || {}).join(','));
      bridgeLog('authToken present: ' + !!authToken + ', b64 length: ' + (attachments[0].data_base64 || attachments[0].data || '').length);
      for (var a = 0; a < attachments.length; a++) {
        try {
          var uRes = await uploadAttachment(attachments[a]);
          bridgeLog('uploadAttachment result #' + a + ': ' + JSON.stringify(uRes));
          if (uRes) uploadedFiles.push(uRes);
        } catch(err) {
          bridgeLog('ERROR uploading #' + a + ': ' + err.message);
        }
      }
      bridgeLog('Upload done. uploadedFiles: ' + uploadedFiles.length);
    } else {
      bridgeLog('No attachments or empty. type=' + typeof attachments + ' isArray=' + Array.isArray(attachments));
    }

    var chatgptMessages = messages.map(function(m, idx) {
      var text = typeof m.content === 'string' ? m.content : JSON.stringify(m.content);
      var cleanText = text.replace(/\[Image attached:[^\]]+\]/g, '').replace(/!\[[^\]]*\]\(data:[^)]+\)/g, '').replace(/!\[[^\]]*\]\(data:[^)]*$/g, '').trim();
      if (idx === messages.length - 1 && uploadedFiles.length > 0) {
        var parts = [];
        var metadataAttachments = [];
        parts.push(cleanText || 'Please analyze the attached image.');
        uploadedFiles.forEach(function(f) {
          parts.push({
            asset_pointer: 'file-service://' + f.file_id,
            content_type: 'image_asset_pointer',
            size_bytes: f.file_size,
            width: 1000,
            height: 1000
          });
          metadataAttachments.push({
            id: f.file_id,
            size: f.file_size,
            name: f.file_name,
            mime_type: f.mime_type,
            width: 1000,
            height: 1000
          });
        });
        bridgeLog('multimodal_text parts=' + parts.length + ' first_type=' + typeof parts[0]);
        var msgObj = {
          id: crypto.randomUUID(),
          author: { role: m.role || 'user' },
          create_time: Date.now() / 1000,
          content: { content_type: 'multimodal_text', parts: parts },
          metadata: {},
        };
        if (metadataAttachments.length > 0) {
          msgObj.metadata.attachments = metadataAttachments;
        }
        return msgObj;
      }
      return {
        id: crypto.randomUUID(),
        author: { role: m.role || 'user' },
        create_time: Date.now() / 1000,
        content: { content_type: 'text', parts: [cleanText || text] },
        metadata: {},
      };
    });

    var headers = baseHeaders();
    headers['Accept'] = 'text/event-stream';
    if (sentinelToken) headers['Openai-Sentinel-Chat-Requirements-Token'] = sentinelToken;
    if (proofToken) headers['Openai-Sentinel-Proof-Token'] = proofToken;

    var targetModel = model || 'auto';
    if (targetModel.includes('5.6') || targetModel.includes('sol')) {
      targetModel = 'gpt-5.6-sol';
    }

    var body = {
      action: 'next',
      messages: chatgptMessages,
      parent_message_id: parentMessageId || crypto.randomUUID(),
      model: targetModel,
      timezone_offset_min: new Date().getTimezoneOffset(),
      timezone: Intl.DateTimeFormat().resolvedOptions().timeZone,
      conversation_mode: { kind: 'primary_assistant' },
      history_and_training_disabled: false,
      supports_buffering: true,
      supported_encodings: ['v1'],
      reasoning_effort: { kind: 'effort', effort: 'high' },
      client_contextual_info: {
        is_dark_mode: window.matchMedia('(prefers-color-scheme: dark)').matches,
        time_since_loaded: Math.floor(performance.now()),
        page_height: window.innerHeight,
        page_width: window.innerWidth,
        pixel_ratio: window.devicePixelRatio,
        screen_height: window.screen.height,
        screen_width: window.screen.width,
      },
    };

    if (uploadedFiles.length > 0) {
      body.attachments = uploadedFiles.map(function(f) {
        return {
          id: f.file_id,
          name: f.file_name,
          size: f.file_size,
          mime_type: f.mime_type,
          width: 1000,
          height: 1000
        };
      });
    }

    if (conversationId) {
      body.conversation_id = conversationId;
    }

    var resp = await originalFetch('/backend-api/conversation', {
      method: 'POST',
      headers: headers,
      body: JSON.stringify(body),
    });

    if (!resp.ok) {
      var errBody = '';
      try { errBody = await resp.text(); } catch(e) {}
      throw new Error('Conversation API error ' + resp.status + ': ' + errBody.slice(0, 500));
    }

    return resp;
  }

  // ═══════════════════════════════════════════════════════════════
  // SSE Parser — handles v1 delta encoding
  // ═══════════════════════════════════════════════════════════════
  async function parseSSEStream(response, onDelta, onDone, onError, onReader, onIds) {
    var reader = response.body.getReader();
    if (onReader) onReader(reader);
    var decoder = new TextDecoder();
    var buffer = '';
    var fullText = '';
    var conversationId = '';
    var messageId = '';
    var lastPath = '';
    var isAssistant = false;

    try {
      while (true) {
        var result = await reader.read();
        if (result.done) break;
        buffer += decoder.decode(result.value, { stream: true });

        var lines = buffer.split('\n');
        buffer = lines.pop() || '';

        for (var li = 0; li < lines.length; li++) {
          var line = lines[li].trim();
          if (!line || line.startsWith('event:')) continue;
          if (!line.startsWith('data: ')) continue;
          var data = line.slice(6).trim();
          if (data === '[DONE]') continue;

          try {
            var parsed = JSON.parse(data);
            if (!parsed || typeof parsed !== 'object') continue;

            if (parsed.error || parsed.detail) {
              var errMessage = parsed.error ? (parsed.error.message || (typeof parsed.error === 'string' ? parsed.error : JSON.stringify(parsed.error))) : (typeof parsed.detail === 'string' ? parsed.detail : (parsed.detail.message || JSON.stringify(parsed.detail)));
              console.error('[Bridge] ChatGPT returned error in stream:', errMessage);
              onError(errMessage || 'ChatGPT error event');
              return;
            }

            if (parsed.conversation_id) conversationId = parsed.conversation_id;
            if (parsed.v && typeof parsed.v === 'object') {
              if (parsed.v.conversation_id) conversationId = parsed.v.conversation_id;
              if (parsed.v.message && parsed.v.message.conversation_id) conversationId = parsed.v.message.conversation_id;
            }
            // Publish ids as soon as they are known so a cancel arriving
            // mid-stream can address the real message. Waiting for onDone
            // meant activeStreams.messageId was still null at cancel time and
            // the backend cancel call never fired.
            if (onIds && (conversationId || messageId)) onIds(conversationId, messageId);

            if (parsed.v && typeof parsed.v === 'object' && parsed.v.message) {
              var msg = parsed.v.message;
              var role = (msg.author || {}).role || '';
              isAssistant = role !== 'user';
              // Only an ASSISTANT message id may become the next turn's
              // parent_message_id. Recording the user echo (or a trailing
              // system/moderation message) here pointed the following turn at
              // the wrong node and forked the thread into a branch.
              if (isAssistant && msg.id) {
                messageId = msg.id;
                if (onIds) onIds(conversationId, messageId);
              }
              if (isAssistant) {
                var parts = ((msg.content || {}).parts) || [];
                if (parts.length) {
                  var p0 = parts[0];
                  var newText = typeof p0 === 'string' ? p0 : (p0 && typeof p0 === 'object' ? (p0.text || p0.val || '') : '');
                  if (typeof newText === 'string' && newText.length > fullText.length) {
                    var delta = newText.slice(fullText.length);
                    fullText = newText;
                    onDelta(delta);
                  }
                }
              }
            }

            if (parsed.o === 'append') {
              var path = parsed.p || lastPath;
              if (path) lastPath = path;
              if (path && (path.indexOf('/content/parts/') !== -1 || path === '/message/content/parts/0') && isAssistant) {
                var val = String(parsed.v || '');
                fullText += val;
                onDelta(val);
              }
            }

            if (parsed.v && typeof parsed.v === 'string' && !parsed.o && !parsed.p && !parsed.type) {
              if (lastPath && (lastPath.indexOf('/content/parts/') !== -1 || lastPath === '/message/content/parts/0') && isAssistant) {
                fullText += parsed.v;
                onDelta(parsed.v);
              }
            }

            if (parsed.o === 'patch' && Array.isArray(parsed.v)) {
              for (var pi = 0; pi < parsed.v.length; pi++) {
                var op = parsed.v[pi];
                if (op && op.o === 'append' && op.p && (op.p.indexOf('/content/parts/') !== -1 || op.p === '/message/content/parts/0')) {
                  var opVal = String(op.v || '');
                  fullText += opVal;
                  onDelta(opVal);
                }
              }
            }

            if (parsed.message && !parsed.v) {
              var legacyMsg = parsed.message;
              var legRole = (legacyMsg.author || {}).role || '';
              if (legRole !== 'user') {
                isAssistant = true;
                if (legacyMsg.id) messageId = legacyMsg.id;
                var legacyParts = ((legacyMsg.content || {}).parts) || [];
                if (legacyParts.length) {
                  var lp0 = legacyParts[0];
                  var lt = typeof lp0 === 'string' ? lp0 : (lp0 && typeof lp0 === 'object' ? (lp0.text || '') : '');
                  if (typeof lt === 'string' && lt.length > fullText.length) {
                    onDelta(lt.slice(fullText.length));
                    fullText = lt;
                  }
                }
              }
            }
          } catch(e) {}
        }
      }
    } catch(e) {
      onError(e.message);
      return;
    }

    onDone(fullText, conversationId, messageId);
  }

  // ═══════════════════════════════════════════════════════════════
  // Message handler — receives requests from content script
  // ═══════════════════════════════════════════════════════════════
  var activeStreams = new Map();

  async function cancelChatGPTStream(conversationId, messageId) {
    try {
      var headers = baseHeaders();
      headers['Content-Type'] = 'application/json';
      await originalFetch('/backend-api/conversation/cancel', {
        method: 'POST',
        headers: headers,
        body: JSON.stringify({
          conversation_id: conversationId,
          message_id: messageId
        })
      });
      console.log('[Bridge] Sent cancel signal to chatgpt.com backend API');
    } catch(e) {
      console.warn('[Bridge] Failed to cancel chatgpt backend stream:', e);
    }
  }

  window.addEventListener('message', async function(event) {
    if (event.source !== window) return;
    if (!event.data) return;

    if (event.data.type === 'CHATGPT_BRIDGE_CANCEL') {
      var cancelReqId = event.data.requestId;
      var active = activeStreams.get(cancelReqId);
      if (active) {
        console.log('[Bridge] Processing cancel request for:', cancelReqId);
        // Mark BEFORE cancelling the reader: reader.cancel() makes the next
        // read() resolve {done:true}, which walks parseSSEStream out of its
        // loop and into onDone — posting a bogus 'done' carrying whatever
        // partial text had arrived, as though the turn had finished normally.
        active.cancelled = true;
        if (active.reader) {
          try { active.reader.cancel(); } catch(e) {}
        }
        if (active.conversationId && active.messageId) {
          cancelChatGPTStream(active.conversationId, active.messageId);
        } else {
          console.warn('[Bridge] Cancelled before ChatGPT returned message ids — ' +
            'the backend turn may keep generating.');
        }
        activeStreams.delete(cancelReqId);
      }
      return;
    }

    if (event.data.type !== 'CHATGPT_BRIDGE_REQUEST') return;

    var requestId = event.data.requestId;
    bridgeLog('Received request requestId=' + requestId + ' atts=' + (Array.isArray(event.data.attachments) ? event.data.attachments.length : 'none'));
    var messages = event.data.messages;
    var model = event.data.model;
    var conversationId = event.data.conversationId;
    var parentMessageId = event.data.parentMessageId;

    var streamState = {
      reader: null,
      conversationId: conversationId || null,
      messageId: null,
      cancelled: false,
    };

    try {
      var response = await sendConversation(messages, model, requestId, conversationId, parentMessageId, event.data.attachments);
      activeStreams.set(requestId, streamState);

      await parseSSEStream(response,
        function onDelta(delta) {
          if (streamState.cancelled) return;
          window.postMessage({
            type: 'CHATGPT_BRIDGE_RESPONSE',
            requestId: requestId,
            event: 'delta',
            delta: delta,
          }, '*');
        },
        function onDone(fullText, convId, msgId) {
          activeStreams.delete(requestId);
          if (streamState.cancelled) return;
          window.postMessage({
            type: 'CHATGPT_BRIDGE_RESPONSE',
            requestId: requestId,
            event: 'done',
            fullText: fullText,
            conversationId: convId,
            messageId: msgId,
          }, '*');
        },
        function onError(err) {
          activeStreams.delete(requestId);
          if (streamState.cancelled) return;
          window.postMessage({
            type: 'CHATGPT_BRIDGE_RESPONSE',
            requestId: requestId,
            event: 'error',
            error: err,
          }, '*');
        },
        function onReader(r) {
          streamState.reader = r;
        },
        function onIds(convId, msgId) {
          if (convId) streamState.conversationId = convId;
          if (msgId) streamState.messageId = msgId;
        }
      );
    } catch(e) {
      activeStreams.delete(requestId);
      if (streamState.cancelled) return;
      window.postMessage({
        type: 'CHATGPT_BRIDGE_RESPONSE',
        requestId: requestId,
        event: 'error',
        error: e && e.message ? e.message : String(e),
      }, '*');
    }
  });

  // Signal readiness
  refreshPageInfo();
  ensureAuth().then(function() {
    window.postMessage({ type: 'CHATGPT_BRIDGE_READY' }, '*');
    console.log('[ChatGPT Bridge] Injected script ready. Auth:', !!authToken, 'DPL:', cachedDpl.slice(0, 20));
  });
})();
