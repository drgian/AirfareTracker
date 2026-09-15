"""Rebuild site/airports.json from OurAirports (public domain): scheduled-service airports with IATA codes.

Each entry: [code, name, city, country ISO code, size rank (0 large, 1 medium, 2 small), search extras].
Search extras add region names (e.g. "Bonaire", "Michigan") and alternate names so people can type them.
Copy the result to /opt/flightfare/api*/airports.json too; the API validates codes against it.
"""
import csv, io, json, re, sys, urllib.request

BASE = "https://davidmegginson.github.io/ourairports-data/"
fetch = lambda name: csv.DictReader(io.StringIO(urllib.request.urlopen(BASE + name).read().decode("utf-8")))
regions = {r["code"]: r["name"] for r in fetch("regions.csv")}
rank = {"large_airport": 0, "medium_airport": 1, "small_airport": 2}
out = []
for r in fetch("airports.csv"):
    code = r["iata_code"]
    if r["scheduled_service"] != "yes" or len(code) != 3 or not code.isalpha() or r["type"] not in rank:
        continue
    city = re.sub(r"\s*\(.*?\)\s*", " ", r["municipality"]).strip() or r["name"]
    region = regions.get(r["iso_region"], "")
    region = "" if region.startswith("(unassigned)") or region == city else region
    extra = " ".join(x for x in (region, r["keywords"].replace(",", " "), r["municipality"]) if x).strip()
    out.append([code, r["name"].strip(), city, r["iso_country"], rank[r["type"]], extra])
out.sort(key=lambda a: (a[4], a[0]))
dest = sys.argv[1] if len(sys.argv) > 1 else "site/airports.json"
json.dump(out, open(dest, "w", encoding="utf-8"), ensure_ascii=False, separators=(",", ":"))
print(f"{len(out)} airports -> {dest}")
