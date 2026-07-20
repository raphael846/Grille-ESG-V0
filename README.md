# Critères ESG géolocalisés — S6 et S2

Génère des **rapports PDF vérifiables** pour les critères géolocalisés de la
grille ESG :

- **S6 — Exposition à la biodiversité** : espace vert praticable à moins
  d'1 km à pied de l'actif (score 4/4 ou 0/4) ;
- **S2 — Présence de services** : 3 services de catégories différentes
  (restaurant, hôtel, commerce, école, banque…) à moins d'1 km à pied, une
  preuve par service (score 3/3 ou 0/3).

L'**adresse est la seule entrée** (+ un locataire optionnel pour confirmer ou
ancrer le bâtiment). Le reste est automatique : géocodage, recherche,
itinéraires piétons réels, cartes/captures de preuve, contrôles de cohérence,
PDF. Dans la page web, un sélecteur choisit le critère ; dans Claude Code,
`/s6` et `/s2` ; en CLI, `s6_auto.py` et `s2_auto.py`.

---

## Trois façons de l'utiliser

### 1. 👥 Équipe — un fichier à double-cliquer (gratuit, zéro installation)

Ouvrir **[`webapp/s6.html`](webapp/s6.html)** dans un navigateur (le
télécharger puis double-cliquer). Taper l'adresse → le PDF se télécharge en
~30 s. Aucune installation, aucun serveur, aucune IA requise, services
OpenStreetMap gratuits.

- La fiche du PDF inclut des **contrôles automatiques** (nom suspect type
  découpage administratif, vitesse de marche anormale, distances incohérentes).
- En option, un champ « clé API Anthropic » active une **vérification du
  résultat par Claude Haiku** (~0,2 centime/rapport). Sans clé, tout marche.
- Distribution : joindre le fichier à une page Notion avec deux lignes de mode
  d'emploi ; chacun le garde sur son bureau.

### 2. 🤖 Claude Code — capture Google Maps réelle (couvert par l'abonnement)

Dans une session Claude Code sur ce repo :

```
/s6 4 rue de la Pompe, 75116 Paris
```

Claude exécute le pipeline, **capture le vrai Google Maps** (itinéraire piéton)
dans un navigateur invisible et l'intègre au PDF, puis vérifie la cohérence.
En session cloud (claude.ai/code), l'environnement doit autoriser le trafic
sortant (google.com, openstreetmap.org) — sinon bascule automatique sur la
carte OSM.

### 3. 💻 Ligne de commande / serveur interne

```bash
pip install -r requirements.txt
python3 skills/s6-biodiversite/scripts/s6_auto.py "adresse de l'actif" --out rapport.pdf
```

Ou héberger la page web pour l'équipe : `python3 webapp/app.py` puis ouvrir
`http://<machine>:8517`.

---

## La preuve dans le PDF (toujours présente, jamais inventée)

Le PDF intègre la meilleure preuve disponible, avec une légende honnête :

1. **Capture d'écran Google Maps** de l'itinéraire piéton (voie Claude Code /
   CLI, nécessite navigateur + réseau) ;
2. **Carte OpenStreetMap** avec l'itinéraire piéton réel ;
3. **Carte schématique** générée depuis les coordonnées réelles (sans réseau).

Dans tous les cas, le PDF contient le **lien Google Maps vérifiable en un
clic** — c'est lui qui fait foi. Distances et temps proviennent toujours d'une
source citée (routeur piéton OpenStreetMap), jamais d'une estimation non
signalée.

## Structure du dépôt

```
webapp/s6.html                     ← le fichier autonome pour l'équipe
webapp/app.py                      ← la même chose en serveur web (Flask)
skills/s6-biodiversite/SKILL.md    ← les règles du skill pour Claude
skills/s6-biodiversite/scripts/
  s6_auto.py                       ← pipeline complet : adresse → PDF
  build_report.py                  ← génération du PDF + choix de la preuve
  capture_maps.py                  ← capture Google Maps (Chromium headless)
.claude/commands/s6.md             ← la commande /s6 de Claude Code
requirements.txt                   ← dépendances Python (voies 2 et 3)
```

## Historique / fiabilisation

Ce projet est né d'un skill qui promettait des captures d'écran impossibles à
tenir (les captures de l'outil navigateur de Claude ne sont pas des fichiers).
Il a été fiabilisé itérativement : preuve toujours intégrée avec provenance
honnête, rotation de serveurs cartographiques quand ils saturent, préférence
aux parcs nommés sous le seuil, détection des artefacts OpenStreetMap (cas
« Swords-Forrest DED 1986 »), contrôles de cohérence imprimés dans le PDF.
