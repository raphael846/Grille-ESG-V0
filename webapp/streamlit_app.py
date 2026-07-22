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

import io
import json
import os
import re
import sys
import tempfile
import unicodedata
import urllib.parse
import zipfile

import streamlit as st

# Réutilise le socle des scripts (géocodage, POI, itinéraires, preuve, PDF).
_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_HERE, "..", "skills", "s6-biodiversite", "scripts"))
sys.path.insert(0, _HERE)  # pour importer geoapify_s6 (même dossier)
import ai_control     # noqa: E402  -> contrôle IA S6 (alternative + décision)
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
        @keyframes koala-bob { 0%,100% { bottom: 12px; } 50% { bottom: 18px; } }
        @keyframes koala-vanish { 0%,90% { opacity: 1; } 100% { opacity: 0; } }
        #koala-walker {
          position: fixed; bottom: 12px; left: -64px; font-size: 42px;
          z-index: 0; pointer-events: none; will-change: left, bottom, transform;
          animation: koala-walk 48s linear infinite,
                     koala-bob 1.6s ease-in-out infinite,
                     koala-vanish 30s ease-out forwards;
        }
        /* Le contenu passe DEVANT le koala : il se balade derrière les encadrés
           (formulaire, commentaire, bandeaux, tableaux…). */
        [data-testid="stForm"], [data-testid="stAlert"], [data-testid="stCode"],
        [data-testid="stTable"], [data-testid="stDataFrame"],
        [data-testid="stExpander"], [data-testid="stDownloadButton"],
        [data-testid="stNotification"], .stCodeBlock {
          position: relative; z-index: 1;
        }
        /* Bouton « Générer » en vert ; grisé quand désactivé / pendant le traitement */
        [data-testid="stFormSubmitButton"] button {
          background-color: #188038; color: #fff; border: 0; font-weight: 600;
        }
        [data-testid="stFormSubmitButton"] button:hover {
          background-color: #146c2e; color: #fff;
        }
        [data-testid="stFormSubmitButton"] button:disabled,
        [data-testid="stFormSubmitButton"] button[disabled] {
          background-color: #b8c6bc; color: #eef2ee; cursor: not-allowed;
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


def research_only(kind, address, locataire, force_green_space=None,
                  force_selection=None):
    """Étape 1 — recherche (géocodage + POI + itinéraires), SANS générer le PDF.

    Renvoie cfg. La vérification IA tourne sur ce cfg avant la création du PDF.
    Géocodage/POI/itinéraire via Geoapify/BAN (patches geoapify_s6, repli OSM).
    `force_green_space` (S6) impose un espace vert réel désigné ;
    `force_selection` (S2/S7) impose la liste de services/arrêts retenus —
    servent à appliquer une décision validée par l'utilisateur.
    """
    if kind == "S6":
        kwargs = {"locataire": locataire}
        if force_green_space:  # ne pas passer l'argument pour un run normal
            kwargs["force_green_space"] = force_green_space
        cfg = s6_auto.research(address, **kwargs)
    elif kind == "S2":
        kwargs = {"locataire": locataire}
        if force_selection:
            kwargs["force_services"] = force_selection
        cfg = s2_auto.research(address, **kwargs)
    else:  # S7
        kwargs = {"locataire": locataire}
        if force_selection:
            kwargs["force_stops"] = force_selection
        cfg = s7_auto.research(address, **kwargs)

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


def _regen_pdf(r, kind, cfg):
    """Régénère le PDF + le commentaire d'un résultat à partir d'un cfg mis à
    jour, et renvoie le résultat modifié."""
    date_str = cfg.get("analysis_date", "").replace("/", "")
    stem = f"{kind}_{slugify(r['address'])}" + (f"_{date_str}" if date_str else "")
    out = os.path.join(tempfile.mkdtemp(prefix="esgst_"), f"{stem}.pdf")
    proof = generate_pdf(kind, cfg, out)
    with open(out, "rb") as f:
        pdf_bytes = f.read()
    r.update(cfg=cfg, pdf_bytes=pdf_bytes, filename=os.path.basename(out),
             proof=proof, comment=build_comment(kind, cfg))
    return r


def apply_decision(r, decision):
    """Applique la décision de l'utilisateur face à un doute IA (S6/S2/S7).

    action = "alternative" (retenir un espace/service/arrêt réel, score
    conservé), "downgrade" (score forcé à 0) ou "ignore" (garder tel quel).
    Toute décision est explicite : rien n'est changé sans clic de l'utilisateur.
    """
    kind = r.get("kind")
    if "cfg" not in r:
        return r
    action = decision.get("action")
    if action == "ignore":
        r["cfg"].setdefault("ai_control", {})["resolution"] = "conservé tel quel"
        return r

    geoapify_s6.set_key(geoapify_key_from_secrets())
    prev_ai = dict(r.get("cfg", {}).get("ai_control") or {})
    if action == "alternative":
        if kind == "S6":
            cfg = research_only("S6", r["address"], r.get("locataire"),
                                force_green_space=decision["name"])
            prev_ai["resolution"] = f"espace remplacé par « {decision['name']} »"
        else:  # S2/S7 : garder les items non douteux + le remplaçant réel
            kept = [s["name"] for s in r["cfg"].get("services", [])
                    if s["name"] != decision["remplacer"]]
            cfg = research_only(kind, r["address"], r.get("locataire"),
                                force_selection=kept + [decision["par"]])
            prev_ai["resolution"] = (f"« {decision['remplacer']} » remplacé "
                                     f"par « {decision['par']} »")
        cfg["ai_control"] = prev_ai
    else:  # downgrade -> 0
        cfg = r["cfg"]
        cfg["score"] = 0
        cfg["score_overridden"] = True
        cfg["override_note"] = (decision.get("note")
                                or "élément jugé non valable au contrôle IA")
        prev_ai["resolution"] = "score abaissé à 0 après contrôle"
        cfg["ai_control"] = prev_ai
    return _regen_pdf(r, kind, cfg)


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


def render_result(r, idx=0):
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

    # Contrôle IA (S6/S2/S7) : avis + éventuel remplaçant réel, avec boutons de
    # décision (rien n'est changé sans un clic explicite de l'utilisateur).
    ai = cfg.get("ai_control")
    if ai:
        statut = ai.get("statut")
        smaxv = cfg.get("score_max", 0)
        if ai.get("resolution"):
            st.info(f"Contrôle IA — décision appliquée : {ai['resolution']}")
        elif statut == "confirme":
            st.success(f"Contrôle IA — confirmé : {ai.get('raison', '')} "
                       f"(confiance {ai.get('confiance', '?')})")
        elif statut == "doute":
            st.warning(f"Contrôle IA — doute : {ai.get('raison', '')} "
                       f"(confiance {ai.get('confiance', '?')}). "
                       f"Choisissez la suite :")
            alt = ai.get("alternative")
            cols = st.columns(3)
            if alt:
                if kind == "S6":
                    label = f"✅ Retenir « {alt.get('name', '')} »"
                    dec = {"idx": idx, "action": "alternative",
                           "name": alt.get("name")}
                else:
                    label = f"✅ Remplacer par « {alt.get('par', '')} »"
                    dec = {"idx": idx, "action": "alternative",
                           "remplacer": alt.get("remplacer"),
                           "par": alt.get("par")}
                if cols[0].button(label, key=f"alt_{idx}",
                                  use_container_width=True):
                    st.session_state["ai_decision"] = dec
                    st.rerun()
            if cols[1].button(f"⚠️ Confirmer le doute → 0/{smaxv}",
                              key=f"down_{idx}", use_container_width=True):
                st.session_state["ai_decision"] = {
                    "idx": idx, "action": "downgrade", "note": ai.get("raison", "")}
                st.rerun()
            if cols[2].button("↩️ Ignorer (garder tel quel)", key=f"ign_{idx}",
                              use_container_width=True):
                st.session_state["ai_decision"] = {"idx": idx, "action": "ignore"}
                st.rerun()
            if alt:
                st.caption(f"Proposition de l'IA (élément réel trouvé) : "
                           f"{alt.get('raison', '')}")
        elif statut == "indisponible":
            st.caption(f"Contrôle IA indisponible ({ai.get('raison', '')}) — "
                       "le rapport reste valable.")
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
        placeholder="ex. 4 rue de la Pompe, 75116 Paris",
        help="Géocodage via l'API Adresse de l'État (France) ; POI et itinéraire "
             "via Geoapify (clé lue dans les Secrets de l'app), sinon OpenStreetMap.")
    locataire = st.text_input(
        "Locataire / enseigne (optionnel)",
        placeholder="confirme le bâtiment, ou ancre le point si l'adresse est vague")
    # Bouton désactivé (grisé) pendant le traitement — voir le rerun en 2 temps.
    submitted = st.form_submit_button(
        "Générer le(s) rapport(s)",
        disabled=st.session_state.get("busy", False))

