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
    """Wyszukuje bloki zaczynające się od nazwy lotniska w nowej linii."""
    soup = BeautifulSoup(html, "html.parser")
    # Kasujemy niewidoczne elementy UI oraz stopki z atis.guru
    for tag in soup(["script", "style", "noscript", "svg", "nav", "header", "footer"]):
        tag.decompose()

    text = soup.get_text(separator="\n")
    
    # Redukcja pustych linii dla poprawnego parsowania
    lines = [re.sub(r'\s+', ' ', line.strip()) for line in text.splitlines() if line.strip()]
    clean_text = "\n".join(lines)

    # Wzorzec na twardy początek linii (np. "EPWA ARR ATIS I", "EPKK ATIS INFO D")
    pattern = re.compile(rf'^{icao}\s+(?:ARR\s+|DEP\s+)?(?:ATIS\s+)?(?:INFO\s+)?[A-Z]\b', re.MULTILINE | re.IGNORECASE)
    matches = list(pattern.finditer(clean_text))
    
    blocks = []
    for i, match in enumerate(matches):
        start = match.start()
        end_search_text = clean_text[start+1:]
        
        # Szukamy początku następnej depeszy lub początku sekcji z METARem
        next_header = pattern.search(end_search_text)
        metar_taf = re.search(r'^(?:METAR|TAF|Arrival ATIS|Departure ATIS)\b', end_search_text, re.MULTILINE | re.IGNORECASE)
        
        cut_offsets = []
        if next_header: cut_offsets.append(next_header.start())
        if metar_taf: cut_offsets.append(metar_taf.start())
        
        end = (start + 1 + min(cut_offsets)) if cut_offsets else len(clean_text)
        raw_block = clean_text[start:end].strip()
        
        # Ucinamy bloki zaraz po informacjach typu "END OF", żeby odciąć puste stopki
        block_lines = raw_block.splitlines()
        final_lines = []
        for l in block_lines:
            final_lines.append(l)
            if re.search(r'^(?:END OF\b|REPORT\s+[A-Z]\b|ACK(?:NOWLEDGE)?\b)', l, re.IGNORECASE):
                break
                
        final_text = "\n".join(final_lines).strip()
        
        if len(final_text) > 15:
            header_line = final_lines[0]
            
            # Odczytywanie z nagłówka
            atis_type = "COMBINED"
            if "ARR" in header_line.upper():
                atis_type = "ARRIVAL"
            elif "DEP" in header_line.upper():
                atis_type = "DEPARTURE"
                
            letter_match = re.search(r'\b([A-Z])$', header_line)
            info_letter = letter_match.group(1).upper() if letter_match else "?"
            
            blocks.append({
                "type": atis_type,
                "info_letter": info_letter,
                "raw_text": final_text
            })
            
    return blocks

def parse_atis_simple(raw_text: str, atis_type: str, info_letter: str) -> dict:
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

    m_z = re.search(r'\b(\d{4}Z)\b', raw_text, re.IGNORECASE)
    m_obs = re.search(r'\bTIME\s+(\d{4})\b', raw_text, re.IGNORECASE)
    if m_z:
        item["time"] = m_z.group(1).upper()
    elif m_obs:
        item["time"] = m_obs.group(1) + "Z"

    rwy = re.findall(r'\b(?:DEP\s+RWY|ARR\s+RWY|RWY|RUNWAY)\s+([0-9]{2}[LCR]?)', raw_text, re.IGNORECASE)
    if rwy:
        item["runway"] = ", ".join(dict.fromkeys(rwy))

    apch = re.search(r'\b(EXP[^\.\n]+(?:APCH|ILS)[^\.\n]*)', raw_text, re.IGNORECASE)
    if apch:
        item["approach"] = apch.group(1).strip().rstrip('.')

    tl = re.search(r'\bTRANSITION\s+LEVEL\s+(\d+)\b', raw_text, re.IGNORECASE) or re.search(r'\bTL\s*(\d+)\b', raw_text, re.IGNORECASE)
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

            raw_sections = extract_blocks(response.text, icao)
            print(f"[{icao}] Odczytano {len(raw_sections)} bloków ze strony.")
            if not raw_sections:
                continue

            parsed_reports = [parse_atis_simple(s["raw_text"], s["type"], s["info_letter"]) for s in raw_sections]
            current_raws = [r["raw_text"] for r in parsed_reports]

            # JEDYNY WARUNEK POMINIĘCIA: tekst depeszy jest w 100% taki sam jak w poprzednim pliku
            existing_files = sorted([f for f in os.listdir(airport_dir) if f.endswith(".json")])
            if existing_files:
                last_file_path = os.path.join(airport_dir, existing_files[-1])
                try:
                    with open(last_file_path, "r", encoding="utf-8") as f:
                        last_data = json.load(f)
                        last_reports = last_data.get("reports", [])
                        last_raws = [r.get("raw_text") for r in last_reports if isinstance(r, dict)]
                        
                        if current_raws == last_raws:
                            print(f"[{icao}] Tekst depeszy nie zmienił się od {existing_files[-1]}. Skrypt pomija duplikat.")
                            continue
                except Exception as err:
                    print(f"[{icao}] Problem z plikiem {existing_files[-1]}: {err}")

            filename = f"{icao}_{date_str}_{time_str}z.json"
            filepath = os.path.join(airport_dir, filename)

            payload = {
                "airport": icao,
                "fetch_time_utc": now_utc.strftime("%Y-%m-%d %H:%M:%S UTC"),
                "reports": parsed_reports
            }

            with open(filepath, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)

            summary = ", ".join([f"{r['type']}: {r['info_letter']}" for r in parsed_reports])
            print(f"[{icao}] ---> ZAPISANO NOWY PLIK: {filepath} ({summary})")

        except Exception as err:
            print(f"[{icao}] Błąd połączenia: {err}")

if __name__ == "__main__":
    run()