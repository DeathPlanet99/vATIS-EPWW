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

def extract_blocks(html: str, icao: str) -> list[dict]:
    """Wyszukuje bloki bez usuwania jakichkolwiek linii ze środka depeszy."""
    soup = BeautifulSoup(html, "html.parser")
    
    # Usuwamy tylko zewnętrzne znaczniki HTML (menu, stopki)
    for tag in soup(["script", "style", "nav", "header", "footer"]):
        tag.decompose()

    text = soup.get_text(separator="\n")
    # Zamiana wielokrotnych spacji w jednej linii na pojedyncze, by ułatwić szukanie regexem
    lines = [re.sub(r'[ \t]+', ' ', l).strip() for l in text.splitlines()]
    clean_text = "\n".join([l for l in lines if l])

    # Wzorzec szukający nagłówka ATIS gdziekolwiek w tekście (brak sztywnego ^)
    pattern = re.compile(rf'\b({icao}\s+(?:ARR\s+|DEP\s+)?ATIS(?:\s+INFO(?:RMATION)?)?\s+[A-Z])\b', re.IGNORECASE)
    matches = list(pattern.finditer(clean_text))
    
    # Awaryjnie, gdyby na stronie brakowało słowa ATIS (np. EPWA ARR L)
    if not matches:
        pattern = re.compile(rf'\b({icao}\s+(?:ARR|DEP)\s+[A-Z])\b', re.IGNORECASE)
        matches = list(pattern.finditer(clean_text))

    valid_matches = []
    for m in matches:
        # Jeśli przed nagłówkiem znajduje się czas (np. "1226Z "), to znaczy, 
        # że jest to tylko powtórka w środku depeszy - ignorujemy.
        prefix = clean_text[max(0, m.start()-15):m.start()]
        if re.search(r'\d{4}Z\s*$', prefix, re.IGNORECASE):
            continue
        valid_matches.append(m)

    blocks = []
    for i, m in enumerate(valid_matches):
        start_idx = m.start()
        
        # Koniec tego bloku to początek następnego, albo (jeśli to ostatni) początek sekcji METAR/TAF
        if i + 1 < len(valid_matches):
            end_idx = valid_matches[i+1].start()
        else:
            post_text = clean_text[start_idx:]
            stop_match = re.search(r'\n(?:METAR|TAF)\b', post_text, re.IGNORECASE)
            end_idx = start_idx + stop_match.start() if stop_match else len(clean_text)
            
        # Zapisujemy PEŁNY tekst bez odrzucania jakichkolwiek linijek
        raw_text = clean_text[start_idx:end_idx].strip()
        
        header_str = m.group(1).upper()
        if "ARR" in header_str:
            atis_type = "ARRIVAL"
        elif "DEP" in header_str:
            atis_type = "DEPARTURE"
        else:
            atis_type = "COMBINED"
            
        letter = header_str.split()[-1]
        
        blocks.append({
            "type": atis_type,
            "info_letter": letter,
            "raw_text": raw_text
        })
        
    return blocks

def parse_atis_simple(raw_text: str, atis_type: str, info_letter: str) -> dict:
    """Parsuje kategorie, szukając parametrów w całym surowym tekście."""
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

    # Wyciąganie czasu - najważniejszy parametr do sprawdzania nowych depesz
    m_obs = re.search(r'\bTIME\s+(\d{4})\b', raw_text, re.IGNORECASE)
    m_z = re.search(r'\b(\d{4})Z\b', raw_text, re.IGNORECASE)
    if m_obs:
        item["time"] = m_obs.group(1) + "Z"
    elif m_z:
        item["time"] = m_z.group(1) + "Z"

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

    wx = re.findall(r'\b([+-]?(?:VC)?(?:DZ|RA|SN|SG|IC|PL|GR|GS|UP|BR|FG|FU|VA|DU|SA|HZ|PY|PO|SQ|FC|SS|DS|TSRA|TS|SHRA|SHSN))\b', raw_text)
    if wx:
        item["weather"] = " ".join(dict.fromkeys(wx))

    t = re.search(r'\bT\s*(-?\d+|M\d+)\b', raw_text)
    if t:
        item["temperature"] = t.group(1).replace("M", "-")

    dp = re.search(r'\bDP\s*(-?\d+|M\d+)\b', raw_text)
    if dp:
        item["dewpoint"] = dp.group(1).replace("M", "-")

    q = re.search(r'\b(QNH\s*\d{4}|Q\d{4}|A\d{4})\b', raw_text, re.IGNORECASE)
    if q:
        item["altimeter"] = q.group(1).upper()

    tr = re.search(r'\b(NOSIG|TEMPO[^\.\n]*|BECMG[^\.\n]*)', raw_text)
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
            response = requests.get(url, headers=HEADERS, timeout=20)
            response.raise_for_status()

            raw_blocks = extract_blocks(response.text, icao)
            print(f"[{icao}] Odczytano {len(raw_blocks)} bloków ze strony.")
            if not raw_blocks:
                continue

            parsed_reports = [parse_atis_simple(b["raw_text"], b["type"], b["info_letter"]) for b in raw_blocks]
            
            # Weryfikacja JEDYNIE na podstawie czasu TIME, litery i typu
            current_signature = [(r["type"], r["info_letter"], r["time"]) for r in parsed_reports]

            existing_files = [f for f in os.listdir(airport_dir) if f.endswith(".json")]
            if existing_files:
                # Bierzemy chronologicznie ostatnio zmodyfikowany plik
                existing_files.sort(key=lambda x: os.path.getmtime(os.path.join(airport_dir, x)))
                last_file_path = os.path.join(airport_dir, existing_files[-1])
                try:
                    with open(last_file_path, "r", encoding="utf-8") as f:
                        last_data = json.load(f)
                        last_reports = last_data.get("reports", [])
                        last_signature = [(r.get("type"), r.get("info_letter"), r.get("time")) for r in last_reports if isinstance(r, dict)]
                        
                        # Jeśli Typ, Litera i Czas się zgadzają -> jest to ta sama depesza
                        if current_signature == last_signature:
                            print(f"[{icao}] Czas (TIME) i litery nie uległy zmianie. Pomijam zapis.")
                            continue
                except Exception as err:
                    print(f"[{icao}] Zignorowano błąd odczytu poprzedniego pliku: {err}")

            # Tworzenie nazwy na podstawie zadeklarowanego czasu z depeszy (np. TIME 1100)
            file_time_str = now_utc.strftime("%H%M") # Domyślnie obecna godzina, jeśli brak TIME
            if parsed_reports and parsed_reports[0].get("time"):
                t = parsed_reports[0]["time"]
                digits = re.sub(r'\D', '', t) # Wyciągamy same cyfry (np z 1100Z -> 1100)
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

            summary = ", ".join([f"{r['type']}: {r['info_letter']} ({r['time']})" for r in parsed_reports])
            print(f"[{icao}] ---> ZAPISANO NOWY PLIK: {filepath} [{summary}]")

        except Exception as err:
            print(f"[{icao}] Błąd połączenia lub przetwarzania: {err}")

if __name__ == "__main__":
    run()