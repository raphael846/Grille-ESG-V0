#!/usr/bin/env python3
"""Interface Streamlit du rapport S6 : une adresse, un PDF, une vérif OpenAI optionnelle.

Lancement :
    pip install -r requirements.txt
    streamlit run webapp/streamlit_app.py

L'utilisateur tape l'adresse de l'actif ; l'app géocode (API Adresse de l'État,
repli Nominatim), cherche l'espace vert et l'itinéraire piéton (Geoapify si une
clé est disponible, sinon OpenStreetMap), génère le PDF (carte OSM en ligne ou
schéma — la capture Google Maps est désactivée ici, cf. plus bas) et l'offre en
téléchargement. Si une clé OpenAI est fournie, un avis de cohérence du résultat
est affiché — optionnel, jamais bloquant. Aucune clé n'est stockée ni journalisée.
"""

import json
import os
import re
import sys
import tempfile
import unicodedata

import streamlit as st

# Réutilise le socle des scripts (géocodage, Overpass, OSRM, cascade de preuve, PDF).
_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_HERE, "..", "skills", "s6-biodiversite", "scripts"))
sys.path.insert(0, _HERE)  # pour importer geoapify_s6 (même dossier)
import build_report  # noqa: E402  -> generate(cfg, out) renvoie le niveau de preuve
import s6_auto        # noqa: E402  -> research(address, radius, locataire) -> cfg
import geoapify_s6    # noqa: E402  -> patche s6_auto pour passer par Geoapify/BAN

# Streamlit Community Cloud n'a pas de navigateur Chromium : la capture Google
# Maps (Playwright) échouerait après un long timeout et tenterait de télécharger
# ~150 Mo de Chromium à chaque rapport. On la neutralise ICI seulement (Flask et
# s6.html gardent la capture) ; la preuve tombe alors sur la carte OpenStreetMap
# en ligne avec l'itinéraire piéton OSRM réel, qui fonctionne sur le cloud.
build_report.try_maps_capture = lambda *a, **k: None  # noqa: E731


def geoapify_key_from_secrets():
    """Clé Geoapify depuis les Secrets Streamlit (vide si non configurés)."""
    try:
        return (st.secrets.get("GEOAPIFY_KEY", "") or "").strip()
    except Exception:
        return ""

# Modèle OpenAI de vérification (peu coûteux, structured output). Ajustable ici.
OPENAI_MODEL = "gpt-4o-mini"


def slugify(text):
    """Nom de fichier sûr (repris de webapp/app.py)."""
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return re.sub(r"[^A-Za-z0-9]+", "_", text).strip("_") or "rapport"


def verify_with_openai(cfg, key):
    """Vérifie le résultat S6 via OpenAI. Renvoie {coherent, confiance, remarque}.

    Lève une exception en cas d'échec (clé invalide, réseau, quota) : le PDF est
    déjà produit, l'appelant traite l'échec comme non bloquant.
    """
    from openai import OpenAI  # import paresseux : le reste de l'app marche sans openai

    asset, park = cfg["asset"], cfg["green_space"]
    threshold = cfg.get("threshold_m", 1000)
    others = ", ".join(c for c in cfg.get("checks", [])) or "aucun"
    prompt = (
        "Tu vérifies le résultat d'un outil automatique pour un critère ESG "
        "immobilier (S6 : espace vert praticable à moins d'1 km à pied de l'actif).\n\n"
        "Résultat à vérifier :\n"
        f"- Actif : {asset['address']} ({asset['lat']:.5f}, {asset['lon']:.5f})\n"
        f"- Espace vert retenu : {park['name']} ({park.get('type', '?')}), "
        f"coordonnées {park['lat']:.5f}, {park['lon']:.5f}\n"
        f"- Distance à pied : {park['walk_distance_m']} m, "
        f"{park['walk_time_min']} min (source : {cfg.get('source', '?')})\n"
        f"- Contrôles automatiques déjà relevés : {others}\n\n"
        "Contrôles : (1) le nom retenu désigne-t-il vraisemblablement un vrai "
        "espace vert public praticable — et non un découpage administratif "
        "(district, canton, « DED »), un lieu-dit ou un site privé ? (2) la "
        "distance et le temps sont-ils plausibles et cohérents entre eux "
        "(marche ≈ 4-5 km/h) ? (3) le verdict (critère validé si distance "
        f"≤ {threshold} m) est-il correct ? Réponds en français."
    )

    client = OpenAI(api_key=key)
    resp = client.chat.completions.create(
        model=OPENAI_MODEL,
        messages=[{"role": "user", "content": prompt}],
        response_format={
            "type": "json_schema",
            "json_schema": {
                "name": "verif_s6",
                "strict": True,
                "schema": {
                    "type": "object",
                    "properties": {
                        "coherent": {"type": "boolean"},
                        "confiance": {"type": "string",
                                      "enum": ["haute", "moyenne", "basse"]},
                        "remarque": {"type": "string"},
                    },
                    "required": ["coherent", "confiance", "remarque"],
                    "additionalProperties": False,
                },
            },
        },
    )
    return json.loads(resp.choices[0].message.content)


