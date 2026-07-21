#!/usr/bin/env python3
"""Contrôle IA optionnel des résultats ESG (S6 pour l'instant).

Principe : DÉGRADATION GRACIEUSE. Sans clé OpenAI (variable d'environnement
`OPENAI_API_KEY`), toutes les fonctions renvoient None et le pipeline se
comporte EXACTEMENT comme avant — aucune dépendance, aucun appel réseau. Avec
une clé, une IA relit le résultat du programme et rend un avis.

RÈGLE ABSOLUE : ce module ne modifie JAMAIS le score. Il rend seulement un
avis (« confirme » / « doute »). La décision de changer un score revient à
l'utilisateur, via l'assistant qui relaie le doute.

N'utilise que la bibliothèque standard (urllib) : rien à installer.
"""

import json
import os
import urllib.request

OPENAI_URL = "https://api.openai.com/v1/chat/completions"
DEFAULT_MODEL = "gpt-4o-mini"


def available(api_key=None):
    """Vrai si une clé OpenAI est disponible (argument explicite ou variable
    d'environnement). Sinon, aucun contrôle IA."""
    return bool(api_key or os.environ.get("OPENAI_API_KEY"))


def _call_openai(system, user, api_key=None, timeout=30):
    """Appel minimal à l'API Chat Completions d'OpenAI, réponse JSON forcée.

    La clé peut être passée explicitement (usage web : jamais stockée dans
    l'environnement du serveur partagé) ou lue dans OPENAI_API_KEY (usage
    local via Claude Code).
    """
    api_key = api_key or os.environ.get("OPENAI_API_KEY")
    model = os.environ.get("OPENAI_MODEL", DEFAULT_MODEL)
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": 0,
        "response_format": {"type": "json_object"},
    }
    req = urllib.request.Request(
        OPENAI_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Authorization": f"Bearer {api_key}",
                 "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read().decode())
    content = data["choices"][0]["message"]["content"]
    return json.loads(content), model


S6_SYSTEM = (
    "Tu es un contrôleur qualité ESG. Un programme automatique a retenu un "
    "espace vert comme preuve du critère S6 : existe-t-il un espace vert "
    "PRATICABLE — parc, jardin public, square en accès libre — à moins d'1 km "
    "à pied d'un actif immobilier ? Ton rôle est de dire si ce choix est "
    "crédible, SANS recalculer la distance (fais confiance au programme pour "
    "la distance). Repère surtout les cas douteux : espace en réalité privé "
    "ou d'accès restreint ; objet qui n'est pas un vrai espace vert aménagé "
    "praticable (bois/forêt sans aménagement, terrain vague, rond-point "
    "planté, cimetière, champ) ; espace sans nom donc difficile à vérifier.\n"
    "Si tu as un doute sur l'espace retenu, cherche une ALTERNATIVE dans la "
    "liste 'candidats_alternatifs' fournie : un autre espace, plus crédible "
    "comme espace vert public praticable, ET dont la distance est au maximum "
    "le seuil. RÈGLE ABSOLUE : tu ne peux proposer qu'un candidat présent "
    "dans cette liste, en recopiant EXACTEMENT son champ 'name'. Tu n'as pas "
    "le droit d'inventer un lieu qui n'y figure pas. Si aucun candidat de la "
    "liste n'est crédible sous le seuil, l'alternative est null.\n"
    "En cas de doute réel, dis-le ; sinon confirme. "
    'Réponds STRICTEMENT en JSON : {"statut": "confirme" ou "doute", '
    '"raison": "une phrase courte en français", '
    '"confiance": "haute" ou "moyenne" ou "basse", '
    '"alternative": null ou {"name": "nom exact recopié d\'un candidat", '
    '"raison": "pourquoi elle est plus crédible"}}.'
)


def review_s6(cfg, api_key=None, timeout=30):
    """Avis IA sur le résultat S6.

    Retourne None si aucune clé n'est disponible (le pipeline ne change pas).
    Sinon un dict {critere, statut, raison, confiance, alternative, modele}. Le
    statut vaut « confirme », « doute » ou « indisponible » (clé présente mais
    appel échoué). Ne modifie jamais le score.

    `api_key` : clé explicite (usage web). Sinon, lue dans OPENAI_API_KEY.
    """
    if not available(api_key):
        return None
    gs = cfg["green_space"]
    # Candidats réels que l'IA pourra proposer en alternative (jamais autre chose)
    candidates = [
        {"name": c["name"], "type": c.get("type"),
         "distance_a_pied_m": c["walk_distance_m"], "a_un_nom": c.get("named")}
        for c in cfg.get("candidates", [])
    ]
    facts = {
        "actif": cfg["asset"]["address"],
        "espace_vert_retenu": {
            "name": gs["name"],
            "type": gs.get("type"),
            "a_un_nom_dans_openstreetmap": gs.get("named"),
            "distance_a_pied_m": gs["walk_distance_m"],
        },
        "seuil_m": cfg.get("threshold_m", 1000),
        "score_calcule_par_le_programme": cfg.get("score"),
        "controles_automatiques": cfg.get("checks", []),
        "candidats_alternatifs": candidates,
    }
    user = ("Voici le résultat du programme à contrôler :\n"
            + json.dumps(facts, ensure_ascii=False, indent=2))
    valid_names = {c["name"] for c in candidates}
    try:
        verdict, model = _call_openai(S6_SYSTEM, user, api_key=api_key,
                                      timeout=timeout)
        statut = str(verdict.get("statut", "")).lower()
        # Ne garder l'alternative que si elle désigne un VRAI candidat (garde-fou
        # anti-invention côté code, en plus de la consigne au modèle).
        alt = verdict.get("alternative") or None
        if alt and alt.get("name") not in valid_names:
            alt = None
        return {
            "critere": "S6",
            "statut": "doute" if "dout" in statut else "confirme",
            "raison": verdict.get("raison", ""),
            "confiance": verdict.get("confiance", ""),
            "alternative": alt,
            "modele": model,
        }
    except Exception as e:
        return {
            "critere": "S6",
            "statut": "indisponible",
            "raison": f"contrôle IA non effectué ({e})",
            "confiance": "",
            "alternative": None,
            "modele": os.environ.get("OPENAI_MODEL", DEFAULT_MODEL),
        }
