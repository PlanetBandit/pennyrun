create table if not exists schema_migration (
  name       text primary key,
  applied_at timestamptz not null default now()
);

create table if not exists product (
  item_id        text primary key,
  name           text not null,
  category       text,
  upc            text,
  store_sku      text,
  model_number   text,
  canonical_url  text,
  replacement_id text,
  first_seen     date not null default current_date,
  last_seen      date not null default current_date
);
create index if not exists product_upc_idx on product (upc);

create table if not exists store (
  store_id text primary key,
  name     text,
  street   text,
  city     text,
  state    text,
  zip      text,
  lat      double precision,
  lon      double precision
);
create index if not exists store_zip_idx on store (zip);
create index if not exists store_geo_idx on store (lat, lon);

create table if not exists device (
  device_id     uuid primary key,
  created_at    timestamptz not null default now(),
  last_seen     timestamptz,
  trust_score   numeric(4,2) not null default 0.5,
  transfer_code text unique
);

create table if not exists observation (
  id              bigserial primary key,
  item_id         text not null references product,
  store_id        text not null references store,
  observed_at     timestamptz not null default now(),
  list_price      numeric(10,2),
  clearance_price numeric(10,2),
  pct_off         numeric(5,2),
  quantity        integer,
  store_only      boolean,
  source          text not null check (source in ('discovery','phone','confirmation')),
  device_id       uuid references device,
  trusted         boolean not null default false
);
create index if not exists obs_item_store_idx on observation (item_id, store_id, observed_at desc);
create index if not exists obs_store_idx on observation (store_id, observed_at desc)
  where clearance_price is not null;
create unique index if not exists obs_unique_idx on observation (item_id, store_id, observed_at);

create table if not exists candidate (
  item_id      text primary key references product,
  first_marked date not null,
  last_marked  date not null,
  best_pct_off numeric(5,2),
  store_count  integer,
  penny_score  numeric(5,2),
  updated_at   timestamptz not null default now()
);
create index if not exists candidate_score_idx on candidate (penny_score desc, last_marked desc);

create table if not exists device_store (
  device_id uuid references device,
  store_id  text references store,
  primary key (device_id, store_id)
);

create table if not exists confirmation (
  id            bigserial primary key,
  item_id       text not null references product,
  store_id      text not null references store,
  device_id     uuid references device,
  scanned_price numeric(10,2) not null,
  is_penny      boolean generated always as (scanned_price <= 0.01) stored,
  confirmed_at  timestamptz not null default now()
);
