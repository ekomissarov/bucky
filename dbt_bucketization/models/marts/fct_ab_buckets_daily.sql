{{
    config(
        materialized = 'incremental',
        unique_key = ['date_day', 'country', 'bucket', 'experiment_number', 'experiment_group'],
        incremental_strategy = 'delete+insert'
    )
}}

WITH events AS (
    SELECT * FROM {{ ref('int_ab_events_bucketed') }}
    {% if is_incremental() %}
        -- Фильтруем исходные агрегаты по дате при инкрементальном запуске
        WHERE date_day >= (SELECT MAX(date_day) FROM {{ this }})
    {% endif %}
)

-- Если появится второй источник, создаём CTE 'other_source' и объединяем
SELECT
    date_day,
    country,
    bucket,
    experiment_number,
    experiment_group,
    total_events,
    page_view_count,
    watch_count,
    add_to_cart_count,
    purchase_count,
    total_unique_users,
    u_page_view,
    u_watch,
    u_add_to_cart,
    u_purchase,
    purchase_amount
FROM events