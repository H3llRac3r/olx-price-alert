#!/usr/bin/env python3
"""
Price Alert — versiune completă, pregătită pentru GitHub Actions.

CE FACE:
  1. Descarcă paginile de categorie de pe OLX și Publi24
  2. Normalizează toate prețurile la RON (curs live, cu fallback) ca să
     comparațiile să fie corecte chiar dacă unele anunțuri sunt în EUR
  3. Elimină din start anunțurile "pentru piese"/"defect"/etc. — nu le
     lasă să strice mediana și nu cheltuie bani de AI pe ele
  4. (Opțional) Filtrează după localitate, dacă completezi LOCALITATI_ACCEPTATE
  5. Calculează mediana pe categorie și semnalează ce e cu mult sub ea
  6. Ignoră re-postările (același anunț, titlu aproape identic + același
     preț, link nou) ca să nu-ți repete aceeași alertă
  7. (Opțional) Trimite candidații la Claude — citește descrierea + poze,
     dă un verdict de calitate/încredere + un mesaj de prim contact
  8. Trimite alertă pe Telegram și scrie o linie în jurnal_achizitii.csv,
     pe care o completezi manual (cumpărat? cu cât ai vândut?) ca să afli
     după o lună dacă chiar merită.

RULEAZĂ AUTOMAT pe GitHub Actions (vezi .github/workflows/price_alert.yml)
— nu trebuie pornit manual.

CONFIGUREAZĂ mai jos categoriile, localitățile și pragurile.
"""

import base64
import csv
import json
import os
import re
import statistics
import time
from datetime import datetime
from difflib import SequenceMatcher

import requests
from bs4 import BeautifulSoup

# ============================== CONFIG ==============================

PRAG_CHILIPIR = 0.30                # semnalează sub 30% din mediană
MIN_ANUNTURI_PT_COMPARATIE = 5
PAUZA_INTRE_CERERI_SEC = 3
PRAG_SIMILARITATE_REPOSTARE = 0.85  # cât de asemănătoare trebuie să fie 2 titluri ca să le considere aceeași repostare

# Lasă gol [] ca să nu filtrezi deloc după localitate. Dacă completezi,
# păstrează doar anunțurile a căror localitate conține unul din termeni
# (anunțurile fără localitate detectată NU sunt excluse automat, ca să
# nu pierzi rezultate din cauza unui selector care nu prinde perfect).
LOCALITATI_ACCEPTATE = []           # ex: ["Cluj-Napoca", "Floresti", "Gilau"]

# Cuvinte care exclud un anunț din start (nu intră în calculul medianei,
# nu ajunge la analiza AI)
CUVINTE_EXCLUSE = re.compile(
    r"pentru piese|nu func[țt]ioneaz[ăa]|defect|stricat|pentru repara[țt]ie|avariat",
    re.IGNORECASE,
)

CURS_EUR_RON_FALLBACK = 5.05  # folosit doar dacă API-ul de curs valutar pică

# --- Secrete — vin din variabile de mediu (GitHub Actions Secrets) ---
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

# --- Analiză AI ---
ACTIVEAZA_ANALIZA_AI = True
ANTHROPIC_MODEL = "claude-haiku-4-5-20251001"   # mai ieftin; schimbă în claude-sonnet-5 dacă vrei verdicte mai fine
MAX_IMAGINI_PER_ANUNT = 3
MAX_CANDIDATI_ANALIZATI_PE_RULARE = 10

FISIER_ISTORIC = "price_alert_istoric.json"
FISIER_JURNAL = "jurnal_achizitii.csv"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "ro-RO,ro;q=0.9,en-US;q=0.8,en;q=0.7",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
}

# ======================================================================
# UTILITARE PREȚ / MONEDĂ
# ======================================================================


def parseaza_pret(text_pret):
    if not text_pret:
        return None, None
    text_pret = text_pret.strip()
    moneda = "EUR" if "€" in text_pret else "RON"
    cifre = re.sub(r"[^\d]", "", text_pret)
    if not cifre:
        return None, None
    return float(cifre), moneda


def obtine_curs_eur_ron():
    try:
        r = requests.get("https://api.frankfurter.app/latest?from=EUR&to=RON", timeout=10)
        r.raise_for_status()
        return r.json()["rates"]["RON"]
    except (requests.RequestException, KeyError, ValueError):
        print(f"   ! Nu am putut lua cursul valutar live, folosesc fallback {CURS_EUR_RON_FALLBACK}")
        return CURS_EUR_RON_FALLBACK


