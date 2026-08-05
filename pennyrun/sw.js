/* Penny Run — offline cache.
   Everything the app needs is local, so one pass at install
   makes the whole thing work with zero bars in the store. */

var CACHE = "pennyrun-v38";
var ASSETS = [
  "./",
  "./index.html",
  "./zxing.min.js",
  "./stores.json",
  "./clearance.json",
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
