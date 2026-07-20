#!/usr/bin/env python3
"""Interface Streamlit des rapports ESG géolocalisés — S6, S2 et S7.

Lancement :
    pip install -r requirements.txt
    streamlit run webapp/streamlit_app.py

L'utilisateur choisit un critère et tape l'adresse de l'actif ; l'app géocode
(API Adresse de l'État, repli Nominatim), cherche les POI et l'itinéraire piéton
(Geoapify si une clé est dans les Secrets, sinon OpenStreetMap), génère le PDF
(carte OSM en ligne ou schéma — la capture Google Maps est désactivée ici) et
l'offre en téléchargement. Pour S6, une vérification OpenAI du résultat est
proposée si une clé OpenAI est saisie. Aucune clé n'est stockée ni journalisée.

Critères :
  - S6 : espace vert praticable à moins d'1 km à pied (4/4 ou 0)
  - S2 : 3 services de catégories différentes à moins d'1 km (3/3 ou 0)
  - S7 : ≥ 2 modes de transport en commun à moins d'1 km (3/3 ou 0)
"""

import json
import os
import re
import sys
import tempfile
import unicodedata

import streamlit as st

# Réutilise le socle des scripts (géocodage, POI, itinéraires, preuve, PDF).
_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_HERE, "..", "skills", "s6-biodiversite", "scripts"))
sys.path.insert(0, _HERE)  # pour importer geoapify_s6 (même dossier)
import build_report  # noqa: E402
import s6_auto        # noqa: E402  -> S6
import s2_auto        # noqa: E402  -> S2 (research/gather_proofs/build_pdf)
import s7_auto        # noqa: E402  -> S7 (research/build_pdf)
import geoapify_s6    # noqa: E402  -> patche s6/s2/s7 pour passer par Geoapify/BAN

# Streamlit Community Cloud n'a pas de navigateur Chromium : la capture Google
# Maps (Playwright) échouerait après un long timeout et tenterait de télécharger
# ~150 Mo de Chromium à chaque rapport. On la neutralise ICI seulement (Flask et
# s6.html gardent la capture) ; la preuve tombe alors sur la carte OpenStreetMap
# en ligne, qui fonctionne sur le cloud.
build_report.try_maps_capture = lambda *a, **k: None  # noqa: E731


def geoapify_key_from_secrets():
    """Clé Geoapify depuis les Secrets Streamlit (vide si non configurés)."""
    try:
        return (st.secrets.get("GEOAPIFY_KEY", "") or "").strip()
    except Exception:
        return ""

# Modèle OpenAI de vérification (peu coûteux, structured output). Ajustable ici.
OPENAI_MODEL = "gpt-4o-mini"

CRITERES = {
    "S6 — Exposition à la biodiversité": "S6",
    "S2 — Présence de services": "S2",
    "S7 — Mobilité durable": "S7",
}


def slugify(text):
    """Nom de fichier sûr (repris de webapp/app.py)."""
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return re.sub(r"[^A-Za-z0-9]+", "_", text).strip("_") or "rapport"


def run_pipeline(kind, address, locataire, out):
    """Exécute le pipeline du critère et écrit le PDF dans `out`.

    Retourne (cfg, proof_label). Le géocodage, les POI et l'itinéraire passent
    par Geoapify/BAN via les patches de geoapify_s6 (repli OSM automatique).
    """
    if kind == "S6":
        cfg = s6_auto.research(address, locataire=locataire)
        proof = build_report.generate(cfg, out)
    elif kind == "S2":
        cfg = s2_auto.research(address, locataire=locataire)
        proofs = s2_auto.gather_proofs(cfg)
        s2_auto.build_pdf(cfg, proofs, out)
        proof = "une preuve par service (carte OSM en ligne / schéma)"
    else:  # S7
        cfg = s7_auto.research(address, locataire=locataire)
        proofs = s2_auto.gather_proofs(cfg)  # même forme que S2
        s7_auto.build_pdf(cfg, proofs, out)
        proof = "une preuve par arrêt (carte OSM en ligne / schéma)"

    # Source honnête : refléter les services réellement utilisés.
    cfg["source"] = cfg["source"].replace(
        "OpenStreetMap (Nominatim + Overpass)", geoapify_s6.source_prefix())
    cfg["source"] = cfg["source"].replace(
        "itinéraires piétons OSM", "itinéraires piétons (Geoapify si clé, sinon OSM)")
    return cfg, proof


