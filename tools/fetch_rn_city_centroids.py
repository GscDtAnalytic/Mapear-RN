"""Fetch RN city centroids (latitude/longitude) from IBGE API and merge into seed CSV.

G-07 (Fase C / C3.3) — data acquisition step. Roda uma única vez para popular
`dbt/seeds/rn_cities_mayors.csv` com colunas `latitude`/`longitude`. Idempotente:
se as colunas já existem, atualiza valores; se não existem, adiciona.

IBGE API:
  - Lista de municípios:
    https://servicodados.ibge.gov.br/api/v1/localidades/estados/RN/municipios
  - Centroides (via malhas geojson) — qualquer biblioteca geo-aware faz o trabalho
    de calcular centroide a partir do polígono. Aqui usamos Nominatim por
    simplicidade (1 req/s, respeitar política de uso).

Uso:
    python scripts/fetch_rn_city_centroids.py
    # depois:
    cd dbt && dbt seed --select rn_cities_mayors
    # propagar latitude/longitude no model dim_rn_cities_mayors.sql + schema.yml

Dependencies: requests (já em mapear-core).
"""

from __future__ import annotations

import csv
import logging
import sys
import time
from pathlib import Path

import requests

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[1]
SEED_PATH = REPO_ROOT / "dbt" / "seeds" / "rn_cities_mayors.csv"

NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
USER_AGENT = "mapear-rn/0.2 (mapeardata@gmail.com)"
REQUEST_INTERVAL_SEC = 1.1  # Nominatim policy: max 1 req/s


def geocode_city(city: str, state: str = "RN") -> tuple[float, float] | None:
    params = {
        "q": f"{city}, {state}, Brasil",
        "format": "json",
        "limit": 1,
        "countrycodes": "br",
    }
    headers = {"User-Agent": USER_AGENT}
    resp = requests.get(NOMINATIM_URL, params=params, headers=headers, timeout=30)
    resp.raise_for_status()
    results = resp.json()
    if not results:
        logger.warning("Sem resultado Nominatim para %s/%s", city, state)
        return None
    return float(results[0]["lat"]), float(results[0]["lon"])


def main() -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    if not SEED_PATH.exists():
        logger.error("Seed não encontrado: %s", SEED_PATH)
        return 1

    with SEED_PATH.open(newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        rows = list(reader)
        fieldnames = list(reader.fieldnames or [])

    if "latitude" not in fieldnames:
        fieldnames.append("latitude")
    if "longitude" not in fieldnames:
        fieldnames.append("longitude")

    for row in rows:
        if row.get("latitude") and row.get("longitude"):
            logger.info("skip %s — já tem coords", row["city"])
            continue
        coords = geocode_city(row["city"], row.get("state", "RN"))
        if coords:
            row["latitude"], row["longitude"] = f"{coords[0]:.6f}", f"{coords[1]:.6f}"
            logger.info("ok %s → %s, %s", row["city"], coords[0], coords[1])
        else:
            row["latitude"], row["longitude"] = "", ""
        time.sleep(REQUEST_INTERVAL_SEC)

    with SEED_PATH.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    logger.info("Atualizado %s com %d cidades", SEED_PATH, len(rows))
    return 0


if __name__ == "__main__":
    sys.exit(main())
