-- Web push subscriptions.
--
-- One row per device per browser install, not per user: a person with two
-- phones, or who reinstalls, legitimately has several. Send to all of them
-- and prune each independently.
--
-- iOS does not implement `pushsubscriptionchange`, so a dead subscription is
-- undiscoverable until a send returns 404 or 410. That is the only signal
-- there is -- hence last_failed_at and the prune-on-send rule rather than any
-- attempt at proactive cleanup.

create table if not exists push_subscription (
  id             bigserial primary key,
  device_id      uuid not null references device on delete cascade,
  endpoint       text not null unique,
  p256dh         text not null,
  auth           text not null,
  created_at     timestamptz not null default now(),
  last_sent_at   timestamptz,
  last_failed_at timestamptz
);

create index if not exists push_sub_device on push_subscription (device_id);
