#!/usr/bin/env python3
"""Couche Geoapify pour le pipeline S6 (utilisée par l'app Streamlit).

Remplace les trois accès OpenStreetMap de `s6_auto.research` par des services
à clé / sans quota partagé par IP, avec repli automatique sur OSM — mêmes
sources et même logique que webapp/s6.html :

  - géocodage        : API Adresse de l'État (BAN, data.gouv.fr) -> repli Nominatim
  - espaces verts    : Geoapify Places (+ place-details pour le bord) -> repli Overpass
  - itinéraire piéton: Geoapify Routing (mode=walk) -> repli OSRM

Le patch est installé à l'import (une fois) : `research()` appelle ces
primitives par leur nom de module, donc toute sa logique de sélection,
contrôles et seuil est réutilisée sans duplication.

La clé Geoapify est fournie par l'appelant via `set_key()` — jamais codée en
dur ni committée. Sans clé, seul le géocodage BAN est actif (déjà meilleur que
Nominatim pour la France) ; espaces verts et itinéraire restent sur OSM.
"""

import urllib.parse

import build_report
import s6_auto

_KEY = ""

# Sources réellement utilisées au dernier appel (pour une mention honnête dans
# le PDF). Mises à jour par les fonctions ci-dessous.
last = {"geocode": "Nominatim (OpenStreetMap)", "places": "Overpass (OpenStreetMap)"}

# Primitives OSM d'origine, capturées une fois pour le repli.
_orig_geocode = s6_auto.geocode
_orig_green = s6_auto.find_green_spaces
_orig_route = s6_auto.walk_route


def set_key(key):
    """Définit la clé Geoapify pour les prochains appels (chaîne vide = OSM)."""
    global _KEY
    _KEY = (key or "").strip()


# ---------------------------------------------------------------------------
# Géocodage : BAN (France, sans quota) -> repli Nominatim
# ---------------------------------------------------------------------------

def _ban_geocode(address):
    url = ("https://api-adresse.data.gouv.fr/search/?limit=1&q="
           + urllib.parse.quote(address))
    data = s6_auto.http_json(url, timeout=8)
    feats = data.get("features") or []
    if not feats:
        raise RuntimeError("BAN : aucun résultat")
    f = feats[0]
    p = f.get("properties", {})
    if p.get("score", 0) < 0.4:
        raise RuntimeError("BAN : score trop faible")
    lon, lat = f["geometry"]["coordinates"]
    vague = p.get("type") in ("municipality", "locality")
    scale = "numéro" if p.get("type") == "housenumber" else p.get("type", "?")
    return float(lat), float(lon), vague, scale


def geocode(address):
    try:
        res = _ban_geocode(address)
        last["geocode"] = "API Adresse (BAN, data.gouv.fr)"
        return res
    except Exception:
        last["geocode"] = "Nominatim (OpenStreetMap)"
        return _orig_geocode(address)


# ---------------------------------------------------------------------------
# Espaces verts : Geoapify Places (+ bord via place-details) -> repli Overpass
# ---------------------------------------------------------------------------

# OSM tague parfois une place minérale en leisure=park (ex. « Place José Martí »).
# En français, « Place / Placette / Parvis / Rond-point / Esplanade / Cours »
# désignent des espaces le plus souvent non praticables comme espace vert, alors
# que « Parc / Jardin / Square / Bois / Promenade » sont verts. On DÉPRIORISE ces
# noms (named=False) : un espace clairement vert est préféré quand il en existe un
# sous le seuil, mais la place reste candidate en dernier recours — on ne perd
# donc pas les cas légitimes (Place des Vosges = vrai jardin).
_NON_GREEN_PREFIXES = ("place ", "placette ", "parvis ", "rond-point",
                       "rond point", "esplanade ", "cours ")


def _is_non_green_square(name):
    n = (name or "").strip().lower()
    return any(n.startswith(p) for p in _NON_GREEN_PREFIXES)

def _ga_places(lat, lon, radius, categories, limit=50):
    url = ("https://api.geoapify.com/v2/places?categories=" + categories
           + f"&filter=circle:{lon},{lat},{int(radius)}&limit={limit}"
           + "&apiKey=" + urllib.parse.quote(_KEY))
    data = s6_auto.http_json(url, timeout=20)
    out = []
    for f in data.get("features", []):
        pr = f.get("properties", {})
        if pr.get("lat") is None:
            continue
        out.append({
            "raw": (pr.get("datasource") or {}).get("raw") or {},
            "name": pr.get("name"),
            "lat": float(pr["lat"]), "lon": float(pr["lon"]),
            "categories": pr.get("categories") or [],
            "id": pr.get("place_id"),
        })
    return out


