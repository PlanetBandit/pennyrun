-- observation is an append-only log: it is the whole reason a collapsed
-- collection run can no longer destroy a good list. That guarantee used to
-- rest on nothing but "the application code doesn't happen to UPDATE,
-- DELETE, or TRUNCATE it" -- a promise, not an invariant. This makes it
-- one: any attempt to change, remove, or wipe every row is rejected by the
-- database itself, regardless of which role or code path tries it. (A
-- superuser could still explicitly disable the trigger before acting --
-- that's a deliberate break-glass operation, not something a cleanup
-- script or a collapsed run could do by accident, so it's outside what
-- this guards against.)
--
-- UPDATE and DELETE are blocked by a row-level trigger. TRUNCATE never
-- fires a row-level trigger -- Postgres only invokes TRUNCATE triggers
-- when they're declared FOR EACH STATEMENT -- so it needs its own trigger
-- on the same function; without it, `TRUNCATE observation` would silently
-- destroy the whole history through the one verb the row-level trigger
-- can't see.
--
-- `create or replace function` and `create or replace trigger` (both safe
-- to run repeatedly) rather than `revoke`, because a single `pennyrun`
-- role both runs migrations and does application writes -- revoking a
-- privilege from the table's own owner doesn't stop that owner from using
-- it, but a trigger fires no matter who's connected.

create or replace function observation_append_only() returns trigger as $$
begin
  raise exception
    'observation is an append-only log: % is not allowed. '
    'A bad reading is corrected by inserting a newer observation, never by '
    'changing or removing an old one -- a collapsed collection run must '
    'never be able to overwrite or erase a night''s worth of good history.',
    TG_OP;
end;
$$ language plpgsql;

create or replace trigger observation_append_only_trg
  before update or delete on observation
  for each row execute function observation_append_only();

create or replace trigger observation_append_only_truncate_trg
  before truncate on observation
  for each statement execute function observation_append_only();
