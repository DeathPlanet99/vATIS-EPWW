import os
import re
import json
from datetime import datetime, timezone
import requests
from bs4 import BeautifulSoup

AIRPORTS = ["EPKK", "EPWA", "EPPO", "EPGD"]
BASE_URL = "https://atis.guru/atis/{icao}"
DATA_DIR = "data"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
}

def extract_atis_blocks(html: str, icao: str) -> list[str]:
    """Wyciąga pojedyncze bloki depesz ATIS ze strony, obsługując różne formaty zapisu."""
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript", "svg"]):
        tag.decompose()

    full_text = soup.get_text(separator="\n")
    lines = [line.strip() for line in full_text.splitlines() if line.strip()]
    text = "\n".join(lines)

    # Elastyczny wzorzec: dopasowuje EPWA ARR ATIS I, EPKK ATIS INFO B, EPGD ATIS C itd.
    block_pattern = re.compile(
        rf'(\b{icao}\s+(?:(?:ARR|DEP)\s+)?ATIS(?:\s+INFO)?\s+[A-Z]\b[\s\S]+?)(?=(?:\b{icao}\s+(?:(?:ARR|DEP)\s+)?ATIS|\bMETAR\b|\bTAF\b|\Z))',
        re.IGNORECASE
    )

    matches = block_pattern.findall(text)
    blocks = []

    for raw_block in matches:
        block_lines = [l.strip() for l in raw_block.splitlines() if l.strip()]
        trimmed = []
        for l in block_lines:
            # Odcinamy ewentualne nagłówki kolejnych kart
            if trimmed and re.search(r'^(?:Arrival ATIS|Departure ATIS|METAR|TAF|Formatting)\b', l, re.IGNORECASE):
                break
            trimmed.append(l)
            # Znaczniki jednoznacznego zakończenia depeszy
            if re.search(r'^(?:END OF\b|REPORT\s+[A-Z]\b|ACK(?:NOWLEDGE)?\b)', l, re.IGNORECASE):
                break

        cleaned = "\n".join(trimmed).strip()
        if len(cleaned) > 20 and cleaned not in blocks:
            blocks.append(cleaned)

    return blocks

def parse_atis_simple(raw_text: str, icao: str) -> dict:
    """Parsuje czysty tekst depeszy do płaskich pól tekstowych."""
    item = {
        "type": "COMBINED",
        "info_letter": None,
        "time": None,
        "runway": None,
        "approach": None,
        "transition_level": None,
        "wind": None,
        "visibility": None,
        "clouds": None,
        "weather": None,
        "temperature": None,
        "dewpoint": None,
        "altimeter": None,
        "trend": None,
        "remarks": None,
        "raw_text": raw_text
    }

    # 1. Typ depeszy
    if "ARR ATIS" in raw_text or f"{icao} ARR" in raw_text:
        item["type"] = "ARRIVAL"
    elif "DEP ATIS" in raw_text or f"{icao} DEP" in raw_text:
        item["type"] = "DEPARTURE"

    # 2. Litera informacji
    m_let = re.search(rf'\b{icao}\s+(?:ARR\s+|DEP\s+)?ATIS(?:\s+INFO)?\s+([A-Z])\b', raw_text, re.IGNORECASE) or \
            re.search(r'\b(?:ATIS|INFO(?:RMATION)?)\s+([A-Z])\b', raw_text, re.IGNORECASE)
    if m_let:
        item["info_letter"] = m_let.group(1).upper()

    # 3. Czas (Zulu lub TIME)
    m_z = re.search(r'\b(\d{4}Z)\b', raw_text)
    m_obs = re.search(r'\bTIME\s+(\d{4})\b', raw_text)
    if m_z:
        item["time"] = m_z.group(1)
    elif m_obs:
        item["time"] = m_obs.group(1) + "Z"

    # 4. Pasy w użyciu
    rwy = re.findall(r'\b(?:DEP\s+RWY|ARR\s+RWY|RWY|RUNWAY)\s+([^\.\n]+)', raw_text, re.IGNORECASE)
    if rwy:
        item["runway"] = ", ".join([r.strip().rstrip('.') for r in rwy])

    # 5. Podejście (Approach)
    apch = re.search(r'\b(EXP[^\.\n]+(?:APCH|ILS)[^\.\n]*)', raw_text, re.IGNORECASE)
    if apch:
        item["approach"] = apch.group(1).strip().rstrip('.')

    # 6. Poziom przejściowy (Transition Level)
    tl = re.search(r'\bTRANSITION\s+LEVEL\s+(\d+)\b', raw_text, re.IGNORECASE)
    if tl:
        item["transition_level"] = tl.group(1)

    # 7. Wiatr
    w = re.search(r'\bWIND\s+([^\.\n]+)', raw_text, re.IGNORECASE)
    if w:
        item["wind"] = w.group(1).strip().rstrip('.')

    # 8. Widzialność i chmury
    if "CAVOK" in raw_text:
        item["visibility"] = "CAVOK"
        item["clouds"] = "CAVOK"
    else:
        vis = re.search(r'\bVIS(?:IBILITY)?\s+([^\.\n]+)', raw_text, re.IGNORECASE)
        if vis:
            item["visibility"] = vis.group(1).strip().rstrip('.')

        cld = re.findall(r'\b((?:FEW|SCT|BKN|OVC|VV)\d{3}[A-Z]*|NSC|SKC|NCD)\b', raw_text)
        if cld:
            item["clouds"] = " ".join(cld)

    # 9. Zjawiska pogody
    wx = re.findall(r'\b([+-]?(?:VC)?(?:DZ|RA|SN|SG|IC|PL|GR|GS|UP|BR|FG|FU|VA|DU|SA|HZ|PY|PO|SQ|FC|SS|DS|TSRA|TS|SHRA|SHSN))\b', raw_text)
    if wx:
        item["weather"] = " ".join(list(dict.fromkeys(wx)))

    # 10. Temperatura i punkt rosy
    t = re.search(r'\bT\s*(-?\d+|M\d+)\b', raw_text)
    if t:
        item["temperature"] = t.group(1).replace("M", "-")

    dp = re.search(r'\bDP\s*(-?\d+|M\d+)\b', raw_text)
    if dp:
        item["dewpoint"] = dp.group(1).replace("M", "-")

    # 11. Ciśnienie (QNH)
    q = re.search(r'\b(QNH\s*\d{4}|Q\d{4}|A\d{4})\b', raw_text, re.IGNORECASE)
    if q:
        item["altimeter"] = q.group(1).upper()

    # 12. Prognoza trendu
    tr = re.search(r'\b(NOSIG|TEMPO[^\.\n]*|BECMG[^\.\n]*)', raw_text)
    if tr:
        item["trend"] = tr.group(1).strip().rstrip('.')

    # 13. Uwagi
    rmk = re.findall(r'\b(CAUTION[^\.\n]+)', raw_text, re.IGNORECASE)
    if rmk:
        item["remarks"] = "; ".join([r.strip().rstrip('.') for r in rmk])

    return item

