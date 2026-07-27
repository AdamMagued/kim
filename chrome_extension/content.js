(function() {
  'use strict';

  var WS_URL = 'ws://127.0.0.1:10533';
  var ws = null;
  var reconnectTimer = null;
  var bridgeReady = false;
  var messageBuffer = [];

  function injectScript() {
    var s = document.createElement('script');
    s.src = chrome.runtime.getURL('injected.js');
    s.onload = function() { s.remove(); };
    (document.head || document.documentElement).appendChild(s);
  }

  // While the proxy is down every outgoing frame was queued forever. A long
  // streaming turn produces thousands of deltas, so a disconnect during one
  // grew this array without bound — and replaying stale deltas after
  // reconnect is useless anyway, because the proxy-side request they belonged
  // to is already gone. Cap the queue and drop deltas first: 'done'/'error'
  // are the frames a reconnected proxy could still act on.
  var MAX_BUFFERED = 200;

  function bufferMessage(msg, isDelta) {
    if (messageBuffer.length >= MAX_BUFFERED) {
      if (isDelta) return;
      var dropIdx = messageBuffer.findIndex(function(m) { return m.isDelta; });
      if (dropIdx === -1) dropIdx = 0;
      messageBuffer.splice(dropIdx, 1);
    }
    messageBuffer.push({ msg: msg, isDelta: !!isDelta });
  }

  function sendToProxy(data) {
    var msg = JSON.stringify(data);
    if (ws && ws.readyState === WebSocket.OPEN) {
      try {
        ws.send(msg);
      } catch(e) {
        bufferMessage(msg, data && data.event === 'delta');
      }
    } else {
      bufferMessage(msg, data && data.event === 'delta');
    }
  }

  function flushBuffer() {
    if (!ws || ws.readyState !== WebSocket.OPEN) return;
    while (messageBuffer.length > 0) {
      var entry = messageBuffer.shift();
      try { ws.send(entry.msg); } catch(e) {
        messageBuffer.unshift(entry);
        break;
      }
    }
  }

  function connectWS() {
    if (ws && (ws.readyState === WebSocket.CONNECTING || ws.readyState === WebSocket.OPEN)) return;

    try {
      ws = new WebSocket(WS_URL);
    } catch(e) {
      scheduleReconnect();
      return;
    }

    ws.onopen = function() {
      console.log('[Bridge Content] Connected to proxy');
      clearTimeout(reconnectTimer);
      injectScript();
      ws.send(JSON.stringify({ type: 'extension_connected', ready: bridgeReady }));
      flushBuffer();
    };

    ws.onmessage = function(event) {
      try {
        var msg = JSON.parse(event.data);
        if (msg.type === 'request') {
          window.postMessage({
            type: 'CHATGPT_BRIDGE_REQUEST',
            requestId: msg.requestId,
            messages: msg.messages,
            attachments: msg.attachments,
            model: msg.model,
            conversationId: msg.conversationId,
            parentMessageId: msg.parentMessageId,
          }, '*');
        } else if (msg.type === 'cancel') {
          window.postMessage({
            type: 'CHATGPT_BRIDGE_CANCEL',
            requestId: msg.requestId,
          }, '*');
        }
      } catch(e) {
        console.error('[Bridge Content] Bad message from proxy:', e);
      }
    };

    ws.onclose = function() {
      ws = null;
      scheduleReconnect();
    };

    ws.onerror = function() {
      // Close explicitly before dropping the reference. Clearing `ws` alone
      // left a live socket that later fired its own onclose and queued a
      // SECOND reconnect, so each error halved the backoff.
      var dead = ws;
      ws = null;
      if (dead) {
        dead.onclose = null;
        try { dead.close(); } catch(e) {}
      }
      scheduleReconnect();
    };
  }

  function scheduleReconnect() {
    clearTimeout(reconnectTimer);
    reconnectTimer = setTimeout(connectWS, 1000);
  }

  window.addEventListener('message', function(event) {
    if (event.source !== window) return;
    if (!event.data) return;

    if (event.data.type === 'CHATGPT_BRIDGE_READY') {
      bridgeReady = true;
      console.log('[Bridge Content] Injected script ready');
      sendToProxy({ type: 'bridge_ready' });
      return;
    }

    if (event.data.type === 'CHATGPT_BRIDGE_RESPONSE') {
      sendToProxy({
        type: 'response',
        requestId: event.data.requestId,
        event: event.data.event,
        delta: event.data.delta,
        fullText: event.data.fullText,
        conversationId: event.data.conversationId,
        messageId: event.data.messageId,
        error: event.data.error,
      });
    }
  });

  injectScript();
  connectWS();

  setInterval(function() {
    if (!ws || ws.readyState !== WebSocket.OPEN) {
      connectWS();
    } else {
      ws.send(JSON.stringify({ type: 'ping' }));
      flushBuffer();
    }
  }, 10000);
})();
