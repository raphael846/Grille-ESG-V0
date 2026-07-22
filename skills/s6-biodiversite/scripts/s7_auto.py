#!/usr/bin/env python3
"""Critère S7 : mobilité durable — transports en commun à moins d'1 km.

Usage :
    python3 s7_auto.py "adresse de l'actif" --out rapport.pdf
                       [--locataire "Nom"] [--offline]

Méthodologie : recenser les transports en commun (bus, tram, métro, train,
ferry) à moins d'1 km à pied de l'actif, avec le nombre de lignes. Critère
validé si au moins 2 modes d'acheminement différents (2 modes, ou une desserte
bus multi-lignes) → 3 points, sinon 0. Une preuve par arrêt retenu (capture
Google Maps de l'itinéraire piéton → carte OSM → schéma).

Réutilise le socle s6_auto (géocodage, locataire, routeur, Overpass) et
s2_auto (preuves parallèles).
"""

import argparse
import datetime
import sys
import urllib.parse

import ai_control
import build_report
import s2_auto
import s6_auto

THRESHOLD_M = 1000
POINTS = 3

ROUTE_MODES = {"bus": "Bus", "trolleybus": "Bus", "coach": "Bus",
               "tram": "Tram", "subway": "Métro", "train": "Train",
               "light_rail": "Train", "ferry": "Ferry"}


def mode_of_stop(t):
    if t.get("highway") == "bus_stop" or t.get("amenity") == "bus_station":
        return "Bus"
    if t.get("railway") == "tram_stop":
        return "Tram"
    if t.get("railway") in ("station", "halt"):
        return "Métro" if t.get("station") == "subway" else "Train"
    if t.get("amenity") == "ferry_terminal":
        return "Ferry"
    return None


def find_transit(lat, lon, radius=THRESHOLD_M):
    """Arrêts (requête légère) puis lignes (relations, avec repli route_ref).

    Retourne (stops, lines, lines_partial) ou None si même les arrêts sont
    injoignables (ne jamais conclure 0 dans ce cas).
    """
    stops_q = f"""[out:json][timeout:25];
(
  node["highway"="bus_stop"](around:{radius},{lat},{lon});
  node["railway"~"^(tram_stop|station|halt)$"](around:{radius},{lat},{lon});
  node["amenity"~"^(bus_station|ferry_terminal)$"](around:{radius},{lat},{lon});
);
out body;"""
    res = s6_auto.overpass_query(stops_q)
    if res is None:
        return None
    stops, ref_lines = [], {}
    for el in res.get("elements", []):
        t = el.get("tags", {})
        mode = mode_of_stop(t)
        if not mode or "lat" not in el:
            continue
        stops.append({
            "name": t.get("name", f"Arrêt {mode.lower()}"), "mode": mode,
            "id": el.get("id"),
            "lat": float(el["lat"]), "lon": float(el["lon"]),
            "crow_m": build_report.haversine_m(lat, lon,
                                               float(el["lat"]),
                                               float(el["lon"]))})
        for ref in (t.get("route_ref") or "").replace(",", ";").split(";"):
            if ref.strip():
                ref_lines[(mode, ref.strip())] = {"mode": mode,
                                                  "ref": ref.strip()}

    lines_q = (f'[out:json][timeout:25];rel["route"~"^('
               + "|".join(ROUTE_MODES) + f')$"](around:{radius},{lat},{lon});'
               f'out tags;')
    res2 = s6_auto.overpass_query(lines_q)
    if res2 is not None:
        lines = {}
        for el in res2.get("elements", []):
            t = el.get("tags", {})
            mode = ROUTE_MODES.get(t.get("route"))
            ref = t.get("ref") or t.get("name")
            if mode and ref:
                lines[(mode, ref)] = {"mode": mode, "ref": ref}
        return stops, list(lines.values()), False
    return stops, list(ref_lines.values()), True


def stop_lines(stop):
    """Lignes passant par CET arrêt (relations contenant le nœud, ou un
    stop_position à moins de 80 m). None = indéterminable (repli secteur)."""
    modes = "|".join(ROUTE_MODES)
    q = (f'[out:json][timeout:15];('
         f'node({stop["id"]});'
         f'node(around:80,{stop["lat"]},{stop["lon"]})'
         f'["public_transport"="stop_position"];'
         f');rel(bn)["route"~"^({modes})$"];out tags;')
    res = s6_auto.overpass_query(q)
    if res is None:
        return None
    refs = set()
    for el in res.get("elements", []):
        if el.get("type") != "relation":
            continue
        t = el.get("tags", {})
        if ROUTE_MODES.get(t.get("route")) != stop["mode"]:
            continue
        ref = t.get("ref") or t.get("name")
        if ref:
            refs.add(ref)
    return sorted(refs)


