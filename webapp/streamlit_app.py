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
import urllib.parse

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


def koala_walk():
    """Un koala qui se balade en bas de l'appli (déco, n'intercepte pas les clics)."""
    st.markdown(
        """
        <style>
        @keyframes koala-walk {
          0%   { left: -64px; transform: scaleX(1); }
          49%  { left: calc(100vw - 8px); transform: scaleX(1); }
          50%  { left: calc(100vw - 8px); transform: scaleX(-1); }
          99%  { left: -64px; transform: scaleX(-1); }
          100% { left: -64px; transform: scaleX(1); }
        }
        @keyframes koala-bob { 0%,100% { bottom: 12px; } 50% { bottom: 22px; } }
        #koala-walker {
          position: fixed; bottom: 12px; left: -64px; font-size: 42px;
          z-index: 9999; pointer-events: none; will-change: left, bottom, transform;
          animation: koala-walk 26s linear infinite,
                     koala-bob 0.6s ease-in-out infinite;
        }
        </style>
        <div id="koala-walker">🐨</div>
        """,
        unsafe_allow_html=True,
    )


def slugify(text):
    """Nom de fichier sûr (repris de webapp/app.py)."""
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return re.sub(r"[^A-Za-z0-9]+", "_", text).strip("_") or "rapport"


def geocode_candidates(address, limit=5):
    """Candidats d'adresse via l'API Adresse (BAN). Vide si non-France / échec."""
    url = ("https://api-adresse.data.gouv.fr/search/?limit=%d&q=%s"
           % (limit, urllib.parse.quote(address)))
    try:
        data = s6_auto.http_json(url, timeout=8)
    except Exception:
        return []
    out = []
    for f in data.get("features", []):
        p = f.get("properties", {})
        out.append({"label": p.get("label", ""), "city": p.get("city", ""),
                    "postcode": p.get("postcode", ""),
                    "score": p.get("score", 0), "type": p.get("type", "")})
    return out


def research_only(kind, address, locataire):
    """Étape 1 — recherche (géocodage + POI + itinéraires), SANS générer le PDF.

    Renvoie cfg. La vérification IA tourne sur ce cfg avant la création du PDF.
    Géocodage/POI/itinéraire via Geoapify/BAN (patches geoapify_s6, repli OSM).
    """
    if kind == "S6":
        cfg = s6_auto.research(address, locataire=locataire)
    elif kind == "S2":
        cfg = s2_auto.research(address, locataire=locataire)
    else:  # S7
        cfg = s7_auto.research(address, locataire=locataire)

    # Source honnête : refléter les services réellement utilisés.
    cfg["source"] = cfg["source"].replace(
        "OpenStreetMap (Nominatim + Overpass)", geoapify_s6.source_prefix())
    cfg["source"] = cfg["source"].replace(
        "itinéraires piétons OSM", "itinéraires piétons (Geoapify si clé, sinon OSM)")
    return cfg


def generate_pdf(kind, cfg, out):
    """Étape 3 — génère le PDF depuis un cfg déjà recherché. Renvoie le libellé
    de preuve."""
    if kind == "S6":
        return build_report.generate(cfg, out)
    if kind == "S2":
        s2_auto.build_pdf(cfg, s2_auto.gather_proofs(cfg), out)
        return "une preuve par service (carte Geoapify / schéma)"
    s7_auto.build_pdf(cfg, s2_auto.gather_proofs(cfg), out)  # preuves = forme S2
    return "une preuve par arrêt (carte Geoapify / schéma)"


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


def _verif_prompt(kind, cfg):
    """Construit le prompt de vérification adapté au critère (S2/S6/S7)."""
    asset = cfg["asset"]
    threshold = cfg.get("threshold_m", 1000)
    score, smax = cfg.get("score", 0), cfg.get("score_max", 0)
    checks = "; ".join(cfg.get("checks", [])) or "aucun"
    head = (f"Actif : {asset['address']} ({asset['lat']:.5f}, {asset['lon']:.5f}). "
            f"Seuil : {threshold} m à pied. Score calculé par l'outil : {score}/{smax}. "
            f"Source : {cfg.get('source', '?')}. "
            f"Contrôles automatiques déjà relevés : {checks}.")

    if kind == "S6":
        p = cfg["green_space"]
        body = (f"Critère S6 — espace vert praticable à moins d'1 km à pied. "
                f"Espace vert retenu : {p['name']} ({p.get('type', '?')}), "
                f"{p['walk_distance_m']} m ({p['walk_time_min']} min), "
                f"coordonnées {p['lat']:.5f}, {p['lon']:.5f}.")
        controls = ("(1) est-ce un vrai espace vert public praticable (et non un "
                    "découpage administratif, un lieu-dit ou un site privé) ? "
                    "(2) distance/temps plausibles et cohérents (marche ≈ 4-5 km/h) ? "
                    "(3) verdict correct (validé si distance ≤ seuil) ?")
    elif kind == "S2":
        det = "; ".join(f"{s['cat']} — {s['name']}, {s['walk_distance_m']} m "
                        f"({s['walk_time_min']} min)" for s in cfg["services"])
        body = (f"Critère S2 — au moins 3 services de catégories différentes à moins "
                f"d'1 km à pied. Services retenus : {det or 'aucun'}.")
        controls = ("(1) chaque service est-il vraisemblablement un vrai établissement "
                    "de la catégorie indiquée ? (2) les catégories sont-elles bien "
                    "différentes les unes des autres ? (3) distances/temps plausibles "
                    "et cohérents (marche ≈ 4-5 km/h) ? (4) verdict correct (validé si "
                    "≥ 3 services de catégories différentes sous le seuil) ?")
    else:  # S7
        det = "; ".join(f"{s['mode']} — {s['name']}, {s['walk_distance_m']} m, "
                        f"{len(s.get('lines', []))} ligne(s)" for s in cfg["services"])
        body = (f"Critère S7 — transports en commun à moins d'1 km à pied, au moins "
                f"2 modes d'acheminement. Desserte retenue : {det or 'aucune'}. "
                f"Verdict transports : {cfg.get('transports', '')}.")
        controls = ("(1) sont-ce de vrais arrêts de transport en commun, avec les "
                    "bons modes ? (2) y a-t-il au moins 2 modes différents (ou une "
                    "desserte bus multi-lignes) ? (3) distances plausibles ? "
                    "(4) verdict correct ?")

    return (f"Tu vérifies le résultat d'un outil automatique pour un critère ESG "
            f"immobilier.\n\n{head}\n\n{body}\n\nContrôles à effectuer : {controls} "
            f"Réponds en français.")


