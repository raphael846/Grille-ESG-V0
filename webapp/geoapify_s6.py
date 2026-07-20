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
import urllib.request

import build_report
import s6_auto
import s2_auto
import s7_auto

UA = {"User-Agent": "esg-geoapify/1.0"}

_KEY = ""

# Sources réellement utilisées au dernier appel (pour une mention honnête dans
# le PDF). Mises à jour par les fonctions ci-dessous.
last = {"geocode": "Nominatim (OpenStreetMap)", "places": "Overpass (OpenStreetMap)"}

# Primitives OSM d'origine, capturées une fois pour le repli.
_orig_geocode = s6_auto.geocode
_orig_green = s6_auto.find_green_spaces
_orig_route = s6_auto.walk_route
_orig_find_services = s2_auto.find_services      # S2
_orig_find_transit = s7_auto.find_transit        # S7
_orig_stop_lines = s7_auto.stop_lines            # S7
_orig_online_map = build_report.try_online_map   # carte de preuve


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


def _places_retry(lat, lon, radius, categories, limit, tries=2):
    """_ga_places avec quelques tentatives : un blip réseau ne doit pas faire
    basculer tout un critère sur Overpass (qui est justement throttlé sur le cloud)."""
    err = None
    for _ in range(tries):
        try:
            return _ga_places(lat, lon, radius, categories, limit)
        except Exception as e:
            err = e
    raise err


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
        feats = _places_retry(lat, lon, radius, "leisure.park,national_park,beach", 50)
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


# ---------------------------------------------------------------------------
# S2 — services : Geoapify Places (par familles) -> repli Overpass
# ---------------------------------------------------------------------------

# Correspondance taxonomie Geoapify -> catégorie de la grille (le générique
# « commercial » reste en dernier). Repris de webapp/s6.html.
GA_CAT_LABEL = (
    ("catering.restaurant", "Restaurant"), ("catering.fast_food", "Restaurant"),
    ("catering.cafe", "Café"), ("catering.bar", "Bar"), ("catering.pub", "Bar"),
    ("accommodation.hotel", "Hôtel"), ("accommodation.guest_house", "Hôtel"),
    ("commercial.supermarket", "Supermarché"),
    ("commercial.convenience", "Supermarché"),
    ("commercial.books", "Librairie"),
    ("education.school", "École"), ("education.kindergarten", "École"),
    ("education.college", "École"), ("education.university", "École"),
    ("healthcare", "Médecin / santé"),
    ("service.financial", "Banque"),
    ("commercial", "Commerce"),
)


def _ga_categorize(p):
    # D'abord les tags OSM bruts (plus précis), sinon la taxonomie Geoapify.
    cat = s2_auto.categorize(p["raw"])
    if cat:
        return cat
    for prefix, label in GA_CAT_LABEL:
        for c in p["categories"]:
            if c == prefix or c.startswith(prefix + "."):
                return label
    return None


def find_services(lat, lon, radius):
    if not _KEY:
        last["places"] = "Overpass (OpenStreetMap)"
        return _orig_find_services(lat, lon, radius)
    try:
        # Une requête PAR famille : l'API Places ne renvoie qu'une famille quand
        # on en combine plus de deux (constaté aussi côté s6.html).
        groups = ["catering", "commercial",
                  "accommodation.hotel,accommodation.guest_house",
                  "education", "healthcare"]
        feats = []
        for g in groups:
            try:
                feats += _ga_places(lat, lon, radius, g, 100)
            except Exception:
                pass
        out = []
        for p in feats:
            cat = _ga_categorize(p)
            nm = p["name"] or p["raw"].get("name")
            if not cat or not nm:
                continue
            out.append({"name": nm, "cat": cat, "lat": p["lat"], "lon": p["lon"],
                        "crow_m": build_report.haversine_m(lat, lon,
                                                           p["lat"], p["lon"])})
        if out:
            last["places"] = "Geoapify Places"
            return out, False
    except Exception:
        pass
    last["places"] = "Overpass (OpenStreetMap)"
    return _orig_find_services(lat, lon, radius)


# ---------------------------------------------------------------------------
# S7 — transports : Geoapify Places (public_transport) -> repli Overpass
# ---------------------------------------------------------------------------

_CAT_MODE = {"bus": "Bus", "tram": "Tram", "subway": "Métro", "train": "Train",
             "light_rail": "Train", "ferry": "Ferry"}


def find_transit(lat, lon, radius=s7_auto.THRESHOLD_M):
    if _KEY:
        try:
            feats = _places_retry(lat, lon, radius, "public_transport", 60)
            stops, ref_lines = [], {}
            for p in feats:
                t = p["raw"]
                mode = s7_auto.mode_of_stop(t)
                if not mode:
                    for c in p["categories"]:
                        parts = c.split(".")
                        m = _CAT_MODE.get(parts[1]) if len(parts) > 1 else None
                        if m:
                            mode = m
                            break
                if not mode:
                    continue
                stops.append({
                    "name": p["name"] or t.get("name") or f"Arrêt {mode.lower()}",
                    "mode": mode,
                    "id": t.get("osm_id") if t.get("osm_type") in ("node", "n") else None,
                    "lat": p["lat"], "lon": p["lon"],
                    "crow_m": build_report.haversine_m(lat, lon, p["lat"], p["lon"]),
                })
                for ref in (t.get("route_ref") or "").replace(",", ";").split(";"):
                    ref = ref.strip()
                    if ref:
                        ref_lines[(mode, ref)] = {"mode": mode, "ref": ref}
            if stops:
                last["places"] = "Geoapify Places"
                return stops, list(ref_lines.values()), (len(ref_lines) == 0)
        except Exception:
            pass
    last["places"] = "Overpass (OpenStreetMap)"
    return _orig_find_transit(lat, lon, radius)


