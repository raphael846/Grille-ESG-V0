#!/usr/bin/env python3
"""Critère S2 : présence de services à moins d'1 km à pied de l'actif.

Usage :
    python3 s2_auto.py "adresse de l'actif" --out rapport.pdf
                       [--locataire "Nom"] [--offline] [--radius 1200]

Méthodologie : repérer 3 services de catégories différentes (restaurant,
hôtel, commerce, bar, café, station-service, supermarché, école, banque,
médecin, librairie) à moins d'1 km à pied. Une preuve visuelle par service
(capture Google Maps de l'itinéraire piéton → carte OSM → schéma). Score :
3/3 si 3 services validés, sinon 0/3.

Réutilise le socle de s6_auto (géocodage, locataire, routeur piéton) et de
build_report (captures, cartes, styles).
"""

import argparse
import datetime
import math
import os
import sys
import tempfile
import urllib.parse

import ai_control
import build_report
import s6_auto

THRESHOLD_M = 1000
REQUIRED = 3

# Classement d'un objet OSM dans les catégories de la grille ESG
def categorize(tags):
    a, s, t = tags.get("amenity"), tags.get("shop"), tags.get("tourism")
    if a in ("restaurant", "fast_food"):
        return "Restaurant"
    if a == "cafe":
        return "Café"
    if a in ("bar", "pub"):
        return "Bar"
    if t in ("hotel", "guest_house"):
        return "Hôtel"
    if s in ("supermarket", "convenience"):
        return "Supermarché"
    if a == "fuel":
        return "Station-service"
    if a in ("school", "kindergarten", "college", "university"):
        return "École"
    if a == "bank":
        return "Banque"
    if a in ("doctors", "clinic", "hospital", "pharmacy"):
        return "Médecin / santé"
    if s == "books":
        return "Librairie"
    if s:
        return "Commerce"
    return None


OVERPASS_FILTERS = (
    '["amenity"~"^(restaurant|fast_food|cafe|bar|pub|fuel|school|kindergarten'
    '|college|university|bank|doctors|clinic|hospital|pharmacy)$"]',
    '["shop"~"^(supermarket|convenience|bakery|butcher|greengrocer|clothes|shoes|hairdresser|florist|books|chemist|optician|hardware|doityourself|department_store|mall|general)$"]',
    '["tourism"~"^(hotel|guest_house)$"]',
)


def find_services(lat, lon, radius):
    """Services praticables autour du point (OpenStreetMap/Overpass)."""
    parts = "\n".join(f"  nwr{f}(around:{radius},{lat},{lon});"
                      for f in OVERPASS_FILTERS)
    query = f"[out:json][timeout:25];(\n{parts}\n);\nout tags center;"
    res = s6_auto.overpass_query(query)
    if res is None:
        print("Tous les serveurs Overpass sont saturés : recherche "
              "alternative via Nominatim.", file=sys.stderr)
        return nominatim_services(lat, lon, radius), True

    services = []
    for el in res.get("elements", []):
        tags = el.get("tags", {})
        cat = categorize(tags)
        if not cat or not tags.get("name"):
            continue  # un service sans nom n'est pas une preuve exploitable
        slat = el.get("lat") or el.get("center", {}).get("lat")
        slon = el.get("lon") or el.get("center", {}).get("lon")
        if slat is None:
            continue
        services.append({
            "name": tags["name"], "cat": cat,
            "lat": float(slat), "lon": float(slon),
            "crow_m": build_report.haversine_m(lat, lon,
                                               float(slat), float(slon)),
        })
    return services, False


NOMINATIM_TERMS = (("restaurant", "Restaurant"), ("cafe", "Café"),
                   ("hotel", "Hôtel"), ("supermarket", "Supermarché"),
                   ("school", "École"), ("bank", "Banque"),
                   ("pharmacy", "Médecin / santé"))