def similar(a, b):
    return SequenceMatcher(None, a, b).ratio()


# ======================================================================
# PARSERE PAGINĂ DE CATEGORIE
# ======================================================================


def parseaza_olx(html):
    soup = BeautifulSoup(html, "html.parser")
    anunturi = []
    for card in soup.select('[data-cy="l-card"]'):
        link_tag = card.find("a", href=True)
        titlu_tag = card.find(["h4", "h6"])
        pret_tag = card.select_one('[data-testid="ad-price"]')
        locatie_tag = card.select_one('[data-testid="location-date"]')
        if not (link_tag and titlu_tag and pret_tag):
            continue
        link = link_tag["href"]
        if link.startswith("/"):
            link = "https://www.olx.ro" + link
        pret, moneda = parseaza_pret(pret_tag.get_text())
        if pret is None:
            continue
        titlu = titlu_tag.get_text(strip=True)
        if CUVINTE_EXCLUSE.search(titlu):
            continue
        anunturi.append({
            "titlu": titlu, "pret": pret, "moneda": moneda, "link": link,
            "locatie": locatie_tag.get_text(strip=True) if locatie_tag else "",
        })
    return anunturi


def parseaza_publi24(html):
    # NEVERIFICAT live — ajustează selectoarele după inspectare reală
    soup = BeautifulSoup(html, "html.parser")
    anunturi = []
    for card in soup.select(".listing-item, .announcement-item, article"):
        link_tag = card.find("a", href=True)
        titlu_tag = card.find(["h2", "h3", "h4"])
        pret_tag = card.find(class_=re.compile("price", re.I))
        locatie_tag = card.find(class_=re.compile("location|zone", re.I))
        if not (link_tag and titlu_tag and pret_tag):
            continue
        link = link_tag["href"]
        if link.startswith("/"):
            link = "https://www.publi24.ro" + link
        pret, moneda = parseaza_pret(pret_tag.get_text())
        if pret is None:
            continue
        titlu = titlu_tag.get_text(strip=True)
        if CUVINTE_EXCLUSE.search(titlu):
            continue
        anunturi.append({
            "titlu": titlu, "pret": pret, "moneda": moneda, "link": link,
            "locatie": locatie_tag.get_text(strip=True) if locatie_tag else "",
        })
    return anunturi


# ======================================================================
# PARSERE PAGINĂ DE DETALIU
# ======================================================================


def detalii_olx(link):
    try:
        r = requests.get(link, headers=HEADERS, timeout=15)
        r.raise_for_status()
    except requests.RequestException:
        return None, []
    soup = BeautifulSoup(r.text, "html.parser")
    descriere_tag = soup.select_one('[data-cy="ad_description"]')
    descriere = descriere_tag.get_text(strip=True) if descriere_tag else ""
    imagini = [img["src"] for img in soup.select('[data-cy="ad-photo"] img') if img.get("src")]
    return descriere, imagini[:MAX_IMAGINI_PER_ANUNT]


def detalii_publi24(link):
    try:
        r = requests.get(link, headers=HEADERS, timeout=15)
        r.raise_for_status()
    except requests.RequestException:
        return None, []
    soup = BeautifulSoup(r.text, "html.parser")
    descriere_tag = soup.find(class_=re.compile("description", re.I))
    descriere = descriere_tag.get_text(strip=True) if descriere_tag else ""
    imagini = [img["src"] for img in soup.select(".gallery img, .photos img") if img.get("src")]
    return descriere, imagini[:MAX_IMAGINI_PER_ANUNT]


PLATFORME = {
    "OLX": {
        "parser": parseaza_olx,
        "parser_detalii": detalii_olx,
        "categorii": {
            "electronice": "https://www.olx.ro/electronice-si-electrocasnice/",
            "moda": "https://www.olx.ro/moda-si-frumusete/",
        },
    },
    "Publi24": {
        "parser": parseaza_publi24,
        "parser_detalii": detalii_publi24,
        "categorii": {
            "electronice": "https://www.publi24.ro/anunturi/electronice/",
        },
    },
}

# ======================================================================
# ANALIZĂ AI
# ======================================================================


