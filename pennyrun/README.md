# Penny Run — deploy notes

Static files only. No build step, no runtime, no outbound calls. The
barcode library is bundled, so once the page loads the app never touches
the network again.

## Files

```
index.html            app (UI + logic inline)
zxing.min.js          barcode decoder, bundled at 332 KB
ocr/                  tesseract.js OCR engine + English data (~10 MB,
                      each device downloads one core variant, ~6 MB)
sw.js                 service worker, caches everything for offline use
manifest.webmanifest  makes it installable to the home screen
icon-192.png
icon-512.png
apple-touch-icon.png
```

After the barcode lands, the camera stays up and prompts for one snap
of the clearance tag. On-device OCR (bundled tesseract.js — no network)
reads the price, "was" price, and date and pre-fills the clue sheet;
you confirm or correct before saving.

## Why it has to be served over HTTPS

iOS will not hand the camera to a page unless it's a secure context.
Opening `index.html` off the filesystem gives you the checklist but a
dead scanner. A real certificate is the whole difference.

## Deploy with Caddy (easiest — cert is automatic)

```bash
sudo apt install -y caddy
sudo mkdir -p /var/www/pennyrun
sudo cp -r ./* /var/www/pennyrun/
sudo rm -rf /var/www/pennyrun/build /var/www/pennyrun/README.md
```

Then in `/etc/caddy/Caddyfile`:

```
penny.yourdomain.com {
    root * /var/www/pennyrun
    file_server
    header /sw.js Cache-Control "no-cache"
}
```

```bash
sudo systemctl reload caddy
```

## Deploy with nginx (if it's already running)

```bash
sudo mkdir -p /var/www/pennyrun
sudo cp -r ./* /var/www/pennyrun/
sudo certbot --nginx -d penny.yourdomain.com
```

Server block:

```nginx
server {
    server_name penny.yourdomain.com;
    root /var/www/pennyrun;
    index index.html;

    location = /sw.js {
        add_header Cache-Control "no-cache";
    }
}
```

Serving `sw.js` with `no-cache` matters — otherwise a stale worker will
keep handing you an old build after you edit anything.

## On your phone

1. Open the https address in Safari.
2. Share → Add to Home Screen.
3. Launch from the icon. Tap **Start camera** and allow access once.
4. Airplane-mode it to confirm the offline cache took.

## How verdicts work

There is no preloaded list. Scanning a box opens an assessment sheet:
tap the clues you can see (dated tag, "was" price, app availability,
out-of-place stock, price ending) and the app scores them into a green,
yellow, or red flag. A register result overrides everything — $0.01 is
a confirmed green, full price or stop-sale is red. The weights live in
`CLUES` and `ENDING_SCORE` near the top of the script block in
`index.html`. Checked items are saved in `localStorage` under
`pennyrun.v2`; bump `KEY` or the `CACHE` name in `sw.js` to force a
reset after edits.