def nominatim_services(lat, lon, radius):
    """Plan B quand Overpass est saturé (couverture réduite mais fiable)."""
    import time

    d_lat = radius / 111320.0
    d_lon = radius / (111320.0 * math.cos(math.radians(lat)))
    viewbox = f"{lon - d_lon},{lat + d_lat},{lon + d_lon},{lat - d_lat}"
    services = []
    for term, cat in NOMINATIM_TERMS:
        try:
            url = ("https://nominatim.openstreetmap.org/search?"
                   + urllib.parse.urlencode({
                       "q": term, "format": "json", "limit": 5,
                       "bounded": 1, "viewbox": viewbox}))
            for r in s6_auto.http_json(url):
                if not r.get("name"):
                    continue
                s = {"name": r["name"], "cat": cat,
                     "lat": float(r["lat"]), "lon": float(r["lon"]),
                     "crow_m": build_report.haversine_m(
                         lat, lon, float(r["lat"]), float(r["lon"]))}
                if s["crow_m"] <= radius:
                    services.append(s)
        except Exception as e:
            print(f"Nominatim « {term} » : {e}", file=sys.stderr)
        time.sleep(1.1)
    return services


def research(address, radius=1200, locataire=None, force_services=None):
    """Sélectionne 3 services de catégories différentes validant le seuil.

    `force_services` (liste de noms) impose des services réels désignés — sert
    à appliquer un remplacement validé par l'utilisateur après un doute IA.
    """
    lat, lon, vague, scale = s6_auto.geocode(address)
    checks = []
    if locataire:
        t = s6_auto.find_tenant(locataire, lat, lon,
                                radius=5000 if vague else 1500)
        if t and vague:
            lat, lon = t["lat"], t["lon"]
            checks.append(f"point de l'actif calé sur le locataire "
                          f"« {t['label']} » (adresse résolue "
                          f"approximativement, échelle : {scale})")
        elif t and t["crow"] <= 250:
            checks.append(f"locataire « {t['label']} » localisé à "
                          f"{t['crow']:.0f} m du point géocodé — bâtiment "
                          f"confirmé")
        elif t:
            checks.append(f"ATTENTION : locataire « {t['label']} » trouvé à "
                          f"{t['crow']:.0f} m du point géocodé — à vérifier")
        else:
            checks.append(f"locataire « {locataire} » introuvable dans "
                          f"OpenStreetMap autour de l'adresse (non bloquant)")
    elif vague:
        checks.append(f"ATTENTION : adresse résolue approximativement "
                      f"(échelle : {scale}) — précisez l'adresse ou "
                      f"renseignez un locataire")

    candidates, degraded = find_services(lat, lon, radius)
    if not candidates:
        raise RuntimeError(
            f"Aucun service nommé trouvé dans un rayon de {radius} m autour "
            f"de « {address} » (OpenStreetMap"
            + (", recherche dégradée : serveurs saturés — réessayer dans "
               "quelques minutes" if degraded else "") + ").")

    # Le plus proche de chaque catégorie, catégories triées par proximité,
    # puis itinéraires piétons réels jusqu'à obtenir 3 services validés.
    by_cat = {}
    for s in candidates:
        if s["cat"] not in by_cat or s["crow_m"] < by_cat[s["cat"]]["crow_m"]:
            by_cat[s["cat"]] = s
    ordered = sorted(by_cat.values(), key=lambda s: s["crow_m"])

    import concurrent.futures
    if force_services:
        # Sélection imposée : retenir des services réels désignés (ex.
        # remplacement validé par l'utilisateur après un doute du contrôle IA).
        chosen = []
        for name in force_services:
            m = next((s for s in candidates
                      if name.lower() in (s["name"] or "").lower()), None)
            if m and m not in chosen:
                chosen.append(m)
        ordered = chosen or ordered[:REQUIRED * 2]
    else:
        # Itinéraires calculés en parallèle sur les 6 catégories les plus proches
        ordered = ordered[:REQUIRED * 2]

    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as ex:
        routes = list(ex.map(
            lambda s: s6_auto.walk_route(lat, lon, s["lat"], s["lon"]),
            ordered))
    for s, (dist, mins, route_src) in zip(ordered, routes):
        s.update(walk_distance_m=dist, walk_time_min=mins, route_src=route_src,
                 maps_url=(f"https://www.google.com/maps/dir/{lat},{lon}/"
                           f"{s['lat']},{s['lon']}/data=!4m2!4m1!3e2"))
    # Pool de candidats réels (distances calculées) pour le contrôle IA.
    candidates_list = [
        {"name": s["name"], "cat": s["cat"], "lat": s["lat"], "lon": s["lon"],
         "walk_distance_m": s["walk_distance_m"],
         "walk_time_min": s["walk_time_min"]}
        for s in sorted(ordered, key=lambda s: s["walk_distance_m"])
    ]
    selected = []
    for s in ordered:
        if len(selected) >= REQUIRED:
            break
        if force_services or s["walk_distance_m"] <= THRESHOLD_M:
            selected.append(s)
            if 900 <= s["walk_distance_m"] <= THRESHOLD_M:
                checks.append(f"ATTENTION : {s['name']} à "
                              f"{s['walk_distance_m']} m — proche du seuil "
                              f"d'1 km")

    if degraded and len(selected) < REQUIRED:
        checks.append("ATTENTION : recherche de services DÉGRADÉE (serveurs "
                      "cartographiques saturés) — le score peut être "
                      "sous-évalué, relancer l'analyse avant de conclure")

    today = datetime.date.today().strftime("%d/%m/%Y")
    return {
        "asset": {"address": address, "lat": lat, "lon": lon},
        "services": selected,
        "source": "OpenStreetMap (Nominatim + Overpass) ; itinéraires piétons "
                  f"OSM ; consulté le {today}",
        "analysis_date": today,
        "score": REQUIRED if len(selected) >= REQUIRED else 0,
        "score_max": REQUIRED,
        "threshold_m": THRESHOLD_M,
        "checks": checks,
        "candidates": candidates_list,
    }


