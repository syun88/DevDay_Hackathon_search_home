"""Pure-Python reference core. No listings are fabricated or downloaded."""
from math import asin, cos, radians, sin, sqrt


def haversine_km(a_lat: float, a_lon: float, b_lat: float, b_lon: float) -> float:
    """Great-circle distance (km), NOT road or transit distance."""
    p1, p2 = radians(a_lat), radians(b_lat)
    dp, dl = p2 - p1, radians(b_lon - a_lon)
    h = sin(dp / 2)**2 + cos(p1) * cos(p2) * sin(dl / 2)**2
    return 2 * 6371.0088 * asin(min(1.0, sqrt(h)))


def filter_listings(listings, origins, rent_cap_yen, combine="all"):
    """origins=[(lat,lon,max_km), ...]; returns provably matching listings.

    listings contain lat/lon/rent_yen/management_fee_yen; unknown values are excluded.
    This is only a verified, straight-line hard filter; route times require an API.
    """
    if combine not in {"all", "any"}:
        raise ValueError("combine must be 'all' or 'any'")
    output = []
    for item in listings:
        needed = ("lat", "lon", "rent_yen", "management_fee_yen")
        if any(item.get(key) is None for key in needed):
            continue
        cost = item["rent_yen"] + item["management_fee_yen"]
        if cost > rent_cap_yen:
            continue
        checks = [haversine_km(lat, lon, item["lat"], item["lon"]) <= km
                  for lat, lon, km in origins]
        if not checks or not (all(checks) if combine == "all" else any(checks)):
            continue
        output.append({**item, "monthly_base_yen": cost})
    return output


if __name__ == "__main__":
    # Example tests only; not actual rental ads.
    assert haversine_km(35.681236, 139.767125, 35.681236, 139.767125) == 0.0
    sample = [{"lat": 35.681236, "lon": 139.767125,
               "rent_yen": 70000, "management_fee_yen": 5000, "source_url": "example-only"}]
    assert len(filter_listings(sample, [(35.681236, 139.767125, 3)], 80000)) == 1
    assert filter_listings(sample, [(35.681236, 139.767125, 3)], 74000) == []
    assert filter_listings([{**sample[0], "management_fee_yen": None}],
                           [(35.681236, 139.767125, 3)], 80000) == []
    print("OK: distance/cost/unknown-field tests passed")
