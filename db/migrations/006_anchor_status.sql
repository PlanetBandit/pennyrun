-- Home Depot's own per-store status for an item: CLEARANCE, ACTIVE or
-- INACTIVE. Recorded so we can watch an item MOVE between them.
--
-- Measured over 192 item x store pairs, CLEARANCE agreed with the presence
-- of a clearance price 191 times, so as a detector it is redundant. Two
-- things make it worth a column anyway:
--
--   The 192nd pair. An item the store calls CLEARANCE while the pricing
--   block shows full price -- a markdown the price feed has not caught up
--   with. Those produce no row today, so they are invisible.
--
--   INACTIVE. It appears on items that are not on clearance, never
--   co-occurred with our replacementOMSID "being replaced" flag, and is
--   plausibly an item on its way out of a store, which is where penny
--   items come from. Testing that needs history, and history cannot be
--   backfilled -- the only way to have it in a month is to start now.
alter table observation add column if not exists anchor_status text;

-- Rows with no clearance price are now a normal thing to hold: an
-- observation is "what we saw for this item at this store", not "a
-- clearance price we found". Every read path already filters on
-- `clearance_price is not null`, so nothing starts showing them.
create index if not exists observation_status_idx
  on observation (store_id, anchor_status)
  where clearance_price is null;
