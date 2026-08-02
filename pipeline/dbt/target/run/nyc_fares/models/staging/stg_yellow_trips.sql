
  
  create view "warehouse"."main_staging"."stg_yellow_trips__dbt_tmp" as (
    -- Cleaning layer: drop physically impossible trips, keep everything else.
--
-- Note what this filter does NOT do: it does not validate vendor_id against a
-- known set. A trip from an unknown vendor passes through untouched, which is
-- correct behaviour for a staging model and is exactly why nothing alerts.

select
    vendor_id,
    pickup_at,
    dropoff_at,
    passenger_count,
    trip_distance,
    ratecode_id,
    store_and_fwd_flag,
    pu_location_id,
    do_location_id,
    payment_type,
    fare_amount,
    tip_amount,
    total_amount,
    feed_month,
    date_diff('second', pickup_at, dropoff_at) / 60.0 as trip_minutes

from "warehouse"."raw"."yellow_trips"

where trip_distance between 0.1 and 100
  and total_amount between 1 and 500
  and fare_amount > 0
  and pickup_at is not null
  and dropoff_at is not null
  );
