# Rapports ESG géolocalisés — S6, S2, S7

Ce repo génère des rapports PDF de conformité ESG pour un actif immobilier.
**Une adresse en entrée**, un PDF vérifiable en sortie (preuves Google
Maps/OSM, contrôles de cohérence, liens cliquables).

## Commandes (utiliser directement, sans explorer le repo)

| Commande | Critère | Barème |
|---|---|---|
| `/s6 <adresse>` | Espace vert praticable à moins d'1 km à pied | 4 pts ou 0 |
| `/s2 <adresse>` | 3 services de catégories différentes à moins d'1 km | 3 pts ou 0 |
| `/s7 <adresse>` | Transports en commun (≥ 2 modes d'acheminement) à moins d'1 km | 3 pts ou 0 |
| `/esg <adresse>` | Les trois critères d'un coup (3 PDF) | — |

Équivalents CLI :
`python3 skills/s6-biodiversite/scripts/{s6_auto,s2_auto,s7_auto}.py "adresse" --out X.pdf [--locataire "Nom"]`

**Repli impératif** : si un message utilisateur contient `/esg`, `/s6`, `/s2`
ou `/s7` (même en majuscules, même si la commande slash n'a pas été
interceptée par l'interface et arrive comme simple texte), exécuter
immédiatement la procédure correspondante : lire `.claude/commands/<nom>.md`
et suivre ses étapes. Idem pour toute demande en langage naturel du type
« génère le(s) rapport(s) ESG / S6 / S2 / S7 pour <adresse> ». Ne jamais
répondre qu'une commande est inconnue : la procédure est dans ce repo.

## Règles de vitesse (impératives)

1. Dépendances : `python3 -c "import reportlab, PIL, staticmap, playwright" 2>/dev/null || pip install reportlab pillow staticmap playwright` — jamais d'installation à l'aveugle.
2. Une seule commande par critère ; pas de géocodage manuel, pas de test
   isolé de capture, pas de lecture du code des scripts.
3. Ne pas ré-ouvrir/convertir les PDF : le stdout des scripts rend compte de
   tout (résultat, preuves, contrôles). Envoyer les PDF tels quels.
4. « Serveurs saturés » dans la sortie = réessayer une fois après ~1 min,
   jamais conclure un score 0 là-dessus.

Règles d'intégrité, locataire-ancre, niveaux de preuve :
`skills/s6-biodiversite/SKILL.md`. La page équipe autonome : `webapp/s6.html`.

## Règle repo propre (impératif)

Les sessions de génération de rapports **ne committent rien** : PDF écrits
sous `/tmp/rapports-esg/` (hors du repo) et envoyés à l'utilisateur comme
fichiers. `*.pdf` est de toute façon dans le .gitignore. Seules les sessions
de développement du pipeline committent du code.
