-- On-demand area checks.
--
-- A user asks for prices at their nearby stores. Home Depot will not answer
-- the droplet and will not answer a browser (their edge refuses the CORS
-- preflight), so the residential collector does the asking. This is the queue
-- between the two.
--
-- The important idea: a job belongs to a STORE, not to a user. Two people
-- asking for Towson share one sweep, and a store priced twenty minutes ago
-- costs nothing at all. Cost scales with stale stores rather than with users,
-- so once a metro is covered everyone in it is served for free.

create table if not exists check_job (
  job_id       bigserial primary key,
  store_id     text not null references store,
  state        text not null default 'queued'
               check (state in ('queued', 'running', 'done', 'failed')),
  requested_at timestamptz not null default now(),
  claimed_at   timestamptz,
  finished_at  timestamptz,
  claimed_by   text,          -- collector hostname; for working out which box stalled
  hits         integer,
  refused      integer,
  note         text
);

-- This index is the coalescing rule, expressed where it cannot be forgotten:
-- at most one live job per store. Two users asking for the same store race
-- into the same `insert ... on conflict do nothing` and exactly one job
-- survives, with no application-level locking and no read-then-write window.
create unique index if not exists check_job_live
  on check_job (store_id) where state in ('queued', 'running');

create index if not exists check_job_queue on check_job (state, requested_at);

-- Who to tell when a job finishes. A device may watch many jobs and a job may
-- have many watchers -- that is the point of coalescing.
create table if not exists check_watcher (
  job_id    bigint not null references check_job,
  device_id uuid not null references device,
  added_at  timestamptz not null default now(),
  primary key (job_id, device_id)
);

create index if not exists check_watcher_device on check_watcher (device_id);

-- The collector polls for work whether or not there is any, so this is a
-- truthful "is anyone home" signal even when the queue is empty. Without it
-- the app cannot tell "queued, two minutes" from "queued forever, the Mac is
-- asleep" -- and showing a countdown that will never run down is the same
-- class of lie as reporting stores that were never priced.
create table if not exists collector_heartbeat (
  collector   text primary key,
  last_seen   timestamptz not null default now(),
  last_job_id bigint references check_job
);
