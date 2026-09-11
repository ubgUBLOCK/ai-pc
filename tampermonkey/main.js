// ==UserScript==
// @name         Zen
// @namespace    http://tampermonkey.net/
// @version      2.1
// @description  Connects Zen Browser to your Arch KDE Discord Bot
// @match        *://*/*
// @grant        GM_xmlhttpRequest
// @grant        window.close
// @connect      127.0.0.1
// ==/UserScript==


(function() {
    'use strict';

    let isPolling = false;

    function pollBot() {
        if (isPolling) return;
        isPolling = true;

        GM_xmlhttpRequest({
            method: "GET",
            url: "http://127.0.0.1:5005/poll",
            timeout: 2500,
            onload: function(response) {
                isPolling = false;
                try {
                    let data = JSON.parse(response.responseText);
                    if (data && data.code) {
                        let result;
                        try {
                            // Spotify quick metadata helper
                            if (data.code === "spotify_info") {
                                let track = document.querySelector('[data-testid="context-item-info-title"]')?.innerText || "Unknown Track";
                                let artist = document.querySelector('[data-testid="context-item-info-subtitles"]')?.innerText || "Unknown Artist";
                                result = "Spotify Web: " + track + " by " + artist;
                            } else {
                                // Dynamic JS evaluation
                                result = String(Function('"use strict";return (' + data.code + ')')());
                            }
                        } catch (err) {
                            result = "Bridge Eval Error: " + err.message;
                        }

                        // Post result back to bot
                        GM_xmlhttpRequest({
                            method: "POST",
                            url: "http://127.0.0.1:5005/result",
                            headers: { "Content-Type": "application/json" },
                            data: JSON.stringify({
                                result: result,
                                url: window.location.href,
                                title: document.title
                            })
                        });
                    }
                } catch(e) {}
            },
            onerror: function() { isPolling = false; },
            ontimeout: function() { isPolling = false; }
        });
    }

    setInterval(pollBot, 1000);
})();
