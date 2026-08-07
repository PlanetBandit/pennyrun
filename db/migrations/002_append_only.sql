-- observation is an append-only log: it is the whole reason a collapsed
-- collection run can no longer destroy a good list. That guarantee used to
-- rest on nothing but "the application code doesn't happen to UPDATE or
-- DELETE it" -- a promise, not an invariant. This makes it one: any attempt
-- to change or remove a row is rejected by the database itself, regardless
-- of which role or code path tries it.
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
