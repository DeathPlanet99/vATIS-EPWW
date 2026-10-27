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
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
}

def extract_blocks(html: str, icao: str) -> list[str]:
    """
    Najprostszy i najbardziej niezawodny ekstraktor od zera.
    Czyta linijka po linijce, nie wycina niczego w środku depeszy.
    """
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript", "svg"]):
        tag.decompose()

    text = soup.get_text(separator="\n")
    lines = [line.strip() for line in text.splitlines() if line.strip()]

    blocks = []
    current_block = []
    in_block = False

    # Jakie słowa po kodzie ICAO oznaczają główny nagłówek ATIS
    valid_starts = ["ARR", "DEP", "ATIS", "INFO", "INFORMATION"]

    for line in lines:
        parts = line.split()
        
        # 1. Wykrywanie twardego początku depeszy
        if len(parts) >= 2 and parts[0].upper() == icao.upper() and parts[1].upper() in valid_starts:
            if in_block:
                blocks.append("\n".join(current_block))
            in_block = True
            current_block = [line]
            continue

        if in_block:
            upper_line = line.upper()
            
            # 2. Wykrycie znaczników z innej bajki (np. nagłówki strony), które zamykają czytanie ATIS-u
            if upper_line in ["METAR", "TAF", "FORMATTING", "HISTORY"] or upper_line.startswith("METAR ") or upper_line.startswith("TAF "):
                blocks.append("\n".join(current_block))
                in_block = False
                current_block = []
                continue
                
            # Zbieramy KAŻDĄ LINIJKĘ tekstu
            current_block.append(line)

            # 3. Wykrycie końca lotniczej transmisji
            if upper_line.startswith("END OF") or upper_line.startswith("REPORT ") or upper_line.startswith("ACKNOWLEDGE "):
                blocks.append("\n".join(current_block))
                in_block = False
                current_block = []
                continue

    if in_block and current_block:
        blocks.append("\n".join(current_block))

    return blocks

