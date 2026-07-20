#!/usr/bin/env python3
"""Capture d'écran automatique d'un itinéraire Google Maps (navigateur headless).

Usage :
    python3 capture_maps.py "<url_google_maps>" --out capture.png
                            [--width 1400] [--height 900] [--timeout 45] [--debug]

Stratégie de lancement du navigateur, dans l'ordre :
  1. Chromium pointé par PLAYWRIGHT_BROWSERS_PATH (environnements gérés)
  2. Chromium de Playwright (~/.cache/ms-playwright)
  3. Google Chrome installé sur la machine (channel "chrome")
  4. Téléchargement automatique du Chromium Playwright, puis nouvel essai

Code de sortie : 0 = capture OK, 2 = aucun navigateur utilisable,
3 = échec réseau/chargement, 4 = page chargée mais carte non rendue
(la capture est quand même enregistrée en <out>.debug.png pour diagnostic).
"""

import argparse
import os
import subprocess
import sys


def find_chromium():
    """Chemin explicite du Chromium pré-installé, sinon None (Playwright choisit)."""
    base = os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "/opt/pw-browsers")
    for sub in ("chromium", ""):
        root = os.path.join(base, sub) if sub else base
        if not os.path.isdir(root):
            continue
        for dirpath, _dirnames, filenames in os.walk(root):
            for name in ("chrome", "headless_shell", "chromium"):
                if name in filenames:
                    return os.path.join(dirpath, name)
    return None


def proxy_settings():
    """Proxy HTTPS de l'environnement (HTTPS_PROXY), au format Playwright.

    Chromium headless ne lit pas toujours ces variables tout seul : on les
    transmet explicitement pour que la capture marche derrière un proxy
    d'egress (environnements gérés type Claude Code sur le web).
    """
    server = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
    if not server:
        return None
    proxy = {"server": server}
    bypass = os.environ.get("NO_PROXY") or os.environ.get("no_proxy")
    if bypass:
        proxy["bypass"] = bypass
    return proxy


def launch_browser(p, width_args, proxy=None):
    """Essaie plusieurs navigateurs ; retourne (browser, description) ou lève."""
    attempts = []
    exe = find_chromium()
    if exe:
        attempts.append(("Chromium pré-installé (" + exe + ")",
                         dict(executable_path=exe)))
    attempts.append(("Chromium de Playwright", dict()))
    attempts.append(("Google Chrome installé sur la machine",
                     dict(channel="chrome")))

    errors = []
    for label, kwargs in attempts:
        try:
            return p.chromium.launch(headless=True, args=width_args,
                                     proxy=proxy, **kwargs), label
        except Exception as e:
            errors.append(f"{label} : {str(e).splitlines()[0]}")

    # Dernier recours : installer le Chromium de Playwright puis réessayer.
    print("Aucun navigateur trouvé, téléchargement du Chromium Playwright "
          "(~150 Mo, une seule fois)...", file=sys.stderr)
    try:
        subprocess.run([sys.executable, "-m", "playwright", "install", "chromium"],
                       check=True, capture_output=True, text=True, timeout=600)
        return p.chromium.launch(headless=True, args=width_args, proxy=proxy), \
            "Chromium Playwright (installé automatiquement)"
    except Exception as e:
        errors.append(f"installation automatique : {str(e).splitlines()[0]}")
        raise RuntimeError("aucun navigateur utilisable — " + " | ".join(errors))


def accept_consent(page):
    """Ferme le bandeau de consentement Google (consent.google.com ou overlay)."""
    selectors = [
        'button:has-text("Tout accepter")',
        'button:has-text("Accept all")',
        'button[aria-label*="Tout accepter"]',
        'button[aria-label*="Accept all"]',
        'form[action*="consent"] button',
    ]
    for sel in selectors:
        try:
            btn = page.locator(sel).first
            if btn.is_visible(timeout=1500):
                btn.click(timeout=3000)
                page.wait_for_load_state("domcontentloaded", timeout=15000)
                return True
        except Exception:
            continue
    return False


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("url")
    ap.add_argument("--out", required=True)
    ap.add_argument("--width", type=int, default=1400)
    ap.add_argument("--height", type=int, default=900)
    ap.add_argument("--timeout", type=int, default=45, help="secondes")
    ap.add_argument("--debug", action="store_true",
                    help="affiche l'URL finale et le titre de la page")
    args = ap.parse_args()

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("Playwright n'est pas installé : pip install playwright",
              file=sys.stderr)
        sys.exit(2)

    timeout_ms = args.timeout * 1000
    base_args = ["--disable-gpu", "--no-sandbox", "--lang=fr-FR"]
    proxy = proxy_settings()
    # Derrière certains proxys MITM d'environnement, le handshake TLS 1.3 de
    # Chromium est rejeté (connexion réinitialisée) alors que TLS 1.2 passe :
    # on retente alors en plafonnant la version. La vérification des
    # certificats reste active dans les deux cas.
    arg_sets = [base_args]
    if proxy:
        arg_sets.append(base_args + ["--ssl-version-max=tls1.2"])

    with sync_playwright() as p:
        browser = page = None
        load_error = None
        for i, launch_args in enumerate(arg_sets):
            try:
                browser, label = launch_browser(p, launch_args, proxy)
                if i == 0:
                    print(f"Navigateur : {label}", file=sys.stderr)
            except Exception as e:
                print(f"Chromium indisponible : {e}", file=sys.stderr)
                print("Solution : pip install playwright && "
                      "python3 -m playwright install chromium", file=sys.stderr)
                sys.exit(2)

            ctx = browser.new_context(
                viewport={"width": args.width, "height": args.height},
                locale="fr-FR",
                user_agent=("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                            "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"),
            )
            page = ctx.new_page()
            try:
                page.goto(args.url, timeout=timeout_ms, wait_until="domcontentloaded")
                load_error = None
                break
            except Exception as e:
                load_error = str(e).splitlines()[0]
                browser.close()
                browser = page = None
                if i + 1 < len(arg_sets):
                    print("Échec de chargement, nouvel essai en TLS <= 1.2 "
                          "(contournement proxy MITM)...", file=sys.stderr)

        if page is None:
            print(f"Échec de chargement de la page : {load_error}",
                  file=sys.stderr)
            sys.exit(3)

        try:

            clicked = accept_consent(page)
            if args.debug:
                print(f"Consentement cliqué : {clicked}", file=sys.stderr)

            # Attendre le rendu de la carte : canvas WebGL de Maps ou, à défaut,
            # stabilisation du réseau puis délai de rendu.
            rendered = False
            try:
                page.wait_for_selector("canvas", timeout=timeout_ms // 2)
                rendered = True
            except Exception:
                pass
            try:
                page.wait_for_load_state("networkidle", timeout=timeout_ms // 3)
            except Exception:
                pass
            page.wait_for_timeout(2500)

            if args.debug:
                print(f"URL finale : {page.url}", file=sys.stderr)
                try:
                    print(f"Titre : {page.title()}", file=sys.stderr)
                except Exception:
                    pass

            if not rendered:
                debug_path = args.out + ".debug.png"
                page.screenshot(path=debug_path)
                print("Carte non rendue (canvas absent). Causes fréquentes : "
                      "bandeau de consentement non fermé, captcha, page "
                      f"d'erreur. Capture de diagnostic : {debug_path} — "
                      f"URL finale : {page.url}", file=sys.stderr)
                sys.exit(4)

            page.screenshot(path=args.out)
            print(f"Capture enregistrée : {args.out}")
        finally:
            browser.close()


if __name__ == "__main__":
    main()
