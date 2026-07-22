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


# Une « Place / Parvis / Rond-point / Esplanade / Cours » est le plus souvent
# minérale : jamais acceptée comme alternative d'espace vert S6 (garde-fou en
# plus de la consigne au modèle).
_NON_GREEN_PREFIXES = ("place ", "placette ", "parvis ", "rond-point",
                       "rond point", "esplanade ", "cours ")


def _looks_mineral(name):
    n = (name or "").strip().lower()
    return any(n.startswith(p) for p in _NON_GREEN_PREFIXES)


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
    "Tu es un contrôleur qualité ESG. Critère S6 (BINAIRE : validé ou non) : "
    "existe-t-il UN espace vert praticable — parc, jardin public, square en "
    "accès libre — à moins d'1 km à pied de l'actif ? Un programme a retenu "
    "un espace. Ton rôle : CONFIRMER PAR DÉFAUT. Ne doute que s'il y a une "
    "raison SÉRIEUSE que le critère ne soit pas rempli.\n"
    "NE DOUTE PAS pour ces raisons (ce ne sont PAS des doutes → confirme) : un "
    "autre espace vert plus proche existe ; l'espace pourrait être « mieux » ; "
    "le nom t'est inconnu ; incertitude vague. Si l'espace retenu est un vrai "
    "parc/jardin/square/bois/promenade crédible, CONFIRME, même si un autre "
    "est plus proche.\n"
    "DOUTE seulement si l'espace retenu n'est vraisemblablement PAS un espace "
    "vert public praticable : lieu privé ou d'accès restreint ; place/parvis/"
    "rond-point/esplanade MINÉRAL (pas de la vraie verdure) ; cimetière, "
    "terrain vague, champ ; ou distance manifestement au-delà d'1 km.\n"
    "Ne propose une ALTERNATIVE que si tu DOUTES vraiment de l'espace retenu, "
    "et seulement un candidat de 'candidats_alternatifs' qui est un espace "
    "vert PLUS crédible (jamais une place minérale, jamais moins crédible "
    "qu'un vrai parc nommé), sous le seuil. Recopie EXACTEMENT son 'name'. "
    "N'invente jamais. Sinon alternative=null.\n"
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
        if alt and (alt.get("name") not in valid_names
                    or _looks_mineral(alt.get("name", ""))):
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
    "Tu es un contrôleur qualité ESG. Critère S2 (BINAIRE : validé ou non) : "
    "au moins 3 services de catégories DIFFÉRENTES à moins d'1 km à pied. Un "
    "programme a retenu une liste. Ton rôle : CONFIRMER PAR DÉFAUT. Ne doute "
    "que s'il y a une raison SÉRIEUSE que le critère ne soit pas rempli.\n"
    "NE DOUTE PAS pour ces raisons (→ confirme) : un service est un hôtel, un "
    "restaurant, une banque, une école, un commerce (ce sont TOUTES des "
    "catégories S2 valables) ; le nom t'est inconnu ; il pourrait y avoir "
    "mieux. Un établissement nommé issu d'OpenStreetMap/Geoapify existe très "
    "probablement : ne le remets pas en cause sans raison forte.\n"
    "DOUTE seulement si : il y a en réalité moins de 3 catégories DIFFÉRENTES "
    "(ex. deux services retenus sont manifestement la même catégorie), ou un "
    "service retenu est clairement faux (catégorie manifestement erronée).\n"
    "Ne propose un REMPLACEMENT que si tu DOUTES vraiment, et seulement un "
    "candidat réel de 'candidats_alternatifs' (sous le seuil) qui rétablit 3 "
    "catégories différentes. Recopie EXACTEMENT son 'name'. N'invente jamais. "
    "Sinon alternative=null.\n"
    'Réponds STRICTEMENT en JSON : {"statut": "confirme" ou "doute", '
    '"raison": "une phrase courte en français", '
    '"confiance": "haute" ou "moyenne" ou "basse", '
    '"alternative": null ou {"remplacer": "nom exact d\'un service retenu", '
    '"par": "nom exact d\'un candidat", "raison": "pourquoi"}}.'
)

S7_SYSTEM = (
    "Tu es un contrôleur qualité ESG. Critère S7 (BINAIRE : validé ou non) : "
    "transports en commun à moins d'1 km à pied, avec au moins 2 MODES "
    "différents (bus, tram, métro, train, ferry) OU une desserte bus "
    "multi-lignes. Un programme a retenu une desserte. Ton rôle : CONFIRMER "
    "PAR DÉFAUT. Ne doute que s'il y a une raison SÉRIEUSE.\n"
    "NE DOUTE PAS pour ces raisons (→ confirme) : une gare RER/Transilien est "
    "un mode « train » valable ; un arrêt de bus est un mode « bus » valable ; "
    "le nom t'est inconnu. Fais CONFIANCE au mode indiqué par le programme. "
    "Dès qu'il y a au moins 2 modes différents (ou une desserte bus "
    "multi-lignes), CONFIRME.\n"
    "DOUTE seulement si : il n'y a en réalité qu'UN seul mode (et pas de bus "
    "multi-lignes), ou un arrêt retenu est manifestement du mauvais mode.\n"
    "Ne propose un REMPLACEMENT que si tu DOUTES vraiment, et seulement un "
    "candidat réel de 'candidats_alternatifs' (sous le seuil) qui aide à avoir "
    "2 modes différents. Recopie EXACTEMENT son 'name'. N'invente jamais. "
    "Sinon alternative=null.\n"
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
