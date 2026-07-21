#!/usr/bin/env python3
"""Rapport S6 entièrement automatique : l'adresse est la seule entrée.

Usage :
    python3 s6_auto.py "4 rue de la Pompe, 75116 Paris" --out rapport.pdf
                       [--screenshot cap.png] [--offline] [--radius 1200]

Pipeline :
  1. géocodage de l'adresse (Nominatim / OpenStreetMap)
  2. recherche des espaces verts praticables autour (Overpass / OpenStreetMap)
  3. itinéraire piéton réel vers les plus proches (OSRM), choix du meilleur
  4. génération du PDF avec preuve visuelle (via build_report.generate :
     capture Google Maps automatique → carte OSM → schéma hors-ligne)

Nécessite un accès réseau à openstreetmap.org / overpass-api.de /
routing.openstreetmap.de pour la recherche (étapes 1-3). Les erreurs réseau
produisent un message clair, jamais un rapport avec des données inventées.
"""

import argparse
import datetime
import json
import math
import sys
import urllib.parse
import urllib.request

import ai_control
import build_report

UA = {"User-Agent": "s6-skill/1.0 (rapport ESG interne)"}

PARK_TYPES_FR = {
    "park": "Parc public",
    "garden": "Jardin public",
    "nature_reserve": "Réserve naturelle",
    "village_green": "Espace vert communal",
    "recreation_ground": "Terrain de loisirs",
    "common": "Espace vert public",
    "beach": "Plage",
    "beach_resort": "Plage aménagée",
    "wood": "Bois",
    "forest": "Forêt",
}


