{{
    config(
        materialized = 'view'
    )
}}

SELECT
    DATE(date) AS date_day,
    user_id,
    country,
    event_type,
    amount,

    -- Вычисляем бакет
    abs(hash_id) % 200 AS bucket,

    -- Распаковываем JSON эксперимента
    json_keys(experiment)[1] AS experiment_number,
    json_extract_string(experiment, '$.' || json_keys(experiment)[1]) AS experiment_group

FROM {{ source('raw_data', 'events') }}

WHERE
    experiment IS NOT NULL
    AND experiment != ''