---
name: s6-biodiversite
description: Génère les rapports PDF des critères ESG géolocalisés d'un actif immobilier — S6 (espace vert praticable à moins d'1 km à pied) et S2 (3 services de catégories différentes à moins d'1 km à pied) — avec preuves visuelles réellement intégrées dans le PDF. Utiliser dès que l'utilisateur demande une analyse S6, S2, biodiversité, services de proximité, ou un justificatif « à moins d'1 km » pour un actif.
---

# Critères ESG géolocalisés — S6 et S2

Deux critères partagent le même socle (géocodage, locataire-ancre, itinéraires
piétons, captures Google Maps, contrôles) et les mêmes données d'entrée :
l'adresse de l'actif, plus un locataire optionnel.

## Critère S2 — Présence de services (3 points)

Repérer **3 services de catégories différentes** (restaurant, hôtel, commerce,
bar, café, station-service, supermarché, école, banque, médecin, librairie) à
moins d'**1 km à pied** de l'actif, avec **une preuve visuelle par service**
(capture Google Maps de l'itinéraire piéton montrant la distance, sinon carte
OSM, sinon schéma). Score : 3/3 si 3 services validés, sinon 0/3.

```bash
python3 scripts/s2_auto.py "adresse de l'actif" --out S2_<actif>.pdf \
    [--locataire "Nom"]
```

Règles : **si l'adresse manque, la demander à l'utilisateur avant de
commencer**. Ne jamais conclure 0/3 quand la recherche était dégradée
(serveurs saturés — le script l'imprime dans les contrôles) : relancer.
Les règles d'intégrité, de preuve et de locataire ci-dessous s'appliquent
à l'identique.

## Critère S7 — Mobilité durable (3 points)

Recenser les **transports en commun** (bus, tram, métro, train, ferry) à
moins d'**1 km à pied**, avec le **nombre de lignes** par mode. Critère
validé si au moins **2 modes d'acheminement différents** (2 modes, ou une
desserte bus multi-lignes) → 3/3, sinon 0/3. Une capture Google Maps de
l'itinéraire piéton par arrêt retenu.

```bash
python3 scripts/s7_auto.py "adresse de l'actif" --out S7_<actif>.pdf \
    [--locataire "Nom"]
```

Mêmes règles que S2 : demander l'adresse si absente ; « serveurs saturés »
= relancer après ~1 min, jamais conclure 0. `/esg <adresse>` enchaîne les
trois critères.

# S6 — Exposition à la biodiversité

Ce skill produit un rapport PDF pour le critère S6 : existe-t-il un espace vert
praticable (parc, jardin public, square en accès libre) à moins d'1 km **à pied**
de l'actif ? Score : 4/4 si oui, 0/4 sinon.

## Règle d'or sur la preuve visuelle

**Ne promets JAMAIS d'intégrer dans le PDF une capture d'écran prise avec ton
outil navigateur / recherche web.** Ces captures restent dans la conversation :
elles n'existent pas comme fichiers dans l'environnement d'exécution du code et
ne peuvent donc pas être insérées dans le PDF. C'est la cause historique d'échec
de ce skill — ne pas retomber dans ce piège.

La preuve intégrée au PDF suit cet ordre de priorité :

1. **Capture d'écran fournie par l'utilisateur** (fichier uploadé dans la
   conversation, souvent sous `/mnt/user-data/uploads/`), passée via
   `--screenshot`. Optionnelle — ne pas la réclamer, juste la mentionner.
2. **Capture d'écran automatique de Google Maps** : le script ouvre l'URL
   d'itinéraire dans un Chromium headless (Playwright), accepte le bandeau de
   consentement et capture la carte rendue (`scripts/capture_maps.py`).
   Nécessite un navigateur Chromium ET un accès réseau à google.com dans
   l'environnement d'exécution : disponible dans les sessions Claude Code dont
   la politique réseau autorise google.com ; PAS disponible dans le sandbox du
   chat claude.ai (ni navigateur, ni accès à google.com). Les environnements
   dont le trafic sortant passe par un proxy d'egress (variable `HTTPS_PROXY`,
   TLS ré-terminé) sont gérés automatiquement : le script transmet le proxy à
   Chromium et retente en TLS ≤ 1.2 si le handshake TLS 1.3 est rejeté.
3. **Carte en ligne** : fond OpenStreetMap avec l'itinéraire piéton réel
   (OSRM). Fonctionne seulement si l'environnement a accès au réseau.
4. **Carte schématique hors-ligne** : générée à partir des coordonnées réelles
   (marqueurs, distance, échelle, nord). Toujours disponible, aucun réseau
   requis.

Le script gère seul les bascules 2 → 3 → 4. Il y a donc **toujours** une image
de preuve dans le PDF, avec une légende honnête sur sa provenance. Il affiche
sur stdout le niveau utilisé — le relayer à l'utilisateur, et si la capture
automatique a échoué faute de réseau/navigateur, expliquer que c'est une limite
de l'environnement d'exécution, pas du skill.

