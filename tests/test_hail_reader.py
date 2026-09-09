"""Real and synthetic metadata/geometry tests; no network or personal location."""

import hashlib
import json
from datetime import timedelta
from io import BytesIO

import h5py
import numpy as np
import pytest

from custom_components.meteoswiss_rain_radar.hail_reader import HailGrid, read_hail

from .hail_helpers import FIXTURES, OBSERVATION, hail_bytes


@pytest.mark.parametrize(
    "index,quantity,unit,maximum", [(0, "POH", "%", 100), (1, "MESH", "mm", 40)]
)
def test_official_files(index, quantity, unit, maximum):
    metadata = json.loads((FIXTURES / "provenance.json").read_text())["fixtures"][index]
    content = (FIXTURES / metadata["name"]).read_bytes()
    assert len(content) == metadata["bytes"]
    assert hashlib.sha256(content).hexdigest() == metadata["sha256"]
    assert metadata["stac_checksum"] == "1220" + metadata["sha256"]
    grid = HailGrid.from_bytes(content, OBSERVATION)
    assert (grid.quantity, grid.unit) == (quantity, unit)
    assert grid.values.shape == (640, 710)
    assert np.count_nonzero(np.isnan(grid.values)) == 101335
    assert np.nanmax(grid.values) == maximum
    assert (grid.xscale, grid.yscale) == (1000, 1000)
    assert grid.ul_x == pytest.approx(2255000.232908, abs=0.01)
    assert grid.ul_y == pytest.approx(1480003.009772, abs=0.01)
    if quantity == "POH":
        assert np.count_nonzero(grid.values >= 80) == 63
    else:
        with pytest.raises(ValueError, match="Only POH"):
            grid.detect(0, 0, 10, 80)


def test_gain_offset_nodata_nan_and_undetect_before_scaling():
    content = hail_bytes(
        [[0, 40, 80, 255, np.nan, np.inf]],
        gain=0.01,
        offset=0.1,
        nodata=255,
        undetect=0,
    )
    grid = HailGrid.from_bytes(content, OBSERVATION)
    np.testing.assert_allclose(grid.values, [[0, 50, 90, np.nan, np.nan, np.nan]])


@pytest.mark.parametrize(
    "unit,values,gain,offset",
    [
        ("%", [[0, 80, 100]], 1, 0),
        ("1", [[0, 0.8, 1]], 1, 0),
        ("mm", [[0, 10, 20]], 2, 3),
    ],
)
def test_explicit_units(unit, values, gain, offset):
    quantity = "MESH" if unit == "mm" else "POH"
    grid = HailGrid.from_bytes(
        hail_bytes(values, quantity=quantity, unit=unit, gain=gain, offset=offset),
        OBSERVATION,
    )
    np.testing.assert_allclose(
        grid.values, [[0, 23, 43]] if unit == "mm" else [[0, 80, 100]]
    )


@pytest.mark.parametrize(
    "kwargs",
    [
        {"quantity": "MESH", "unit": "cm"},
        {"quantity": "MESH", "unit": ""},
        {"quantity": "MESH", "unit": "mm", "data_unit": "cm"},
        {"quantity": "POH", "unit": "%", "data_unit": "1"},
        {"quantity": "OTHER"},
        {"gain": 0},
        {"gain": np.nan},
        {"offset": np.inf},
        {"nodata": 0},
        {"values": [[-0.1]]},
        {"values": [[1.01]]},
        {"xscale": -500},
    ],
)
def test_reject_unsupported_units_or_metadata(kwargs):
    with pytest.raises(ValueError):
        HailGrid.from_bytes(hail_bytes(**kwargs), OBSERVATION)


@pytest.mark.parametrize(
    "changes",
    [
        [("where", "xsize", 8)],
        [("where", "yscale", 2000)],
        [("where", "UL_lat", -1)],
        [("where", "projdef", "EPSG:4326")],
        [("dataset1/what", "starttime", "000000")],
        [("dataset1/what", "endtime", "082000")],
        [("what", "object", "SCAN")],
        [("/", "Conventions", "other")],
    ],
)
def test_reject_dimensions_transform_orientation_and_interval(changes):
    with pytest.raises(ValueError):
        HailGrid.from_bytes(hail_bytes(changes=changes), OBSERVATION)


def test_timestamp_must_match_asset():
    with pytest.raises(ValueError, match="timestamp"):
        HailGrid.from_bytes(hail_bytes(), OBSERVATION + timedelta(minutes=5))


def test_empty_files_and_malformed_are_distinct():
    assert read_hail(b"", OBSERVATION, 0, 0, 10, 80).health == "empty"
    assert HailGrid.from_bytes(hail_bytes(np.empty((0, 0))), OBSERVATION) is None
    buffer = BytesIO(hail_bytes())
    with h5py.File(buffer, "r+") as h5:
        del h5["dataset1"]
    assert HailGrid.from_bytes(buffer.getvalue(), OBSERVATION) is None
    with pytest.raises(OSError):
        HailGrid.from_bytes(b"not hdf5", OBSERVATION)


