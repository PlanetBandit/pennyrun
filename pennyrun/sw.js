/* Penny Run — offline cache.
   Everything the app needs is local, so one pass at install
   makes the whole thing work with zero bars in the store. */

var CACHE = "pennyrun-v59";
var ASSETS = [
  "./",
  "./index.html",
  "./zxing.min.js",
  "./stores.json",
  "./clearance.json",
  "./hd-stores.json",
  "./manifest.webmanifest",
  "./icon-192.png",
  "./icon-512.png",
  "./apple-touch-icon.png",
  "./ocr/tesseract.min.js",
  "./ocr/worker.min.js",
  "./ocr/tesseract-core-lstm.wasm.js",
  "./ocr/tesseract-core-simd-lstm.wasm.js",
  "./ocr/eng.traineddata.gz"
];

self.addEventListener("install", function(e){
  e.waitUntil(
    caches.open(CACHE).then(function(c){ return c.addAll(ASSETS); })
      .then(function(){ return self.skipWaiting(); })
  );
});

self.addEventListener("activate", function(e){
  e.waitUntil(
    caches.keys().then(function(keys){
      return Promise.all(keys.map(function(k){
        if(k !== CACHE) return caches.delete(k);
      }));
    }).then(function(){ return self.clients.claim(); })
  );
});

self.addEventListener("fetch", function(e){
  if(e.request.method !== "GET") return;

  /* The page itself goes network-first so every open picks up the
     latest build immediately; the cache only answers offline. */
  var accept = e.request.headers.get("accept") || "";
  if(e.request.mode === "navigate" || accept.indexOf("text/html") !== -1){
    e.respondWith(
      fetch(e.request).then(function(res){
        if(res && res.status === 200){
          var copy = res.clone();
          caches.open(CACHE).then(function(c){ c.put("./index.html", copy); });
        }
        return res;
      }).catch(function(){
        return caches.match("./index.html");
      })
    );
    return;
  }

  /* The sweep is re-run nightly, so it must never be answered from a
     stale cache while online. Small file, network-first, and the cached
     copy still carries the whole list into a dead-signal aisle. */
  if(e.request.url.indexOf("clearance.json") !== -1){
    e.respondWith(
      fetch(e.request).then(function(res){
        if(res && res.status === 200){
          var copy = res.clone();
          caches.open(CACHE).then(function(c){ c.put(e.request, copy); });
        }
        return res;
      }).catch(function(){ return caches.match(e.request); })
    );
    return;
  }

  /* Heavy assets (scanner, OCR, store data) stay cache-first. */
  e.respondWith(
    caches.match(e.request).then(function(hit){
      if(hit) return hit;
      return fetch(e.request).then(function(res){
        if(res && res.status === 200 && res.type === "basic"){
          var copy = res.clone();
          caches.open(CACHE).then(function(c){ c.put(e.request, copy); });
        }
        return res;
      }).catch(function(){
        return caches.match("./index.html");
      });
    })
  );
});

/* ---------------------------------------------------------------- push

   showNotification() MUST be inside event.waitUntil(). Without it iOS treats
   the push as silent and revokes the subscription after about three of them --
   spec-compliant, undocumented in the place you'd look, and it bites almost
   everyone once. The whole subscription dies, quietly, and the only symptom is
   that notifications stop.

   Every push also shows something. A push that decides it has nothing worth
   showing is exactly the "silent push" that gets counted against you. */
self.addEventListener("push", function(e){
  var d = { title: "Penny Run", body: "", url: "/" };
  try { if(e.data) d = Object.assign(d, e.data.json()); } catch(_){ }
  e.waitUntil(
    self.registration.showNotification(d.title, {
      body: d.body,
      icon: "./icon-192.png",
      badge: "./icon-192.png",
      tag: "pennyrun",
      data: { url: d.url }
    })
  );
});

/* Tapping the notification should land in the open app if there is one,
   rather than opening a second copy. */
self.addEventListener("notificationclick", function(e){
  e.notification.close();
  var target = (e.notification.data && e.notification.data.url) || "/";
  e.waitUntil(
    clients.matchAll({ type: "window", includeUncontrolled: true }).then(function(list){
      for(var i = 0; i < list.length; i++){
        if("focus" in list[i]) return list[i].focus();
      }
      if(clients.openWindow) return clients.openWindow(target);
    })
  );
});
