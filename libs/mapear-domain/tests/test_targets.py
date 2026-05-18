"""Unit tests for mapear_domain.targets — Stage 2C v1 onboarding library."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from mapear_domain.targets import (
    TargetOperationError,
    TargetSpec,
    add_target,
    list_targets,
    main,
    remove_target,
    validate_region,
)

_BUNDLED_SEEDS = Path(__file__).resolve().parents[1] / "src" / "mapear_domain" / "seeds"


@pytest.fixture()
def seeds_dir(tmp_path: Path) -> Path:
    """Fresh copy of the bundled ``test`` region seeds."""
    dst = tmp_path / "seeds"
    dst.mkdir()
    shutil.copytree(_BUNDLED_SEEDS / "test", dst / "test")
    return dst


def _read_count(seeds_dir: Path) -> int:
    return len(list_targets("test", seeds_dir=seeds_dir))


# --- TargetSpec validation --------------------------------------------------


def test_target_spec_rejects_bad_person_id():
    with pytest.raises(ValueError, match="person_id"):
        TargetSpec(person_id="X-Bad", name="Anyone", role="senator")


def test_target_spec_requires_city_for_mayor():
    with pytest.raises(ValueError, match="role=mayor requires"):
        TargetSpec(person_id="mayor_x", name="Pref X", role="mayor")


def test_target_spec_round_trip_csv():
    spec = TargetSpec(
        person_id="sen_demo",
        name="Demo Senator",
        role="senator",
        aliases=["Demo", "Sen Demo"],
        party="PSB",
        term_start=2024,
        is_incumbent=True,
        x_handle="demo_senator",
    )
    row = spec.to_csv_row()
    rebuilt = TargetSpec.from_csv_row(row)
    assert rebuilt == spec


# --- add_target -------------------------------------------------------------


def test_add_target_appends_row(seeds_dir: Path):
    before = _read_count(seeds_dir)
    spec = TargetSpec(
        person_id="new_dep",
        name="New Deputado",
        role="deputy_federal",
        party="PT",
    )
    result = add_target(spec, "test", seeds_dir=seeds_dir)
    assert result["status"] == "added"
    assert _read_count(seeds_dir) == before + 1


def test_add_target_idempotent_on_identical_row(seeds_dir: Path):
    spec = TargetSpec(
        person_id="new_sen", name="New Senator", role="senator", party="PSB"
    )
    add_target(spec, "test", seeds_dir=seeds_dir)
    again = add_target(spec, "test", seeds_dir=seeds_dir)
    assert again["status"] == "unchanged"


def test_add_target_rejects_duplicate_person_id_different_data(seeds_dir: Path):
    spec1 = TargetSpec(person_id="dup_pid", name="First", role="senator", party="PT")
    spec2 = TargetSpec(
        person_id="dup_pid", name="Different", role="senator", party="PSB"
    )
    add_target(spec1, "test", seeds_dir=seeds_dir)
    with pytest.raises(TargetOperationError, match="already exists"):
        add_target(spec2, "test", seeds_dir=seeds_dir)


def test_add_target_rejects_canonical_name_collision(seeds_dir: Path):
    spec1 = TargetSpec(person_id="pid_a", name="Same Name", role="senator")
    spec2 = TargetSpec(person_id="pid_b", name="Same Name", role="senator")
    add_target(spec1, "test", seeds_dir=seeds_dir)
    with pytest.raises(TargetOperationError, match="already used"):
        add_target(spec2, "test", seeds_dir=seeds_dir)


def test_add_target_rejects_alias_collision_with_existing_canonical(
    seeds_dir: Path,
):
    # Pre-populate a canonical name.
    add_target(
        TargetSpec(person_id="pid_first", name="Carla Souza", role="senator"),
        "test",
        seeds_dir=seeds_dir,
    )
    bad = TargetSpec(
        person_id="pid_second",
        name="Other Person",
        role="senator",
        aliases=["Carla Souza"],
    )
    with pytest.raises(TargetOperationError, match="canonical name"):
        add_target(bad, "test", seeds_dir=seeds_dir)


def test_add_target_rejects_alias_used_by_another_target(seeds_dir: Path):
    add_target(
        TargetSpec(
            person_id="pid_a",
            name="A Person",
            role="senator",
            aliases=["A.P.", "Aperson"],
        ),
        "test",
        seeds_dir=seeds_dir,
    )
    with pytest.raises(TargetOperationError, match="already aliases"):
        add_target(
            TargetSpec(
                person_id="pid_b",
                name="B Person",
                role="senator",
                aliases=["A.P."],
            ),
            "test",
            seeds_dir=seeds_dir,
        )


def test_add_target_mayor_orphan_city_fk_rejected(seeds_dir: Path):
    bad_mayor = TargetSpec(
        person_id="mayor_void",
        name="Prefeito do Vazio",
        role="mayor",
        city="DoesNotExist",
        party="PT",
    )
    with pytest.raises(TargetOperationError, match="not in region"):
        add_target(bad_mayor, "test", seeds_dir=seeds_dir)


def test_add_target_mayor_valid_city_accepted(seeds_dir: Path):
    spec = TargetSpec(
        person_id="mayor_new_testopolis",
        name="Novo Prefeito de Testópolis",
        role="mayor",
        city="Testópolis",  # exists in seeds/test/cities_mayors.csv
        party="PSDB",
    )
    result = add_target(spec, "test", seeds_dir=seeds_dir)
    assert result["status"] == "added"


# --- remove_target ----------------------------------------------------------


def test_remove_target_idempotent_on_missing(seeds_dir: Path):
    out = remove_target("not_a_real_pid", "test", seeds_dir=seeds_dir)
    assert out["status"] == "missing"


def test_remove_target_then_re_add(seeds_dir: Path):
    spec = TargetSpec(person_id="cycle_pid", name="Cycle", role="senator")
    add_target(spec, "test", seeds_dir=seeds_dir)
    out = remove_target("cycle_pid", "test", seeds_dir=seeds_dir)
    assert out["status"] == "removed"
    # Re-add must succeed now that the canonical name is free again.
    again = add_target(spec, "test", seeds_dir=seeds_dir)
    assert again["status"] == "added"


# --- list_targets -----------------------------------------------------------


def test_list_targets_filters_by_role(seeds_dir: Path):
    mayors = list_targets("test", role="mayor", seeds_dir=seeds_dir)
    assert all(t.role == "mayor" for t in mayors)
    senators = list_targets("test", role="senator", seeds_dir=seeds_dir)
    assert all(t.role == "senator" for t in senators)


def test_list_targets_no_filter_returns_all(seeds_dir: Path):
    all_t = list_targets("test", seeds_dir=seeds_dir)
    assert len(all_t) >= 5  # the bundled test seeds have at least 5 rows


# --- validate_region --------------------------------------------------------


def test_validate_region_clean_baseline(seeds_dir: Path):
    report = validate_region("test", seeds_dir=seeds_dir)
    assert report["n_issues"] == 0


def test_validate_region_catches_orphan_city_after_manual_edit(seeds_dir: Path):
    # Write a row manually that bypasses add_target's FK check.
    csv_path = seeds_dir / "test" / "targets.csv"
    text = csv_path.read_text()
    csv_path.write_text(
        text
        + "mayor_orphan,Orphan Mayor,,mayor,Atlantis,PT,2025,2028,true,,,,,Bad row\n"
    )
    report = validate_region("test", seeds_dir=seeds_dir)
    assert report["n_issues"] >= 1
    kinds = {iss["kind"] for iss in report["issues"]}
    assert "orphan_city_fk" in kinds


# --- RN write protection ----------------------------------------------------


def test_rn_writes_refused_by_default(monkeypatch: pytest.MonkeyPatch):
    spec = TargetSpec(person_id="rn_test_pid", name="RN Test", role="senator")
    # Use no seeds_dir override → triggers RN guard.
    with pytest.raises(TargetOperationError, match="canonical and managed via dbt"):
        add_target(spec, "rn")


# --- CLI smoke --------------------------------------------------------------


def test_cli_list_runs(
    capsys: pytest.CaptureFixture[str], seeds_dir: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr("mapear_domain.targets._SEEDS_DIR", seeds_dir)
    exit_code = main(["list", "--region", "test", "--json"])
    assert exit_code == 0
    out = capsys.readouterr().out
    assert "person_id" in out


def test_cli_validate_returns_zero_on_clean_region(
    capsys: pytest.CaptureFixture[str],
    seeds_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr("mapear_domain.targets._SEEDS_DIR", seeds_dir)
    exit_code = main(["validate", "--region", "test"])
    assert exit_code == 0


def test_cli_add_then_remove(
    capsys: pytest.CaptureFixture[str],
    seeds_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr("mapear_domain.targets._SEEDS_DIR", seeds_dir)
    add_code = main(
        [
            "add",
            "--region",
            "test",
            "--person-id",
            "cli_demo",
            "--name",
            "CLI Demo",
            "--role",
            "deputy_federal",
            "--party",
            "PT",
            "--aliases",
            "Demo;Demo Deputado",
        ]
    )
    assert add_code == 0
    remove_code = main(["remove", "--region", "test", "--person-id", "cli_demo"])
    assert remove_code == 0