# ---------------------------------------------------------------------------
# Preuves + PDF
# ---------------------------------------------------------------------------

def gather_proofs(cfg, offline=False):
    """Une image de preuve par service : capture Google Maps → carte OSM →
    schéma hors-ligne. Retourne [(chemin, description de la provenance), ...]
    aligné sur cfg["services"]."""
    import concurrent.futures

    asset = cfg["asset"]
    asset.setdefault("short_label", asset["address"].split(",")[0])
    tmpdir = tempfile.mkdtemp(prefix="s2_")

    def one_proof(args):
        i, s = args
        path = os.path.join(tmpdir, f"service_{i}.png")
        if not offline and build_report.try_maps_capture(
                s["maps_url"], path, width=1200, height=820):
            print(f"Preuve {i} : capture Google Maps ({s['name']})")
            return (path,
                    f"Capture d'écran Google Maps (itinéraire piéton), prise "
                    f"automatiquement le {cfg['analysis_date']}. Google "
                    f"affiche son itinéraire recommandé ; la fiche cite le "
                    f"calcul OpenStreetMap.")
        park_like = dict(s)
        if not offline and build_report.try_online_map(asset, park_like, path):
            print(f"Preuve {i} : carte OSM ({s['name']})")
            return (path, "Carte OpenStreetMap générée automatiquement avec "
                          "l'itinéraire piéton réel.")
        build_report.offline_schematic(
            asset, park_like, s["walk_distance_m"], s["walk_time_min"], path)
        print(f"Preuve {i} : schéma hors-ligne ({s['name']})")
        return (path, "Carte schématique générée à partir des coordonnées "
                      "réelles.")

    # Les captures (un Chromium headless chacune) tournent en parallèle :
    # le temps total ≈ la plus lente au lieu de la somme des trois.
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as ex:
        return list(ex.map(one_proof, enumerate(cfg["services"], 1)))


