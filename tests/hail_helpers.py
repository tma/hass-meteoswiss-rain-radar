"""Synthetic ODIM grids at the public projection origin, not a home location."""

from datetime import UTC, datetime, timedelta
from io import BytesIO
from pathlib import Path

import h5py
import numpy as np
from pyproj import Transformer

OBSERVATION = datetime(2026, 9, 9, 8, 25, tzinfo=UTC)
FIXTURES = Path(__file__).parent / "fixtures" / "hail"


def hail_bytes(
    values=None,
    *,
    xscale=500,
    yscale=1000,
    quantity="POH",
    unit="",
    data_unit=None,
    gain=1,
    offset=0,
    nodata=np.nan,
    undetect=0,
    observation=OBSERVATION,
    changes=None,
):
    if values is None:
        values = np.zeros((7, 7))
    values = np.asarray(values)
    height, width = values.shape
    ul_x, ul_y = -width * xscale / 2, height * yscale / 2
    inverse = Transformer.from_crs("EPSG:3857", "EPSG:4326", always_xy=True)
    buffer = BytesIO()
    with h5py.File(buffer, "w") as h5:
        h5.attrs["Conventions"] = "ODIM_H5/V2_4"
        what = h5.create_group("what").attrs
        what.update(
            object="COMP",
            date=observation.strftime("%Y%m%d"),
            time=observation.strftime("%H%M%S"),
        )
        interval = h5.create_group("dataset1/what").attrs
        start = observation - timedelta(minutes=5)
        interval.update(
            product="COMP",
            enddate=observation.strftime("%Y%m%d"),
            endtime=observation.strftime("%H%M%S"),
            startdate=start.strftime("%Y%m%d"),
            starttime=start.strftime("%H%M%S"),
        )
        h5.create_dataset("dataset1/data1/data", data=values)
        attrs = h5.create_group("dataset1/data1/what").attrs
        attrs.update(
            quantity=quantity,
            unit=unit,
            gain=gain,
            offset=offset,
            nodata=nodata,
            undetect=undetect,
        )
        if data_unit is not None:
            h5.create_group("how/MeteoSwiss").attrs["data_unit"] = data_unit
        where = h5.create_group("where").attrs
        where.update(
            projdef="EPSG:3857",
            xsize=width,
            ysize=height,
            xscale=xscale,
            yscale=yscale,
        )
        for corner, x, y in (
            ("UL", ul_x, ul_y),
            ("UR", -ul_x, ul_y),
            ("LL", ul_x, -ul_y),
            ("LR", -ul_x, -ul_y),
        ):
            lon, lat = inverse.transform(x, y)
            where[f"{corner}_lon"], where[f"{corner}_lat"] = lon, lat
        for path, name, value in changes or []:
            h5[path].attrs[name] = value
    return buffer.getvalue()
