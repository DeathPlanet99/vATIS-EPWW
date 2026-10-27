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
    """Precyzyjnie wyciąga bloki ATIS, ignorując sklejony tekst i powtórzenia wewnątrz depeszy."""
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript", "svg", "nav", "header", "footer"]):
        tag.decompose()

    text = soup.get_text(separator="\n")
    lines = [re.sub(r'[ \t]+', ' ', l.strip()) for l in text.splitlines() if l.strip()]
    clean_text = "\n".join(lines)

    # Wzorzec szukający nazwy lotniska (np. EPWA ARR ATIS L, EPKK ATIS INFO D)
    # \b pozwala na wykrycie nagłówka, nawet jeśli został sklejony w tej samej linii co 'TIME 1225.'
    pattern = re.compile(rf'\b{icao}\s+(?:(?:ARR|DEP)\s+)?(?:ATIS\s+)?(?:INFO(?:RMATION)?\s+)?[A-Z]\b', re.IGNORECASE)
    all_matches = list(pattern.finditer(clean_text))
    
    valid_matches = []
    for m in all_matches:
        prefix = clean_text[max(0, m.start()-15):m.start()]
        # Jeśli przed nagłówkiem znajduje się czas np. "1226Z ", to jest to 
        # tylko powtórzenie wewnątrz depeszy, a nie główny nagłówek - odrzucamy.
        if re.search(r'\d{4}Z\s*$', prefix, re.IGNORECASE):
            continue
        valid_matches.append(m)
        
    blocks = []
    for i, m in enumerate(valid_matches):
        start_idx = m.start()
        end_idx = valid_matches[i+1].start() if i + 1 < len(valid_matches) else len(clean_text)
        
        raw_segment = clean_text[start_idx:end_idx]
        seg_lines = raw_segment.splitlines()
        
        final_lines = []
        for l in seg_lines:
            # Sekcje menu i pogody na stronie to granica depeszy
            if re.search(r'^(?:METAR|TAF|Arrival ATIS|Departure ATIS|Formatting|History)\b', l, re.IGNORECASE):
                break
            final_lines.append(l)
            # Znaczniki lotnicze kończące transmisję ATIS
            if re.search(r'^(?:END OF\b|REPORT\s+[A-Z]\b|ACK(?:NOWLEDGE)?\b)', l, re.IGNORECASE):
                break
                
        final_text = "\n".join(final_lines).strip()
        
        if len(final_text) > 15:
            header_line = final_lines[0]
            
            atis_type = "COMBINED"
            if "ARR" in header_line.upper():
                atis_type = "ARRIVAL"
            elif "DEP" in header_line.upper():
                atis_type = "DEPARTURE"
                
            let_m = re.search(r'\b([A-Z])$', header_line.upper())
            info_letter = let_m.group(1) if let_m else "?"
            
            blocks.append({
                "type": atis_type,
                "info_letter": info_letter,
                "raw_text": final_text
            })
            
    return blocks

def parse_atis_simple(raw_text: str, atis_type: str, info_letter: str) -> dict:
    """Konwertuje tekst depeszy do płaskiego formatu JSON."""
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

    # Szukamy czasu (priorytetowo TIME, awaryjnie Zulu z początku tekstu)
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

            raw_sections = extract_blocks(response.text, icao)
            if not raw_sections:
                print(f"[{icao}] Brak aktywnych depesz na stronie.")
                continue

            parsed_reports = [parse_atis_simple(s["raw_text"], s["type"], s["info_letter"]) for s in raw_sections]
            current_raws = [r["raw_text"] for r in parsed_reports]

            # Porównywanie ze zlokalizowanym plikiem, posortowanym według fizycznego czasu modyfikacji
            existing_files = [f for f in os.listdir(airport_dir) if f.endswith(".json")]
            if existing_files:
                existing_files.sort(key=lambda x: os.path.getmtime(os.path.join(airport_dir, x)))
                last_file_path = os.path.join(airport_dir, existing_files[-1])
                try:
                    with open(last_file_path, "r", encoding="utf-8") as f:
                        last_data = json.load(f)
                        last_reports = last_data.get("reports", [])
                        last_raws = [r.get("raw_text") for r in last_reports if isinstance(r, dict)]
                        
                        if current_raws == last_raws:
                            print(f"[{icao}] Brak zmian w tekście (identyczny z {existing_files[-1]}). Pomijam.")
                            continue
                except Exception as err:
                    print(f"[{icao}] Zignorowano błąd odczytu poprzedniego pliku: {err}")

            # Wyznaczanie nazwy pliku na podstawie zadeklarowanego w depeszy czasu TIME (np. 1100z)
            file_time_str = now_utc.strftime("%H%M") # Fallback, jeśli depesza nie ma czasu
            if parsed_reports:
                first_time = parsed_reports[0].get("time")
                if first_time:
                    digits_only = first_time.upper().replace("Z", "")
                    if digits_only.isdigit() and len(digits_only) == 4:
                        file_time_str = digits_only

            filename = f"{icao}_{date_str}_{file_time_str}z.json"
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