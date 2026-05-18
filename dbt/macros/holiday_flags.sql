{#-
    Macros para classificação temporal de uma coluna DATE em dim_dates:
      - is_holiday_br: feriados nacionais fixos + móveis 2024-2027
      - is_holiday_rn: estaduais RN (apenas Mártires de Cunhaú e Uruaçu, 03-out)
      - electoral_phase: pre_campaign / campaign / quiet / runoff / post (ciclo 2026)

    Datas de feriados móveis (Carnaval, Sexta-feira Santa, Corpus Christi) calculadas
    offline via tabela fixa — preferimos lookup explícito a algoritmo de Páscoa
    para manter SQL portátil e auditável (cobre 2024-2027, janela do dim_dates).

    Calendário eleitoral 2026 baseado em TSE (Lei 9504/97 Art. 36): campanha começa
    16 de agosto do ano eleitoral; eleição em 1º domingo de outubro (2026-10-04);
    2º turno se houver em 2026-10-25.
-#}

{% macro is_holiday_br(date_col) %}
    CASE
        -- Fixos nacionais
        WHEN EXTRACT(MONTH FROM {{ date_col }}) = 1
             AND EXTRACT(DAY FROM {{ date_col }}) = 1   THEN TRUE  -- Confraternização
        WHEN EXTRACT(MONTH FROM {{ date_col }}) = 4
             AND EXTRACT(DAY FROM {{ date_col }}) = 21  THEN TRUE  -- Tiradentes
        WHEN EXTRACT(MONTH FROM {{ date_col }}) = 5
             AND EXTRACT(DAY FROM {{ date_col }}) = 1   THEN TRUE  -- Trabalho
        WHEN EXTRACT(MONTH FROM {{ date_col }}) = 9
             AND EXTRACT(DAY FROM {{ date_col }}) = 7   THEN TRUE  -- Independência
        WHEN EXTRACT(MONTH FROM {{ date_col }}) = 10
             AND EXTRACT(DAY FROM {{ date_col }}) = 12  THEN TRUE  -- N. Sra. Aparecida
        WHEN EXTRACT(MONTH FROM {{ date_col }}) = 11
             AND EXTRACT(DAY FROM {{ date_col }}) = 2   THEN TRUE  -- Finados
        WHEN EXTRACT(MONTH FROM {{ date_col }}) = 11
             AND EXTRACT(DAY FROM {{ date_col }}) = 15  THEN TRUE  -- Proclamação
        WHEN EXTRACT(MONTH FROM {{ date_col }}) = 11
             AND EXTRACT(DAY FROM {{ date_col }}) = 20  THEN TRUE  -- Consciência Negra (federal desde 2024)
        WHEN EXTRACT(MONTH FROM {{ date_col }}) = 12
             AND EXTRACT(DAY FROM {{ date_col }}) = 25  THEN TRUE  -- Natal
        -- Móveis 2024-2027 (Carnaval ter, Sexta Santa, Corpus Christi)
        WHEN CAST({{ date_col }} AS DATE) IN (
            DATE '2024-02-13', DATE '2024-03-29', DATE '2024-05-30',
            DATE '2025-03-04', DATE '2025-04-18', DATE '2025-06-19',
            DATE '2026-02-17', DATE '2026-04-03', DATE '2026-06-04',
            DATE '2027-02-09', DATE '2027-03-26', DATE '2027-05-27'
        ) THEN TRUE
        ELSE FALSE
    END
{% endmacro %}


{% macro is_holiday_rn(date_col) %}
    CASE
        WHEN EXTRACT(MONTH FROM {{ date_col }}) = 10
             AND EXTRACT(DAY FROM {{ date_col }}) = 3   THEN TRUE  -- Mártires de Cunhaú e Uruaçu (Lei 9.443/2010)
        ELSE FALSE
    END
{% endmacro %}


{#-
    Ciclo 2026 (Lei 9504/97):
      até 2026-08-15        : pre_campaign
      2026-08-16 → 2026-10-03 : campaign
      2026-10-04            : campaign (dia da eleição 1º turno)
      2026-10-05 → 2026-10-25 : runoff (campanha 2º turno se houver)
      2026-10-26+           : post

    Anos não-eleitorais ficam como `none` (interpretação: período não-eleitoral).
-#}
{% macro electoral_phase(date_col) %}
    CASE
        WHEN CAST({{ date_col }} AS DATE) BETWEEN DATE '2026-01-01' AND DATE '2026-08-15'
            THEN 'pre_campaign'
        WHEN CAST({{ date_col }} AS DATE) BETWEEN DATE '2026-08-16' AND DATE '2026-10-04'
            THEN 'campaign'
        WHEN CAST({{ date_col }} AS DATE) BETWEEN DATE '2026-10-05' AND DATE '2026-10-25'
            THEN 'runoff'
        WHEN CAST({{ date_col }} AS DATE) BETWEEN DATE '2026-10-26' AND DATE '2026-12-31'
            THEN 'post'
        ELSE 'none'
    END
{% endmacro %}