def verify_with_openai(cfg, key):
    """Vérifie le résultat S6 via OpenAI. Renvoie {coherent, confiance, remarque}.

    Lève une exception en cas d'échec : le PDF est déjà produit, l'appelant
    traite l'échec comme non bloquant.
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


st.set_page_config(page_title="Rapports ESG géolocalisés", page_icon="🌳")
st.title("Rapports ESG géolocalisés — S6 · S2 · S7")

with st.form("esg"):
    choice = st.selectbox("Critère", list(CRITERES))
    address = st.text_input(
        "Adresse de l'actif",
        placeholder="ex. 4 rue de la Pompe, 75116 Paris")
    locataire = st.text_input(
        "Locataire / enseigne (optionnel)",
        placeholder="confirme le bâtiment, ou ancre le point si l'adresse est vague")
    st.caption("Géocodage via l'API Adresse de l'État (France) ; POI et itinéraire "
               "via Geoapify (clé lue dans les Secrets de l'app), sinon OpenStreetMap.")
    openai_key = st.text_input(
        "Clé API OpenAI (optionnelle — vérification du résultat, S6 uniquement)",
        type="password", placeholder="sk-...")
    st.caption("La clé n'est ni stockée ni journalisée ; pensez à fixer une "
               "limite de dépense sur platform.openai.com.")
    submitted = st.form_submit_button("Générer le rapport PDF")

if submitted:
    kind = CRITERES[choice]
    if not address.strip():
        st.error("Renseignez l'adresse de l'actif avant de générer le rapport.")
        st.stop()
    with st.spinner("Recherche des POI, itinéraire piéton et carte (30 à 60 s)…"):
        try:
            geoapify_s6.set_key(geoapify_key_from_secrets())
            out = os.path.join(tempfile.mkdtemp(prefix="esgst_"),
                               f"{kind}_{slugify(address)}.pdf")
            cfg, proof = run_pipeline(kind, address.strip(),
                                      locataire.strip() or None, out)
            with open(out, "rb") as f:
                pdf_bytes = f.read()
            sources = dict(geoapify_s6.last)
        except Exception as e:
            st.error(f"Recherche impossible : {e}")
            st.stop()

    verif, verif_error = None, None
    if kind == "S6" and openai_key.strip():
        with st.spinner("Vérification du résultat par OpenAI…"):
            try:
                verif = verify_with_openai(cfg, openai_key.strip())
            except Exception as e:
                verif_error = str(e)

    # Persiste le résultat : sinon un clic sur le bouton de téléchargement
    # relance le script et efface l'affichage.
    st.session_state["result"] = {
        "kind": kind, "cfg": cfg, "proof": proof, "pdf_bytes": pdf_bytes,
        "filename": os.path.basename(out), "verif": verif,
        "verif_error": verif_error, "sources": sources,
    }

res = st.session_state.get("result")
if res:
    cfg = res["cfg"]
    kind = res["kind"]
    score = cfg.get("score", 0)
    score_max = cfg.get("score_max", 0)
    validated = score >= score_max
    verdict = "Critère validé" if validated else "Critère non validé"

    if kind == "S6":
        park = cfg["green_space"]
        detail = (f"{park['name']} à {park['walk_distance_m']} m à pied "
                  f"({park['walk_time_min']} min)")
    elif kind == "S2":
        detail = f"{len(cfg['services'])} service(s) de catégories différentes sous le seuil"
    else:
        detail = f"{cfg.get('transports', '')} à moins d'1 km"

    banner = f"SCORE {score}/{score_max} — {verdict} — {detail}"
    (st.success if validated else st.error)(banner)

    st.download_button("⬇️ Télécharger le PDF", data=res["pdf_bytes"],
                       file_name=res["filename"], mime="application/pdf")

    st.markdown(f"**Preuve :** {res['proof']}")
    st.markdown(f"**Source :** {cfg.get('source', '—')}")
    src = res.get("sources") or {}
    if src:
        st.caption(f"Géocodage : {src.get('geocode', '?')} · POI : "
                   f"{src.get('places', '?')}")

    # Détail par critère
    if kind == "S2":
        st.subheader("Services retenus")
        st.table([{"Catégorie": s["cat"], "Nom": s["name"],
                   "Distance": f"{s['walk_distance_m']} m",
                   "Temps": f"{s['walk_time_min']} min"} for s in cfg["services"]])
    elif kind == "S7":
        st.subheader("Desserte retenue")
        rows = []
        for s in cfg["services"]:
            refs = ", ".join(s["lines"][:8]) + ("…" if len(s["lines"]) > 8 else "")
            rows.append({"Mode": s["mode"], "Arrêt": s["name"],
                         "Distance": f"{s['walk_distance_m']} m",
                         "Lignes": (f"{len(s['lines'])} — {refs}" if s["lines"] else "n.c.")})
        st.table(rows)

    checks = cfg.get("checks") or []
    if checks:
        st.subheader("Contrôles automatiques")
        for c in checks:
            (st.warning if c.startswith("ATTENTION") else st.info)(c)

    st.subheader("Vérification OpenAI")
    if kind != "S6":
        st.caption("Vérification IA : disponible pour le critère S6 uniquement.")
    elif res["verif"]:
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
