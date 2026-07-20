---
description: Génère les trois rapports ESG géolocalisés (S6, S2, S7) pour une adresse
---

Génère les trois rapports ESG pour l'actif : $ARGUMENTS

1. **Si aucune adresse n'est fournie, demande-la avant tout** (AskUserQuestion).
2. Dépendances (n'installe que si l'import échoue) :
   `python3 -c "import reportlab, PIL, staticmap, playwright" 2>/dev/null || pip install reportlab pillow staticmap playwright`
3. Lance les trois pipelines (dans cet ordre, en réutilisant la même adresse
   et le même locataire éventuel) :
   - `python3 skills/s6-biodiversite/scripts/s6_auto.py "<adresse>" --out S6_<actif>.pdf [--locataire "Nom"]`
   - `python3 skills/s6-biodiversite/scripts/s2_auto.py "<adresse>" --out S2_<actif>.pdf [--locataire "Nom"]`
   - `python3 skills/s6-biodiversite/scripts/s7_auto.py "<adresse>" --out S7_<actif>.pdf [--locataire "Nom"]`
4. Un pipeline en échec « serveurs saturés » : relance-le une fois après
   ~1 min ; n'abandonne pas les autres.
5. Envoie les trois PDF tels quels avec un récapitulatif : S6 x/4 · S2 x/3 ·
   S7 x/3 et les points d'attention des contrôles.