def build_pdf(cfg, proofs, out_pdf):
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_CENTER, TA_RIGHT
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.platypus import (Image as RLImage, KeepTogether, Paragraph,
                                    SimpleDocTemplate, Spacer, Table,
                                    TableStyle)
    from PIL import Image

    ok = cfg["score"] >= cfg["score_max"]
    styles = getSampleStyleSheet()
    st_title = ParagraphStyle("t", parent=styles["Title"], fontSize=16,
                              spaceAfter=1)
    st_sub = ParagraphStyle("s", parent=styles["Normal"], fontSize=9.5,
                            textColor=colors.HexColor("#555"),
                            alignment=TA_CENTER)
    st_h2 = ParagraphStyle("h2", parent=styles["Heading2"], fontSize=11.5,
                           spaceBefore=8, spaceAfter=3)
    st_body = ParagraphStyle("b", parent=styles["Normal"], fontSize=9.5,
                             leading=13)
    st_score = ParagraphStyle("sc", parent=styles["Normal"], fontSize=11.5,
                              alignment=TA_CENTER, textColor=colors.white,
                              leading=15)
    st_cap = ParagraphStyle("cap", parent=styles["Normal"], fontSize=7.5,
                            textColor=colors.HexColor("#666"), leading=10)

    doc = SimpleDocTemplate(out_pdf, pagesize=A4,
                            leftMargin=15 * mm, rightMargin=15 * mm,
                            topMargin=11 * mm, bottomMargin=11 * mm)
    story = [
        Paragraph("Critère S2 — Présence de services", st_title),
        Paragraph(f"Au moins {REQUIRED} services de catégories différentes à "
                  f"moins d'1 km à pied du site", st_sub),
        Spacer(1, 3),
        Paragraph(f"Actif : {cfg['asset']['address']} &nbsp;·&nbsp; Date de "
                  f"l'analyse : {cfg['analysis_date']}", st_sub),
        Spacer(1, 6),
    ]

    n = len(cfg["services"])
    verdict = "Critère validé" if ok else "Critère non validé"
    banner = Table([[Paragraph(
        f"<b>SCORE : {cfg['score']} / {cfg['score_max']}</b> — {verdict} — "
        f"{n} service(s) de catégories différentes sous le seuil", st_score)]],
        colWidths=[doc.width])
    banner.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1),
         colors.HexColor("#188038") if ok else colors.HexColor("#d93025")),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
    ]))
    story.append(banner)

    story.append(Paragraph("Services retenus", st_h2))
    rows = [[Paragraph("<b>Catégorie</b>", st_body),
             Paragraph("<b>Nom</b>", st_body),
             Paragraph("<b>Distance à pied</b>", st_body),
             Paragraph("<b>Temps</b>", st_body)]]
    for s in cfg["services"]:
        rows.append([Paragraph(s["cat"], st_body),
                     Paragraph(s["name"], st_body),
                     Paragraph(f"{s['walk_distance_m']} m", st_body),
                     Paragraph(f"{s['walk_time_min']} min", st_body)])
    table = Table(rows, colWidths=[35 * mm, doc.width - 95 * mm,
                                   32 * mm, 28 * mm])
    table.setStyle(TableStyle([
        ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#cccccc")),
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#f2f2f2")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
    ]))
    story.append(table)

    if cfg.get("checks"):
        story.append(Spacer(1, 3))
        story.append(Paragraph("Contrôles automatiques : "
                               + " ; ".join(cfg["checks"]), st_cap))
    ai = cfg.get("ai_control")
    if ai:
        prefix = {"confirme": "Confirmé", "doute": "DOUTE",
                  "indisponible": "Indisponible"}.get(ai["statut"], "")
        txt = f"Contrôle IA : {prefix} — {ai.get('raison', '')}"
        if ai.get("confiance"):
            txt += f" (confiance {ai['confiance']})"
        if ai.get("resolution"):
            txt += f" ; décision : {ai['resolution']}"
        story.append(Spacer(1, 3))
        story.append(Paragraph(txt, st_cap))

    story.append(Paragraph("Preuves", st_h2))
    proof_colors = ("#1a73e8", "#188038", "#9333ea")
    st_head = ParagraphStyle("ph", parent=st_body, textColor=colors.white,
                             fontSize=9.5)
    st_head_r = ParagraphStyle("phr", parent=st_head, alignment=TA_RIGHT)
    for i, (s, (img_path, provenance)) in enumerate(
            zip(cfg["services"], proofs), 1):
        color = colors.HexColor(proof_colors[(i - 1) % len(proof_colors)])
        head = Table([[
            Paragraph(f"<b>Preuve {i} — {s['cat']}</b>", st_head),
            Paragraph(f"<b>{s['name']}</b>", st_head),
            Paragraph(f"<b>{s['walk_distance_m']} m · "
                      f"{s['walk_time_min']} min à pied</b>", st_head_r),
        ]], colWidths=[48 * mm, doc.width - 48 * mm - 42 * mm, 42 * mm])
        head.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, -1), color),
            ("TOPPADDING", (0, 0), (-1, -1), 4),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ("LEFTPADDING", (0, 0), (-1, -1), 6),
            ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ]))
        with Image.open(img_path) as im:
            iw, ih = im.size
        ratio = min((doc.width - 2) / iw, 66 * mm / ih)
        framed = Table([[RLImage(img_path, width=iw * ratio,
                                 height=ih * ratio)]],
                       colWidths=[iw * ratio + 2])
        framed.setStyle(TableStyle([
            ("BOX", (0, 0), (-1, -1), 0.75, colors.HexColor("#bbbbbb")),
            ("TOPPADDING", (0, 0), (-1, -1), 1),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 1),
            ("LEFTPADDING", (0, 0), (-1, -1), 1),
            ("RIGHTPADDING", (0, 0), (-1, -1), 1),
        ]))
        caption = Paragraph(
            f"{provenance} Itinéraire vérifiable : "
            f'<link href="{s["maps_url"]}" color="blue">ouvrir dans Google '
            f"Maps</link>.", st_cap)
        story.append(KeepTogether([head, framed, caption, Spacer(1, 7)]))

    story.append(Paragraph("Conclusion", st_h2))
    if ok:
        det = " ; ".join(f"{s['cat'].lower()} ({s['name']}, "
                         f"{s['walk_distance_m']} m)"
                         for s in cfg["services"])
        concl = (f"{n} services de catégories différentes se trouvent à "
                 f"moins d'1 km à pied du site : {det}. Le critère S2 est "
                 f"validé : {cfg['score']}/{cfg['score_max']}.")
    elif cfg.get("score_overridden"):
        concl = (f"Après contrôle, le critère S2 est jugé non validé : "
                 f"0/{cfg['score_max']}."
                 + (f" {cfg['override_note']}." if cfg.get("override_note")
                    else ""))
    else:
        concl = (f"Seulement {n} service(s) de catégories différentes "
                 f"trouvé(s) à moins d'1 km à pied du site (minimum requis : "
                 f"{REQUIRED}). Le critère S2 n'est pas validé : "
                 f"0/{cfg['score_max']}.")
    story.append(Paragraph(concl, st_body))
    doc.build(story)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("address", help="adresse de l'actif")
    ap.add_argument("--out", required=True, help="chemin du PDF de sortie")
    ap.add_argument("--locataire", default=None,
                    help="locataire/enseigne de l'actif (confirme ou ancre "
                         "le point de départ)")
    ap.add_argument("--radius", type=int, default=1200)
    ap.add_argument("--offline", action="store_true",
                    help="pas de capture ni de carte en ligne")
    ap.add_argument("--force-services", default=None,
                    help="services réels imposés, séparés par « | » — pour "
                         "appliquer un remplacement validé après un doute IA")
    ap.add_argument("--force-score", type=int, choices=(0, REQUIRED),
                    default=None, help="forcer le score après accord utilisateur")
    ap.add_argument("--override-note", default=None,
                    help="explication inscrite dans le PDF si le score est forcé")
    args = ap.parse_args()

    force_services = ([s.strip() for s in args.force_services.split("|")
                       if s.strip()] if args.force_services else None)
    try:
        cfg = research(args.address, radius=args.radius,
                       locataire=args.locataire, force_services=force_services)
    except Exception as e:
        sys.exit(f"Recherche impossible : {e}")

    if args.force_score is not None:
        cfg["score"] = args.force_score
        cfg["score_overridden"] = True
        if args.override_note:
            cfg["override_note"] = args.override_note

    for c in cfg.get("checks", []):
        print(f"Contrôle : {c}")
    for s in cfg["services"]:
        print(f"Service : {s['cat']} — {s['name']} "
              f"({s['walk_distance_m']} m, {s['walk_time_min']} min)")
    if cfg["score"] == 0:
        print(f"Moins de {REQUIRED} services validés : score 0/{REQUIRED}.",
              file=sys.stderr)

    verdict = ai_control.review_s2(cfg)
    if verdict:
        cfg["ai_control"] = verdict
        ai_control.print_verdict(verdict, item="service")

    proofs = gather_proofs(cfg, offline=args.offline)
    build_pdf(cfg, proofs, args.out)
    print(f"PDF généré : {args.out}")


if __name__ == "__main__":
    main()