def research(address, locataire=None, force_stops=None):
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

    tr = find_transit(lat, lon)
    if tr is None:
        raise RuntimeError(
            "serveurs cartographiques publics momentanément saturés (quota "
            "par IP) — relancer dans 1 à 2 minutes, ne pas conclure 0.")
    stops, lines, lines_partial = tr
    if lines_partial:
        checks.append("recensement des lignes partiel (relations OSM "
                      "indisponibles) : le nombre de lignes peut être "
                      "sous-évalué")
    if not stops:
        raise RuntimeError("aucun arrêt de transport en commun à moins "
                           f"d'1 km de « {address} » — critère non validé "
                           f"(0/{POINTS}).")

    # L'arrêt le plus proche de chaque mode, itinéraires piétons en parallèle
    by_mode = {}
    for s in stops:
        if s["mode"] not in by_mode or s["crow_m"] < by_mode[s["mode"]]["crow_m"]:
            by_mode[s["mode"]] = s
    ordered = sorted(by_mode.values(), key=lambda s: s["crow_m"])[:4]
    if force_stops:
        # Sélection imposée : arrêts réels désignés (ex. remplacement validé
        # par l'utilisateur après un doute du contrôle IA).
        chosen = []
        for name in force_stops:
            m = next((s for s in stops
                      if name.lower() in (s.get("name") or "").lower()), None)
            if m and m not in chosen:
                chosen.append(m)
        ordered = chosen or ordered
    import concurrent.futures
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as ex:
        routes = list(ex.map(
            lambda s: s6_auto.walk_route(lat, lon, s["lat"], s["lon"]),
            ordered))

    sector_note = False
    for s, (dist, mins, route_src) in zip(ordered, routes):
        # Lignes de CET arrêt ; à défaut, celles du secteur (avec mention)
        refs = stop_lines(s)
        if not refs:
            refs = sorted({l["ref"] for l in lines if l["mode"] == s["mode"]})
            sector_note = True
        s.update(cat=s["mode"], walk_distance_m=dist, walk_time_min=mins,
                 route_src=route_src, lines=refs,
                 maps_url=(f"https://www.google.com/maps/dir/{lat},{lon}/"
                           f"{s['lat']},{s['lon']}/data=!4m2!4m1!3e2"))
    # Pool de candidats réels (distances calculées) pour le contrôle IA.
    candidates_list = [
        {"name": s["name"], "mode": s["mode"], "lat": s["lat"], "lon": s["lon"],
         "walk_distance_m": s["walk_distance_m"],
         "walk_time_min": s["walk_time_min"], "lines": s.get("lines", [])}
        for s in sorted(ordered, key=lambda s: s["walk_distance_m"])
    ]
    selected = []
    for s in ordered:
        if len(selected) >= 3:
            break
        if force_stops or s["walk_distance_m"] <= THRESHOLD_M:
            selected.append(s)
            if 900 <= s["walk_distance_m"] <= THRESHOLD_M:
                checks.append(f"ATTENTION : arrêt {s['name']} à "
                              f"{s['walk_distance_m']} m — proche du seuil "
                              f"d'1 km")

    if sector_note:
        checks.append("attribution des lignes par arrêt indisponible pour "
                      "certains arrêts — lignes du secteur (< 1 km) affichées")
    bus_lines = next((s["lines"] for s in selected if s["mode"] == "Bus"), [])
    ok = len(selected) >= 2 or len(bus_lines) >= 2
    transports = (f"{len(selected)} modes" if len(selected) >= 2
                  else f"{len(bus_lines)} lignes de bus" if len(bus_lines) >= 2
                  else f"{len(selected)} transport(s)")

    today = datetime.date.today().strftime("%d/%m/%Y")
    return {
        "asset": {"address": address, "lat": lat, "lon": lon},
        "services": selected,  # même forme que S2 → preuves réutilisées
        "transports": transports,
        "source": "OpenStreetMap (arrêts et lignes) ; itinéraires piétons "
                  f"OSM ; consulté le {today}",
        "analysis_date": today,
        "score": POINTS if ok else 0,
        "score_max": POINTS,
        "threshold_m": THRESHOLD_M,
        "checks": checks,
        "candidates": candidates_list,
    }


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
        Paragraph("Critère S7 — Mobilité durable", st_title),
        Paragraph("Transports en commun à moins d'1 km à pied du site — au "
                  "moins 2 modes d'acheminement différents (lignes, "
                  "transports)", st_sub),
        Spacer(1, 3),
        Paragraph(f"Actif : {cfg['asset']['address']} &nbsp;·&nbsp; Date de "
                  f"l'analyse : {cfg['analysis_date']}", st_sub),
        Spacer(1, 6),
    ]

    verdict = "Critère validé" if ok else "Critère non validé"
    banner = Table([[Paragraph(
        f"<b>SCORE : {cfg['score']} / {cfg['score_max']}</b> — {verdict} — "
        f"{cfg['transports']} à moins d'1 km", st_score)]],
        colWidths=[doc.width])
    banner.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1),
         colors.HexColor("#188038") if ok else colors.HexColor("#d93025")),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
    ]))
    story.append(banner)

    story.append(Paragraph("Desserte retenue", st_h2))
    rows = [[Paragraph("<b>Mode</b>", st_body),
             Paragraph("<b>Arrêt le plus proche</b>", st_body),
             Paragraph("<b>Distance à pied</b>", st_body),
             Paragraph("<b>Lignes</b>", st_body)]]
    for s in cfg["services"]:
        refs = ", ".join(s["lines"][:12]) + ("…" if len(s["lines"]) > 12 else "")
        rows.append([Paragraph(s["mode"], st_body),
                     Paragraph(s["name"], st_body),
                     Paragraph(f"{s['walk_distance_m']} m · "
                               f"{s['walk_time_min']} min", st_body),
                     Paragraph(f"{len(s['lines'])} — {refs}"
                               if s["lines"] else "n.c.", st_body)])
    table = Table(rows, colWidths=[22 * mm, doc.width - 22*mm - 34*mm - 62*mm,
                                   34 * mm, 62 * mm])
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
            Paragraph(f"<b>Preuve {i} — {s['mode']}</b>", st_head),
            Paragraph(f"<b>{s['name']}</b>", st_head),
            Paragraph(f"<b>{s['walk_distance_m']} m · "
                      f"{s['walk_time_min']} min à pied</b>", st_head_r),
        ]], colWidths=[42 * mm, doc.width - 42 * mm - 42 * mm, 42 * mm])
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
    det = " ; ".join(
        f"{s['mode'].lower()} ({s['name']}, {s['walk_distance_m']} m"
        + (f", {len(s['lines'])} ligne(s)" if s["lines"] else "") + ")"
        for s in cfg["services"])
    if ok:
        concl = (f"Le site est desservi par {cfg['transports']} à moins "
                 f"d'1 km à pied : {det}. Le critère S7 est validé : "
                 f"{cfg['score']}/{cfg['score_max']}.")
    elif cfg.get("score_overridden"):
        concl = (f"Après contrôle, le critère S7 est jugé non validé : "
                 f"0/{cfg['score_max']}."
                 + (f" {cfg['override_note']}." if cfg.get("override_note")
                    else ""))
    else:
        concl = (f"Desserte insuffisante à moins d'1 km à pied "
                 f"({det or 'aucun arrêt'}) — il faut au moins 2 modes "
                 f"d'acheminement différents. Le critère S7 n'est pas "
                 f"validé : 0/{cfg['score_max']}.")
    story.append(Paragraph(concl, st_body))
    doc.build(story)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("address", help="adresse de l'actif")
    ap.add_argument("--out", required=True, help="chemin du PDF de sortie")
    ap.add_argument("--locataire", default=None)
    ap.add_argument("--offline", action="store_true")
    ap.add_argument("--force-stops", default=None,
                    help="arrêts réels imposés, séparés par « | » — pour "
                         "appliquer un remplacement validé après un doute IA")
    ap.add_argument("--force-score", type=int, choices=(0, POINTS),
                    default=None, help="forcer le score après accord utilisateur")
    ap.add_argument("--override-note", default=None,
                    help="explication inscrite dans le PDF si le score est forcé")
    args = ap.parse_args()

    force_stops = ([s.strip() for s in args.force_stops.split("|")
                    if s.strip()] if args.force_stops else None)
    try:
        cfg = research(args.address, locataire=args.locataire,
                       force_stops=force_stops)
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
        print(f"Transport : {s['mode']} — {s['name']} "
              f"({s['walk_distance_m']} m, {s['walk_time_min']} min, "
              f"{len(s['lines'])} ligne(s))")
    print(f"Verdict : {cfg['transports']} → {cfg['score']}/{cfg['score_max']}")

    verdict = ai_control.review_s7(cfg)
    if verdict:
        cfg["ai_control"] = verdict
        ai_control.print_verdict(verdict, item="arrêt")

    proofs = s2_auto.gather_proofs(cfg, offline=args.offline)
    build_pdf(cfg, proofs, args.out)
    print(f"PDF généré : {args.out}")


if __name__ == "__main__":
    main()
