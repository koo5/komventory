// Minimal service worker so the PWA is installable. No caching strategy yet —
// every fetch goes to the network. Adding a precache for the app shell is the
// natural next step once the API surface stabilises.
self.addEventListener("install", (e) => { self.skipWaiting(); });
self.addEventListener("activate", (e) => { e.waitUntil(self.clients.claim()); });
self.addEventListener("fetch", () => { /* network-only; default behaviour */ });
