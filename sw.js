const CACHE='ale-2026-v2.0.0';
const CORE=['./','./index.html','./manifest.json','./icon-192.png','./icon-512.png','./question-bank-v2.0.0.json'];
self.addEventListener('install',e=>e.waitUntil(caches.open(CACHE).then(c=>c.addAll(CORE)).then(()=>self.skipWaiting())));
self.addEventListener('activate',e=>e.waitUntil(caches.keys().then(keys=>Promise.all(keys.filter(k=>k.startsWith('ale-2026-')&&k!==CACHE).map(k=>caches.delete(k)))).then(()=>self.clients.claim())));
self.addEventListener('fetch',e=>{const u=new URL(e.request.url);if(u.pathname.endsWith('/updates/latest.json')||u.pathname.includes('question-bank-v')){e.respondWith(fetch(e.request,{cache:'no-store'}).catch(()=>caches.match(e.request)));return;}e.respondWith(caches.match(e.request).then(r=>r||fetch(e.request).then(resp=>{let c=resp.clone();caches.open(CACHE).then(cache=>cache.put(e.request,c));return resp;})));});
