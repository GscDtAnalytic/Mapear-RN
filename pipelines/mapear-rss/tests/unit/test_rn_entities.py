"""Tests for entity loading and normalization using the synthetic test Region."""

from mapear_domain.region import load_region

_REGION = load_region("test")


class TestSeedData:
    def test_load_seed_returns_data(self) -> None:
        data = _REGION.load_seed_data()
        assert len(data) == 3  # Testópolis, Vilafake, Cidadezinha

    def test_seed_has_required_columns(self) -> None:
        data = _REGION.load_seed_data()
        for row in data:
            assert "city" in row
            assert "mayor" in row
            assert "party" in row
            assert "population" in row

    def test_testopolis_is_present(self) -> None:
        assert "Testópolis" in _REGION.get_city_names()

    def test_all_parties_present(self) -> None:
        parties = _REGION.get_party_names()
        assert len(parties) > 0
        assert "PT" in parties


class TestNormalization:
    def test_city_alias(self) -> None:
        assert _REGION.normalize_entity("testopolis") == "Testópolis"
        assert _REGION.normalize_entity("testópolis") == "Testópolis"

    def test_mayor_alias(self) -> None:
        assert _REGION.normalize_entity("joao teste") == "João Teste"
        assert _REGION.normalize_entity("j. teste") == "João Teste"

    def test_unknown_entity_returns_stripped(self) -> None:
        assert _REGION.normalize_entity("  Caicó  ") == "Caicó"