## Déroulé

1. **Collecter** : adresse de l'actif ; date d'analyse (aujourd'hui par défaut).
2. **Rechercher** (recherche web / connaissances) : l'espace vert praticable le
   plus proche, son nom, son type, son adresse/entrée, la distance et le temps
   de marche selon l'itinéraire piéton Google Maps, les coordonnées GPS des
   deux points, et l'URL Google Maps de l'itinéraire (elle figure dans le PDF
   comme lien vérifiable — c'est elle qui fait foi, pas l'image).
3. **Proposer** à l'utilisateur d'uploader une capture Google Maps de
   l'itinéraire (optionnel, sans bloquer : s'il ne répond pas ou n'en fournit
   pas, continuer sans).
4. **Écrire** le fichier de configuration JSON (modèle :
   `examples/exemple_pompe.json`, champs documentés ci-dessous).
5. **Exécuter** :

   ```bash
   pip install reportlab pillow staticmap playwright   # si nécessaire
   python3 scripts/build_report.py config.json --out S6_<actif>.pdf \
       [--screenshot /mnt/user-data/uploads/capture.png]
   ```

   Note : `pip install playwright` n'installe PAS de navigateur. Le script
   essaie dans l'ordre le Chromium de Playwright, le Google Chrome de la
   machine, puis installe le Chromium Playwright automatiquement
   (`python3 -m playwright install chromium`, ~150 Mo, une fois).

   La capture automatique Google Maps (niveau 2) est tentée d'office dès que
   `maps_url` est renseigné ; `capture_maps.py` peut aussi être lancé seul
   pour tester : `python3 scripts/capture_maps.py "<url>" --out cap.png`.

6. **Livrer** le PDF à l'utilisateur et indiquer quel niveau de preuve a été
   utilisé (le script l'affiche sur stdout).

## Format du config JSON

```json
{
  "asset":      {"address": "...", "lat": 0.0, "lon": 0.0},
  "green_space": {
    "name": "...", "type": "Parc public", "address": "...",
    "lat": 0.0, "lon": 0.0,
    "walk_distance_m": 300, "walk_time_min": 4,
    "route_note": "via ..."
  },
  "source": "Google Maps, itinéraire piéton, consulté le JJ/MM/AAAA",
  "maps_url": "https://www.google.com/maps/dir/...",
  "analysis_date": "JJ/MM/AAAA",
  "score": 4, "score_max": 4, "threshold_m": 1000
}
```

Notes :
- `walk_distance_m` comparé à `threshold_m` détermine le verdict affiché ;
  garder `score` cohérent (4 si validé, 0 sinon).
- `screenshot_caption` (optionnel, racine du JSON) remplace la légende par
  défaut de la capture.
- `--offline` force le niveau 3 (utile pour tester sans réseau).

## Locataire / enseigne (recommandé pour les actifs de foncière)

Si l'utilisateur connaît un locataire de l'actif (enseigne, hôtel, magasin),
le passer à `s6_auto.py --locataire "Nom"` : il confirme le bâtiment (contrôle
imprimé dans le PDF) et sert d'ancre quand l'adresse est résolue
approximativement. Proposer ce champ quand l'adresse semble vague ; un
locataire introuvable est non bloquant.

## Croisement Google ↔ OpenStreetMap

Quand la capture Google Maps automatique a réussi, regarder la distance et le
temps affichés par Google sur la capture et les comparer aux valeurs de la
fiche (issues d'OpenStreetMap). Signaler la concordance à l'utilisateur
(« Google affiche 600 m / 8 min, cohérent avec les 603 m / 8 min calculés »)
ou l'écart s'il dépasse ~20 % — deux sources indépendantes qui concordent
renforcent le rapport.

## Vitesse d'exécution — IMPORTANT

L'utilisateur attend le PDF en 1 à 2 minutes. Pour ne pas gaspiller de temps :

1. **Ne pas réinstaller les dépendances à l'aveugle.** Vérifier d'abord :
   `python3 -c "import reportlab, PIL, staticmap, playwright" 2>/dev/null || pip install reportlab pillow staticmap playwright`
2. **Une seule commande** : lancer directement `s6_auto.py` / `s2_auto.py`.
   Pas de géocodage manuel préalable, pas de test isolé de `capture_maps.py`,
   pas de lecture du code des scripts.
3. **Ne pas ré-ouvrir ni convertir le PDF pour le vérifier** : la sortie
   stdout du script (services/parc retenus, niveau de preuve, contrôles)
   suffit à en rendre compte. Envoyer le PDF tel quel.
4. Les scripts parallélisent déjà itinéraires et captures — ne pas les
   séquencer à la main.

## Intégrité

- Ne jamais inventer distance, temps ou coordonnées : ils doivent venir d'une
  source vérifiable (itinéraire Google Maps), citée dans `source` et `maps_url`.
- La légende de l'image doit toujours dire ce qu'elle est vraiment (capture
  fournie / carte OSM générée / schéma hors-ligne).
