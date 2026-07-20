#!/usr/bin/env python3
"""Interface Streamlit des rapports ESG géolocalisés — S6, S2 et S7.

Lancement :
    pip install -r requirements.txt
    streamlit run webapp/streamlit_app.py

L'utilisateur coche un ou plusieurs critères et tape l'adresse de l'actif ;
l'app géocode (API Adresse de l'État, repli Nominatim), cherche les POI et
l'itinéraire piéton (Geoapify si une clé est dans les Secrets, sinon
OpenStreetMap), génère un PDF par critère (carte OSM en ligne ou schéma — la
capture Google Maps est désactivée ici) et, pour chaque critère, un commentaire
prêt à coller dans Soneka. Pour S6, une vérification OpenAI est proposée si une
clé OpenAI est saisie. Aucune clé n'est stockée ni journalisée.

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

# Ordre d'affichage / de lancement des critères (S2 -> S6 -> S7).
CRITERES = [
    ("S2", "S2 — Présence de services"),
    ("S6", "S6 — Exposition à la biodiversité"),
    ("S7", "S7 — Mobilité durable"),
]


def slugify(text):
    """Nom de fichier sûr (repris de webapp/app.py)."""
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return re.sub(r"[^A-Za-z0-9]+", "_", text).strip("_") or "rapport"


def run_pipeline(kind, address, locataire, out):
    """Exécute le pipeline du critère et écrit le PDF dans `out`.

    Retourne (cfg, proof_label). Géocodage, POI et itinéraire passent par
    Geoapify/BAN via les patches de geoapify_s6 (repli OSM automatique).
    """
    if kind == "S6":
        cfg = s6_auto.research(address, locataire=locataire)
        proof = build_report.generate(cfg, out)
    elif kind == "S2":
        cfg = s2_auto.research(address, locataire=locataire)
        s2_auto.build_pdf(cfg, s2_auto.gather_proofs(cfg), out)
        proof = "une preuve par service (carte OSM en ligne / schéma)"
    else:  # S7
        cfg = s7_auto.research(address, locataire=locataire)
        s7_auto.build_pdf(cfg, s2_auto.gather_proofs(cfg), out)  # preuves = forme S2
        proof = "une preuve par arrêt (carte OSM en ligne / schéma)"

    # Source honnête : refléter les services réellement utilisés.
    cfg["source"] = cfg["source"].replace(
        "OpenStreetMap (Nominatim + Overpass)", geoapify_s6.source_prefix())
    cfg["source"] = cfg["source"].replace(
        "itinéraires piétons OSM", "itinéraires piétons (Geoapify si clé, sinon OSM)")
    return cfg, proof


def build_comment(kind, cfg):
    """Commentaire court à coller dans Soneka : nom + distance (typologie pour S2)."""
    score, smax = cfg.get("score", 0), cfg.get("score_max", 0)
    verdict = "VALIDÉ" if score >= smax else "NON VALIDÉ"

    if kind == "S6":
        p = cfg["green_space"]
        return (f"S6 — Biodiversité : {verdict} ({score}/{smax}) — "
                f"{p['name']}, {p['walk_distance_m']} m à pied.")

    if kind == "S2":
        det = ", ".join(f"{s['name']} ({s['cat'].lower()}, {s['walk_distance_m']} m)"
                        for s in cfg["services"])
        return f"S2 — Services : {verdict} ({score}/{smax}) — {det or 'aucun'}."

    # S7 : nom du transport + distance
    det = ", ".join(f"{s['mode'].lower()} {s['name']} ({s['walk_distance_m']} m)"
                    for s in cfg["services"])
    return f"S7 — Mobilité : {verdict} ({score}/{smax}) — {det or 'aucun arrêt'}."


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


def render_result(r):
    """Affiche un résultat de critère (bandeau, PDF, commentaire Soneka, détail)."""
    st.divider()
    if r.get("error"):
        st.error(f"{r['kind']} — échec : {r['error']}")
        return
    kind, cfg = r["kind"], r["cfg"]
    score, smax = cfg.get("score", 0), cfg.get("score_max", 0)
    validated = score >= smax
    verdict = "Critère validé" if validated else "Critère non validé"

    if kind == "S6":
        p = cfg["green_space"]
        detail = f"{p['name']} à {p['walk_distance_m']} m à pied ({p['walk_time_min']} min)"
    elif kind == "S2":
        detail = f"{len(cfg['services'])} service(s) de catégories différentes sous le seuil"
    else:
        detail = f"{cfg.get('transports', '')} à moins d'1 km"

    st.subheader(dict(CRITERES)[kind])
    (st.success if validated else st.error)(
        f"SCORE {score}/{smax} — {verdict} — {detail}")

    st.download_button("⬇️ Télécharger le PDF", data=r["pdf_bytes"],
                       file_name=r["filename"], mime="application/pdf",
                       key=f"dl_{kind}")

    st.markdown("**Commentaire à coller dans Soneka** (icône copier en haut à droite) :")
    st.code(r["comment"], language=None)

    src = r.get("sources") or {}
    st.caption(f"Preuve : {r['proof']} · Géocodage : {src.get('geocode', '?')} · "
               f"POI : {src.get('places', '?')}")

    if kind == "S2" and cfg.get("services"):
        st.table([{"Catégorie": s["cat"], "Nom": s["name"],
                   "Distance": f"{s['walk_distance_m']} m",
                   "Temps": f"{s['walk_time_min']} min"} for s in cfg["services"]])
    elif kind == "S7" and cfg.get("services"):
        rows = []
        for s in cfg["services"]:
            refs = ", ".join(s["lines"][:8]) + ("…" if len(s["lines"]) > 8 else "")
            rows.append({"Mode": s["mode"], "Arrêt": s["name"],
                         "Distance": f"{s['walk_distance_m']} m",
                         "Lignes": (f"{len(s['lines'])} — {refs}" if s["lines"] else "n.c.")})
        st.table(rows)

    checks = cfg.get("checks") or []
    if checks:
        with st.expander("Contrôles automatiques"):
            for c in checks:
                (st.warning if c.startswith("ATTENTION") else st.info)(c)

    if kind == "S6":
        if r.get("verif"):
            v = r["verif"]
            (st.success if v.get("coherent") else st.warning)(
                f"Vérification OpenAI — cohérent : {'oui' if v.get('coherent') else 'non'} "
                f"(confiance {v.get('confiance', '?')}). {v.get('remarque', '')}")
        elif r.get("verif_error"):
            st.caption(f"Vérification IA indisponible ({r['verif_error']}) — "
                       "le rapport reste valable.")


st.set_page_config(page_title="Rapports ESG géolocalisés", page_icon="🌳")
st.title("Rapports ESG géolocalisés — S6 · S2 · S7")

with st.form("esg"):
    st.markdown("**Critères à instruire** (cochez-en un ou plusieurs) :")
    checks_ui = {k: st.checkbox(label, value=(k == "S6")) for k, label in CRITERES}
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
    submitted = st.form_submit_button("Générer le(s) rapport(s)")

if submitted:
    kinds = [k for k, _ in CRITERES if checks_ui[k]]
    if not address.strip():
        st.error("Renseignez l'adresse de l'actif avant de générer les rapports.")
        st.stop()
    if not kinds:
        st.error("Cochez au moins un critère.")
        st.stop()

    geoapify_s6.set_key(geoapify_key_from_secrets())
    results = []
    for kind in kinds:
        with st.spinner(f"{kind} — recherche des POI, itinéraire et carte…"):
            try:
                out = os.path.join(tempfile.mkdtemp(prefix="esgst_"),
                                   f"{kind}_{slugify(address)}.pdf")
                cfg, proof = run_pipeline(kind, address.strip(),
                                          locataire.strip() or None, out)
                with open(out, "rb") as f:
                    pdf_bytes = f.read()
                r = {"kind": kind, "cfg": cfg, "proof": proof, "pdf_bytes": pdf_bytes,
                     "filename": os.path.basename(out), "sources": dict(geoapify_s6.last),
                     "comment": build_comment(kind, cfg), "verif": None,
                     "verif_error": None}
                if kind == "S6" and openai_key.strip():
                    try:
                        r["verif"] = verify_with_openai(cfg, openai_key.strip())
                    except Exception as e:
                        r["verif_error"] = str(e)
                results.append(r)
            except Exception as e:
                results.append({"kind": kind, "error": str(e)})
    # Persiste : un clic sur un bouton de téléchargement relance le script.
    st.session_state["results"] = results
    if any("cfg" in r for r in results):
        st.balloons()  # confettis quand la recherche est terminée

for r in st.session_state.get("results", []):
    render_result(r)