def run():
    now_utc = datetime.now(timezone.utc)
    date_str = now_utc.strftime("%d%m%Y")
    time_str = now_utc.strftime("%H%M")

    for icao in AIRPORTS:
        airport_dir = os.path.join(DATA_DIR, icao)
        os.makedirs(airport_dir, exist_ok=True)

        url = BASE_URL.format(icao=icao)
        try:
            response = requests.get(url, headers=HEADERS, timeout=20)
            response.raise_for_status()

            blocks = extract_atis_blocks(response.text, icao)
            if not blocks:
                print(f"[{icao}] Brak aktywnego ATIS na stronie (pomijam).")
                continue

            parsed_reports = [parse_atis_simple(b, icao) for b in blocks]
            current_raw = [r["raw_text"] for r in parsed_reports]

            # Sprawdzenie, czy depesza zmieniła się od ostatniego zapisanego pliku
            existing_files = sorted([f for f in os.listdir(airport_dir) if f.endswith(".json")])
            if existing_files:
                last_file_path = os.path.join(airport_dir, existing_files[-1])
                try:
                    with open(last_file_path, "r", encoding="utf-8") as f:
                        last_data = json.load(f)
                        last_reports = last_data.get("reports", [])
                        last_raw = [r.get("raw_text") for r in last_reports if isinstance(r, dict)]
                        if current_raw == last_raw:
                            print(f"[{icao}] ATIS bez zmian od ostatniego pliku ({existing_files[-1]}). Pomijam.")
                            continue
                except Exception as err:
                    print(f"[{icao}] Błąd sprawdzania poprzedniego pliku: {err}")

            filename = f"{icao}_{date_str}_{time_str}z.json"
            filepath = os.path.join(airport_dir, filename)

            payload = {
                "airport": icao,
                "fetch_time_utc": now_utc.strftime("%Y-%m-%d %H:%M:%S UTC"),
                "reports": parsed_reports
            }

            with open(filepath, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)

            summary = ", ".join([f"{r['type']}: {r['info_letter']}" for r in parsed_reports if r['info_letter']])
            print(f"[{icao}] Utworzono plik: {filepath} ({summary})")

        except Exception as err:
            print(f"[{icao}] Błąd podczas pobierania {url}: {err}")

if __name__ == "__main__":
    run()