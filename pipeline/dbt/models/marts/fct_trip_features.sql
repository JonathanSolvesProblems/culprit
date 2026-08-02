-- Feature table for the fare prediction model.
--
-- Authored in 2024, when the TLC feed contained vendors 1, 2 and 6. The vendor
-- encoding below is an exhaustive CASE over the vendors that existed at the
-- time. This is ordinary, defensible dbt code. It is also the culprit.
--
-- Two things happen to a trip from a vendor this model has never heard of:
--
--   1. every is_vendor_* flag is 0, so the row asserts "no vendor", a
--      combination that appears nowhere in the training data
--   2. the coalesce on avg_speed_mph converts a degenerate duration into a
--      confident 0.0 rather than a NULL
--
-- That second point is the reason no monitor fires. The null-safety guard that
-- keeps the column clean is exactly what hides the corruption.

select
    -- grain
    pickup_at,
    feed_month,

    -- continuous features
    trip_distance,
    trip_minutes,
    coalesce(trip_distance / nullif(trip_minutes / 60.0, 0), 0) as avg_speed_mph,
    coalesce(passenger_count, 1)                                as passenger_count,

    -- temporal features
    extract(hour from pickup_at)                                as pickup_hour,
    extract(dow from pickup_at)                                 as pickup_dow,

    -- geography
    pu_location_id,
    do_location_id,

    -- vendor encoding: exhaustive over vendors known at authoring time
    case when vendor_id = 1 then 1 else 0 end                   as is_vendor_cmt,
    case when vendor_id = 2 then 1 else 0 end                   as is_vendor_curb,
    case when vendor_id = 6 then 1 else 0 end                   as is_vendor_myle,

    -- fare context
    case when ratecode_id in (2, 3) then 1 else 0 end           as is_airport_rate,
    case when payment_type = 1 then 1 else 0 end                as is_card_payment,

    -- passthrough for evaluation and attribution
    vendor_id,

    -- target
    total_amount

from {{ ref('stg_yellow_trips') }}