def verify_with_openai(kind, cfg, key):
    """Vérifie le résultat d'un critère (S2/S6/S7) via OpenAI.

    Renvoie {coherent, confiance, remarque}. Lève une exception en cas d'échec :
    le PDF est déjà produit, l'appelant traite l'échec comme non bloquant.
    """
    from openai import OpenAI  # import paresseux : le reste de l'app marche sans openai

    prompt = _verif_prompt(kind, cfg)
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

    if r.get("verif"):
        v = r["verif"]
        (st.success if v.get("coherent") else st.warning)(
            f"Vérification OpenAI — cohérent : {'oui' if v.get('coherent') else 'non'} "
            f"(confiance {v.get('confiance', '?')}). {v.get('remarque', '')}")
    elif r.get("verif_error"):
        st.caption(f"Vérification IA indisponible ({r['verif_error']}) — "
                   "le rapport reste valable.")


st.set_page_config(page_title="Greenfast — grille ESG", page_icon="🌿")
st.title("🌿 Greenfast")
st.caption("Instruction automatisée de la grille ESG d'un actif — critères "
           "géolocalisés services (S2), biodiversité (S6) et mobilité (S7)")
koala_walk()

with st.form("esg"):
    st.markdown("**Critères à instruire** (cochez-en un ou plusieurs) :")
    checks_ui = {k: st.checkbox(label, value=False) for k, label in CRITERES}
    address = st.text_input(
        "Adresse de l'actif",
        placeholder="ex. 4 rue de la Pompe, 75116 Paris")
    locataire = st.text_input(
        "Locataire / enseigne (optionnel)",
        placeholder="confirme le bâtiment, ou ancre le point si l'adresse est vague")
    st.caption("Géocodage via l'API Adresse de l'État (France) ; POI et itinéraire "
               "via Geoapify (clé lue dans les Secrets de l'app), sinon OpenStreetMap.")
    openai_key = st.text_input(
        "Clé API OpenAI (optionnelle — vérification IA de chaque critère avant le PDF)",
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

    # Adresse ambiguë (ex. « rue de Paris » -> plusieurs villes) : demander une
    # précision plutôt que de géocoder au hasard. Ne bloque que les adresses
    # françaises ambiguës ; une adresse hors France (absente de la BAN) passe.
    # La BAN note ~0,98 chaque correspondance exacte du nom de voie : une voie
    # présente dans plusieurs communes ressort donc en plusieurs candidats forts.
    # Ambiguïté = plusieurs communes parmi les candidats à score élevé. Un score
    # faible partout (adresse hors France) => aucun candidat fort => on laisse
    # passer (le pipeline tentera Nominatim).
    cands = geocode_candidates(address.strip())
    high = [c for c in cands if c["score"] >= 0.6]
    if high and len({(c["postcode"], c["city"]) for c in high}) > 1:
        st.warning("Adresse ambiguë — cette voie existe dans plusieurs communes. "
                   "Précisez la **ville** ou le **code postal**, puis relancez. "
                   "Par exemple :")
        for c in high[:6]:
            st.write(f"• {c['label']}")
        st.stop()

    geoapify_s6.set_key(geoapify_key_from_secrets())
    key = openai_key.strip()
    results = []
    for kind in kinds:
        # 1) Recherche (sans PDF)
        with st.spinner(f"{kind} — recherche des POI et itinéraires…"):
            try:
                cfg = research_only(kind, address.strip(), locataire.strip() or None)
                sources = dict(geoapify_s6.last)
            except Exception as e:
                results.append({"kind": kind, "error": str(e)})
                continue

        # 2) Vérification IA AVANT la création du PDF (chaque critère)
        verif, verif_error = None, None
        if key:
            with st.spinner(f"{kind} — vérification IA du résultat…"):
                try:
                    verif = verify_with_openai(kind, cfg, key)
                except Exception as e:
                    verif_error = str(e)

        # 3) Génération du PDF
        with st.spinner(f"{kind} — génération du PDF…"):
            try:
                out = os.path.join(tempfile.mkdtemp(prefix="esgst_"),
                                   f"{kind}_{slugify(address)}.pdf")
                proof = generate_pdf(kind, cfg, out)
                with open(out, "rb") as f:
                    pdf_bytes = f.read()
            except Exception as e:
                results.append({"kind": kind, "error": str(e)})
                continue

        results.append({
            "kind": kind, "cfg": cfg, "proof": proof, "pdf_bytes": pdf_bytes,
            "filename": os.path.basename(out), "sources": sources,
            "comment": build_comment(kind, cfg), "verif": verif,
            "verif_error": verif_error,
        })
    # Persiste : un clic sur un bouton de téléchargement relance le script.
    st.session_state["results"] = results
    if any("cfg" in r for r in results):
        st.balloons()  # confettis quand la recherche est terminée

for r in st.session_state.get("results", []):
    render_result(r)