def _ga_outline_point(place_id, lat, lon):
    """Point du contour Geoapify le plus proche de l'actif (l'API Places ne
    renvoie qu'un centre : le bord d'une grande plage/parc peut être bien plus
    près). None si un seul point (= simple centre)."""
    url = ("https://api.geoapify.com/v2/place-details?id="
           + urllib.parse.quote(place_id) + "&apiKey=" + urllib.parse.quote(_KEY))
    data = s6_auto.http_json(url, timeout=15)
    pts = []

    def walk(c):
        if isinstance(c, list) and c and isinstance(c[0], (int, float)):
            pts.append((c[1], c[0]))  # GeoJSON = [lon, lat]
        elif isinstance(c, list):
            for x in c:
                walk(x)

    for f in data.get("features", []):
        g = f.get("geometry")
        if g:
            walk(g.get("coordinates"))
    if len(pts) < 2:
        return None
    return min(pts, key=lambda p: build_report.haversine_m(lat, lon, p[0], p[1]))


def find_green_spaces(lat, lon, radius):
    if not _KEY:
        last["places"] = "Overpass (OpenStreetMap)"
        return _orig_green(lat, lon, radius)
    try:
        feats = _ga_places(lat, lon, radius, "leisure.park,national_park,beach", 50)
        best = {}
        for p in feats:
            raw = p["raw"]
            if raw.get("access") in ("private", "no", "customers"):
                continue
            kind = (raw.get("leisure") or raw.get("natural") or raw.get("landuse")
                    or ("beach" if "beach" in p["categories"] else ""))
            real_name = p["name"] or raw.get("name")
            name = real_name or "Espace vert (sans nom OSM)"
            # named = nom d'un vrai espace vert (une « Place… » minérale est
            # dépriorisée pour ne pas passer devant un parc/jardin/square voisin).
            named = bool(real_name) and not _is_non_green_square(real_name)
            s = {
                "name": name,
                "type": s6_auto.PARK_TYPES_FR.get(kind, "Espace vert"),
                "lat": p["lat"], "lon": p["lon"],
                "named": named,
                "crow_m": build_report.haversine_m(lat, lon, p["lat"], p["lon"]),
                # Pas de bbox/osm_id Geoapify : le bord est déjà affiné ci-dessous,
                # ce qui neutralise refine_park_target (qui exige un way OSM).
                "bbox": None, "osm_type": None, "osm_id": None,
                "_ga_id": p["id"],
            }
            if name not in best or s["crow_m"] < best[name]["crow_m"]:
                best[name] = s
        spaces = sorted(best.values(), key=lambda s: (not s["named"], s["crow_m"]))
        # Ramener les 6 candidats de tête à leur bord réel avant la sélection.
        for s in spaces[:6]:
            if not s.get("_ga_id"):
                continue
            try:
                pt = _ga_outline_point(s["_ga_id"], lat, lon)
                if pt:
                    s["lat"], s["lon"] = float(pt[0]), float(pt[1])
                    s["crow_m"] = build_report.haversine_m(lat, lon,
                                                           s["lat"], s["lon"])
            except Exception:
                pass  # centre conservé
        spaces.sort(key=lambda s: (not s["named"], s["crow_m"]))
        if not spaces:
            last["places"] = "Overpass (OpenStreetMap)"
            return _orig_green(lat, lon, radius)
        last["places"] = "Geoapify Places"
        return spaces, False
    except Exception:
        last["places"] = "Overpass (OpenStreetMap)"
        return _orig_green(lat, lon, radius)


# ---------------------------------------------------------------------------
# Itinéraire piéton : Geoapify Routing -> repli OSRM
# ---------------------------------------------------------------------------

def walk_route(lat1, lon1, lat2, lon2):
    if _KEY:
        try:
            url = ("https://api.geoapify.com/v1/routing?waypoints="
                   + f"{lat1},{lon1}|{lat2},{lon2}&mode=walk"
                   + "&apiKey=" + urllib.parse.quote(_KEY))
            data = s6_auto.http_json(url, timeout=20)
            p = data["features"][0]["properties"]
            return (round(p["distance"]), max(1, round(p["time"] / 60)),
                    "itinéraire piéton Geoapify (données OpenStreetMap)")
        except Exception:
            pass  # repli sur les routeurs OSM publics
    return _orig_route(lat1, lon1, lat2, lon2)


def source_prefix():
    """Préfixe honnête à substituer dans cfg['source'] après research()."""
    return f"{last['geocode']} + {last['places']}"


# Installe les patches sur s6_auto (une seule fois, à l'import du module).
s6_auto.geocode = geocode
s6_auto.find_green_spaces = find_green_spaces
s6_auto.walk_route = walk_route
