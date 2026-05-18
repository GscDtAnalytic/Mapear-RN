"""Tests for the Region abstraction and load_region()."""

from pathlib import Path

import pytest

from mapear_domain.region import Politician, Region, load_region

# ---------------------------------------------------------------------------
# Synthetic seed helpers
# ---------------------------------------------------------------------------

_CM_CSV = """\
city,state,population,mayor,party
Testópolis,TX,50000,Prefeito Silva,PT
Vilafake,TX,20000,Prefeita Souza,MDB
"""

_GOV_CSV = """\
name,role,state,party,term_start,term_end
Governador Teste,governor,TX,PT,2023,2026
Ex-Governador,former_governor,TX,PSDB,2019,2022
"""

_CAND_CSV = """\
name,party,is_incumbent
Candidato A,PT,true
Candidato B,PSDB,false
"""

_ALIASES = """\
{
  "city_aliases": {"testopolis": "Testópolis"},
  "mayor_aliases": {"silva": "Prefeito Silva"},
  "governor_aliases": {"gov teste": "Governador Teste"},
  "politician_aliases": {"pol x": "Político X"}
}
"""

_TARGETS_CSV = """\
person_id,name,aliases,role,city,party,term_start,term_end,is_incumbent,facebook_page,instagram_username,x_handle,tiktok_handle,notes
gov_test_alice,Alice Governadora,Alice;Governadora Alice,governor,,PT,2023,2026,true,alicegovfb,alicegov,alicegov,,Governadora sintética
mayor_test_bob,Bob Prefeito,Bob;Bobão,mayor,Testópolis,MDB,2025,2028,true,,bobprefeito,,bobprefeito,Prefeito sintético
mayor_test_carol,Carol Prefeita,Carol,mayor,Vilafake,PSDB,2025,2028,true,,carolprefeita,,,Prefeita sintética
mayor_test_no_handles,No Handles Mayor,,mayor,Testópolis,PT,2025,2028,false,,,,, Prefeito sem handles
cand_test_mixed_case,Mixed Case Cand,,governor_candidate,,PT,,,false,MixedFB,MixedIG,MixedX,MixedTT,Teste normalização case
"""


@pytest.fixture()
def seed_dir(tmp_path: Path) -> Path:
    d = tmp_path / "myregion"
    d.mkdir()
    (d / "cities_mayors.csv").write_text(_CM_CSV, encoding="utf-8")
    (d / "governor.csv").write_text(_GOV_CSV, encoding="utf-8")
    (d / "governor_candidates.csv").write_text(_CAND_CSV, encoding="utf-8")
    (d / "aliases.json").write_text(_ALIASES, encoding="utf-8")
    (d / "targets.csv").write_text(_TARGETS_CSV, encoding="utf-8")
    return tmp_path


# ---------------------------------------------------------------------------
# load_region
# ---------------------------------------------------------------------------


def test_load_region_returns_region(seed_dir):
    region = load_region("myregion", seeds_base=seed_dir)
    assert isinstance(region, Region)
    assert region.id == "myregion"


def test_load_region_missing_dir_returns_empty_region(tmp_path):
    region = load_region("nonexistent", seeds_base=tmp_path)
    assert region.id == "nonexistent"
    assert region.cities_mayors_rows == []


# ---------------------------------------------------------------------------
# get_city_names
# ---------------------------------------------------------------------------


def test_get_city_names_includes_csv_and_aliases(seed_dir):
    region = load_region("myregion", seeds_base=seed_dir)
    names = region.get_city_names()
    assert "Testópolis" in names
    assert "Vilafake" in names
    # alias canonical value
    assert "Testópolis" in names  # "testopolis" alias → "Testópolis"


def test_get_city_names_alias_canonical_present(seed_dir):
    region = load_region("myregion", seeds_base=seed_dir)
    assert "Testópolis" in region.get_city_names()


# ---------------------------------------------------------------------------
# get_mayor_names
# ---------------------------------------------------------------------------


def test_get_mayor_names_from_csv(seed_dir):
    region = load_region("myregion", seeds_base=seed_dir)
    assert "Prefeito Silva" in region.get_mayor_names()
    assert "Prefeita Souza" in region.get_mayor_names()


def test_get_mayor_names_includes_aliases(seed_dir):
    region = load_region("myregion", seeds_base=seed_dir)
    assert "Prefeito Silva" in region.get_mayor_names()  # alias canonical


