-- Row 14 — TDT-TOPIC-01: topic_id >= -1 (sentinel mínimo por design).
--
-- -1 = "não classificado" — único valor negativo permitido por design.
-- topic_id < -1 é impossível nos três regimes semânticos:
--   Regime 1 (GCP ordinal ≥ 0), Regime 2 (TOPIC_ID_MAP 1–10), Regime 3 (zeros bug).
-- Qualquer valor abaixo de -1 indica corrupção de pipeline ou migration incorreta.
--
-- Severidade M (warn): violação indica corrupção real do pipeline (impossível
-- nos três regimes). Não escala para 'error' enquanto TDT-TOPIC-01 está aberto
-- — política: testes novos sobre tech debts conhecidos não bloqueiam CI até
-- resolução do tech debt principal.
{{ config(severity='warn') }}

SELECT
    event_id,
    source_type,
    platform,
    topic_id
FROM {{ ref('mapear_events') }}
WHERE
    topic_id IS NOT NULL
    AND topic_id < -1
