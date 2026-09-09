"""Decode ODIM hail grids and measure projected cell-center distances.

ODIM 2.4 tables 5/16 and section 5.2 define outer corners, POH [0, 1],
MESH mm and north-to-south rows. See tests/fixtures/hail/provenance.json.
This reader deliberately does not change the legacy rain decoder.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from io import BytesIO

import h5py
import numpy as np
from pyproj import CRS, Transformer

MAX_CELLS = 4_000_000
CORNER_TOLERANCE_M = 10.0  # Real rounded lon/lat corners disagree by up to 7 m.
DISTANCE_EPSILON_M = 1e-6  # Projection round-off at an exact circle boundary.


def _text(value) -> str:
    return value.decode("ascii") if isinstance(value, bytes) else str(value)


def _timestamp(attrs, date_key: str, time_key: str) -> datetime:
    return datetime.strptime(
        _text(attrs[date_key]) + _text(attrs[time_key]), "%Y%m%d%H%M%S"
    ).replace(tzinfo=UTC)


@dataclass(frozen=True, slots=True)
class HailAnalysis:
    health: str
    detected: bool | None = None
    max_poh: float | None = None
    distance_km: float | None = None
    coverage_complete: bool = False


@dataclass(slots=True)
class HailGrid:
    values: np.ndarray
    quantity: str
    unit: str
    transformer: Transformer
    ul_x: float
    ul_y: float
    xscale: float
    yscale: float

    @classmethod
    def from_bytes(cls, data: bytes, observation: datetime) -> HailGrid | None:
        """Read and validate a five-minute file; None means an empty product."""
        if not data:
            return None
        with h5py.File(BytesIO(data), "r") as h5:
            if _text(h5.attrs["Conventions"]) != "ODIM_H5/V2_4":
                raise ValueError("Unsupported hail ODIM convention")
            if _text(h5["what"].attrs["object"]) != "COMP":
                raise ValueError("Hail grid must be a composite")
            if _timestamp(h5["what"].attrs, "date", "time") != observation:
                raise ValueError("Hail filename and observation timestamp disagree")
            if "dataset1" not in h5:
                return None
            interval = h5["dataset1/what"].attrs
            if (
                _timestamp(interval, "enddate", "endtime") != observation
                or _timestamp(interval, "startdate", "starttime")
                != observation - timedelta(minutes=5)
                or _text(interval["product"]) != "COMP"
            ):
                raise ValueError("Hail grid is not the expected five-minute composite")
            dataset = h5["dataset1/data1/data"]
            if dataset.size == 0:
                return None
            where = h5["where"].attrs
            width, height = int(where["xsize"]), int(where["ysize"])
            if (
                width <= 0
                or height <= 0
                or width * height > MAX_CELLS
                or dataset.shape != (height, width)
                or width != where["xsize"]
                or height != where["ysize"]
                or dataset.dtype.kind not in "fiu"
                or dataset.dtype.itemsize > 8
            ):
                raise ValueError("Invalid hail grid dimensions")
            xscale, yscale = float(where["xscale"]), float(where["yscale"])
            if not all(math.isfinite(v) and v > 0 for v in (xscale, yscale)):
                raise ValueError("Invalid hail cell scales")
            crs = CRS.from_user_input(_text(where["projdef"]))
            if not crs.is_projected or any(
                axis.unit_conversion_factor != 1 for axis in crs.axis_info
            ):
                raise ValueError("Hail projection must use metres")
            transformer = Transformer.from_crs("EPSG:4326", crs, always_xy=True)
            ul_x, ul_y = transformer.transform(where["UL_lon"], where["UL_lat"])
            if not all(math.isfinite(v) for v in (ul_x, ul_y)):
                raise ValueError("Invalid hail grid origin")
            for corner, dx, dy in (
                ("UR", width * xscale, 0),
                ("LL", 0, -height * yscale),
                ("LR", width * xscale, -height * yscale),
            ):
                x, y = transformer.transform(
                    where[f"{corner}_lon"], where[f"{corner}_lat"]
                )
                if not math.hypot(x - ul_x - dx, y - ul_y - dy) <= CORNER_TOLERANCE_M:
                    raise ValueError("Hail corners disagree with scales or row order")

            attrs = h5["dataset1/data1/what"].attrs
            quantity = _text(attrs["quantity"])
            units = {_text(attrs.get("unit", "")).strip()}
            if "how/MeteoSwiss" in h5:
                units.add(
                    _text(h5["how/MeteoSwiss"].attrs.get("data_unit", "")).strip()
                )
            units.discard("")
            if quantity == "POH" and units <= {"1"}:
                factor, unit = 100.0, "%"
            elif quantity == "POH" and units == {"%"}:
                factor, unit = 1.0, "%"
            elif quantity == "MESH" and units == {"mm"}:
                factor, unit = 1.0, "mm"
            else:
                raise ValueError(f"Unsupported hail quantity/units: {quantity}/{units}")

            gain, offset = float(attrs["gain"]), float(attrs["offset"])
            if not math.isfinite(gain) or gain <= 0 or not math.isfinite(offset):
                raise ValueError("Invalid hail gain/offset")
            raw = dataset[:]
            nodata, undetect = float(attrs["nodata"]), float(attrs["undetect"])
            if nodata == undetect:
                raise ValueError("Hail nodata and undetect must differ")
            valid = np.isfinite(raw) & (raw != nodata)
            values = (raw.astype(np.float64) * gain + offset) * factor
            values[valid & (raw == undetect)] = 0.0
            values[~valid] = np.nan

            if quantity == "POH" and factor == 100 and gain == 1 and offset == 0:
                # The official float64 file contains float32 percent * 0.01 values.
                # Normalize only exact encodings, not an isclose threshold band.
                # In particular float32(80) * float32(.01) = .7999999523162842.
                percent = np.rint(values)
                encoded = percent.astype(np.float32) * np.float32(0.01)
                exact_encoding = valid & (raw == encoded.astype(np.float64))
                values[exact_encoding] = percent[exact_encoding]
            if np.any(valid & ~np.isfinite(values)):
                raise ValueError("Nonfinite scaled hail values")
            finite = values[np.isfinite(values)]
            if np.any(finite < 0) or (quantity == "POH" and np.any(finite > 100)):
                raise ValueError("Hail values outside the supported physical range")
        return cls(values, quantity, unit, transformer, ul_x, ul_y, xscale, yscale)

    def detect(
        self, latitude: float, longitude: float, radius_km: float, threshold: float
    ) -> HailAnalysis:
        """Use a true circle, retaining unknown when coverage cannot prove clear."""
        if self.quantity != "POH":
            raise ValueError("Only POH may trigger hail detection")
        x, y = self.transformer.transform(longitude, latitude)
        radius = radius_km * 1000
        if not all(math.isfinite(v) for v in (x, y, radius, threshold)) or radius <= 0:
            raise ValueError("Invalid hail detection location or settings")
        height, width = self.values.shape
        right = self.ul_x + width * self.xscale
        bottom = self.ul_y - height * self.yscale
        epsilon = DISTANCE_EPSILON_M
        if not (
            self.ul_x - epsilon <= x <= right + epsilon
            and bottom - epsilon <= y <= self.ul_y + epsilon
        ):
            return HailAnalysis("outside_grid")

        complete = (
            x - radius >= self.ul_x - epsilon
            and x + radius <= right + epsilon
            and y - radius >= bottom - epsilon
            and y + radius <= self.ul_y + epsilon
        )
        # Intersect the search window before allocating distance arrays.
        left_col = max(0, math.floor((x - radius - self.ul_x) / self.xscale))
        right_col = min(width, math.ceil((x + radius - self.ul_x) / self.xscale))
        top_row = max(0, math.floor((self.ul_y - y - radius) / self.yscale))
        bottom_row = min(height, math.ceil((self.ul_y - y + radius) / self.yscale))
        xs = self.ul_x + (np.arange(left_col, right_col) + 0.5) * self.xscale
        ys = self.ul_y - (np.arange(top_row, bottom_row) + 0.5) * self.yscale
        distances = np.hypot(xs[None, :] - x, ys[:, None] - y)
        circle = distances <= radius + DISTANCE_EPSILON_M
        if not np.any(circle):
            return HailAnalysis("no_cells")
        values = self.values[top_row:bottom_row, left_col:right_col]
        valid = circle & np.isfinite(values)
        if not np.any(valid):
            return HailAnalysis("all_nodata")
        complete = complete and bool(np.all(np.isfinite(values[circle])))
        qualifying = valid & (values >= threshold)
        detected = bool(np.any(qualifying))
        maximum = float(np.max(values[valid]))
        return HailAnalysis(
            health="ok" if complete else "partial_coverage",
            detected=detected if detected or complete else None,
            max_poh=maximum if complete or maximum > 0 else None,
            distance_km=float(np.min(distances[qualifying]) / 1000)
            if detected
            else None,
            coverage_complete=complete,
        )


def read_hail(
    data: bytes,
    observation: datetime,
    latitude: float,
    longitude: float,
    radius_km: float,
    threshold: float,
) -> HailAnalysis:
    """Byte-to-result worker: HDF5, projection and NumPy all run in the executor."""
    grid = HailGrid.from_bytes(data, observation)
    if grid is None:
        return HailAnalysis("empty")
    return grid.detect(latitude, longitude, radius_km, threshold)