def parse_atis_simple(raw_text: str) -> dict:
    """Parsowanie parametrów bezpośrednio z wyłapanego całego bloku tekstu."""
    lines = raw_text.splitlines()
    first_line = lines[0].upper()
    
    # Ustalanie typu
    if "ARR" in first_line:
        atis_type = "ARRIVAL"
    elif "DEP" in first_line:
        atis_type = "DEPARTURE"
    else:
        atis_type = "COMBINED"

    # Ustalanie litery
    m_let = re.search(r'\b([A-Z])$', first_line)
    info_letter = m_let.group(1) if m_let else "?"

    item = {
        "type": atis_type,
        "info_letter": info_letter,
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

    m_obs = re.search(r'\bTIME\s+(\d{4})\b', raw_text, re.IGNORECASE)
    m_z = re.search(r'\b(\d{4})Z\b', raw_text, re.IGNORECASE)
    if m_obs:
        item["time"] = m_obs.group(1) + "Z"
    elif m_z:
        item["time"] = m_z.group(1).upper()

    rwy = re.findall(r'\b(?:DEP\s+RWY|ARR\s+RWY|RWY|RUNWAY)\s+([0-9]{2}[LCR]?)', raw_text, re.IGNORECASE)
    if rwy:
        item["runway"] = ", ".join(dict.fromkeys(rwy))

    apch = re.search(r'\b(EXP[^\.\n]+(?:APCH|ILS)[^\.\n]*)', raw_text, re.IGNORECASE)
    if apch:
        item["approach"] = apch.group(1).strip().rstrip('.')

    tl = re.search(r'\b(?:TRANSITION\s+LEVEL|TL)\s*(\d+)\b', raw_text, re.IGNORECASE)
    if tl:
        item["transition_level"] = tl.group(1)

    w = re.search(r'\bWIND\s+([^\.\n]+)', raw_text, re.IGNORECASE)
    if w:
        item["wind"] = w.group(1).strip().rstrip('.')

    if "CAVOK" in raw_text.upper():
        item["visibility"] = "CAVOK"
        item["clouds"] = "CAVOK"
    else:
        vis = re.search(r'\bVIS(?:IBILITY)?\s+([^\.\n]+)', raw_text, re.IGNORECASE)
        if vis:
            item["visibility"] = vis.group(1).strip().rstrip('.')
        cld = re.findall(r'\b((?:FEW|SCT|BKN|OVC|VV)\d{3}[A-Z]*|NSC|SKC|NCD)\b', raw_text, re.IGNORECASE)
        if cld:
            item["clouds"] = " ".join(cld).upper()

    wx = re.findall(r'\b([+-]?(?:VC)?(?:DZ|RA|SN|SG|IC|PL|GR|GS|UP|BR|FG|FU|VA|DU|SA|HZ|PY|PO|SQ|FC|SS|DS|TSRA|TS|SHRA|SHSN))\b', raw_text, re.IGNORECASE)
    if wx:
        item["weather"] = " ".join(dict.fromkeys(wx)).upper()

    t = re.search(r'\bT\s*(-?\d+|M\d+)\b', raw_text, re.IGNORECASE)
    if t:
        item["temperature"] = t.group(1).upper().replace("M", "-")

    dp = re.search(r'\bDP\s*(-?\d+|M\d+)\b', raw_text, re.IGNORECASE)
    if dp:
        item["dewpoint"] = dp.group(1).upper().replace("M", "-")

    q = re.search(r'\b(QNH\s*\d{4}|Q\d{4}|A\d{4})\b', raw_text, re.IGNORECASE)
    if q:
        item["altimeter"] = q.group(1).upper()

    tr = re.search(r'\b(NOSIG|TEMPO[^\.\n]*|BECMG[^\.\n]*)', raw_text, re.IGNORECASE)
    if tr:
        item["trend"] = tr.group(1).strip().rstrip('.')

    rmk = re.findall(r'\b(CAUTION[^\.\n]+|ACK(?:NOWLEDGE)?[^\.\n]+)', raw_text, re.IGNORECASE)
    if rmk:
        item["remarks"] = "; ".join([r.strip().rstrip('.') for r in rmk])

    return item

def run():
    now_utc = datetime.now(timezone.utc)
    date_str = now_utc.strftime("%d%m%Y")
    
    print(f"--- URUCHOMIENIE SKRYPTU: {now_utc.strftime('%Y-%m-%d %H:%M:%S UTC')} ---")

    for icao in AIRPORTS:
        airport_dir = os.path.join(DATA_DIR, icao)
        os.makedirs(airport_dir, exist_ok=True)

        url = BASE_URL.format(icao=icao)
        try:
            response = requests.get(url, headers=HEADERS, timeout=15)
            response.raise_for_status()

            raw_blocks = extract_blocks(response.text, icao)
            print(f"[{icao}] Odczytano {len(raw_blocks)} bloków ze strony.")
            
            if not raw_blocks:
                continue

            parsed_reports = [parse_atis_simple(b) for b in raw_blocks]
            current_texts = [r["raw_text"] for r in parsed_reports]

            # Sprawdzenie duplikatów: jeśli surowy tekst co do linijki jest TEN SAM, to nie zapisujemy
            existing_files = [f for f in os.listdir(airport_dir) if f.endswith(".json")]
            if existing_files:
                # Bierzemy plik najpóźniej modyfikowany w systemie
                existing_files.sort(key=lambda x: os.path.getmtime(os.path.join(airport_dir, x)))
                last_file_path = os.path.join(airport_dir, existing_files[-1])
                try:
                    with open(last_file_path, "r", encoding="utf-8") as f:
                        last_data = json.load(f)
                        last_texts = [r.get("raw_text") for r in last_data.get("reports", []) if isinstance(r, dict)]
                        
                        if current_texts == last_texts:
                            print(f"[{icao}] Brak zmian względem {existing_files[-1]}. Skrypt pomija zapis.")
                            continue
                except Exception as err:
                    print(f"[{icao}] Uwaga: Nie udało się wczytać starego pliku: {err}")

            # Wyznaczanie nazwy pliku na podstawie zadeklarowanego TIME (np. 1100z)
            file_time_str = now_utc.strftime("%H%M") # Godzina systemowa jako zabezpieczenie
            if parsed_reports and parsed_reports[0].get("time"):
                t = parsed_reports[0]["time"]
                digits = re.sub(r'\D', '', t) # Wyciągnięcie samych 4 cyfr
                if len(digits) == 4:
                    file_time_str = digits

            filename = f"{icao}_{date_str}_{file_time_str}z.json"
            filepath = os.path.join(airport_dir, filename)

            payload = {
                "airport": icao,
                "fetch_time_utc": now_utc.strftime("%Y-%m-%d %H:%M:%S UTC"),
                "reports": parsed_reports
            }

            with open(filepath, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)

            summary = ", ".join([f"{r['type']} ({r['time']})" for r in parsed_reports])
            print(f"[{icao}] ---> ZAPISANO NOWY PLIK: {filepath} [{summary}]")

        except Exception as err:
            print(f"[{icao}] Błąd połączenia lub przetwarzania: {err}")

if __name__ == "__main__":
    run()