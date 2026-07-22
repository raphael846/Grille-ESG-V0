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


# ---------------------------------------------------------------------------
# S2 et S7 : même principe (avis + remplaçant réel éventuel, jamais inventé)
# ---------------------------------------------------------------------------

S2_SYSTEM = (
    "Tu es un contrôleur qualité ESG. Critère S2 : au moins 3 services de "
    "catégories DIFFÉRENTES à moins d'1 km à pied d'un actif. Un programme a "
    "retenu une liste de services. Dis si ce choix est crédible, SANS "
    "recalculer les distances (fais confiance au programme). Repère les cas "
    "douteux : service qui n'existe vraisemblablement plus ou n'est pas de la "
    "catégorie annoncée ; deux services en réalité de la même catégorie "
    "comptés comme différents ; nom peu crédible.\n"
    "Si tu doutes d'UN service retenu, cherche un remplaçant dans "
    "'candidats_alternatifs' : un vrai service, distance ≤ seuil, d'une "
    "catégorie qui conserve 3 catégories différentes une fois le service "
    "douteux retiré. RÈGLE ABSOLUE : tu ne peux proposer qu'un candidat "
    "présent dans la liste, en recopiant EXACTEMENT son 'name'. N'invente "
    "jamais. Si aucun candidat ne convient, alternative=null.\n"
    'Réponds STRICTEMENT en JSON : {"statut": "confirme" ou "doute", '
    '"raison": "une phrase courte en français", '
    '"confiance": "haute" ou "moyenne" ou "basse", '
    '"alternative": null ou {"remplacer": "nom exact d\'un service retenu", '
    '"par": "nom exact d\'un candidat", "raison": "pourquoi"}}.'
)

S7_SYSTEM = (
    "Tu es un contrôleur qualité ESG. Critère S7 : transports en commun à "
    "moins d'1 km à pied, avec au moins 2 MODES d'acheminement différents "
    "(bus, tram, métro, train, ferry ; ou une desserte bus multi-lignes). Un "
    "programme a retenu une desserte. Dis si c'est crédible, SANS recalculer "
    "les distances. Repère : arrêt qui n'est pas du mode annoncé ; desserte "
    "en réalité insuffisante (un seul mode réel) ; arrêt peu crédible.\n"
    "Si tu doutes d'UN arrêt retenu, cherche un remplaçant dans "
    "'candidats_alternatifs' : un vrai arrêt, distance ≤ seuil, dont le mode "
    "aide à conserver au moins 2 modes différents. RÈGLE ABSOLUE : tu ne peux "
    "proposer qu'un candidat présent dans la liste, en recopiant EXACTEMENT "
    "son 'name'. N'invente jamais. Si aucun candidat ne convient, "
    "alternative=null.\n"
    'Réponds STRICTEMENT en JSON : {"statut": "confirme" ou "doute", '
    '"raison": "une phrase courte en français", '
    '"confiance": "haute" ou "moyenne" ou "basse", '
    '"alternative": null ou {"remplacer": "nom exact d\'un arrêt retenu", '
    '"par": "nom exact d\'un candidat", "raison": "pourquoi"}}.'
)


def _review_list(cfg, critere, system, item_label, api_key, timeout):
    """Socle commun S2/S7 : avis + éventuel remplacement (swap) d'un item
    retenu par un candidat réel. Ne modifie jamais le score."""
    if not available(api_key):
        return None
    services = cfg.get("services", [])
    retenus = [{"name": s.get("name"), item_label: s.get(item_label),
                "distance_a_pied_m": s.get("walk_distance_m")}
               for s in services]
    candidates = [
        {"name": c.get("name"), item_label: c.get(item_label),
         "distance_a_pied_m": c.get("walk_distance_m")}
        for c in cfg.get("candidates", [])
        if c.get("name") not in {s.get("name") for s in services}
    ]
    facts = {
        "actif": cfg["asset"]["address"],
        "items_retenus": retenus,
        "seuil_m": cfg.get("threshold_m", 1000),
        "score_calcule_par_le_programme": cfg.get("score"),
        "controles_automatiques": cfg.get("checks", []),
        "candidats_alternatifs": candidates,
    }
    user = ("Voici le résultat du programme à contrôler :\n"
            + json.dumps(facts, ensure_ascii=False, indent=2))
    retenus_names = {s.get("name") for s in services}
    cand_names = {c["name"] for c in candidates}
    try:
        verdict, model = _call_openai(system, user, api_key=api_key,
                                      timeout=timeout)
        statut = str(verdict.get("statut", "")).lower()
        alt = verdict.get("alternative") or None
        # Garde-fou anti-invention : le remplaçant doit être un vrai candidat
        # et l'item retiré un item réellement retenu.
        if alt and (alt.get("par") not in cand_names
                    or alt.get("remplacer") not in retenus_names):
            alt = None
        return {
            "critere": critere,
            "statut": "doute" if "dout" in statut else "confirme",
            "raison": verdict.get("raison", ""),
            "confiance": verdict.get("confiance", ""),
            "alternative": alt,
            "modele": model,
        }
    except Exception as e:
        return {
            "critere": critere,
            "statut": "indisponible",
            "raison": f"contrôle IA non effectué ({e})",
            "confiance": "",
            "alternative": None,
            "modele": os.environ.get("OPENAI_MODEL", DEFAULT_MODEL),
        }


def review_s2(cfg, api_key=None, timeout=30):
    """Avis IA sur le résultat S2 (services). Voir review_s6 pour le contrat."""
    return _review_list(cfg, "S2", S2_SYSTEM, "cat", api_key, timeout)


def review_s7(cfg, api_key=None, timeout=30):
    """Avis IA sur le résultat S7 (transports). Voir review_s6 pour le contrat."""
    return _review_list(cfg, "S7", S7_SYSTEM, "mode", api_key, timeout)


def print_verdict(verdict, item="élément"):
    """Affiche sur stdout le verdict d'un contrôle IA (S2/S7 : remplacement).

    Ne modifie rien : sert à ce que l'assistant relaie le doute et demande à
    l'utilisateur avant tout changement de score.
    """
    if not verdict:
        return
    suffix = (f"(confiance {verdict.get('confiance', '')}, modèle "
              f"{verdict.get('modele', '')})")
    statut = verdict.get("statut")
    if statut == "confirme":
        print(f"Contrôle IA : confirmé — {verdict.get('raison', '')} {suffix}")
    elif statut == "doute":
        print(f"Contrôle IA : DOUTE — {verdict.get('raison', '')} {suffix}")
        alt = verdict.get("alternative")
        if alt and alt.get("par"):
            print(f"  → Remplacement RÉEL proposé : « {alt.get('remplacer')} » "
                  f"→ « {alt.get('par')} » ({alt.get('raison', '')}). Le score "
                  f"peut être conservé en appliquant ce remplacement.")
        else:
            print(f"  → Aucun remplaçant valable parmi les {item}s réels "
                  f"trouvés. Si le doute est confirmé, le score devrait "
                  f"passer à 0.")
        print("  → Le score n'est PAS modifié pour l'instant. L'assistant doit "
              "demander à l'utilisateur avant tout changement (jamais "
              "inventer un élément).")
    else:
        print(f"Contrôle IA : indisponible — {verdict.get('raison', '')}")
