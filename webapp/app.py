#!/usr/bin/env python3
"""Interface web du rapport S6 : un champ adresse, un bouton, un PDF.

Lancement :
    pip install flask reportlab pillow staticmap playwright
    python3 webapp/app.py            # puis ouvrir http://localhost:8517

Chaque membre de l'équipe tape l'adresse de l'actif ; le serveur fait la
recherche (OpenStreetMap), tente la capture Google Maps automatique et renvoie
le PDF en téléchargement. Le niveau de preuve utilisé est affiché.
"""

import os
import re
import sys
import tempfile
import traceback
import unicodedata

from flask import Flask, request, send_file, render_template_string

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "skills", "s6-biodiversite", "scripts"))
import build_report  # noqa: E402
import s6_auto  # noqa: E402

app = Flask(__name__)

PAGE = """<!doctype html><html lang="fr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Rapport S6 — Biodiversité</title><style>
body{font-family:system-ui,sans-serif;background:#f4f6f4;margin:0;
     display:flex;min-height:100vh;align-items:center;justify-content:center}
main{background:#fff;padding:40px 44px;border-radius:12px;max-width:560px;
     box-shadow:0 2px 14px rgba(0,0,0,.08)}
h1{font-size:1.3rem;margin:0 0 4px}
p.sub{color:#5f6b5f;margin:0 0 24px;font-size:.92rem}
input[type=text]{width:100%;padding:12px 14px;font-size:1rem;border:1px solid
     #c8d0c8;border-radius:8px;box-sizing:border-box}
button{margin-top:14px;width:100%;padding:12px;font-size:1rem;border:0;
     border-radius:8px;background:#188038;color:#fff;cursor:pointer}
button:disabled{background:#9bb8a4}
.err{background:#fdeceb;border:1px solid #f5c6c3;color:#8a1f16;padding:12px;
     border-radius:8px;margin-top:18px;font-size:.9rem;white-space:pre-wrap}
.note{color:#777;font-size:.8rem;margin-top:22px}
#wait{display:none;margin-top:16px;color:#5f6b5f;font-size:.9rem}
</style></head><body><main>
<h1>Critère S6 — Exposition à la biodiversité</h1>
<p class="sub">Espace vert praticable à moins d'1 km à pied de l'actif</p>
<form method="post" action="/generate"
      onsubmit="document.getElementById('wait').style.display='block';
                document.getElementById('go').disabled=true;">
  <input type="text" name="address" required autofocus
         placeholder="Adresse de l'actif — ex. 4 rue de la Pompe, 75116 Paris"
         value="{{ address or '' }}">
  <button id="go" type="submit">Générer le rapport PDF</button>
  <div id="wait">Recherche de l'espace vert, itinéraire piéton et capture de la
  carte en cours… (30 à 60 secondes)</div>
</form>
{% if error %}<div class="err">{{ error }}</div>{% endif %}
<p class="note">Sources : OpenStreetMap (géocodage, espaces verts, itinéraire
piéton) ; carte Google Maps capturée automatiquement quand l'environnement le
permet, sinon carte OpenStreetMap ou schéma. La provenance de l'image est
toujours indiquée dans le PDF.</p>
</main></body></html>"""


def slugify(text):
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return re.sub(r"[^A-Za-z0-9]+", "_", text).strip("_") or "rapport"


@app.get("/")
def index():
    return render_template_string(PAGE, error=None, address=None)


@app.post("/generate")
def generate():
    address = request.form.get("address", "").strip()
    if not address:
        return render_template_string(PAGE, error="Adresse vide.", address=None)
    try:
        cfg = s6_auto.research(address)
        out = os.path.join(tempfile.mkdtemp(prefix="s6web_"),
                           f"S6_{slugify(address)}.pdf")
        proof = build_report.generate(cfg, out)
        app.logger.info("Rapport %s — preuve : %s", out, proof)
        return send_file(out, as_attachment=True,
                         download_name=os.path.basename(out))
    except Exception as e:
        app.logger.error("Échec pour « %s »\n%s", address, traceback.format_exc())
        return render_template_string(
            PAGE, error=f"Échec de la génération : {e}", address=address)


if __name__ == "__main__":
    port = int(os.environ.get("S6_PORT", "8517"))
    print(f"Rapport S6 — ouvrir http://localhost:{port}")
    app.run(host="0.0.0.0", port=port)