def test_exact_float32_percent_encoding_not_a_threshold_tolerance():
    nominal = float(np.float32(80) * np.float32(0.01))
    below = np.nextafter(nominal, -np.inf)
    grid = HailGrid.from_bytes(
        hail_bytes([[nominal, below, 0.7999999, 0.8]]), OBSERVATION
    )
    assert grid.values[0, 0] == 80
    assert grid.values[0, 1] < 80
    assert grid.values[0, 2] < 80
    assert grid.values[0, 3] == 80
    content = hail_bytes(np.full((7, 7), nominal))
    assert read_hail(content, OBSERVATION, 0, 0, 1, 80).detected is True
    assert read_hail(content, OBSERVATION, 0, 0, 1, 80.000001).detected is False
    # Scaled integer and percent-unit files do not use fractional float normalization.
    grid = HailGrid.from_bytes(hail_bytes([[79.99999523162842]], unit="%"), OBSERVATION)
    assert grid.values[0, 0] < 80


def test_circular_fractional_radius_nonsquare_cells_and_nearest_distance():
    values = np.zeros((7, 7))
    values[2, 4] = 1  # NE diagonal center: sqrt(500^2 + 1000^2) m, not a square.
    values[3, 5] = 0.8  # East: 1000 m.
    content = hail_bytes(values)
    assert read_hail(content, OBSERVATION, 0, 0, 0.999, 80).detected is False
    result = read_hail(content, OBSERVATION, 0, 0, 1.0, 80)
    assert result.detected is True
    assert result.max_poh == 80
    assert result.distance_km == pytest.approx(1.0)
    result = read_hail(content, OBSERVATION, 0, 0, 1.119, 80)
    assert result.max_poh == 100
    assert result.distance_km == pytest.approx(1.0)


def test_row_order_and_half_cell_centers_without_location_snapping():
    values = np.zeros((7, 7))
    values[1, 3] = 0.9
    grid = HailGrid.from_bytes(hail_bytes(values), OBSERVATION)
    # Use a cell-relative synthetic location, 100 m east of row 1's center.
    from pyproj.enums import TransformDirection

    longitude, latitude = grid.transformer.transform(
        grid.ul_x + 3.5 * grid.xscale + 100,
        grid.ul_y - 1.5 * grid.yscale,
        direction=TransformDirection.INVERSE,
    )
    result = grid.detect(latitude, longitude, 0.2, 80)
    assert result.detected is True
    assert result.distance_km == pytest.approx(0.1)
    result = grid.detect(-latitude, longitude, 0.2, 80)
    assert result.detected is False


@pytest.mark.parametrize(
    "missing,positive,health,detected,maximum",
    [
        (False, False, "ok", False, 0),
        (True, False, "partial_coverage", None, None),
        (True, True, "partial_coverage", True, 90),
    ],
)
def test_clear_and_partial_coverage(missing, positive, health, detected, maximum):
    values = np.zeros((7, 7))
    if missing:
        values[3, 2] = np.nan
    if positive:
        values[3, 3] = 0.9
    result = read_hail(hail_bytes(values), OBSERVATION, 0, 0, 1, 80)
    assert result.health == health
    assert result.detected is detected
    assert result.max_poh == maximum
    assert result.coverage_complete is (not missing)
    if positive:
        assert result.distance_km == pytest.approx(0, abs=1e-12)
    else:
        assert result.distance_km is None


def test_grid_edge_outside_all_nodata_and_no_cell_centers():
    clear = hail_bytes()
    result = read_hail(clear, OBSERVATION, 0, 0, 2, 80)
    assert result.health == "partial_coverage"
    assert result.detected is None
    result = read_hail(clear, OBSERVATION, 1, 1, 10, 80)
    assert result.health == "outside_grid"
    assert result.max_poh is None
    result = read_hail(hail_bytes(np.full((7, 7), np.nan)), OBSERVATION, 0, 0, 1, 80)
    assert result.health == "all_nodata"
    assert result.detected is None and result.max_poh is None
    result = read_hail(hail_bytes(np.zeros((2, 2))), OBSERVATION, 0, 0, 0.1, 80)
    assert result.health == "no_cells"
    assert result.detected is None and result.distance_km is None


def test_partial_nonqualifying_positive_is_not_a_clear_signal():
    values = np.zeros((7, 7))
    values[3, 2] = np.nan
    values[3, 3] = 0.5
    result = read_hail(hail_bytes(values), OBSERVATION, 0, 0, 1, 80)
    assert result.health == "partial_coverage"
    assert result.detected is None
    assert result.max_poh == 50
    assert result.distance_km is None
    assert not result.coverage_complete
