"""Web push, and the three iOS rules that decide whether it works at all.

Push on iOS is real — 16.4+, no Apple Developer account, no APNs
certificate. It is plain VAPID over HTTPS to `web.push.apple.com`, which
`pywebpush` speaks. But three constraints are unforgiving and none of them
announce themselves:

  **Installed to the home screen, or nothing.** A plain Safari tab cannot
  subscribe. The client checks `display-mode: standalone` before it even
  offers, because offering a feature that silently cannot work is worse than
  not offering it.

  **The service worker must call `showNotification()` inside
  `event.waitUntil()`.** Without it iOS counts a silent push and revokes the
  subscription after about three. That rule lives in `sw.js`; it is written
  down here too because the two files fail together.

  **A dead subscription is invisible until you send to it.** iOS does not
  implement `pushsubscriptionchange`, so 404/410 on send is the only signal
  there will ever be. Prune then, and never treat a failed send as an error
  worth retrying.

Because of the last one, push is never the only path: `GET /checks/{id}`
tells the app the same thing whenever it is next opened, so a push that never
lands costs nothing.
"""
import logging
import os

from fastapi import APIRouter, Header, HTTPException

from api.db import rows

log = logging.getLogger("pennyrun.push")
router = APIRouter()
V = "/api/v1"

# Apple rejects a VAPID `sub` that is not mailto: or an https: URL with a 403
# that says nothing useful. Other push services are more forgiving, so this
# breaks only on iOS and only in production.
VAPID_SUBJECT = os.environ.get("PENNYRUN_VAPID_SUBJECT", "mailto:planetbandit@protonmail.com")


def _keys():
    return (os.environ.get("PENNYRUN_VAPID_PUBLIC"),
            os.environ.get("PENNYRUN_VAPID_PRIVATE"))


@router.get(V + "/push/key")
def public_key():
    """The client needs this to subscribe. Public by definition."""
    pub, _ = _keys()
    if not pub:
        raise HTTPException(503, "push is not configured on this server")
    return {"key": pub}


@router.post(V + "/push/subscribe")
def subscribe(payload: dict):
    """Store a subscription for a device. Idempotent on endpoint."""
    device_id = payload.get("device_id")
    sub = payload.get("subscription") or {}
    endpoint = sub.get("endpoint")
    keys = sub.get("keys") or {}
    if not device_id or not endpoint or not keys.get("p256dh") or not keys.get("auth"):
        raise HTTPException(400, "device_id and a full subscription are required")

    import uuid
    try:
        device_id = str(uuid.UUID(str(device_id)))
    except (ValueError, TypeError):
        raise HTTPException(400, "device_id must be a uuid")

    with rows() as cur:
        cur.execute("insert into device (device_id) values (%s) "
                    "on conflict (device_id) do update set last_seen = now()",
                    (device_id,))
        cur.execute(
            "insert into push_subscription (device_id, endpoint, p256dh, auth)"
            " values (%s,%s,%s,%s)"
            " on conflict (endpoint) do update set device_id = excluded.device_id,"
            "   p256dh = excluded.p256dh, auth = excluded.auth, last_failed_at = null"
            " returning id", (device_id, endpoint, keys["p256dh"], keys["auth"]))
        got = cur.fetchone()
    return {"ok": True, "subscription_id": got["id"]}


def send_to_devices(device_ids, title, body, url="/"):
    """Best effort, always. A push that fails must never fail the caller.

    Returns (sent, pruned). Callers report jobs whether or not this worked --
    the status endpoint is the durable path and this is the courtesy.
    """
    pub, priv = _keys()
    if not priv or not device_ids:
        return 0, 0

    try:
        from pywebpush import WebPushException, webpush
    except ImportError:
        log.warning("pywebpush is not installed; no notification sent")
        return 0, 0

    import json
    sent = pruned = 0
    with rows() as cur:
        cur.execute("select id, endpoint, p256dh, auth from push_subscription"
                    " where device_id = any(%s)", (list(device_ids),))
        subs = cur.fetchall()

    dead = []
    for s in subs:
        try:
            webpush(
                subscription_info={"endpoint": s["endpoint"],
                                   "keys": {"p256dh": s["p256dh"], "auth": s["auth"]}},
                data=json.dumps({"title": title, "body": body, "url": url}),
                vapid_private_key=priv,
                vapid_claims={"sub": VAPID_SUBJECT},
                timeout=10,
            )
            sent += 1
        except WebPushException as e:
            code = getattr(getattr(e, "response", None), "status_code", None)
            if code in (404, 410):
                # The only signal iOS will ever give that this is dead.
                dead.append(s["id"])
            else:
                log.warning("push to %s failed: %s", s["id"], code or e)
        except Exception as e:                       # noqa: BLE001
            log.warning("push to %s errored: %s", s["id"], e)

    if dead:
        with rows() as cur:
            cur.execute("delete from push_subscription where id = any(%s)", (dead,))
        pruned = len(dead)

    return sent, pruned
