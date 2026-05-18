{{
    config(
        materialized='table',
        unique_key='person_sk'
    )
}}

WITH current_seed AS (

    SELECT
        person_id,
        name,
        aliases,
        role,
        city,
        party,
        term_start,
        term_end,
        is_incumbent,
        facebook_page,
        instagram_username,
        x_handle,
        tiktok_handle,
        notes,
        -- Surrogate key combina person_id + atributos versionáveis (SCD2)
        {{ dbt_utils.generate_surrogate_key([
            'person_id', 'role', 'party', 'city',
            'facebook_page', 'instagram_username', 'x_handle', 'tiktok_handle'
        ]) }} AS person_sk
    FROM {{ ref('rn_targets') }}

),

{% if is_incremental() %}

existing AS (

    SELECT * FROM {{ this }}
    WHERE is_current = TRUE

),

-- Detecta mudanças: novo registro OU alteração em atributos versionáveis
changed AS (

    SELECT
        c.person_id,
        c.name,
        c.aliases,
        c.role,
        c.city,
        c.party,
        c.term_start,
        c.term_end,
        c.is_incumbent,
        c.facebook_page,
        c.instagram_username,
        c.x_handle,
        c.tiktok_handle,
        c.notes,
        c.person_sk
    FROM current_seed AS c
    LEFT JOIN existing AS e
        ON c.person_id = e.person_id
    WHERE
        e.person_id IS NULL
        OR e.role != c.role
        OR e.party != c.party
        OR COALESCE(e.city, '') != COALESCE(c.city, '')
        OR COALESCE(e.facebook_page, '') != COALESCE(c.facebook_page, '')
        OR COALESCE(e.instagram_username, '') != COALESCE(c.instagram_username, '')
        OR COALESCE(e.x_handle, '') != COALESCE(c.x_handle, '')
        OR COALESCE(e.tiktok_handle, '') != COALESCE(c.tiktok_handle, '')

),

-- Fecha registros antigos cujos atributos mudaram
closed AS (

    SELECT
        e.person_sk,
        e.person_id,
        e.name,
        e.aliases,
        e.role,
        e.city,
        e.party,
        e.term_start,
        e.term_end,
        e.is_incumbent,
        e.facebook_page,
        e.instagram_username,
        e.x_handle,
        e.tiktok_handle,
        e.notes,
        e.valid_from,
        CURRENT_DATE AS valid_to,
        FALSE AS is_current
    FROM existing AS e
    INNER JOIN changed AS c
        ON e.person_id = c.person_id

),

-- Abre novos registros (atual)
opened AS (

    SELECT
        c.person_sk,
        c.person_id,
        c.name,
        c.aliases,
        c.role,
        c.city,
        c.party,
        c.term_start,
        c.term_end,
        c.is_incumbent,
        c.facebook_page,
        c.instagram_username,
        c.x_handle,
        c.tiktok_handle,
        c.notes,
        CURRENT_DATE AS valid_from,
        CAST(NULL AS DATE) AS valid_to,
        TRUE AS is_current
    FROM changed AS c

),

-- Mantém registros sem mudança
unchanged AS (

    SELECT
        e.person_sk,
        e.person_id,
        e.name,
        e.aliases,
        e.role,
        e.city,
        e.party,
        e.term_start,
        e.term_end,
        e.is_incumbent,
        e.facebook_page,
        e.instagram_username,
        e.x_handle,
        e.tiktok_handle,
        e.notes,
        e.valid_from,
        e.valid_to,
        e.is_current
    FROM existing AS e
    LEFT JOIN changed AS c
        ON e.person_id = c.person_id
    WHERE c.person_id IS NULL

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
        person_sk,
        person_id,
        name,
        aliases,
        role,
        city,
        party,
        term_start,
        term_end,
        is_incumbent,
        facebook_page,
        instagram_username,
        x_handle,
        tiktok_handle,
        notes,
        CAST('2026-04-20' AS DATE) AS valid_from,
        CAST(NULL AS DATE) AS valid_to,
        TRUE AS is_current
    FROM current_seed

)

SELECT * FROM final

{% endif %}

ORDER BY role, name, valid_from DESC
