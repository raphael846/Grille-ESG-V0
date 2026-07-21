#!/usr/bin/env python3
"""Génère le rapport PDF du critère S6 (exposition à la biodiversité).

Usage :
    python3 build_report.py config.json --out rapport.pdf [--screenshot capture1.png ...]

La preuve visuelle intégrée au PDF est choisie dans cet ordre :
  1. capture(s) d'écran passée(s) via --screenshot (fichiers réels, ex. upload utilisateur)
  2. carte en ligne (fond OpenStreetMap + itinéraire piéton OSRM) si le réseau le permet
  3. carte schématique générée hors-ligne à partir des coordonnées réelles

Le script n'échoue jamais faute de réseau : il dégrade la preuve et l'indique
honnêtement dans la légende.
"""

import argparse
import json
import math
import os
import socket
import sys
import tempfile

from PIL import Image, ImageDraw, ImageFont

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.lib.utils import ImageReader
from reportlab.platypus import (
    Image as RLImage,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

# ---------------------------------------------------------------------------
# Géométrie
# ---------------------------------------------------------------------------

def haversine_m(lat1, lon1, lat2, lon2):
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


# ---------------------------------------------------------------------------
# Preuve niveau 1bis : capture d'écran automatique de Google Maps (headless)
# ---------------------------------------------------------------------------

def try_maps_capture(maps_url, out_path, timeout=60, width=1400, height=900):
    """Capture l'itinéraire Google Maps via capture_maps.py (Playwright headless).

    Retourne le chemin de la capture, ou None si le navigateur ou le réseau
    manquent. Seul le code de sortie 0 est accepté : une page mal rendue ne
    doit pas servir de preuve.
    """
    import subprocess

    script = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "capture_maps.py")
    try:
        res = subprocess.run(
            [sys.executable, script, maps_url, "--out", out_path,
             "--timeout", str(max(timeout - 10, 15)),
             "--width", str(width), "--height", str(height)],
            capture_output=True, text=True, timeout=timeout,
        )
    except Exception:
        return None
    if res.returncode == 0 and os.path.exists(out_path):
        return out_path
    if res.stderr.strip():
        print("Capture Google Maps impossible :", file=sys.stderr)
        for line in res.stderr.strip().splitlines():
            print(f"  {line}", file=sys.stderr)
    return None


# ---------------------------------------------------------------------------
# Preuve niveau 2 : carte en ligne (OSM + OSRM), best-effort
# ---------------------------------------------------------------------------

