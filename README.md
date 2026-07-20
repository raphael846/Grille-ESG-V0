# Greenfast — instruction ESG géolocalisée

Génère des **rapports PDF vérifiables** et des **commentaires prêts à coller
dans Soneka** pour les critères géolocalisés de la grille ESG d'un actif
immobilier. Une **adresse en entrée**, un PDF + un commentaire en sortie.

Vocation à terme : couvrir toute la grille ESG. Aujourd'hui, les trois critères
géolocalisés (tout se joue **à moins d'1 km à pied** de l'actif) :

| Critère | Question | Barème |
|---|---|---|
| **S2 — Présence de services** | 3 services de catégories différentes (restaurant, hôtel, commerce, école, banque…) à moins d'1 km à pied | 3/3 ou 0 |
| **S6 — Exposition à la biodiversité** | Espace vert praticable (parc, jardin, square…) à moins d'1 km à pied | 4/4 ou 0 |
| **S7 — Mobilité durable** | ≥ 2 modes de transport en commun à moins d'1 km à pied | 3/3 ou 0 |

Le reste est automatique : géocodage, recherche des POI, itinéraires piétons
réels, carte de preuve, contrôles de cohérence, PDF, commentaire.

---

## L'application (recommandé) — Greenfast sur Streamlit

`webapp/streamlit_app.py` : l'interface d'équipe, déployée sur **Streamlit
Community Cloud**. Cocher un ou plusieurs critères (S2/S6/S7), taper l'adresse,
lancer → un bloc de résultat par critère avec le PDF téléchargeable et le
commentaire Soneka copiable.

- **Géocodage** : API Adresse de l'État (BAN, `adresse.data.gouv.fr`) — sans
  quota, idéale pour les adresses françaises — avec repli Nominatim.
- **POI, itinéraires et carte de preuve** : **Geoapify** (Places, Routing,
  Static Maps), avec repli OpenStreetMap (Overpass / OSRM / tuiles) si Geoapify
  est indisponible. Le tracé de la carte est l'**itinéraire piéton réel**.
- **Adresse ambiguë** (ex. « rue de Paris », présente dans plusieurs communes) :
  l'app demande de préciser la ville ou le code postal au lieu de géocoder au
  hasard.
- **Vérification IA (optionnelle)** : en renseignant une clé OpenAI, chaque
  critère est vérifié par le modèle **avant** la génération du PDF (résultat
  cohérent ? verdict correct ?).
- **Commentaire Soneka** : sous chaque résultat, un commentaire synthétique
  (nom + distance ; typologie pour les services) avec bouton copier.

### Lancer en local

```bash
pip install -r requirements.txt
streamlit run webapp/streamlit_app.py
```

### Clés (aucune n'est stockée ni committée)

- **Geoapify** : lue dans les *Secrets* Streamlit sous `GEOAPIFY_KEY`
  (Manage app → Settings → Secrets : `GEOAPIFY_KEY = "..."`). Sans clé, le
  géocodage BAN fonctionne quand même et les POI/itinéraires retombent sur OSM.
- **OpenAI** : saisie dans l'UI par l'utilisateur (facultatif, pour la vérif IA).

Dépôt : `raphael846/Grille-ESG-V0`. Chaque `push` sur `main` redéploie l'app.

---

## Les autres interfaces (S6 uniquement, socle d'origine)

Ces trois voies utilisent le socle OpenStreetMap (Nominatim + Overpass + OSRM)
et, quand un navigateur Chromium et le réseau le permettent, une **capture
Google Maps réelle** de l'itinéraire piéton intégrée au PDF.

1. **Page autonome** — `webapp/s6.html` : un fichier à double-cliquer, zéro
   installation. Optionnellement : clé Geoapify (côté navigateur) et
   vérification par Claude Haiku.
2. **Serveur Flask** — `python3 webapp/app.py` puis `http://localhost:8517`.
3. **Ligne de commande** :
   ```bash
   python3 skills/s6-biodiversite/scripts/s6_auto.py "adresse" --out S6.pdf [--locataire "Nom"]
   python3 skills/s6-biodiversite/scripts/s2_auto.py "adresse" --out S2.pdf
   python3 skills/s6-biodiversite/scripts/s7_auto.py "adresse" --out S7.pdf
   ```
   Dans une session Claude Code : `/s6`, `/s2`, `/s7`, ou `/esg` pour les trois.

---

## La preuve dans le PDF (toujours présente, jamais inventée)

Le PDF intègre la meilleure preuve disponible, avec une légende honnête sur sa
provenance :

1. Carte **Geoapify** avec l'itinéraire piéton réel (app Streamlit) ;
2. sinon carte **OpenStreetMap** (tuiles + itinéraire OSRM) ;
3. sinon capture **Google Maps** headless (voies CLI / Flask / page autonome) ;
4. sinon carte schématique générée hors-ligne à partir des coordonnées réelles.

Dans tous les cas le PDF contient le **lien Google Maps vérifiable en un clic**
— c'est lui qui fait foi. Distances et temps proviennent toujours d'une source
citée (routeur piéton), jamais d'une estimation non signalée.

---

## Structure du dépôt

```
webapp/streamlit_app.py            ← l'app Greenfast (S2/S6/S7, Geoapify, OpenAI, Soneka)
webapp/geoapify_s6.py              ← couche Geoapify/BAN (patche les pipelines pour l'app)
webapp/s6.html                     ← page autonome S6 (client-side)
webapp/app.py                      ← serveur Flask S6
skills/s6-biodiversite/scripts/
  s6_auto.py                       ← pipeline S6 : adresse → cfg
  s2_auto.py                       ← pipeline S2 (services)
  s7_auto.py                       ← pipeline S7 (transports)
  build_report.py                  ← génération du PDF + choix de la preuve
  capture_maps.py                  ← capture Google Maps (Chromium headless)
.claude/commands/{esg,s6,s2,s7}.md ← commandes Claude Code
requirements.txt                   ← dépendances Python
```

---

## Intégrité

- Distances, temps et coordonnées viennent toujours d'une source vérifiable
  (BAN, Geoapify/OSM), citée dans le PDF ; jamais inventés.
- La légende de la carte dit toujours ce qu'elle est vraiment (Geoapify / OSM /
  Google Maps / schéma hors-ligne) et si le tracé est l'itinéraire réel ou une
  liaison directe de repli.
- Les recherches de rapports ne committent rien : PDF générés hors du dépôt,
  `*.pdf` est dans le `.gitignore`.
