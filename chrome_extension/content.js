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

  function sendToProxy(data) {
    var msg = JSON.stringify(data);
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(msg);
    } else {
      messageBuffer.push(msg);
    }
  }

  function flushBuffer() {
    if (!ws || ws.readyState !== WebSocket.OPEN) return;
    while (messageBuffer.length > 0) {
      var msg = messageBuffer.shift();
      try { ws.send(msg); } catch(e) {
        messageBuffer.unshift(msg);
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
      ws = null;
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
