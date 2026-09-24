{{
    config(
        materialized = 'view'
    )
}}

SELECT
    date_day,
    country,
    bucket,
    experiment_number,
    experiment_group,

    -- Total метрики
    COUNT(*) AS total_events,
    COUNT(CASE WHEN event_type = 'page_view' THEN 1 END) AS page_view_count,
    COUNT(CASE WHEN event_type = 'watch' THEN 1 END) AS watch_count,
    COUNT(CASE WHEN event_type = 'add_to_cart' THEN 1 END) AS add_to_cart_count,
    COUNT(CASE WHEN event_type = 'purchase' THEN 1 END) AS purchase_count,

    -- User метрики (уникальные пользователи)
    COUNT(DISTINCT user_id) AS total_unique_users,
    COUNT(DISTINCT CASE WHEN event_type = 'page_view' THEN user_id END) AS u_page_view,
    COUNT(DISTINCT CASE WHEN event_type = 'watch' THEN user_id END) AS u_watch,
    COUNT(DISTINCT CASE WHEN event_type = 'add_to_cart' THEN user_id END) AS u_add_to_cart,
    COUNT(DISTINCT CASE WHEN event_type = 'purchase' THEN user_id END) AS u_purchase,

    -- Сумма amount
    SUM(CASE WHEN event_type = 'purchase' THEN amount ELSE 0 END) AS purchase_amount

FROM {{ ref('stg_event_log') }}

GROUP BY
    date_day,
    country,
    bucket,
    experiment_number,
    experiment_group