# ---------------------------------------------------------------------------
# get_governor_names (wide set)
# ---------------------------------------------------------------------------


def test_get_governor_names_includes_incumbent(seed_dir):
    region = load_region("myregion", seeds_base=seed_dir)
    assert "Governador Teste" in region.get_governor_names()


def test_get_governor_names_includes_former(seed_dir):
    region = load_region("myregion", seeds_base=seed_dir)
    assert "Ex-Governador" in region.get_governor_names()


def test_get_governor_names_includes_candidates(seed_dir):
    region = load_region("myregion", seeds_base=seed_dir)
    names = region.get_governor_names()
    assert "Candidato A" in names
    assert "Candidato B" in names


def test_get_governor_names_includes_politician_aliases(seed_dir):
    region = load_region("myregion", seeds_base=seed_dir)
    assert "Político X" in region.get_governor_names()


# ---------------------------------------------------------------------------
# get_incumbent_governor_names (strong signal)
# ---------------------------------------------------------------------------


def test_get_incumbent_governor_names_only_governor_role(seed_dir):
    region = load_region("myregion", seeds_base=seed_dir)
    incumbents = region.get_incumbent_governor_names()
    assert "Governador Teste" in incumbents
    assert "Ex-Governador" not in incumbents


# ---------------------------------------------------------------------------
# get_governor_candidate_names
# ---------------------------------------------------------------------------


def test_get_governor_candidate_names(seed_dir):
    region = load_region("myregion", seeds_base=seed_dir)
    candidates = region.get_governor_candidate_names()
    assert "Candidato A" in candidates
    assert "Candidato B" in candidates


# ---------------------------------------------------------------------------
# get_party_names
# ---------------------------------------------------------------------------


def test_get_party_names(seed_dir):
    region = load_region("myregion", seeds_base=seed_dir)
    parties = region.get_party_names()
    assert "PT" in parties
    assert "MDB" in parties
    assert "PSDB" in parties  # from governor CSV


# ---------------------------------------------------------------------------
# normalize_entity
# ---------------------------------------------------------------------------


def test_normalize_entity_city_alias(seed_dir):
    region = load_region("myregion", seeds_base=seed_dir)
    assert region.normalize_entity("testopolis") == "Testópolis"


def test_normalize_entity_mayor_alias(seed_dir):
    region = load_region("myregion", seeds_base=seed_dir)
    assert region.normalize_entity("silva") == "Prefeito Silva"


def test_normalize_entity_governor_alias(seed_dir):
    region = load_region("myregion", seeds_base=seed_dir)
    assert region.normalize_entity("gov teste") == "Governador Teste"


def test_normalize_entity_unknown_passthrough(seed_dir):
    region = load_region("myregion", seeds_base=seed_dir)
    assert region.normalize_entity("Unknown Name") == "Unknown Name"


# ---------------------------------------------------------------------------
# load_seed_data (compat)
# ---------------------------------------------------------------------------


def test_load_seed_data_returns_rows(seed_dir):
    region = load_region("myregion", seeds_base=seed_dir)
    rows = region.load_seed_data()
    assert len(rows) == 2
    assert rows[0]["city"] == "Testópolis"


# ---------------------------------------------------------------------------
# get_politicians / Politician model
# ---------------------------------------------------------------------------


def test_get_politicians_returns_all(seed_dir):
    region = load_region("myregion", seeds_base=seed_dir)
    pols = region.get_politicians()
    assert len(pols) == 5
    assert all(isinstance(p, Politician) for p in pols)


def test_get_politicians_by_role_mayor(seed_dir):
    region = load_region("myregion", seeds_base=seed_dir)
    mayors = region.get_politicians_by_role("mayor")
    assert len(mayors) == 3
    ids = {p.person_id for p in mayors}
    assert ids == {"mayor_test_bob", "mayor_test_carol", "mayor_test_no_handles"}


def test_get_politicians_by_role_governor(seed_dir):
    region = load_region("myregion", seeds_base=seed_dir)
    governors = region.get_politicians_by_role("governor")
    assert len(governors) == 1
    assert governors[0].person_id == "gov_test_alice"


def test_get_politician_by_handle_found(seed_dir):
    region = load_region("myregion", seeds_base=seed_dir)
    pol = region.get_politician_by_handle("instagram", "bobprefeito")
    assert pol is not None
    assert pol.person_id == "mayor_test_bob"