# Vérification IA en option, placée SOUS le formulaire (plus intuitive). Hors
# du st.form pour que le champ de clé apparaisse dès qu'on active le toggle
# (dans un st.form il ne réagirait qu'à la soumission).
verif_ia = st.toggle(
    "Vérification IA du résultat (OpenAI)", value=False,
    help="Si activé, chaque critère est vérifié par un modèle OpenAI AVANT la "
         "génération du PDF (résultat cohérent ? verdict correct ?).")
openai_key = ""
if verif_ia:
    openai_key = st.text_input(
        "Clé API OpenAI", type="password", placeholder="sk-...",
        help="La clé n'est ni stockée ni journalisée ; pensez à fixer une limite "
             "de dépense sur platform.openai.com.")

# --- Étape A : clic -> validation, puis on grise le bouton et on relance ---
if submitted:
    kinds = [k for k, _ in CRITERES if checks_ui[k]]
    if not address.strip():
        st.error("Renseignez l'adresse de l'actif avant de générer les rapports.")
        st.stop()
    if not kinds:
        st.error("Cochez au moins un critère.")
        st.stop()

    # Adresse ambiguë (ex. « rue de Paris » -> plusieurs villes) : proposer des
    # précisions plutôt que de géocoder au hasard. Ne bloque que les adresses
    # françaises ambiguës (plusieurs communes parmi les candidats BAN forts) ;
    # une adresse hors France (aucun candidat fort) passe (repli Nominatim).
    cands = geocode_candidates(address.strip())
    high = [c for c in cands if c["score"] >= 0.6]
    if high and len({(c["postcode"], c["city"]) for c in high}) > 1:
        st.warning("Adresse ambiguë — cette voie existe dans plusieurs communes. "
                   "Copiez une des adresses ci-dessous (icône copier), collez-la "
                   "dans le champ Adresse, puis relancez :")
        for c in high[:6]:
            st.code(c["label"], language=None)
        st.stop()

    # Validation OK : mémoriser la demande, griser le bouton, traiter au run suivant.
    st.session_state["pending"] = {
        "kinds": kinds, "address": address.strip(),
        "locataire": locataire.strip() or None, "key": openai_key.strip(),
    }
    st.session_state["busy"] = True
    st.rerun()