def stop_lines(stop):
    # Geoapify n'expose pas les lignes par arrêt : renvoyer None fait basculer
    # research() sur les lignes du secteur (issues des route_ref Geoapify),
    # sans requête Overpass supplémentaire.
    if _KEY:
        return None
    return _orig_stop_lines(stop)


# ---------------------------------------------------------------------------
# Carte de preuve : Static Maps Geoapify (1 requête/image) -> repli staticmap OSM
# ---------------------------------------------------------------------------

def _ga_route_coords(lat1, lon1, lat2, lon2, timeout):
    """Géométrie de l'itinéraire piéton Geoapify : liste de points (lon, lat)."""
    url = "https://api.geoapify.com/v1/routing?" + urllib.parse.urlencode(
        {"waypoints": f"{lat1},{lon1}|{lat2},{lon2}", "mode": "walk", "apiKey": _KEY})
    geom = s6_auto.http_json(url, timeout=timeout)["features"][0]["geometry"]
    if geom["type"] == "LineString":
        return [(c[0], c[1]) for c in geom["coordinates"]]
    pts = []  # MultiLineString
    for seg in geom["coordinates"]:
        pts += [(c[0], c[1]) for c in seg]
    return pts


def try_online_map(asset, park, out_path, timeout=15):
    """Carte de preuve via l'API Static Maps de Geoapify : une seule requête par
    image (bien plus rapide que le rendu tuile par tuile de staticmap, et sans
    rate-limit sur les tuiles OSM publiques). Le TRACÉ est l'itinéraire piéton
    réel (Geoapify Routing), pas une droite. Repli sur la carte OSM d'origine."""
    if _KEY:
        try:
            lon1, lat1 = asset["lon"], asset["lat"]
            lon2, lat2 = park["lon"], park["lat"]
            try:
                route = _ga_route_coords(lat1, lon1, lat2, lon2, timeout)
            except Exception:
                route = None
            if route and len(route) >= 2:
                pts = route
                if len(pts) > 120:               # borne la longueur de l'URL
                    step = len(pts) // 120 + 1
                    pts = pts[::step] + [route[-1]]
                itin = "itinéraire piéton réel (Geoapify, données OpenStreetMap)"
            else:
                pts = [(lon1, lat1), (lon2, lat2)]
                itin = "liaison directe (itinéraire piéton indisponible)"
            poly = ",".join(f"{lon},{lat}" for lon, lat in pts)
            xs, ys = [p[0] for p in pts], [p[1] for p in pts]
            pad = max(0.0009, max(max(xs) - min(xs), max(ys) - min(ys)) * 0.15)
            west, east = min(xs) - pad, max(xs) + pad
            south, north = min(ys) - pad, max(ys) + pad
            params = {
                "style": "osm-bright", "width": 1400, "height": 900,
                "area": f"rect:{west},{south},{east},{north}",
                "marker": (f"lonlat:{lon1},{lat1};color:#d93025;size:medium"
                           f"|lonlat:{lon2},{lat2};color:#188038;size:medium"),
                "geometry": (f"polyline:{poly}"
                             ";linecolor:#1a73e8;linewidth:5;lineopacity:0.8"),
                "apiKey": _KEY,
            }
            # urlencode encode les délimiteurs (| ; : , #) — sinon urllib rejette
            # l'URL (caractères illégaux) ; Geoapify les redécode côté serveur.
            url = ("https://maps.geoapify.com/v1/staticmap?"
                   + urllib.parse.urlencode(params))
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = resp.read()
            # Geoapify renvoie du JPEG (ou PNG) : accepter les deux.
            if not (data[:3] == b"\xff\xd8\xff" or data[:8] == b"\x89PNG\r\n\x1a\n"):
                raise RuntimeError("réponse Static Maps non-image")
            with open(out_path, "wb") as f:
                f.write(data)
            return out_path, f"carte Geoapify — {itin}"
        except Exception:
            pass  # repli sur la carte OSM (staticmap) d'origine
    return _orig_online_map(asset, park, out_path, timeout)


def source_prefix():
    """Préfixe honnête à substituer dans cfg['source'] après research()."""
    return f"{last['geocode']} + {last['places']}"


# Installe les patches (une seule fois, à l'import du module).
s6_auto.geocode = geocode
s6_auto.find_green_spaces = find_green_spaces
s6_auto.walk_route = walk_route
s2_auto.find_services = find_services
s7_auto.find_transit = find_transit
s7_auto.stop_lines = stop_lines
build_report.try_online_map = try_online_map