def http_json(url, data=None, timeout=30):
    req = urllib.request.Request(url, data=data, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


VAGUE_TYPES = {"city", "town", "village", "suburb", "neighbourhood", "quarter",
               "postcode", "administrative", "county", "state", "municipality",
               "region"}


def geocode(address):
    """Retourne (lat, lon, vague, échelle) — vague = résolu au quartier/ville."""
    url = ("https://nominatim.openstreetmap.org/search?"
           + urllib.parse.urlencode({"q": address, "format": "json", "limit": 1}))
    res = http_json(url)
    if not res:
        raise RuntimeError(f"Adresse introuvable via Nominatim : {address}")
    r = res[0]
    # NB : Nominatim classe les adresses au numéro en place/house — seule la
    # granularité (type) est fiable pour juger la précision, pas la classe.
    vague = r.get("type") in VAGUE_TYPES or r.get("class") == "boundary"
    return float(r["lat"]), float(r["lon"]), vague, r.get("type", "?")


def find_tenant(name, lat, lon, radius=1500):
    """Localise un locataire/enseigne autour du point. None si introuvable."""
    import time
    time.sleep(1.1)  # politique d'usage Nominatim
    d_lat = radius / 111320.0
    d_lon = radius / (111320.0 * math.cos(math.radians(lat)))
    url = ("https://nominatim.openstreetmap.org/search?"
           + urllib.parse.urlencode({
               "q": name, "format": "json", "limit": 3, "bounded": 1,
               "viewbox": f"{lon-d_lon},{lat+d_lat},{lon+d_lon},{lat-d_lat}"}))
    res = http_json(url)
    if not res:
        return None
    r = res[0]
    return {"lat": float(r["lat"]), "lon": float(r["lon"]),
            "label": r.get("name") or name,
            "crow": build_report.haversine_m(lat, lon,
                                             float(r["lat"]), float(r["lon"]))}


# Les serveurs Overpass publics saturent parfois : rotation sur plusieurs.
OVERPASS_SERVERS = (
    "https://overpass-api.de/api/interpreter",
    "https://lz4.overpass-api.de/api/interpreter",
    "https://z.overpass-api.de/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
)


def overpass_query(query, timeout=20):
    """Rotation sur les serveurs publics, deux tours avec pause (quotas/IP)."""
    import time
    empty_seen = None
    for round_ in range(2):
        for server in OVERPASS_SERVERS:
            try:
                res = http_json(
                    server,
                    data=urllib.parse.urlencode({"data": query}).encode(),
                    timeout=timeout)
                remark = (res.get("remark") or "")
                if "timed out" in remark or "error" in remark.lower():
                    # 200 OK mais requête expirée côté serveur : résultat
                    # tronqué/vide, à ne surtout pas prendre pour un vrai zéro
                    raise RuntimeError(f"réponse tronquée ({remark[:60]})")
                if not res.get("elements"):
                    # Un « zéro résultat » doit être confirmé par un second
                    # serveur avant d'être cru (données partielles possibles)
                    if empty_seen is not None:
                        return res
                    empty_seen = res
                    raise RuntimeError("0 élément — à confirmer ailleurs")
                return res
            except Exception as e:
                print(f"Overpass indisponible ({server.split('/')[2]} : {e}), "
                      f"serveur suivant...", file=sys.stderr)
        if round_ == 0:
            print("Tous occupés — second tour dans 8 s...", file=sys.stderr)
            time.sleep(8)
    return empty_seen  # None si vrais échecs ; vide confirmé une fois sinon


def find_green_spaces(lat, lon, radius):
    """Espaces verts praticables autour du point (OpenStreetMap/Overpass)."""
    kinds = ("park|garden|nature_reserve|recreation_ground|village_green|"
             "common|beach_resort")
    query = f"""[out:json][timeout:25];
(
  nwr["leisure"~"^({kinds})$"](around:{radius},{lat},{lon});
  nwr["landuse"~"^(village_green|recreation_ground|forest)$"](around:{radius},{lat},{lon});
  nwr["natural"~"^(beach|wood)$"](around:{radius},{lat},{lon});
);
out body center bb;"""
    res = overpass_query(query)
    if res is None:
        print("Tous les serveurs Overpass sont saturés : recherche alternative "
              "via Nominatim.", file=sys.stderr)
        return nominatim_parks(lat, lon, radius), True
    spaces = []
    for el in res.get("elements", []):
        tags = el.get("tags", {})
        if tags.get("access") in ("private", "no", "customers"):
            continue
        b = el.get("bounds")
        plat = el.get("lat") or el.get("center", {}).get("lat") \
            or (b and (b["minlat"] + b["maxlat"]) / 2)
        plon = el.get("lon") or el.get("center", {}).get("lon") \
            or (b and (b["minlon"] + b["maxlon"]) / 2)
        if plat is None:
            continue
        kind = (tags.get("leisure") or tags.get("natural")
                or tags.get("landuse") or "")
        # Cible = le point du parc le plus proche de l'actif (bord de son
        # emprise), pas son centre : un grand parc dont l'entrée est à 200 m
        # ne doit pas être compté à 1,5 km.
        tlat, tlon = float(plat), float(plon)
        # Clamp au bord de l'emprise : toujours quand l'actif est HORS de la
        # bbox (une plage de 3 km de long doit être mesurée à son bord, pas
        # à son milieu) ; quand l'actif est DANS la bbox, seulement si elle
        # est compacte — la bbox d'un bois géant peut contenir l'actif et
        # produire une fausse distance nulle.
        if b:
            diag = build_report.haversine_m(b["minlat"], b["minlon"],
                                            b["maxlat"], b["maxlon"])
            inside = (b["minlat"] <= lat <= b["maxlat"]
                      and b["minlon"] <= lon <= b["maxlon"])
            if diag <= 2000 or not inside:
                tlat = min(max(lat, b["minlat"]), b["maxlat"])
                tlon = min(max(lon, b["minlon"]), b["maxlon"])
        spaces.append({
            "name": tags.get("name", "Espace vert (sans nom OSM)"),
            "type": PARK_TYPES_FR.get(kind, "Espace vert"),
            "lat": float(tlat),
            "lon": float(tlon),
            "named": "name" in tags,
            "crow_m": build_report.haversine_m(lat, lon, float(tlat),
                                               float(tlon)),
            "bbox": b,
            "osm_type": el.get("type"),
            "osm_id": el.get("id"),
        })
    # Déduplication par nom (un parc = plusieurs objets OSM), le plus proche gagne
    best = {}
    for s in spaces:
        key = s["name"]
        if key not in best or s["crow_m"] < best[key]["crow_m"]:
            best[key] = s
    return sorted(best.values(),
                  key=lambda s: (not s["named"], s["crow_m"])), False


def nominatim_parks(lat, lon, radius):
    """Plan B quand Overpass est saturé : recherche bornée via Nominatim.

    Moins complet (objets nommés surtout), mais toujours disponible. Seul le
    vrai champ `name` est utilisé — jamais un morceau d'adresse (le début de
    display_name est le quartier/district, pas le parc).
    """
    import time

    d_lat = radius / 111320.0
    d_lon = radius / (111320.0 * math.cos(math.radians(lat)))
    viewbox = f"{lon - d_lon},{lat + d_lat},{lon + d_lon},{lat - d_lat}"
    best = {}
    for term in ("park", "parc", "garden", "jardin", "plage"):
        try:
            url = ("https://nominatim.openstreetmap.org/search?"
                   + urllib.parse.urlencode({
                       "q": term, "format": "json", "limit": 10,
                       "bounded": 1, "viewbox": viewbox}))
            for r in http_json(url):
                if r.get("class") not in ("leisure", "landuse", "natural"):
                    continue
                name = r.get("name") or "Espace vert (sans nom OSM)"
                tlat, tlon = float(r["lat"]), float(r["lon"])
                bb = r.get("boundingbox")
                bbox = None
                if bb:
                    bbox = {"minlat": float(bb[0]), "maxlat": float(bb[1]),
                            "minlon": float(bb[2]), "maxlon": float(bb[3])}
                    inside = (bbox["minlat"] <= lat <= bbox["maxlat"]
                              and bbox["minlon"] <= lon <= bbox["maxlon"])
                    if not inside or build_report.haversine_m(
                            bbox["minlat"], bbox["minlon"],
                            bbox["maxlat"], bbox["maxlon"]) <= 2000:
                        tlat = min(max(lat, bbox["minlat"]), bbox["maxlat"])
                        tlon = min(max(lon, bbox["minlon"]), bbox["maxlon"])
                s = {"name": name,
                     "type": PARK_TYPES_FR.get(r.get("type"), "Espace vert"),
                     "lat": tlat, "lon": tlon,
                     "named": bool(r.get("name")), "bbox": bbox,
                     "osm_type": r.get("osm_type"), "osm_id": r.get("osm_id"),
                     "crow_m": build_report.haversine_m(lat, lon, tlat, tlon)}
                if s["crow_m"] <= radius and (
                        name not in best
                        or s["crow_m"] < best[name]["crow_m"]):
                    best[name] = s
        except Exception as e:
            print(f"Nominatim « {term} » : {e}", file=sys.stderr)
        time.sleep(1.1)  # politique d'usage Nominatim : 1 requête/seconde
    return sorted(best.values(),
                  key=lambda s: (not s["named"], s["crow_m"]))


def walk_route(lat1, lon1, lat2, lon2):
    """Distance (m) et temps (min) à pied. Retourne aussi la source utilisée."""
    for base, label in (
        ("https://routing.openstreetmap.de/routed-foot/route/v1/foot",
         "itinéraire piéton OSM (routing.openstreetmap.de)"),
        ("https://router.project-osrm.org/route/v1/foot",
         "itinéraire OSRM (router.project-osrm.org)"),
    ):
        try:
            res = http_json(f"{base}/{lon1},{lat1};{lon2},{lat2}?overview=false",
                            timeout=8)
            route = res["routes"][0]
            return (round(route["distance"]), max(1, round(route["duration"] / 60)),
                    label)
        except Exception:
            continue
    dist = round(build_report.haversine_m(lat1, lon1, lat2, lon2) * 1.3)
    return dist, max(1, round(dist / 80)), \
        "ESTIMATION : distance à vol d'oiseau ×1,3 (routeur piéton injoignable)"


def refine_park_target(park, lat, lon):
    """Cible l'entrée du parc si OSM la connaît, sinon le point de son
    contour réel le plus proche de l'actif (le coin d'emprise peut tomber
    du mauvais côté du parc)."""
    if park.get("osm_type") != "way" or not park.get("osm_id"):
        return None
    q = (f'[out:json][timeout:15];way({park["osm_id"]})->.p;'
         f'.p out geom;node(w.p)["entrance"];out body;')
    res = overpass_query(q)
    if not res:
        return None
    entrances, outline = [], []
    for el in res.get("elements", []):
        if el.get("type") == "node" and "lat" in el:
            entrances.append((el["lat"], el["lon"]))
        for g in el.get("geometry") or []:
            outline.append((g["lat"], g["lon"]))
    pts = entrances or outline
    if not pts:
        return None
    kind = "entrée du parc (nœud entrance OSM)" if entrances \
        else "point du contour du parc le plus proche"
    return min(pts, key=lambda p: build_report.haversine_m(lat, lon,
                                                           p[0], p[1])), kind


def research(address, radius=1200, candidates=5, locataire=None,
             force_green_space=None):
    """Construit la config complète du rapport à partir de la seule adresse.

    `locataire` (optionnel) : nom d'un locataire/enseigne de l'actif. Sert à
    confirmer le bâtiment, ou à caler le point de départ quand l'adresse est
    résolue approximativement.
    """
    lat, lon, vague, scale = geocode(address)
    checks = []
    if locataire:
        # Adresse vague = point de départ douteux : chercher le locataire large
        t = find_tenant(locataire, lat, lon, radius=5000 if vague else 1500)
        if t and vague:
            lat, lon = t["lat"], t["lon"]
            checks.append(f"point de l'actif calé sur le locataire "
                          f"« {t['label']} » (adresse résolue approximativement, "
                          f"échelle : {scale})")
        elif t and t["crow"] <= 250:
            checks.append(f"locataire « {t['label']} » localisé à "
                          f"{t['crow']:.0f} m du point géocodé — bâtiment confirmé")
        elif t:
            checks.append(f"ATTENTION : locataire « {t['label']} » trouvé à "
                          f"{t['crow']:.0f} m du point géocodé — adresse ou "
                          f"locataire à vérifier")
        else:
            checks.append(f"locataire « {locataire} » introuvable dans "
                          f"OpenStreetMap autour de l'adresse (non bloquant)")
            if vague:
                checks.append(f"ATTENTION : adresse résolue approximativement "
                              f"(échelle : {scale}) — précisez l'adresse")
    elif vague:
        checks.append(f"ATTENTION : adresse résolue approximativement "
                      f"(échelle : {scale}) — précisez l'adresse ou renseignez "
                      f"un locataire")

    spaces, degraded = find_green_spaces(lat, lon, radius)
    if not spaces and degraded:
        raise RuntimeError(
            "recherche dégradée (serveurs cartographiques saturés/quota IP) "
            "et aucun espace vert trouvé — relancer dans 1 à 2 minutes, ne "
            "surtout pas conclure 0/4 sur cette base.")
    if not spaces:
        raise RuntimeError(
            f"aucun espace vert praticable recensé par OpenStreetMap dans un "
            f"rayon de {radius} m autour de « {address} » — vérifier "
            f"manuellement (Google Maps) avant d'acter un 0/4.")
    if degraded:
        checks.append("ATTENTION : recherche d'espaces verts dégradée "
                      "(serveurs saturés) — résultat possiblement incomplet, "
                      "relancer pour confirmer")

    # Évaluer la marche vers les plus proches, nommés et anonymes séparément :
    # un parc nommé sous le seuil est préféré (preuve plus solide qu'une
    # pelouse anonyme), sinon le plus proche l'emporte.
    threshold = 1000
    named = [s for s in spaces if s["named"]][:candidates]
    unnamed = [s for s in spaces if not s["named"]][:candidates]
    # Itinéraires calculés en parallèle (gain de plusieurs secondes)
    import concurrent.futures
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as ex:
        futures = [(s, ex.submit(walk_route, lat, lon, s["lat"], s["lon"]))
                   for s in named + unnamed]
        evaluated = [(f.result()[0], f.result()[1], f.result()[2], s)
                     for s, f in futures]
    # Liste des espaces verts RÉELS trouvés autour de l'actif. Sert au
    # contrôle IA : s'il doute du parc retenu, il ne peut proposer une
    # alternative QUE parmi ces vrais candidats — jamais un parc inventé.
    candidates_list = [
        {"name": s["name"], "type": s["type"], "named": s["named"],
         "walk_distance_m": d, "walk_time_min": m,
         "lat": s["lat"], "lon": s["lon"]}
        for (d, m, _src, s) in sorted(evaluated, key=lambda e: e[0])
    ]
    named_ok = [e for e in evaluated if e[3]["named"] and e[0] <= threshold]
    any_ok = [e for e in evaluated if e[0] <= threshold]
    if force_green_space:
        # Retenir explicitement un candidat réel désigné (ex. alternative
        # validée par l'utilisateur après un doute du contrôle IA).
        fmatch = [e for e in evaluated
                  if force_green_space.lower() in e[3]["name"].lower()]
        if not fmatch:
            raise RuntimeError(
                f"espace vert imposé « {force_green_space} » introuvable parmi "
                f"les candidats réels de cette adresse — ne rien inventer.")
        dist, mins, route_src, park = min(fmatch, key=lambda e: e[0])
        checks.append(f"espace vert imposé manuellement : « {park['name']} »")
    else:
        dist, mins, route_src, park = min(named_ok or any_ok or evaluated,
                                          key=lambda e: e[0])
    # Viser l'entrée (ou le contour) du parc retenu, puis re-mesurer
    refined = refine_park_target(park, lat, lon)
    if refined:
        (park["lat"], park["lon"]), target_kind = refined
        dist, mins, route_src = walk_route(lat, lon, park["lat"], park["lon"])
        print(f"Cible affinée : {target_kind}")
        checks.append(f"Itinéraire visé sur l'{target_kind}"
                      if target_kind.startswith("entrée")
                      else f"Itinéraire visé sur le {target_kind}")
    # Transparence : si un espace vert anonyme est encore plus proche que le
    # parc nommé retenu, le signaler (le score, lui, ne change pas).
    if any_ok:
        nearest_any = min(any_ok, key=lambda e: e[0])
        if nearest_any[3] is not park and nearest_any[0] < dist:
            checks.append(f"NB : un espace vert plus proche existe "
                          f"({nearest_any[0]} m) ; le parc nommé est retenu "
                          f"comme preuve plus solide")

    # Situer un espace sans nom par géocodage inverse : plus parlant dans le
    # rapport qu'un simple « sans nom », sans rien inventer.
    if not park["named"]:
        try:
            url = ("https://nominatim.openstreetmap.org/reverse?"
                   + urllib.parse.urlencode({
                       "format": "json", "lat": park["lat"],
                       "lon": park["lon"], "zoom": 16}))
            addr = http_json(url).get("address", {})
            loc = (addr.get("road") or addr.get("neighbourhood")
                   or addr.get("suburb") or addr.get("village")
                   or addr.get("town"))
            if loc:
                park["name"] = f"Espace vert public (près de {loc})"
        except Exception:
            pass

    # Contrôles de cohérence sur le résultat retenu
    if 900 <= dist <= 1100:
        checks.append(f"ATTENTION : distance de {dist} m proche du seuil "
                      f"d'1 km — vérification manuelle recommandée")
    speed_kmh = dist / max(mins, 1) / 16.667
    if not 2.5 <= speed_kmh <= 6.5:
        checks.append(f"ATTENTION : vitesse de marche implicite anormale "
                      f"({speed_kmh:.1f} km/h)")
    if park.get("bbox"):
        b = park["bbox"]
        w_m = build_report.haversine_m(b["minlat"], b["minlon"],
                                       b["minlat"], b["maxlon"])
        h_m = build_report.haversine_m(b["minlat"], b["minlon"],
                                       b["maxlat"], b["minlon"])
        if 0 < w_m * h_m < 2500:
            checks.append(f"ATTENTION : emprise très réduite "
                          f"(~{w_m * h_m:.0f} m²) — praticabilité à vérifier")
        diag = math.hypot(w_m, h_m)
        if dist > threshold and dist - diag / 2 <= threshold:
            checks.append("ATTENTION : grand parc, distance mesurée jusqu'à "
                          "son centre — l'entrée la plus proche est "
                          "probablement sous le seuil d'1 km")

    today = datetime.date.today().strftime("%d/%m/%Y")
    maps_url = (f"https://www.google.com/maps/dir/{lat},{lon}/"
                f"{park['lat']},{park['lon']}/data=!4m2!4m1!3e2")
    validated = dist <= threshold
    return {
        "asset": {"address": address, "lat": lat, "lon": lon},
        "green_space": {
            "name": park["name"],
            "type": park["type"],
            "address": "coordonnées OSM : "
                       f"{park['lat']:.5f}, {park['lon']:.5f}",
            "lat": park["lat"],
            "lon": park["lon"],
            "walk_distance_m": dist,
            "walk_time_min": mins,
            "named": park["named"],
        },
        "source": f"OpenStreetMap (Nominatim + Overpass) ; {route_src} ; "
                  f"consulté le {today}",
        "maps_url": maps_url,
        "analysis_date": today,
        "score": 4 if validated else 0,
        "score_max": 4,
        "threshold_m": 1000,
        "checks": checks,
        "candidates": candidates_list[:8],
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("address", help="adresse de l'actif")
    ap.add_argument("--out", required=True, help="chemin du PDF de sortie")
    ap.add_argument("--screenshot", nargs="*", default=[])
    ap.add_argument("--offline", action="store_true")
    ap.add_argument("--radius", type=int, default=1200,
                    help="rayon de recherche en mètres (défaut 1200)")
    ap.add_argument("--locataire", default=None,
                    help="nom d'un locataire/enseigne de l'actif (confirme le "
                         "bâtiment, ou sert d'ancre si l'adresse est imprécise)")
    ap.add_argument("--save-config", help="enregistre la config JSON produite")
    ap.add_argument("--force-green-space", default=None,
                    help="imposer un espace vert précis (parmi les candidats "
                         "réels) — sert à appliquer une alternative validée "
                         "après un doute du contrôle IA")
    ap.add_argument("--force-score", type=int, choices=(0, 4), default=None,
                    help="forcer le score final (0 ou 4) après validation de "
                         "l'utilisateur suite à un doute du contrôle IA")
    ap.add_argument("--override-note", default=None,
                    help="explication à inscrire dans le PDF quand le score "
                         "est forcé (ex. « parc jugé privé au contrôle »)")
    args = ap.parse_args()

    try:
        cfg = research(args.address, radius=args.radius,
                       locataire=args.locataire,
                       force_green_space=args.force_green_space)
    except Exception as e:
        sys.exit(f"Recherche impossible : {e}")

    if args.force_score is not None:
        cfg["score"] = args.force_score
        cfg["score_overridden"] = True
        if args.override_note:
            cfg["override_note"] = args.override_note
    for c in cfg.get("checks", []):
        print(f"Contrôle : {c}")

    park = cfg["green_space"]
    print(f"Espace vert retenu : {park['name']} — {park['walk_distance_m']} m "
          f"à pied ({park['walk_time_min']} min)")

    # Contrôle IA optionnel : n'a lieu que si une clé OpenAI est configurée.
    # Sans clé, review_s6 renvoie None et rien ne change. Le contrôle ne
    # modifie JAMAIS le score : en cas de doute, c'est l'assistant qui relaie
    # la question à l'utilisateur.
    verdict = ai_control.review_s6(cfg)
    if verdict:
        cfg["ai_control"] = verdict
        suffix = (f"(confiance {verdict['confiance']}, modèle "
                  f"{verdict['modele']})")
        if verdict["statut"] == "confirme":
            print(f"Contrôle IA : confirmé — {verdict['raison']} {suffix}")
        elif verdict["statut"] == "doute":
            print(f"Contrôle IA : DOUTE — {verdict['raison']} {suffix}")
            alt = verdict.get("alternative")
            if alt:
                print(f"  → Alternative RÉELLE proposée : « {alt['name']} » "
                      f"({alt.get('raison', '')}). Le 4/4 peut être conservé "
                      f"en retenant cet espace à la place.")
                print(f"    Pour l'appliquer après accord de l'utilisateur : "
                      f"relancer avec --force-green-space \"{alt['name']}\".")
            else:
                print("  → Aucune alternative valable parmi les espaces verts "
                      "réels trouvés. Si le doute est confirmé, le score "
                      "devrait passer à 0/4.")
            print("  → Le score du programme n'est PAS modifié pour l'instant. "
                  "L'assistant doit demander à l'utilisateur avant tout "
                  "changement (jamais inventer un espace).")
        else:
            print(f"Contrôle IA : indisponible — {verdict['raison']}")

    if args.save_config:
        with open(args.save_config, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)

    build_report.generate(cfg, args.out, screenshots=args.screenshot,
                          offline=args.offline)


if __name__ == "__main__":
    main()