# --- Étape B : run « occupé » -> le bouton est grisé, on fait le travail ---
if st.session_state.get("busy"):
    p = st.session_state.get("pending") or {}
    results = []
    try:
        geoapify_s6.set_key(geoapify_key_from_secrets())
        for kind in p.get("kinds", []):
            with st.spinner(f"{kind} — recherche des POI et itinéraires…"):
                try:
                    cfg = research_only(kind, p["address"], p["locataire"])
                    sources = dict(geoapify_s6.last)
                except Exception as e:
                    results.append({"kind": kind, "error": str(e)})
                    continue

            verif, verif_error = None, None  # vérif IA AVANT le PDF
            if p["key"]:
                with st.spinner(f"{kind} — vérification IA du résultat…"):
                    try:
                        # Contrôle intelligent (avis + remplaçant réel
                        # éventuel). Clé passée en argument, jamais stockée
                        # dans l'environnement du serveur partagé.
                        reviewer = {"S6": ai_control.review_s6,
                                    "S2": ai_control.review_s2,
                                    "S7": ai_control.review_s7}[kind]
                        cfg["ai_control"] = reviewer(cfg, api_key=p["key"])
                    except Exception as e:
                        verif_error = str(e)

            with st.spinner(f"{kind} — génération du PDF…"):
                try:
                    date_str = cfg.get("analysis_date", "").replace("/", "")  # 20072026
                    stem = f"{kind}_{slugify(p['address'])}" + (
                        f"_{date_str}" if date_str else "")
                    out = os.path.join(tempfile.mkdtemp(prefix="esgst_"), f"{stem}.pdf")
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
                "address": p["address"], "locataire": p["locataire"],
            })
    finally:
        st.session_state["busy"] = False
        st.session_state.pop("pending", None)
    st.session_state["results"] = results
    st.session_state["just_finished"] = any("cfg" in r for r in results)
    st.rerun()  # réaffiche le bouton en vert + les résultats

if st.session_state.pop("just_finished", False):
    st.balloons()  # confettis une fois la recherche terminée

# Décision de l'utilisateur sur un doute IA (clic sur un bouton) : on
# régénère le résultat concerné, puis on réaffiche.
_decision = st.session_state.pop("ai_decision", None)
if _decision is not None:
    _res = st.session_state.get("results", [])
    _i = _decision.get("idx", -1)
    if 0 <= _i < len(_res):
        with st.spinner("Application de votre décision…"):
            try:
                _res[_i] = apply_decision(_res[_i], _decision)
            except Exception as e:
                _res[_i]["error"] = f"application de la décision impossible : {e}"
        st.session_state["results"] = _res
    st.rerun()

_results = st.session_state.get("results", [])
_pdfs = [r for r in _results if r.get("pdf_bytes")]
if len(_pdfs) > 1:
    _buf = io.BytesIO()
    with zipfile.ZipFile(_buf, "w", zipfile.ZIP_DEFLATED) as _z:
        for r in _pdfs:
            _z.writestr(r["filename"], r["pdf_bytes"])
    st.download_button(f"⬇️ Télécharger les {len(_pdfs)} PDF (ZIP)",
                       data=_buf.getvalue(), file_name="Greenfast_rapports.zip",
                       mime="application/zip", key="dl_all")

for _idx, r in enumerate(_results):
    render_result(r, _idx)
