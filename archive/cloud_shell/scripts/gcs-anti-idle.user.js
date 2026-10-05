// ==UserScript==
// @name         CloudStream Google Cloud Shell 12-Hour Anti-Idle Engine
// @namespace    https://cloud-stream-bridge.onrender.com/
// @version      1.0.0
// @description  Prevents Google Cloud Shell 20-minute inactivity timeout during 12-hour 4K streaming sessions.
// @author       CloudStream
// @match        https://shell.cloud.google.com/*
// @match        https://*.cloud.google.com/cloudshell*
// @icon         https://www.gstatic.com/images/branding/product/2x/cloud_shell_96dp.png
// @grant        none
// @run-at       document-idle
// ==/UserScript==

(function () {
    'use strict';

    // Engine Configuration
    const CONFIG = {
        PULSE_INTERVAL_MS: 42000, // 42s synthetic heartbeat
        TICK_INTERVAL_MS: 1000,   // 1s ticker for UI and countdown
        OSD_ID: 'cloudstream-anti-idle-osd',
        STYLE_ID: 'cloudstream-anti-idle-styles'
    };

    // State Container
    const state = {
        active: false,
        startTime: Date.now(),
        pulses: 0,
        lastPulseTime: null,
        nextPulseCountdown: 42,
        audioActive: false,
        isMinimized: false,
        targetsFound: 0
    };

    let pulseTimer = null;
    let tickerTimer = null;
    let audioContext = null;
    let silentAudioElement = null;

    /**
     * Injects sleek dark-mode glassmorphism stylesheet.
     */
    function injectStyles() {
        if (document.getElementById(CONFIG.STYLE_ID)) return;

        const style = document.createElement('style');
        style.id = CONFIG.STYLE_ID;
        style.textContent = `
            #${CONFIG.OSD_ID} {
                position: fixed;
                bottom: 20px;
                right: 20px;
                z-index: 9999999;
                font-family: 'JetBrains Mono', 'Fira Code', 'Roboto Mono', Menlo, Monaco, Consolas, monospace;
                font-size: 11px;
                color: #e2e8f0;
                background: rgba(11, 15, 25, 0.92);
                backdrop-filter: blur(12px);
                -webkit-backdrop-filter: blur(12px);
                border: 1px solid rgba(56, 189, 248, 0.28);
                border-radius: 12px;
                box-shadow: 0 10px 30px rgba(0, 0, 0, 0.55), 0 0 15px rgba(56, 189, 248, 0.15);
                user-select: none;
                transition: all 0.25s cubic-bezier(0.16, 1, 0.3, 1);
                overflow: hidden;
            }

            #${CONFIG.OSD_ID}.cs-minimized {
                border-radius: 20px;
                padding: 6px 14px;
                cursor: pointer;
            }

            #${CONFIG.OSD_ID} .cs-container {
                padding: 12px 16px;
                display: flex;
                flex-direction: column;
                gap: 8px;
                min-width: 250px;
            }

            #${CONFIG.OSD_ID} .cs-header {
                display: flex;
                align-items: center;
                justify-content: space-between;
                border-bottom: 1px solid rgba(255, 255, 255, 0.08);
                padding-bottom: 8px;
            }

            #${CONFIG.OSD_ID} .cs-brand {
                display: flex;
                align-items: center;
                gap: 8px;
                font-weight: 700;
                letter-spacing: 0.5px;
                color: #f8fafc;
            }

            #${CONFIG.OSD_ID} .cs-dot {
                width: 8px;
                height: 8px;
                border-radius: 50%;
                background: #10b981;
                box-shadow: 0 0 8px #10b981;
                transition: transform 0.2s ease, box-shadow 0.2s ease;
            }

            #${CONFIG.OSD_ID} .cs-dot.cs-pulsing {
                transform: scale(1.6);
                background: #38bdf8;
                box-shadow: 0 0 14px #38bdf8;
            }

            #${CONFIG.OSD_ID} .cs-badge {
                font-size: 9px;
                font-weight: 800;
                padding: 2px 6px;
                border-radius: 4px;
                background: rgba(56, 189, 248, 0.15);
                color: #38bdf8;
                border: 1px solid rgba(56, 189, 248, 0.3);
                text-transform: uppercase;
            }

            #${CONFIG.OSD_ID} .cs-controls {
                display: flex;
                align-items: center;
                gap: 6px;
            }

            #${CONFIG.OSD_ID} .cs-btn-min {
                background: transparent;
                border: none;
                color: #94a3b8;
                cursor: pointer;
                padding: 2px 6px;
                font-size: 13px;
                line-height: 1;
                border-radius: 4px;
                transition: background 0.15s ease, color 0.15s ease;
            }

            #${CONFIG.OSD_ID} .cs-btn-min:hover {
                background: rgba(255, 255, 255, 0.1);
                color: #f8fafc;
            }

            #${CONFIG.OSD_ID} .cs-grid {
                display: grid;
                grid-template-columns: 1fr 1fr;
                gap: 6px;
            }

            #${CONFIG.OSD_ID} .cs-cell {
                background: rgba(255, 255, 255, 0.03);
                border: 1px solid rgba(255, 255, 255, 0.05);
                border-radius: 6px;
                padding: 6px 8px;
                display: flex;
                flex-direction: column;
                gap: 2px;
            }

            #${CONFIG.OSD_ID} .cs-label {
                font-size: 9px;
                color: #64748b;
                text-transform: uppercase;
                letter-spacing: 0.5px;
            }

            #${CONFIG.OSD_ID} .cs-value {
                font-weight: 600;
                color: #f1f5f9;
            }

            #${CONFIG.OSD_ID} .cs-footer {
                display: flex;
                align-items: center;
                justify-content: space-between;
                font-size: 9px;
                color: #64748b;
                border-top: 1px solid rgba(255, 255, 255, 0.06);
                padding-top: 6px;
                margin-top: 2px;
            }

            #${CONFIG.OSD_ID} .cs-audio-status {
                display: flex;
                align-items: center;
                gap: 4px;
            }

            #${CONFIG.OSD_ID} .cs-audio-dot {
                width: 6px;
                height: 6px;
                border-radius: 50%;
                background: #10b981;
            }

            #${CONFIG.OSD_ID} .cs-audio-dot.cs-inactive {
                background: #f59e0b;
            }

            #${CONFIG.OSD_ID} .cs-min-content {
                display: flex;
                align-items: center;
                gap: 8px;
                font-weight: 600;
            }
        `;
        document.head.appendChild(style);
    }

    /**
     * Builds and attaches the floating OSD element.
     */
    function createOSD() {
        if (document.getElementById(CONFIG.OSD_ID)) return;

        injectStyles();

        const osd = document.createElement('div');
        osd.id = CONFIG.OSD_ID;
        osd.innerHTML = `
            <div class="cs-container" id="cs-full-view">
                <div class="cs-header">
                    <div class="cs-brand">
                        <span class="cs-dot" id="cs-pulse-dot"></span>
                        <span>CloudStream</span>
                        <span class="cs-badge">12H SHIELD</span>
                    </div>
                    <div class="cs-controls">
                        <button class="cs-btn-min" id="cs-btn-toggle" title="Minimize / Expand">_</button>
                    </div>
                </div>
                <div class="cs-grid">
                    <div class="cs-cell">
                        <span class="cs-label">Heartbeat</span>
                        <span class="cs-value" id="cs-val-pulses">0 pulses</span>
                    </div>
                    <div class="cs-cell">
                        <span class="cs-label">Next Pulse</span>
                        <span class="cs-value" id="cs-val-next">42s</span>
                    </div>
                    <div class="cs-cell">
                        <span class="cs-label">Uptime</span>
                        <span class="cs-value" id="cs-val-uptime">00:00:00</span>
                    </div>
                    <div class="cs-cell">
                        <span class="cs-label">Targets</span>
                        <span class="cs-value" id="cs-val-targets">Active</span>
                    </div>
                </div>
                <div class="cs-footer">
                    <div class="cs-audio-status">
                        <span class="cs-audio-dot" id="cs-audio-indicator"></span>
                        <span id="cs-audio-text">Audio Keep-Alive</span>
                    </div>
                    <span id="cs-last-time">--:--:--</span>
                </div>
            </div>
            <div class="cs-min-content" id="cs-min-view" style="display: none;">
                <span class="cs-dot" id="cs-min-pulse-dot"></span>
                <span id="cs-min-text">12H Shield Active (0)</span>
            </div>
        `;

        document.body.appendChild(osd);

        // Event listener for minimization toggle
        const toggleBtn = document.getElementById('cs-btn-toggle');
        if (toggleBtn) {
            toggleBtn.addEventListener('click', (e) => {
                e.stopPropagation();
                toggleMinimize();
            });
        }

        osd.addEventListener('click', () => {
            if (state.isMinimized) {
                toggleMinimize();
            }
        });
    }

    /**
     * Toggles OSD between full and minimized pill mode.
     */
    function toggleMinimize() {
        state.isMinimized = !state.isMinimized;
        const osd = document.getElementById(CONFIG.OSD_ID);
        const fullView = document.getElementById('cs-full-view');
        const minView = document.getElementById('cs-min-view');

        if (!osd || !fullView || !minView) return;

        if (state.isMinimized) {
            osd.classList.add('cs-minimized');
            fullView.style.display = 'none';
            minView.style.display = 'flex';
        } else {
            osd.classList.remove('cs-minimized');
            fullView.style.display = 'flex';
            minView.style.display = 'none';
        }
        updateOSD();
    }

    /**
     * Formats seconds into HH:MM:SS.
     */
    function formatTime(totalSeconds) {
        const sec = Math.max(0, Math.floor(totalSeconds));
        const hours = Math.floor(sec / 3600).toString().padStart(2, '0');
        const minutes = Math.floor((sec % 3600) / 60).toString().padStart(2, '0');
        const seconds = Math.floor(sec % 60).toString().padStart(2, '0');
        return `${hours}:${minutes}:${seconds}`;
    }

    /**
     * Updates the text and telemetry values on the OSD badge.
     */
    function updateOSD() {
        const pulsesEl = document.getElementById('cs-val-pulses');
        const nextEl = document.getElementById('cs-val-next');
        const uptimeEl = document.getElementById('cs-val-uptime');
        const targetsEl = document.getElementById('cs-val-targets');
        const audioDot = document.getElementById('cs-audio-indicator');
        const audioText = document.getElementById('cs-audio-text');
        const lastTimeEl = document.getElementById('cs-last-time');
        const minText = document.getElementById('cs-min-text');

        const elapsedSec = (Date.now() - state.startTime) / 1000;
        const uptimeStr = formatTime(elapsedSec);

        if (pulsesEl) pulsesEl.textContent = `${state.pulses} pulses`;
        if (nextEl) nextEl.textContent = `${state.nextPulseCountdown}s`;
        if (uptimeEl) uptimeEl.textContent = uptimeStr;
        if (targetsEl) targetsEl.textContent = `${state.targetsFound} hooked`;

        if (audioDot && audioText) {
            if (state.audioActive) {
                audioDot.classList.remove('cs-inactive');
                audioText.textContent = 'Audio Active';
            } else {
                audioDot.classList.add('cs-inactive');
                audioText.textContent = 'Audio Standby';
            }
        }

        if (lastTimeEl && state.lastPulseTime) {
            const h = state.lastPulseTime.getHours().toString().padStart(2, '0');
            const m = state.lastPulseTime.getMinutes().toString().padStart(2, '0');
            const s = state.lastPulseTime.getSeconds().toString().padStart(2, '0');
            lastTimeEl.textContent = `Last: ${h}:${m}:${s}`;
        }

        if (minText) {
            minText.textContent = `Shield Active (${state.pulses}p | ${uptimeStr})`;
        }
    }

    /**
     * Triggers a visual flash on the pulse indicator dot.
     */
    function triggerOSDPulseAnimation() {
        const dot = document.getElementById('cs-pulse-dot');
        const minDot = document.getElementById('cs-min-pulse-dot');

        [dot, minDot].forEach(d => {
            if (d) {
                d.classList.add('cs-pulsing');
                setTimeout(() => d.classList.remove('cs-pulsing'), 600);
            }
        });
    }

    /**
     * AudioContext Keep-Alive Engine:
     * Plays a continuously running silent buffer through an AudioContext and creates an HTML5 Audio fallback.
     * Prevents Chrome/Edge/Brave background tab freezing and discarding.
     */
    function setupAudioKeepAlive() {
        try {
            const AudioContextClass = window.AudioContext || window.webkitAudioContext;
            if (AudioContextClass) {
                audioContext = new AudioContextClass();

                // Create a silent 2-second audio buffer
                const buffer = audioContext.createBuffer(1, audioContext.sampleRate * 2, audioContext.sampleRate);
                const source = audioContext.createBufferSource();
                source.buffer = buffer;
                source.loop = true;

                // Virtually silent gain node
                const gain = audioContext.createGain();
                gain.gain.value = 0.0001;

                source.connect(gain);
                gain.connect(audioContext.destination);
                source.start(0);

                if (audioContext.state === 'running') {
                    state.audioActive = true;
                } else if (audioContext.state === 'suspended') {
                    // Browser requires a user gesture to resume AudioContext
                    const unlockAudio = () => {
                        if (audioContext && audioContext.state === 'suspended') {
                            audioContext.resume().then(() => {
                                state.audioActive = (audioContext.state === 'running');
                                updateOSD();
                            }).catch(() => {});
                        }
                    };

                    ['click', 'keydown', 'touchstart'].forEach(evt => {
                        window.addEventListener(evt, unlockAudio, { once: true, passive: true });
                    });
                }
            }

            // Fallback: Silent HTML5 Audio Element in an infinite loop
            silentAudioElement = document.createElement('audio');
            silentAudioElement.setAttribute('loop', '');
            silentAudioElement.setAttribute('aria-hidden', 'true');
            silentAudioElement.style.display = 'none';
            // 1-second valid silent WAV base64
            silentAudioElement.src = 'data:audio/wav;base64,UklGRigAAABXQVZFZm10IBIAAAABAAEARKwAAIhYAQACABAAAABkYXRhAgAAAAEA';
            silentAudioElement.volume = 0.001;

            const playAudio = () => {
                silentAudioElement.play().then(() => {
                    state.audioActive = true;
                    updateOSD();
                }).catch(() => {});
            };

            playAudio();
            ['click', 'keydown'].forEach(evt => {
                window.addEventListener(evt, playAudio, { once: true, passive: true });
            });

        } catch (err) {
            console.warn('[CloudStream Anti-Idle] Audio engine warning:', err);
        }
    }

    /**
     * Traverses the main document and accessible iframes to find xterm.js input helpers and textareas.
     */
    function queryTerminalElements() {
        const found = [];

        // Direct page search
        const direct = document.querySelectorAll('.xterm-helper-textarea, textarea.xterm-helper-textarea, textarea');
        direct.forEach(el => found.push(el));

        // Scan iframes (Google Cloud Shell frequently renders terminal inside an iframe)
        const iframes = document.querySelectorAll('iframe');
        iframes.forEach(iframe => {
            try {
                const doc = iframe.contentDocument || iframe.contentWindow?.document;
                if (doc) {
                    const iframeInputs = doc.querySelectorAll('.xterm-helper-textarea, textarea.xterm-helper-textarea, textarea');
                    iframeInputs.forEach(el => found.push(el));
                }
            } catch (_) {
                // Cross-origin iframe boundaries are safely ignored
            }
        });

        return found;
    }

    /**
     * Auto-detects and dismisses Google Cloud Shell idle modals or reconnect dialogs if they appear.
     */
    function autoDismissDialogs() {
        const buttons = document.querySelectorAll('button, [role="button"]');
        buttons.forEach(btn => {
            const text = (btn.textContent || '').trim().toLowerCase();
            if (
                text === 'reconnect' ||
                text === 'continue' ||
                text === 'stay connected' ||
                text === 'keep session open' ||
                text === 'resume'
            ) {
                try {
                    if (btn.offsetParent !== null) { // Element is visible
                        btn.click();
                        console.log('[CloudStream Anti-Idle] Auto-dismissed session timeout modal:', text);
                    }
                } catch (_) {}
            }
        });
    }

    /**
     * Dispatches synthetic keystroke pulse to xterm textarea and triggers activity events.
     */
    function dispatchPulse() {
        const targets = queryTerminalElements();
        state.targetsFound = targets.length;

        targets.forEach(target => {
            try {
                // Dispatch Shift keydown & keyup sequence (benign, doesn't generate characters or disrupt running CLI jobs)
                const shiftDown = new KeyboardEvent('keydown', {
                    key: 'Shift',
                    code: 'ShiftLeft',
                    keyCode: 16,
                    which: 16,
                    bubbles: true,
                    cancelable: true,
                    composed: true
                });

                const shiftUp = new KeyboardEvent('keyup', {
                    key: 'Shift',
                    code: 'ShiftLeft',
                    keyCode: 16,
                    which: 16,
                    bubbles: true,
                    cancelable: true,
                    composed: true
                });

                target.dispatchEvent(shiftDown);
                target.dispatchEvent(shiftUp);
            } catch (err) {
                console.debug('[CloudStream Anti-Idle] Dispatch failure on target:', err);
            }
        });

        // Trigger synthetic window mouse & keyboard movement to reset Cloud Console idle timer
        try {
            const mouseMove = new MouseEvent('mousemove', {
                bubbles: true,
                cancelable: true,
                clientX: Math.floor(Math.random() * 100) + 50,
                clientY: Math.floor(Math.random() * 100) + 50
            });
            window.dispatchEvent(mouseMove);
            document.dispatchEvent(mouseMove);
        } catch (_) {}

        // Check for any modal dialogs
        autoDismissDialogs();

        // Increment stats
        state.pulses++;
        state.lastPulseTime = new Date();
        state.nextPulseCountdown = Math.round(CONFIG.PULSE_INTERVAL_MS / 1000);

        triggerOSDPulseAnimation();
        updateOSD();
    }

    /**
     * Master initialization procedure.
     */
    function init() {
        if (state.active) return;
        state.active = true;

        console.log('[CloudStream Anti-Idle] Initializing 12-Hour Anti-Idle Engine...');

        createOSD();
        setupAudioKeepAlive();

        // Initial pulse after 3 seconds to verify hooks
        setTimeout(() => {
            dispatchPulse();
        }, 3000);

        // Schedule 42s heartbeat pulse interval
        pulseTimer = setInterval(() => {
            dispatchPulse();
        }, CONFIG.PULSE_INTERVAL_MS);

        // Schedule 1s OSD ticker & countdown
        tickerTimer = setInterval(() => {
            if (state.nextPulseCountdown > 0) {
                state.nextPulseCountdown--;
            }
            updateOSD();
        }, CONFIG.TICK_INTERVAL_MS);

        // Expose public API for manual testing and verification
        window.CloudStreamAntiIdle = {
            pulseNow: dispatchPulse,
            toggleMinimize,
            getState: () => ({ ...state }),
            teardown
        };

        // Clean teardown on unload
        window.addEventListener('beforeunload', teardown);
        console.log('[CloudStream Anti-Idle] Engine running. Pulse interval: 42s.');
    }

    /**
     * Clean teardown and interval management.
     */
    function teardown() {
        if (pulseTimer) clearInterval(pulseTimer);
        if (tickerTimer) clearInterval(tickerTimer);
        pulseTimer = null;
        tickerTimer = null;

        if (audioContext && audioContext.state !== 'closed') {
            try {
                audioContext.close();
            } catch (_) {}
        }

        if (silentAudioElement) {
            try {
                silentAudioElement.pause();
                silentAudioElement.remove();
            } catch (_) {}
        }

        const osd = document.getElementById(CONFIG.OSD_ID);
        if (osd) osd.remove();

        const styles = document.getElementById(CONFIG.STYLE_ID);
        if (styles) styles.remove();

        state.active = false;
        console.log('[CloudStream Anti-Idle] Teardown complete.');
    }

    // Auto-initialize when DOM is ready
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }
})();
