# mapear-nlp

[← voltar para README raiz](../../README.md)

NLP em português, **100% determinístico**: NER, sentimento, classificação de tópicos, entity linking. Zero LLM em produção.

---

## O que é

A camada que transforma texto bruto em campos analisáveis. Recebe um artigo ou post e produz:

- **Entidades reconhecidas** (pessoas, organizações, lugares) com tipos.
- **Entidades linkadas** ao domínio (`"prefeito de Mossoró"` → ID canônico).
- **Sentimento por entidade** (não do texto como um todo — do que se fala de **cada** entidade).
- **Tópicos** (taxonomia hierárquica + descoberta via BERTopic).

Tudo com explicação rastreável: cada classificação tem uma regra ou um match documentado.

## Por que existe (e por que sem LLMs)

O instinto inicial seria usar Claude/GPT para tudo. Decidimos contra, e a decisão é deliberada:

| Critério | LLM | Determinístico |
|---|---|---|
| **Custo por documento** | R$ 0,01-0,10 | R$ 0,00 |
| **Latência** | 1-5s | < 100ms |
| **Reprodutibilidade** | Não-determinística | Idêntica sempre |
| **Auditabilidade** | "modelo decidiu" | "regra X linha Y" |
| **Custo em 1M docs/mês** | R$ 10k-100k | ~R$ 0 |

Para um projeto que precisa rodar continuamente e ser defensável academicamente, o trade-off é claro. **LLM entra apenas como explicador** ao usuário final (camada de UX), nunca como inferidor sistemático.

## Stack

| Tecnologia | Versão | Para quê |
|---|---|---|
| **spaCy** | 3.7 | NER + POS tagging em PT-BR. |
| **transformers** | 4.44 | Tokenização e modelos auxiliares. |
| **torch** | 2.4 | Backbone do transformers (CPU). |
| **sentence-transformers** | 3.1 | Embeddings para clustering narrativo (Eixo 2). |
| **HDBSCAN** | 0.8 | Clustering sem `k` pré-definido. |
| **BERTopic** | 0.16 | Topic modeling. |
| **scikit-learn** | 1.5 | Utilitários ML gerais. |
| **langdetect** | 1.0 | Filtro de idioma. |
| **PyYAML** | 6.0 | Gazetteers, regras de pós-processamento, taxonomia de tópicos. |
| **anthropic** | 0.39 (extra `[llm]`) | Claude como **explicador** de clusters — não como inferidor. |

## API pública (essencial)

```python
from mapear_nlp import NLPPipeline

nlp = NLPPipeline.from_settings(settings)
enriched = nlp.process(article)

enriched.entities          # [Entity(text="Allyson Bezerra", type="PERSON", ...)]
enriched.linked_entities   # [LinkedEntity(canonical_id="rn_mossoro_mayor", confidence=0.92)]
enriched.entity_sentiment  # {"rn_mossoro_mayor": SentimentScore(polarity=-0.4, ...)}
enriched.topics            # [Topic(label="saude_publica", confidence=0.78)]
```

Os modelos pesados (spaCy, transformers) são carregados **lazily na primeira chamada** — pipelines que processam batches grandes amortizam o custo de carregamento.

## Decisões locais

1. **NER deterministic-first.** spaCy faz extração; uma camada de regras YAML (`rules/`) refina, expande gazetteers e aplica conhecimento de domínio (todos os nomes dos prefeitos do RN ficam em um gazetteer).
2. **Entity linking via lookup, não embedding.** "Prefeito de Mossoró" resolve por regra (cargo + município → pessoa), não por similaridade de vetor. Mais barato, mais explicável.
3. **Sentimento por entidade, não por documento.** Um artigo pode falar bem do prefeito A e mal do prefeito B no mesmo texto. Polarizar o documento todo perderia essa nuance.
4. **Baselines de qualidade medidos em produção.** Os 35 campos derivados pela lib têm baselines reais (p50/p95) documentados em [`docs/sprint3_b4_baseline_plan.md`](../../docs/sprint3_b4_baseline_plan.md). Não há "f1-score teórico" — há "f1 medido em 10k documentos de produção".

## Como testar

```bash
uv run pytest libs/mapear-nlp/tests/
```

Testes incluem **golden set** de casos rotulados manualmente (`tests/golden/`) — nomes de teste codificam cenário (`test_C1_*`, `test_FP4_*`, `test_TP3_*`) para rastreabilidade com a documentação.

## Estrutura

```
mapear-nlp/
├── src/mapear_nlp/
│   ├── ner/                   # NER spaCy + post-processing
│   ├── sentiment/             # Sentimento por entidade (lexicon + rules)
│   ├── topics/                # Classificação hierárquica + BERTopic
│   ├── linking/               # Entity linking determinístico
│   ├── clustering/            # Narrative clustering (Eixo 2)
│   ├── rules/                 # YAML rules + gazetteers
│   └── pipeline.py            # Orquestra os módulos
├── eval/                      # Golden set evaluation, MLflow tracking
└── tests/
```