def descarca_imagine_base64(url):
    try:
        r = requests.get(url, headers=HEADERS, timeout=10)
        r.raise_for_status()
        media_type = r.headers.get("Content-Type", "image/jpeg").split(";")[0]
        if not media_type.startswith("image/"):
            media_type = "image/jpeg"
        return base64.standard_b64encode(r.content).decode("utf-8"), media_type
    except requests.RequestException:
        return None, None


def evalueaza_cu_ai(titlu, pret, moneda, descriere, imagini_urls):
    if not ANTHROPIC_API_KEY:
        return None

    content = [{
        "type": "text",
        "text": (
            "Ești un asistent care evaluează anunțuri de vânzare second-hand "
            "pentru un cumpărător care caută chilipiruri reale, nu escrocherii.\n\n"
            f"Titlu: {titlu}\nPreț: {pret} {moneda}\n"
            f"Descriere: {descriere or '(fără descriere)'}\n\n"
            "Analizează pozele atașate și descrierea. Răspunde STRICT cu un JSON "
            "valid, fără text în plus, cu exact aceste chei:\n"
            '{"scor_calitate": 1-10, "scor_incredere": 1-10, '
            '"avantajos": true/false, "motiv": "1-2 propoziții", '
            '"semnale_alarma": ["listă scurtă, poate fi goală"], '
            '"mesaj_prim_contact": "un mesaj scurt, politicos, în română, '
            'pe care cumpărătorul l-ar putea trimite vânzătorului"}'
        ),
    }]
    for url in imagini_urls:
        b64, media_type = descarca_imagine_base64(url)
        if b64:
            content.append({"type": "image",
                             "source": {"type": "base64", "media_type": media_type, "data": b64}})

    try:
        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": ANTHROPIC_API_KEY, "anthropic-version": "2023-06-01",
                     "Content-Type": "application/json"},
            json={"model": ANTHROPIC_MODEL, "max_tokens": 600,
                  "messages": [{"role": "user", "content": content}]},
            timeout=30,
        )
        r.raise_for_status()
        text = r.json()["content"][0]["text"]
        text = re.sub(r"^```json\s*|\s*```$", "", text.strip())
        return json.loads(text)
    except (requests.RequestException, KeyError, json.JSONDecodeError) as e:
        print(f"   ! Eroare analiză AI: {e}")
        return None


# ======================================================================
# ISTORIC / DEDUPLICARE / JURNAL
# ======================================================================


def incarca_istoric():
    if os.path.exists(FISIER_ISTORIC):
        with open(FISIER_ISTORIC, "r", encoding="utf-8") as f:
            return json.load(f)
    return []


def salveaza_istoric(istoric):
    with open(FISIER_ISTORIC, "w", encoding="utf-8") as f:
        json.dump(istoric, f, ensure_ascii=False)


def este_deja_vazut(candidat, istoric):
    for intrare in istoric:
        if intrare["link"] == candidat["link"]:
            return True
        if (intrare.get("platforma") == candidat.get("platforma")
                and abs(intrare["pret_ron"] - candidat["pret_ron"]) < 1
                and similar(intrare["titlu"].lower(), candidat["titlu"].lower()) > PRAG_SIMILARITATE_REPOSTARE):
            return True  # probabil repostare
    return False


def asigura_fisier_jurnal():
    """Creează jurnal_achizitii.csv cu header chiar dacă rularea curentă nu
    găsește niciun candidat — altfel fișierul nu există deloc și pasul de
    git add din workflow eșuează."""
    if not os.path.exists(FISIER_JURNAL):
        with open(FISIER_JURNAL, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(["data", "platforma", "categorie", "titlu", "pret", "moneda", "link",
                              "scor_calitate_ai", "avantajos_ai", "cumparat", "pret_vanzare"])


def adauga_in_jurnal(candidat, platforma, categorie, verdict_ai):
    with open(FISIER_JURNAL, "a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow([
            datetime.now().strftime("%Y-%m-%d %H:%M"), platforma, categorie,
            candidat["titlu"], candidat["pret"], candidat["moneda"], candidat["link"],
            verdict_ai.get("scor_calitate") if verdict_ai else "",
            verdict_ai.get("avantajos") if verdict_ai else "",
            "", "",  # completezi TU manual după: cumparat (da/nu), pret_vanzare
        ])


