-- One spelling per department, for rows written before db/categories.py
-- existed. Everything arriving from now on is normalised at the point of
-- writing (api/validate.py for the collector, db/seed.py for the bundled
-- file), so this is a one-time backfill rather than a rule the database
-- enforces.
--
-- These statements must stay in step with ALIASES in db/categories.py;
-- tests/test_categories.py fails if a pair is added to one and not the
-- other. Each is a no-op on a second run, which is what makes re-applying
-- the whole migration set safe.

update product set category = 'Outdoors'
 where category = 'Outdoor';

update product set category = 'Storage'
 where category = 'Storage & Organization';

update product set category = 'Doors & Windows'
 where category in ('Doors', 'Windows');

-- Case variants of a department we already know cannot become a second
-- chip either. Written as a join against the canonical list rather than a
-- statement per name so adding a department to CANONICAL does not need a
-- matching line here.
update product p set category = c.name
  from (values
    ('Appliances'), ('Auto'), ('Bath'), ('Building'), ('Cleaning'),
    ('Decor'), ('Doors & Windows'), ('Electrical'), ('Flooring'),
    ('Furniture'), ('Garage'), ('Garden'), ('Grills'), ('Hardware'),
    ('Holiday'), ('Ladders'), ('Lighting'), ('Lumber & Composites'),
    ('Outdoors'), ('Paint'), ('Patio'), ('Pet'), ('Plumbing'), ('Pools'),
    ('Safety'), ('Smart Home'), ('Storage'), ('Tools')
  ) as c(name)
 where lower(p.category) = lower(c.name)
   and p.category <> c.name;

-- Stray whitespace is the third way one department becomes two.
update product set category = btrim(regexp_replace(category, '\s+', ' ', 'g'))
 where category is not null
   and category <> btrim(regexp_replace(category, '\s+', ' ', 'g'));

update product set category = null
 where category is not null and btrim(category) = '';
