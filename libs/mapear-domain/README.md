# mapear-domain

[← voltar para README raiz](../../README.md)

Modelo de domínio do Mapear-RN: entidades, resolução de pessoas e dados de referência do Rio Grande do Norte.

---

## O que é

A lib mais baixa da pilha. Define **o que são as coisas** no Mapear-RN — pessoas, municípios, fontes, conteúdo — sem nenhum acoplamento a I/O, cloud ou framework. É puro Python + Pydantic.

Se outras libs do workspace pudessem importar apenas uma coisa, importariam `mapear-domain`.

## Por que existe

Antes desta lib, definições de entidades estavam espalhadas entre os pipelines, dbt seeds e código de NLP. Mudar o nome de um campo era uma busca-e-substitua perigosa em vários repositórios. Hoje, `mapear-domain` é a **fonte de verdade dos contratos** — qualquer outra lib que precise falar de "pessoa", "município" ou "artigo" importa daqui.

Combinada com `import-linter`, garante que nenhuma camada superior (storage, nlp, pipelines) reinvente entidades por conta própria.

## Stack

| Tecnologia | Versão | Para quê |
|---|---|---|
| **Pydantic** | 2.9 | Modelos de domínio com validação. |
| **loguru** | 0.7 | Logging estruturado quando há cargas de seed/lookup. |
| **PyYAML** | 6.0 | Carregamento de gazetteers e regras de resolução versionadas. |

Sem dependências cloud, sem httpx, sem pandas. Mantida deliberadamente leve.

## API pública (essencial)

```python
from mapear_domain.entities import Person, Municipality, ContentItem
from mapear_domain.rn_entities import load_rn_cities_mayors, find_municipality
from mapear_domain.resolution import resolve_person_mention

# 167 municípios + prefeitos atuais — fonte: dbt/seeds/rn_cities_mayors.csv
cities = load_rn_cities_mayors()

# Resolver menção ambígua para identidade canônica + confiança [0, 1]
result = resolve_person_mention("o prefeito de Mossoró", context_municipality="Mossoró")
# → ResolvedPerson(canonical_id=..., confidence=0.92, source="rule:mayor_lookup")
```

## Decisões locais

1. **Seed CSV é fonte de verdade, não código.** Os 167 municípios e seus prefeitos moram em [`dbt/seeds/rn_cities_mayors.csv`](../../dbt/seeds/rn_cities_mayors.csv). Esta lib lê o seed; não duplica os dados. Mudou o seed, mudou o domínio.
2. **Resolução de pessoa retorna confiança, não booleano.** "Allyson Bezerra" no contexto de Mossoró é o prefeito com confiança alta; sem contexto, pode ser qualquer Allyson Bezerra do estado. Cabe ao chamador decidir o threshold.
3. **Sem dependências de I/O.** Não importa Google Cloud, não faz HTTP, não toca BigQuery. Isso permite que todas as outras libs dependam dela sem arrastar dependências pesadas.

## Como testar

```bash
uv run pytest libs/mapear-domain/tests/
```

## Estrutura

```
mapear-domain/
├── src/mapear_domain/
│   ├── entities.py        # Person, Municipality, ContentItem (Pydantic)
│   ├── rn_entities.py     # Carregamento e lookup do seed RN
│   └── resolution.py      # Resolução de menções ambíguas
└── tests/
```
