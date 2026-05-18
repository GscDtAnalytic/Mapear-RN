{{
    config(
        materialized='table',
        unique_key='city_mayor_key'
    )
}}

WITH current_seed AS (

    SELECT
        city,
        state,
        population,
        mayor,
        party,
        monitored,
        latitude,
        longitude,
        supports_candidate,
        -- Chave composta para SCD2
        {{ dbt_utils.generate_surrogate_key(['city', 'mayor', 'party']) }}
            AS city_mayor_key
    FROM {{ ref('rn_cities_mayors') }}

),

{% if is_incremental() %}

existing AS (

    SELECT * FROM {{ this }}
    WHERE is_current = TRUE

),

-- Detectar mudanças (novo prefeito, novo partido, nova população, novo apoio)
changed AS (

    SELECT
        c.city,
        c.state,
        c.population,
        c.mayor,
        c.party,
        c.monitored,
        c.latitude,
        c.longitude,
        c.supports_candidate,
        c.city_mayor_key
    FROM current_seed AS c
    LEFT JOIN existing AS e
        ON c.city = e.city
    WHERE e.city IS NULL
       OR e.mayor != c.mayor
       OR e.party != c.party
       OR COALESCE(e.supports_candidate, '') != COALESCE(c.supports_candidate, '')

),

-- Fechar registros antigos
closed AS (

    SELECT
        e.city_mayor_key,
        e.city,
        e.state,
        e.population,
        e.mayor,
        e.party,
        e.monitored,
        e.latitude,
        e.longitude,
        e.supports_candidate,
        e.valid_from,
        CURRENT_DATE AS valid_to,
        FALSE AS is_current
    FROM existing AS e
    INNER JOIN changed AS c
        ON e.city = c.city

),

-- Novos registros
opened AS (

    SELECT
        c.city_mayor_key,
        c.city,
        c.state,
        c.population,
        c.mayor,
        c.party,
        c.monitored,
        c.latitude,
        c.longitude,
        c.supports_candidate,
        CURRENT_DATE AS valid_from,
        CAST(NULL AS DATE) AS valid_to,
        TRUE AS is_current
    FROM changed AS c

),

-- Registros sem mudança
unchanged AS (

    SELECT
        e.city_mayor_key,
        e.city,
        e.state,
        e.population,
        e.mayor,
        e.party,
        e.monitored,
        e.latitude,
        e.longitude,
        e.supports_candidate,
        e.valid_from,
        e.valid_to,
        e.is_current
    FROM existing AS e
    LEFT JOIN changed AS c
        ON e.city = c.city
    WHERE c.city IS NULL

)

SELECT * FROM closed
UNION ALL
SELECT * FROM opened
UNION ALL
SELECT * FROM unchanged

{% else %}

-- Full refresh: tudo começa como current
final AS (

    SELECT
        city_mayor_key,
        city,
        state,
        population,
        mayor,
        party,
        monitored,
        latitude,
        longitude,
        supports_candidate,
        CAST('2025-01-01' AS DATE) AS valid_from,
        CAST(NULL AS DATE) AS valid_to,
        TRUE AS is_current
    FROM current_seed

)

SELECT * FROM final

{% endif %}

ORDER BY city, valid_from DESC
