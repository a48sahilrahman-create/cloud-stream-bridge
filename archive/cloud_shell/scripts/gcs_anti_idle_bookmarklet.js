/**
 * ==============================================================================
 * CloudStream Bridge - Google Cloud Shell (GCS) Anti-Idle Keep-Alive Bookmarklet
 * ==============================================================================
 *
 * ARCHITECTURAL CONTEXT & PROBLEM STATEMENT:
 * 1. Google Cloud Shell idle timeouts: GCS automatically disconnects bash terminal
 *    sessions after 20 minutes of user inactivity, even while a long-running proxy
 *    daemon (like cloud-stream-bridge) is actively serving media traffic.
 * 2. Modern Chromium Background Throttling: Browsers (Chrome, Edge, Brave) aggressively
 *    clamp setTimeout/setInterval timers to 1 tick per minute (or suspend execution entirely)
 *    when tabs are backgrounded or minimized, or when Energy Saver is active.
 *
 * HOW THIS BOOKMARKLET SOLVES BOTH:
 * 1. Web Audio API Keep-Alive: Spawns an inaudible OscillatorNode (gain = 0.0001) connected
 *    to AudioDestination. Chromium tab audio threads run with high priority and are
 *    exempt from background tab throttling, ensuring timers fire deterministically on time.
 * 2. Synthetic xterm.js Pulse Engine: Fires every 42 seconds. Scans the DOM and child
 *    iframes (devshell) for xterm.js helper textarea (.xterm-helper-textarea), focuses it,
 *    and simulates a discrete [Space] followed 100ms later by [Backspace] keyboard event sequence.
 *    This registers valid user keystroke activity with GCS watchdogs while leaving the prompt clean.
 * 3. Idempotent Execution: Verifies `window.__CLOUDSTREAM_ANTI_IDLE_ACTIVE__` before running.
 *    Prevents redundant timer loops and resource contention if clicked multiple times.
 * 4. High-Contrast Floating OSD Badge: Injects a sleek dark-glass pill into the viewport
 *    (bottom: 16px, right: 16px, z-index: 999999) displaying pulse count, uptime counter,
 *    and a 1-click [✕ Stop] button for full teardown and cleanup.
 *
 * ------------------------------------------------------------------------------
 * READY-TO-COPY BOOKMARKLET URI:
 * ------------------------------------------------------------------------------
 * Copy the single line below and paste it as the URL of a new browser bookmark:
 *
 * javascript:(function(){if(window.__CLOUDSTREAM_ANTI_IDLE_ACTIVE__){alert('[CloudStream Anti-Idle] Already active!');return;}window.__CLOUDSTREAM_ANTI_IDLE_ACTIVE__=true;var cfg={pulseMs:42000,uiMs:1000,gain:0.0001,id:'cs-anti-idle-badge'};var start=Date.now(),pulses=0,pTimer=null,uTimer=null,actx=null,osc=null,gain=null;try{var AC=window.AudioContext||window.webkitAudioContext;if(AC){actx=new AC();if(actx.state==='suspended')actx.resume();osc=actx.createOscillator();gain=actx.createGain();gain.gain.setValueAtTime(cfg.gain,actx.currentTime);osc.connect(gain);gain.connect(actx.destination);osc.start();}}catch(e){console.warn('AudioContext failed:',e);}function findTerm(){var sel=['.xterm-helper-textarea','textarea.xterm-helper-textarea','.terminal textarea','textarea'];for(var i=0;i<sel.length;i++){var el=document.querySelector(sel[i]);if(el)return el;}var ifrs=document.querySelectorAll('iframe');for(var j=0;j<ifrs.length;j++){try{var doc=ifrs[j].contentDocument||ifrs[j].contentWindow.document;if(doc){for(var k=0;k<sel.length;k++){var fe=doc.querySelector(sel[k]);if(fe)return fe;}}}catch(_){}}return null;}function sendKey(t,k,c,kc){var opt={key:k,code:c,keyCode:kc,which:kc,bubbles:true,cancelable:true,composed:true};t.dispatchEvent(new KeyboardEvent('keydown',opt));t.dispatchEvent(new KeyboardEvent('keypress',opt));t.dispatchEvent(new KeyboardEvent('keyup',opt));}function pulse(){var t=findTerm()||document.activeElement||document.body;if(t&&typeof t.focus==='function'){try{t.focus();sendKey(t,' ','Space',32);setTimeout(function(){try{sendKey(t,'Backspace','Backspace',8);}catch(_){}},100);}catch(err){}}pulses++;updateOSD();}var badge=document.createElement('div');badge.id=cfg.id;badge.style.cssText='position:fixed;bottom:16px;right:16px;z-index:999999;display:flex;align-items:center;gap:10px;padding:8px 14px;background:rgba(17,24,39,0.92);backdrop-filter:blur(8px);-webkit-backdrop-filter:blur(8px);color:#f3f4f6;font-family:-apple-system,BlinkMacSystemFont,Segoe UI,Roboto,monospace,sans-serif;font-size:12px;font-weight:500;border:1px solid rgba(59,130,246,0.4);border-radius:9999px;box-shadow:0 4px 14px rgba(0,0,0,0.4);user-select:none;';var stText=document.createElement('span');stText.id=cfg.id+'-st';stText.textContent='🟢 CloudStream Anti-Idle Active | Pulses: 0 | Uptime: 0m';var stopBtn=document.createElement('button');stopBtn.textContent='✕ Stop';stopBtn.style.cssText='background:#dc2626;color:#fff;border:none;padding:3px 8px;border-radius:9999px;font-size:11px;font-weight:600;cursor:pointer;';stopBtn.onclick=stop;badge.appendChild(stText);badge.appendChild(stopBtn);document.body.appendChild(badge);function updateOSD(){var el=document.getElementById(cfg.id+'-st');if(el){var m=Math.floor((Date.now()-start)/60000);el.textContent='🟢 CloudStream Anti-Idle Active | Pulses: '+pulses+' | Uptime: '+m+'m';}}function stop(){clearInterval(pTimer);clearInterval(uTimer);if(osc){try{osc.stop();osc.disconnect();}catch(_){}}if(gain){try{gain.disconnect();}catch(_){}}if(actx&&actx.state!=='closed'){try{actx.close();}catch(_){}}var b=document.getElementById(cfg.id);if(b&&b.parentNode)b.parentNode.removeChild(b);delete window.__CLOUDSTREAM_ANTI_IDLE_ACTIVE__;console.log('[CloudStream Anti-Idle] Stopped.');}pulse();pTimer=setInterval(pulse,cfg.pulseMs);uTimer=setInterval(updateOSD,cfg.uiMs);})();
 * ==============================================================================
 */

