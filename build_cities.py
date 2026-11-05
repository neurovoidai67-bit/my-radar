import json
import urllib.parse
import urllib.request
from pathlib import Path

# OpenStreetMap Overpass query for named populated places in Ukraine.
QUERY = r"""
[out:json][timeout:120];
area["ISO3166-1"="UA"][admin_level=2]->.ua;
(
  node["place"~"^(city|town|village|hamlet)$"](area.ua);
);
out body;
"""

ENDPOINT = "https://overpass-api.de/api/interpreter"
OUTPUT = Path(__file__).resolve().parent / "cities.json"

request = urllib.request.Request(
    ENDPOINT,
    data=urllib.parse.urlencode({"data": QUERY}).encode("utf-8"),
    headers={"User-Agent": "RegionalReferenceMap/1.0"},
)

print("Requesting populated-place coordinates from OpenStreetMap...")
with urllib.request.urlopen(request, timeout=180) as response:
    payload = json.loads(response.read().decode("utf-8"))

unique = {}
for item in payload.get("elements", []):
    tags = item.get("tags", {})
    name = tags.get("name:uk") or tags.get("name") or tags.get("name:ru")
    if not name or "lat" not in item or "lon" not in item:
        continue

    lat = float(item["lat"])
    lon = float(item["lon"])

    # Broad geographic sanity bounds for Ukraine; not used as live tracking.
    if not (44.0 <= lat <= 53.0 and 22.0 <= lon <= 41.0):
        continue

    key = (name.casefold(), round(lat, 5), round(lon, 5))
    unique[key] = {
        "name": name,
        "lat": lat,
        "lon": lon,
        "place_type": tags.get("place", "unknown"),
    }

cities = sorted(unique.values(), key=lambda row: row["name"].casefold())
OUTPUT.write_text(
    json.dumps(cities, ensure_ascii=False, indent=2),
    encoding="utf-8",
)

print(f"Saved {len(cities)} populated places to {OUTPUT.name}")
print("The count depends on current OpenStreetMap coverage; it is not hard-coded.")