st.set_page_config(page_title="Rapport S6 — Biodiversité", page_icon="🌳")
st.title("Critère S6 — Exposition à la biodiversité")
st.caption("Espace vert praticable à moins d'1 km à pied de l'actif")

with st.form("s6"):
    address = st.text_input(
        "Adresse de l'actif",
        placeholder="ex. 4 rue de la Pompe, 75116 Paris")
    locataire = st.text_input(
        "Locataire / enseigne (optionnel)",
        placeholder="confirme le bâtiment, ou ancre le point si l'adresse est vague")
    st.caption("Géocodage via l'API Adresse de l'État (France) ; espaces verts "
               "et itinéraire via Geoapify (clé lue dans les Secrets de l'app), "
               "sinon OpenStreetMap.")
    openai_key = st.text_input(
        "Clé API OpenAI (optionnelle — active la vérification du résultat)",
        type="password", placeholder="sk-...")
    st.caption("La clé n'est ni stockée ni journalisée ; pensez à fixer une "
               "limite de dépense sur platform.openai.com.")
    submitted = st.form_submit_button("Générer le rapport PDF")

if submitted:
    if not address.strip():
        st.error("Renseignez l'adresse de l'actif avant de générer le rapport.")
        st.stop()
    with st.spinner("Recherche de l'espace vert, itinéraire piéton et carte "
                    "(30 à 60 s)…"):
        try:
            geoapify_s6.set_key(geoapify_key_from_secrets())
            cfg = s6_auto.research(address.strip(),
                                   locataire=locataire.strip() or None)
            # Source honnête : refléter les services réellement utilisés
            # (BAN/Nominatim + Geoapify/Overpass) au lieu du libellé OSM figé.
            cfg["source"] = cfg["source"].replace(
                "OpenStreetMap (Nominatim + Overpass)",
                geoapify_s6.source_prefix())
            out = os.path.join(tempfile.mkdtemp(prefix="s6st_"),
                               f"S6_{slugify(address)}.pdf")
            proof = build_report.generate(cfg, out)
            with open(out, "rb") as f:
                pdf_bytes = f.read()
            sources = dict(geoapify_s6.last)
        except Exception as e:
            st.error(f"Recherche impossible : {e}")
            st.stop()

    verif, verif_error = None, None
    if openai_key.strip():
        with st.spinner("Vérification du résultat par OpenAI…"):
            try:
                verif = verify_with_openai(cfg, openai_key.strip())
            except Exception as e:
                verif_error = str(e)

    # Persiste le résultat : sinon un clic sur le bouton de téléchargement
    # relance le script et efface l'affichage.
    st.session_state["result"] = {
        "cfg": cfg, "proof": proof, "pdf_bytes": pdf_bytes,
        "filename": os.path.basename(out), "verif": verif,
        "verif_error": verif_error, "sources": sources,
    }

res = st.session_state.get("result")
if res:
    cfg = res["cfg"]
    park = cfg["green_space"]
    validated = cfg.get("score", 0) >= cfg.get("score_max", 4)
    detail = (f"{park['name']} à {park['walk_distance_m']} m à pied "
              f"({park['walk_time_min']} min)")
    banner = (f"SCORE {cfg.get('score', 0)}/{cfg.get('score_max', 4)} — "
              f"{'Critère validé' if validated else 'Critère non validé'} — {detail}")
    (st.success if validated else st.error)(banner)

    st.download_button("⬇️ Télécharger le PDF", data=res["pdf_bytes"],
                       file_name=res["filename"], mime="application/pdf")

    st.markdown(f"**Niveau de preuve :** {res['proof']}")
    st.markdown(f"**Source :** {cfg.get('source', '—')}")
    src = res.get("sources") or {}
    if src:
        st.caption(f"Géocodage : {src.get('geocode', '?')} · Espaces verts : "
                   f"{src.get('places', '?')}")
    if cfg.get("maps_url"):
        st.markdown(f"**Itinéraire vérifiable :** [{cfg['maps_url']}]({cfg['maps_url']})")

    checks = cfg.get("checks") or []
    if checks:
        st.subheader("Contrôles automatiques")
        for c in checks:
            (st.warning if c.startswith("ATTENTION") else st.info)(c)

    st.subheader("Vérification OpenAI")
    if res["verif"]:
        v = res["verif"]
        (st.success if v.get("coherent") else st.warning)(
            f"Cohérent : {'oui' if v.get('coherent') else 'non'} — "
            f"confiance {v.get('confiance', '?')}")
        st.write(v.get("remarque", ""))
    elif res["verif_error"]:
        st.info(f"Vérification IA indisponible ({res['verif_error']}) — "
                "le rapport reste valable.")
    else:
        st.caption("Renseignez une clé OpenAI ci-dessus pour activer la "
                   "vérification du résultat.")