(function initCloudStreamAntiIdle() {
  'use strict';

  // ----------------------------------------------------------------------------
  // 1. IDEMPOTENT EXECUTION GUARD
  // ----------------------------------------------------------------------------
  if (window.__CLOUDSTREAM_ANTI_IDLE_ACTIVE__) {
    const warnMsg = '[CloudStream Anti-Idle] Script is already running in this tab.';
    console.warn(warnMsg);
    if (typeof alert === 'function') {
      alert(warnMsg);
    }
    return;
  }
  window.__CLOUDSTREAM_ANTI_IDLE_ACTIVE__ = true;

  // ----------------------------------------------------------------------------
  // CONFIGURATION CONSTANTS
  // ----------------------------------------------------------------------------
  const CONFIG = {
    PULSE_INTERVAL_MS: 42000, // 42 seconds (sub-60s keeps GCS and SSH alive)
    UI_TICK_MS: 1000,         // 1000ms UI clock refresh
    GAIN_VOLUME: 0.0001,      // Inaudible audio level to avoid background throttling
    KEY_RESTORE_DELAY_MS: 100,// 100ms pause between Space and Backspace
    OSD_ID: 'cloudstream-anti-idle-badge'
  };

  const startTime = Date.now();
  let pulseCount = 0;
  let pulseInterval = null;
  let uiInterval = null;
  let audioCtx = null;
  let oscillator = null;
  let gainNode = null;

  // ----------------------------------------------------------------------------
  // 2. SILENT WEB AUDIO KEEP-ALIVE
  // ----------------------------------------------------------------------------
  // Browsers throttle background tabs to save battery/CPU. Playing active audio
  // flags the tab as media-active, maintaining high timer resolution and
  // preventing Chromium from freezing setInterval loops when tab is in background.
  try {
    const AudioContextClass = window.AudioContext || window.webkitAudioContext;
    if (AudioContextClass) {
      audioCtx = new AudioContextClass();
      if (audioCtx.state === 'suspended') {
        audioCtx.resume();
      }
      oscillator = audioCtx.createOscillator();
      gainNode = audioCtx.createGain();
      gainNode.gain.setValueAtTime(CONFIG.GAIN_VOLUME, audioCtx.currentTime);
      oscillator.connect(gainNode);
      gainNode.connect(audioCtx.destination);
      oscillator.start();
      console.log('[CloudStream Anti-Idle] Web Audio keep-alive active (background throttling disabled).');
    }
  } catch (audioErr) {
    console.warn('[CloudStream Anti-Idle] Web Audio init failed (proceeding with fallback timers):', audioErr);
  }

  // ----------------------------------------------------------------------------
  // 3. DOM / IFRAME TERMINAL LOCATOR
  // ----------------------------------------------------------------------------
  // Google Cloud Shell embeds terminal sessions either directly in the top frame
  // or inside an iframe (devshell). We recursively search both scopes.
  function findTerminalTextarea() {
    const candidateSelectors = [
      '.xterm-helper-textarea',
      'textarea.xterm-helper-textarea',
      '.terminal textarea',
      '#terminal textarea',
      'div[role="region"] textarea',
      'textarea'
    ];

    // Priority 1: Check main document
    for (const selector of candidateSelectors) {
      const el = document.querySelector(selector);
      if (el) return el;
    }

    // Priority 2: Check accessible iframes
    const iframes = document.querySelectorAll('iframe');
    for (let i = 0; i < iframes.length; i++) {
      try {
        const frameDoc = iframes[i].contentDocument || (iframes[i].contentWindow && iframes[i].contentWindow.document);
        if (frameDoc) {
          for (const selector of candidateSelectors) {
            const el = frameDoc.querySelector(selector);
            if (el) return el;
          }
        }
      } catch (crossOriginErr) {
        // Cross-origin iframe boundaries reject access; ignore and continue
      }
    }

    return null;
  }

  // ----------------------------------------------------------------------------
  // 4. SYNTHETIC KEYBOARD DISPATCHER
  // ----------------------------------------------------------------------------
  function dispatchKeyEvents(target, key, code, keyCode) {
    const eventParams = {
      key: key,
      code: code,
      keyCode: keyCode,
      which: keyCode,
      charCode: keyCode === 32 ? 32 : 0,
      bubbles: true,
      cancelable: true,
      composed: true
    };

    target.dispatchEvent(new KeyboardEvent('keydown', eventParams));
    target.dispatchEvent(new KeyboardEvent('keypress', eventParams));
    target.dispatchEvent(new KeyboardEvent('keyup', eventParams));
  }

  // ----------------------------------------------------------------------------
  // 5. SYNTHETIC XTERM.JS PULSE DISPATCHER
  // ----------------------------------------------------------------------------
  function dispatchTerminalPulse() {
    const target = findTerminalTextarea() || document.activeElement || document.body;

    if (target && typeof target.focus === 'function') {
      try {
        target.focus();

        // 1. Dispatch ' ' (Space, keyCode 32)
        dispatchKeyEvents(target, ' ', 'Space', 32);

        // 2. Dispatch 'Backspace' after 100ms to keep shell prompt clean
        setTimeout(() => {
          try {
            dispatchKeyEvents(target, 'Backspace', 'Backspace', 8);
          } catch (bkErr) {
            console.error('[CloudStream Anti-Idle] Backspace dispatch error:', bkErr);
          }
        }, CONFIG.KEY_RESTORE_DELAY_MS);
      } catch (err) {
        console.error('[CloudStream Anti-Idle] Key dispatch failure:', err);
      }
    }

    pulseCount++;
    updateOSD();
    console.log(`[CloudStream Anti-Idle] Synthetic pulse #${pulseCount} dispatched at ${new Date().toLocaleTimeString()}`);
  }

  // ----------------------------------------------------------------------------
  // 6. ON-SCREEN DISPLAY (OSD) FLOATING STATUS BADGE
  // ----------------------------------------------------------------------------
  const badgeContainer = document.createElement('div');
  badgeContainer.id = CONFIG.OSD_ID;
  badgeContainer.setAttribute('style', [
    'position: fixed',
    'bottom: 16px',
    'right: 16px',
    'z-index: 999999',
    'display: flex',
    'align-items: center',
    'gap: 10px',
    'padding: 8px 14px',
    'background: rgba(17, 24, 39, 0.92)',
    'backdrop-filter: blur(8px)',
    '-webkit-backdrop-filter: blur(8px)',
    'color: #f3f4f6',
    'font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, monospace, sans-serif',
    'font-size: 12px',
    'font-weight: 500',
    'border: 1px solid rgba(59, 130, 246, 0.4)',
    'border-radius: 9999px',
    'box-shadow: 0 4px 14px rgba(0, 0, 0, 0.4)',
    'user-select: none',
    'transition: all 0.2s ease-in-out'
  ].join('; ') + ';');

  const statusSpan = document.createElement('span');
  statusSpan.id = `${CONFIG.OSD_ID}-status`;
  statusSpan.textContent = '🟢 CloudStream Anti-Idle Active | Pulses: 0 | Uptime: 0m';

  const stopButton = document.createElement('button');
  stopButton.id = `${CONFIG.OSD_ID}-stop`;
  stopButton.textContent = '✕ Stop';
  stopButton.setAttribute('style', [
    'background: #dc2626',
    'color: #ffffff',
    'border: none',
    'outline: none',
    'padding: 3px 8px',
    'border-radius: 9999px',
    'font-size: 11px',
    'font-weight: 600',
    'cursor: pointer',
    'transition: background 0.15s ease'
  ].join('; ') + ';');

  stopButton.onmouseover = () => { stopButton.style.background = '#b91c1c'; };
  stopButton.onmouseout = () => { stopButton.style.background = '#dc2626'; };
  stopButton.onclick = stopAntiIdle;

  badgeContainer.appendChild(statusSpan);
  badgeContainer.appendChild(stopButton);
  document.body.appendChild(badgeContainer);

  function updateOSD() {
    const el = document.getElementById(`${CONFIG.OSD_ID}-status`);
    if (el) {
      const minutes = Math.floor((Date.now() - startTime) / 60000);
      el.textContent = `🟢 CloudStream Anti-Idle Active | Pulses: ${pulseCount} | Uptime: ${minutes}m`;
    }
  }

  // ----------------------------------------------------------------------------
  // 7. TEARDOWN & CLEANUP
  // ----------------------------------------------------------------------------
  function stopAntiIdle() {
    clearInterval(pulseInterval);
    clearInterval(uiInterval);

    if (oscillator) {
      try {
        oscillator.stop();
        oscillator.disconnect();
      } catch (_) {}
    }
    if (gainNode) {
      try {
        gainNode.disconnect();
      } catch (_) {}
    }
    if (audioCtx && audioCtx.state !== 'closed') {
      try {
        audioCtx.close();
      } catch (_) {}
    }

    const domBadge = document.getElementById(CONFIG.OSD_ID);
    if (domBadge && domBadge.parentNode) {
      domBadge.parentNode.removeChild(domBadge);
    }

    delete window.__CLOUDSTREAM_ANTI_IDLE_ACTIVE__;
    console.log('[CloudStream Anti-Idle] Terminated. Resources released.');
  }

  // ----------------------------------------------------------------------------
  // 8. ACTIVATION TRIGGER
  // ----------------------------------------------------------------------------
  dispatchTerminalPulse(); // Trigger pulse immediately upon activation
  pulseInterval = setInterval(dispatchTerminalPulse, CONFIG.PULSE_INTERVAL_MS);
  uiInterval = setInterval(updateOSD, CONFIG.UI_TICK_MS);

  console.log('[CloudStream Anti-Idle] Bookmarklet successfully active.');
})();