def test_get_politician_by_handle_not_found(seed_dir):
    region = load_region("myregion", seeds_base=seed_dir)
    assert region.get_politician_by_handle("instagram", "nobody") is None


def test_get_city_for_person_id_found(seed_dir):
    region = load_region("myregion", seeds_base=seed_dir)
    assert region.get_city_for_person_id("mayor_test_bob") == "Testópolis"


def test_get_city_for_person_id_none_for_governor(seed_dir):
    region = load_region("myregion", seeds_base=seed_dir)
    assert region.get_city_for_person_id("gov_test_alice") is None


def test_get_city_for_person_id_missing(seed_dir):
    region = load_region("myregion", seeds_base=seed_dir)
    assert region.get_city_for_person_id("nonexistent") is None


def test_politician_aliases_parsed(seed_dir):
    region = load_region("myregion", seeds_base=seed_dir)
    bob = next(p for p in region.politicians if p.person_id == "mayor_test_bob")
    assert "Bob" in bob.aliases
    assert "Bobão" in bob.aliases


def test_politician_handles_populated(seed_dir):
    region = load_region("myregion", seeds_base=seed_dir)
    alice = next(p for p in region.politicians if p.person_id == "gov_test_alice")
    assert alice.handles["instagram"] == "alicegov"
    assert alice.handles["facebook"] == "alicegovfb"
    assert alice.handles["x"] == "alicegov"
    assert "tiktok" not in alice.handles


def test_politician_mandate_years(seed_dir):
    region = load_region("myregion", seeds_base=seed_dir)
    bob = next(p for p in region.politicians if p.person_id == "mayor_test_bob")
    assert bob.mandate_start == 2025
    assert bob.mandate_end == 2028
    assert bob.is_incumbent is True


def test_politician_city_none_for_governor(seed_dir):
    region = load_region("myregion", seeds_base=seed_dir)
    alice = next(p for p in region.politicians if p.person_id == "gov_test_alice")
    assert alice.city is None


# ---------------------------------------------------------------------------
# Bundled "test" region
# ---------------------------------------------------------------------------


def test_bundled_test_region_loads():
    region = load_region("test")
    assert region.id == "test"
    assert len(region.cities_mayors_rows) > 0
    assert len(region.governor_rows) > 0


def test_bundled_test_region_has_politicians():
    region = load_region("test")
    assert len(region.politicians) > 0
    mayors = region.get_politicians_by_role("mayor")
    assert len(mayors) >= 2


# ---------------------------------------------------------------------------
# Handles — normalização e case-insensitive (Ajuste 2)
# ---------------------------------------------------------------------------


def test_politician_with_all_blank_handles_has_empty_dict(seed_dir):
    region = load_region("myregion", seeds_base=seed_dir)
    p = next(p for p in region.politicians if p.person_id == "mayor_test_no_handles")
    assert p.handles == {}


def test_handles_normalized_to_lowercase_on_load(seed_dir):
    region = load_region("myregion", seeds_base=seed_dir)
    p = next(p for p in region.politicians if p.person_id == "cand_test_mixed_case")
    assert p.handles["facebook"] == "mixedfb"
    assert p.handles["instagram"] == "mixedig"
    assert p.handles["x"] == "mixedx"
    assert p.handles["tiktok"] == "mixedtt"


def test_get_politician_by_handle_case_insensitive_platform(seed_dir):
    region = load_region("myregion", seeds_base=seed_dir)
    pol = region.get_politician_by_handle("Instagram", "bobprefeito")
    assert pol is not None
    assert pol.person_id == "mayor_test_bob"


def test_get_politician_by_handle_case_insensitive_handle(seed_dir):
    region = load_region("myregion", seeds_base=seed_dir)
    pol = region.get_politician_by_handle("instagram", "BobPrefeito")
    assert pol is not None
    assert pol.person_id == "mayor_test_bob"


def test_get_politician_by_handle_normalizes_both(seed_dir):
    region = load_region("myregion", seeds_base=seed_dir)
    pol1 = region.get_politician_by_handle("instagram", "mixedig")
    pol2 = region.get_politician_by_handle("Instagram", "MixedIG")
    pol3 = region.get_politician_by_handle("INSTAGRAM", "MIXEDIG")
    assert pol1 is not None
    assert pol1 == pol2 == pol3
    assert pol1.person_id == "cand_test_mixed_case"