def trimite_alerta_telegram(candidat, platforma, categorie, verdict_ai):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    mesaj = (
        f"🔥 Chilipir pe {platforma} — {categorie}\n{candidat['titlu']}\n"
        f"Preț: {candidat['pret']:.0f} {candidat['moneda']} "
        f"(-{candidat['diferenta_procent']}% față de mediana categoriei)\n"
    )
    if candidat.get("locatie"):
        mesaj += f"📍 {candidat['locatie']}\n"
    if verdict_ai:
        mesaj += (f"\n🤖 Calitate {verdict_ai.get('scor_calitate')}/10, "
                   f"încredere {verdict_ai.get('scor_incredere')}/10\n{verdict_ai.get('motiv', '')}\n")
        if verdict_ai.get("semnale_alarma"):
            mesaj += f"⚠️ {', '.join(verdict_ai['semnale_alarma'])}\n"
        if verdict_ai.get("mesaj_prim_contact"):
            mesaj += f"\n💬 Mesaj sugerat:\n{verdict_ai['mesaj_prim_contact']}\n"
    mesaj += f"\n{candidat['link']}"

    try:
        requests.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                       data={"chat_id": TELEGRAM_CHAT_ID, "text": mesaj}, timeout=10)
    except requests.RequestException as e:
        print(f"  ! Eroare Telegram: {e}")


# ======================================================================


def gaseste_chilipiruri(anunturi):
    if len(anunturi) < MIN_ANUNTURI_PT_COMPARATIE:
        return []
    mediana = statistics.median(a["pret_ron"] for a in anunturi)
    rezultat = []
    for a in anunturi:
        if a["pret_ron"] <= mediana * (1 - PRAG_CHILIPIR):
            a["diferenta_procent"] = round((1 - a["pret_ron"] / mediana) * 100)
            rezultat.append(a)
    return rezultat


def ruleaza():
    print(f"\n=== Rulare {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} ===")
    asigura_fisier_jurnal()
    istoric = incarca_istoric()
    curs = obtine_curs_eur_ron()
    gasite_noi = 0

    for nume_platforma, config in PLATFORME.items():
        parser = config["parser"]
        parser_detalii = config["parser_detalii"]

        for nume_categorie, url in config["categorii"].items():
            print(f"-> {nume_platforma} / {nume_categorie}")
            try:
                r = requests.get(url, headers=HEADERS, timeout=15)
                r.raise_for_status()
            except requests.RequestException as e:
                print(f"   ! Nu am putut încărca: {e}")
                continue

            anunturi = parser(r.text)
            for a in anunturi:
                a["pret_ron"] = a["pret"] if a["moneda"] == "RON" else round(a["pret"] * curs)
                a["platforma"] = nume_platforma

            if LOCALITATI_ACCEPTATE:
                anunturi = [a for a in anunturi if not a["locatie"]
                            or any(loc.lower() in a["locatie"].lower() for loc in LOCALITATI_ACCEPTATE)]

            print(f"   {len(anunturi)} anunțuri (după filtre)")
            candidati = [c for c in gaseste_chilipiruri(anunturi) if not este_deja_vazut(c, istoric)]

            for candidat in candidati[:MAX_CANDIDATI_ANALIZATI_PE_RULARE]:
                istoric.append({"link": candidat["link"], "titlu": candidat["titlu"],
                                 "pret_ron": candidat["pret_ron"], "platforma": nume_platforma})
                gasite_noi += 1
                print(f"   💰 {candidat['titlu']} — {candidat['pret']:.0f} {candidat['moneda']} "
                      f"(-{candidat['diferenta_procent']}%)")

                verdict_ai = None
                if ACTIVEAZA_ANALIZA_AI and ANTHROPIC_API_KEY:
                    descriere, imagini = parser_detalii(candidat["link"])
                    if descriere is not None and not CUVINTE_EXCLUSE.search(descriere):
                        verdict_ai = evalueaza_cu_ai(candidat["titlu"], candidat["pret"],
                                                      candidat["moneda"], descriere, imagini)

                adauga_in_jurnal(candidat, nume_platforma, nume_categorie, verdict_ai)
                trimite_alerta_telegram(candidat, nume_platforma, nume_categorie, verdict_ai)
                time.sleep(1)

            time.sleep(PAUZA_INTRE_CERERI_SEC)

    salveaza_istoric(istoric)
    print(f"=== Gata. {gasite_noi} chilipiruri noi. ===")


if __name__ == "__main__":
    ruleaza()