def try_online_map(asset, park, out_path, timeout=12):
    """Tente de générer une vraie carte (tuiles OSM + itinéraire piéton OSRM).

    Retourne (chemin, description_itinéraire) ou None si le réseau est bloqué.
    """
    socket.setdefaulttimeout(timeout)
    try:
        import contextlib
        import io
        import urllib.request

        # Sonde rapide : une seule tuile. Si le réseau est bloqué, on abandonne
        # tout de suite au lieu de laisser staticmap réessayer tuile par tuile.
        probe = urllib.request.Request(
            "https://tile.openstreetmap.org/1/0/0.png",
            headers={"User-Agent": "s6-skill/1.0"})
        with urllib.request.urlopen(probe, timeout=min(timeout, 6)):
            pass

        from staticmap import CircleMarker, Line, StaticMap

        route = None
        try:

            url = (
                "https://router.project-osrm.org/route/v1/foot/"
                f"{asset['lon']},{asset['lat']};{park['lon']},{park['lat']}"
                "?overview=full&geometries=geojson"
            )
            req = urllib.request.Request(url, headers={"User-Agent": "s6-skill/1.0"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode())
            route = data["routes"][0]["geometry"]["coordinates"]  # [[lon, lat], ...]
        except Exception:
            route = None

        m = StaticMap(1400, 900, url_template="https://tile.openstreetmap.org/{z}/{x}/{y}.png")
        if route and len(route) >= 2:
            m.add_line(Line(route, "#1a73e8", 6))
            itineraire = "itinéraire piéton réel (OSRM/OpenStreetMap)"
        else:
            m.add_line(Line(
                [[asset["lon"], asset["lat"]], [park["lon"], park["lat"]]],
                "#1a73e8", 5,
            ))
            itineraire = "liaison directe entre les deux points"
        m.add_marker(CircleMarker((asset["lon"], asset["lat"]), "#d93025", 16))
        m.add_marker(CircleMarker((park["lon"], park["lat"]), "#188038", 16))
        with contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            image = m.render()
        image.save(out_path)
        return out_path, itineraire
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Preuve niveau 3 : carte schématique hors-ligne (PIL)
# ---------------------------------------------------------------------------

def _load_font(size):
    for path in (
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/System/Library/Fonts/Helvetica.ttc",
    ):
        if os.path.exists(path):
            try:
                return ImageFont.truetype(path, size)
            except Exception:
                pass
    return ImageFont.load_default()


def offline_schematic(asset, park, walk_distance_m, walk_time_min, out_path):
    """Carte schématique dessinée à partir des coordonnées réelles (aucun réseau)."""
    w, h = 1400, 900
    img = Image.new("RGB", (w, h), "#f6f4ef")
    d = ImageDraw.Draw(img)

    # Grille discrète
    for x in range(0, w, 100):
        d.line([(x, 0), (x, h)], fill="#e8e4da", width=1)
    for y in range(0, h, 100):
        d.line([(0, y), (w, y)], fill="#e8e4da", width=1)

    # Projection locale équirectangulaire, cadrage avec marges
    mean_lat = math.radians((asset["lat"] + park["lat"]) / 2)
    pts_geo = [(asset["lon"], asset["lat"]), (park["lon"], park["lat"])]
    xs = [lon * math.cos(mean_lat) for lon, _ in pts_geo]
    ys = [lat for _, lat in pts_geo]
    span = max(max(xs) - min(xs), max(ys) - min(ys)) or 1e-6
    span *= 1.7  # marge
    cx, cy = (max(xs) + min(xs)) / 2, (max(ys) + min(ys)) / 2
    scale = min(w, h) / span

    def to_px(lon, lat):
        x = (lon * math.cos(mean_lat) - cx) * scale + w / 2
        y = h / 2 - (lat - cy) * scale
        return (x, y)

    pa = to_px(asset["lon"], asset["lat"])
    pp = to_px(park["lon"], park["lat"])

    # Zone verte stylisée autour du parc
    rr = 110
    d.ellipse([pp[0] - rr, pp[1] - rr, pp[0] + rr, pp[1] + rr],
              fill="#cde8c4", outline="#8fc47e", width=3)

    # Liaison en pointillés
    seg = math.hypot(pp[0] - pa[0], pp[1] - pa[1])
    n = max(int(seg / 26), 1)
    for i in range(n):
        t0, t1 = i / n, (i + 0.55) / n
        d.line(
            [
                (pa[0] + (pp[0] - pa[0]) * t0, pa[1] + (pp[1] - pa[1]) * t0),
                (pa[0] + (pp[0] - pa[0]) * t1, pa[1] + (pp[1] - pa[1]) * t1),
            ],
            fill="#1a73e8", width=7,
        )

    f_big, f_med, f_small = _load_font(38), _load_font(30), _load_font(24)

    # Marqueurs
    for (px, py), color in ((pa, "#d93025"), (pp, "#188038")):
        d.ellipse([px - 18, py - 18, px + 18, py + 18], fill=color, outline="white", width=4)

    def label(px, py, lines, anchor_above):
        pad = 10
        widths = [d.textlength(t, font=f_med) for t in lines]
        bw = max(widths) + 2 * pad
        bh = len(lines) * 38 + 2 * pad
        bx = min(max(px - bw / 2, 10), w - bw - 10)
        by = py - 30 - bh if anchor_above else py + 30
        d.rounded_rectangle([bx, by, bx + bw, by + bh], radius=8,
                            fill="white", outline="#bbb", width=2)
        for i, t in enumerate(lines):
            d.text((bx + pad, by + pad + i * 38), t, fill="#222", font=f_med)

    # Étiquettes sous les marqueurs, badge distance au-dessus de la liaison :
    # avec une liaison proche de l'horizontale ils ne se chevauchent pas.
    label(pa[0], pa[1], ["Actif — " + asset["short_label"]], anchor_above=False)
    label(pp[0], pp[1], [park["name"]], anchor_above=False)

    midx, midy = (pa[0] + pp[0]) / 2, (pa[1] + pp[1]) / 2
    dist_txt = f"{walk_distance_m} m à pied · {walk_time_min} min"
    tw = d.textlength(dist_txt, font=f_big)
    d.rounded_rectangle([midx - tw / 2 - 14, midy - 80, midx + tw / 2 + 14, midy - 24],
                        radius=10, fill="#1a73e8")
    d.text((midx - tw / 2, midy - 72), dist_txt, fill="white", font=f_big)

    # Barre d'échelle : `span` degrés de latitude couvrent min(w, h) pixels
    m_per_px = span * 111320.0 / min(w, h)
    for bar_m in (50, 100, 200, 250, 500, 1000):
        if bar_m / m_per_px >= 120:
            break
    bar_px = bar_m / m_per_px
    x0, y0 = 40, h - 50
    d.line([(x0, y0), (x0 + bar_px, y0)], fill="#222", width=5)
    d.line([(x0, y0 - 10), (x0, y0 + 10)], fill="#222", width=5)
    d.line([(x0 + bar_px, y0 - 10), (x0 + bar_px, y0 + 10)], fill="#222", width=5)
    d.text((x0, y0 - 45), f"{bar_m} m", fill="#222", font=f_small)

    # Flèche nord
    nx, ny = w - 60, 90
    d.polygon([(nx, ny - 40), (nx - 16, ny + 12), (nx + 16, ny + 12)], fill="#222")
    d.text((nx - 10, ny + 18), "N", fill="#222", font=f_med)

    img.save(out_path)
    return out_path


# ---------------------------------------------------------------------------
# PDF
# ---------------------------------------------------------------------------

def build_pdf(cfg, proof_images, out_pdf):
    asset, park = cfg["asset"], cfg["green_space"]
    threshold = cfg.get("threshold_m", 1000)
    validated = park["walk_distance_m"] <= threshold
    score = cfg.get("score", 4 if validated else 0)
    score_max = cfg.get("score_max", 4)
    # Score forcé par l'utilisateur (après un doute du contrôle IA) : le verdict
    # affiché suit alors le score décidé, pas seulement la distance.
    if cfg.get("score_overridden"):
        validated = score >= score_max

    styles = getSampleStyleSheet()
    st_title = ParagraphStyle("t", parent=styles["Title"], fontSize=16, spaceAfter=1)
    st_sub = ParagraphStyle("s", parent=styles["Normal"], fontSize=9.5,
                            textColor=colors.HexColor("#555"), alignment=TA_CENTER)
    st_h2 = ParagraphStyle("h2", parent=styles["Heading2"], fontSize=11.5,
                           spaceBefore=8, spaceAfter=3)
    st_body = ParagraphStyle("b", parent=styles["Normal"], fontSize=9.5, leading=13)
    st_score = ParagraphStyle("sc", parent=styles["Normal"], fontSize=11.5,
                              alignment=TA_CENTER,
                              textColor=colors.white, leading=15)
    st_cap = ParagraphStyle("cap", parent=styles["Normal"], fontSize=7.5,
                            textColor=colors.HexColor("#666"), leading=10)

    doc = SimpleDocTemplate(out_pdf, pagesize=A4,
                            leftMargin=15 * mm, rightMargin=15 * mm,
                            topMargin=11 * mm, bottomMargin=11 * mm)
    story = []

    story.append(Paragraph("Critère S6 — Exposition à la biodiversité", st_title))
    story.append(Paragraph(
        f"Espace vert praticable à moins de {threshold / 1000:g} km à pied du site",
        st_sub))
    story.append(Spacer(1, 3))
    story.append(Paragraph(
        f"Actif : {asset['address']} &nbsp;·&nbsp; Date de l'analyse : {cfg['analysis_date']}",
        st_sub))
    story.append(Spacer(1, 6))

    verdict = "Critère validé" if validated else "Critère non validé"
    detail = (f"{park['name']} à {park['walk_distance_m']} m à pied "
              f"({park['walk_time_min']} min)")
    banner = Table(
        [[Paragraph(f"<b>SCORE : {score} / {score_max}</b> — {verdict} — {detail}",
                    st_score)]],
        colWidths=[doc.width])
    banner.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1),
         colors.HexColor("#188038") if validated else colors.HexColor("#d93025")),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ("LEFTPADDING", (0, 0), (-1, -1), 10),
        ("RIGHTPADDING", (0, 0), (-1, -1), 10),
    ]))
    story.append(banner)

    story.append(Paragraph("Fiche récapitulative", st_h2))
    rows = [
        ["Type d'espace vert", park.get("type", "—")],
        ["Nom", park["name"]],
        ["Localisation", park.get("address", "—")],
        ["Distance à pied", f"{park['walk_distance_m']} m"],
        ["Temps de marche",
         f"{park['walk_time_min']} min"
         + (f" ({park['route_note']})" if park.get("route_note") else "")],
        ["Source", cfg.get("source", "—")],
    ]
    if cfg.get("checks"):
        rows.append(["Contrôles automatiques", " ; ".join(cfg["checks"])])
    ai = cfg.get("ai_control")
    if ai:
        prefix = {"confirme": "Confirmé", "doute": "DOUTE",
                  "indisponible": "Indisponible"}.get(ai["statut"], "")
        detail = f"{prefix} — {ai['raison']}" if ai.get("raison") else prefix
        if ai.get("confiance"):
            detail += f" (confiance {ai['confiance']})"
        if ai.get("alternative"):
            detail += (f". Alternative envisagée : "
                       f"{ai['alternative'].get('name', '')}")
        rows.append(["Contrôle IA", detail])
    table = Table([[Paragraph(f"<b>{k}</b>", st_body), Paragraph(str(v), st_body)]
                   for k, v in rows],
                  colWidths=[45 * mm, doc.width - 45 * mm])
    table.setStyle(TableStyle([
        ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#cccccc")),
        ("BACKGROUND", (0, 0), (0, -1), colors.HexColor("#f2f2f2")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
    ]))
    story.append(table)

    if cfg.get("maps_url"):
        story.append(Spacer(1, 6))
        story.append(Paragraph(
            f'Itinéraire vérifiable : <link href="{cfg["maps_url"]}" '
            f'color="blue">{cfg["maps_url"]}</link>', st_cap))

    story.append(Paragraph(
        f"Preuve — {park['name']} ({park['walk_distance_m']} m · "
        f"{park['walk_time_min']} min à pied)", st_h2))

    for img_path, caption in proof_images:
        with Image.open(img_path) as im:
            iw, ih = im.size
        max_w = doc.width
        max_h = 108 * mm
        ratio = min(max_w / iw, max_h / ih)
        story.append(RLImage(img_path, width=iw * ratio, height=ih * ratio))
        story.append(Spacer(1, 3))
        story.append(Paragraph(caption, st_cap))
        story.append(Spacer(1, 8))

    story.append(Paragraph("Conclusion", st_h2))
    if validated:
        concl = (f"Un espace vert praticable ({park['name']}, "
                 f"{park.get('type', 'espace vert').lower()}) se trouve à "
                 f"{park['walk_distance_m']} m à pied du site, sous le seuil "
                 f"de {threshold / 1000:g} km. Le critère S6 est validé : "
                 f"{score}/{score_max}.")
    else:
        concl = (f"Aucun espace vert praticable n'a été identifié sous le seuil de "
                 f"{threshold / 1000:g} km à pied ({park['name']} est à "
                 f"{park['walk_distance_m']} m). Le critère S6 n'est pas validé : "
                 f"{score}/{score_max}.")
    if cfg.get("override_note"):
        concl += (f" Score ajusté après contrôle : {cfg['override_note']}.")
    story.append(Paragraph(concl, st_body))

    doc.build(story)


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def generate(cfg, out_pdf, screenshots=(), offline=False):
    """Choisit la meilleure preuve disponible et construit le PDF.

    Retourne un libellé du niveau de preuve utilisé.
    """
    asset, park = cfg["asset"], cfg["green_space"]
    asset.setdefault("short_label", asset["address"].split(",")[0])

    proof_images = []
    proof_level = None
    for s in screenshots:
        if not os.path.exists(s):
            raise FileNotFoundError(f"Capture introuvable : {s}")
        proof_images.append((s, cfg.get(
            "screenshot_caption",
            f"Capture d'écran de l'itinéraire piéton Google Maps, "
            f"consultée le {cfg['analysis_date']}. L'itinéraire officiel reste "
            f"vérifiable via le lien de la fiche récapitulative.")))
        proof_level = f"capture d'écran fournie ({s})"

    if not proof_images and not offline and cfg.get("maps_url"):
        tmpdir = tempfile.mkdtemp(prefix="s6cap_")
        cap_path = os.path.join(tmpdir, "maps_capture.png")
        if try_maps_capture(cfg["maps_url"], cap_path):
            proof_images.append((cap_path,
                f"Capture d'écran de l'itinéraire piéton Google Maps, prise "
                f"automatiquement le {cfg['analysis_date']} "
                f"(navigateur headless). Itinéraire vérifiable via le lien de "
                f"la fiche récapitulative."))
            proof_level = "capture d'écran Google Maps automatique"

    if not proof_images:
        tmpdir = tempfile.mkdtemp(prefix="s6map_")
        map_path = os.path.join(tmpdir, "map.png")
        online = None if offline else try_online_map(asset, park, map_path)
        if online:
            path, itin = online
            proof_images.append((path,
                f"Carte OpenStreetMap générée automatiquement ({itin}) ; "
                f"distance et temps affichés issus de la source citée dans la "
                f"fiche récapitulative."))
            proof_level = "carte en ligne OpenStreetMap"
        else:
            offline_schematic(asset, park, park["walk_distance_m"],
                              park["walk_time_min"], map_path)
            proof_images.append((map_path,
                f"Carte schématique générée à partir des coordonnées réelles "
                f"(actif : {asset['lat']:.5f}, {asset['lon']:.5f} ; "
                f"{park['name']} : {park['lat']:.5f}, {park['lon']:.5f}). "
                f"La distance et le temps proviennent de l'itinéraire piéton "
                f"cité en source, vérifiable via le lien de la fiche "
                f"récapitulative."))
            proof_level = "carte schématique hors-ligne"

    print(f"Preuve : {proof_level}")
    build_pdf(cfg, proof_images, out_pdf)
    print(f"PDF généré : {out_pdf}")
    return proof_level


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("config", help="fichier JSON de configuration")
    ap.add_argument("--out", required=True, help="chemin du PDF de sortie")
    ap.add_argument("--screenshot", nargs="*", default=[],
                    help="capture(s) d'écran (fichiers image) à intégrer comme preuve")
    ap.add_argument("--offline", action="store_true",
                    help="ne pas tenter la carte en ligne")
    args = ap.parse_args()

    with open(args.config, encoding="utf-8") as f:
        cfg = json.load(f)
    try:
        generate(cfg, args.out, screenshots=args.screenshot, offline=args.offline)
    except FileNotFoundError as e:
        sys.exit(str(e))


if __name__ == "__main__":
    main()
