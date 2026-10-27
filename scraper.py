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
    Ekstrakcja bloków ATIS w oparciu o czysty tekst i precyzyjne dopasowanie początku linii.
    """
    soup = BeautifulSoup(html, "html.parser")
    
    # Usuwamy całe UI strony, aby nie kolidowało z tekstem lotniczym
    for tag in soup(["script", "style", "nav", "header", "footer", "noscript", "svg"]):
        tag.decompose()

    # Zrzut do tekstu z wymuszonymi nowymi liniami dla każdego bloku HTML
    text = soup.get_text(separator="\n")
    
    # Czyszczenie białych znaków (każda linijka będzie zaczynać się od razu od tekstu)
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    clean_text = "\n".join(lines)

    # Wzorzec: Nowa linia (^) -> Kod lotniska -> (opcjonalnie ARR/DEP) -> ATIS/INFO -> Litera
    # Przykłady: "EPWA ARR ATIS L", "EPKK ATIS INFO B", "EPGD DEP ATIS Z"
    pattern = re.compile(rf'^{icao}\s+(?:ARR\s+|DEP\s+)?(?:ATIS|INFO(?:RMATION)?)\s+[A-Z]\b', re.MULTILINE | re.IGNORECASE)
    
    matches = list(pattern.finditer(clean_text))
    blocks = []
    
    for i, m in enumerate(matches):
        start_idx = m.start()
        # Koniec to start następnego dopasowania lub koniec całego tekstu
        end_idx = matches[i+1].start() if i + 1 < len(matches) else len(clean_text)
        
        sub_text = clean_text[start_idx:end_idx]
        
        # Jeśli w międzyczasie pojawi się nagłówek METAR, TAF lub UI strony - ucinamy wszystko poniżej
        trunc_match = re.search(r'^(?:METAR|TAF|Arrival ATIS|Departure ATIS)\b', sub_text, re.MULTILINE | re.IGNORECASE)
        if trunc_match:
            sub_text = sub_text[:trunc_match.start()]
            
        sub_lines = sub_text.strip().splitlines()
        final_lines = []
        
        for line in sub_lines:
            final_lines.append(line)
            # Definitywne znaczniki końca lotniczej depeszy cyfrowej
            if re.search(r'^(?:END OF\b|REPORT\s+[A-Z]\b|ACK(?:NOWLEDGE)?\b)', line, re.IGNORECASE):
                break
                
        final_block = "\n".join(final_lines).strip()
        if len(final_block) > 15:
            blocks.append(final_block)
            
    return blocks

def parse_atis_simple(raw_text: str) -> dict:
    """Parsuje kategorie bezpośrednio z czystego bloku tekstu."""
    lines = raw_text.splitlines()
    first_line = lines[0].upper()
    
    if "ARR" in first_line:
        atis_type = "ARRIVAL"
    elif "DEP" in first_line:
        atis_type = "DEPARTURE"
    else:
        atis_type = "COMBINED"
        
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

    m_z = re.search(r'\b(\d{4}Z)\b', raw_text, re.IGNORECASE)
    m_obs = re.search(r'\bTIME\s+(\d{4})\b', raw_text, re.IGNORECASE)
    if m_z:
        item["time"] = m_z.group(1).upper()
    elif m_obs:
        item["time"] = m_obs.group(1) + "Z"

    rwy = re.findall(r'\b(?:DEP\s+RWY|ARR\s+RWY|RWY|RUNWAY)\s+([0-9]{2}[LCR]?)', raw_text, re.IGNORECASE)
    if rwy:
        item["runway"] = ", ".join(list(dict.fromkeys(rwy)))

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
        item["weather"] = " ".join(list(dict.fromkeys(wx)))

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

def main():
    now_utc = datetime.now(timezone.utc)
    date_str = now_utc.strftime("%d%m%Y")
    time_str = now_utc.strftime("%H%M")
    
    print(f"--- URUCHOMIENIE SKRYPTU: {now_utc.strftime('%Y-%m-%d %H:%M:%S UTC')} ---")

    for icao in AIRPORTS:
        airport_dir = os.path.join(DATA_DIR, icao)
        os.makedirs(airport_dir, exist_ok=True)
        
        url = BASE_URL.format(icao=icao)
        try:
            response = requests.get(url, headers=HEADERS, timeout=15)
            response.raise_for_status()
            
            raw_blocks = extract_blocks(response.text, icao)
            print(f"[{icao}] Znaleziono depesz na stronie: {len(raw_blocks)}")
            
            if not raw_blocks:
                continue
                
            parsed_reports = [parse_atis_simple(b) for b in raw_blocks]
            current_raws = [r["raw_text"] for r in parsed_reports]
            
            # Weryfikacja różnic względem ostatniego pliku (zapobiega tworzeniu setek duplikatów)
            existing_files = sorted([f for f in os.listdir(airport_dir) if f.endswith(".json")])
            if existing_files:
                last_file_path = os.path.join(airport_dir, existing_files[-1])
                try:
                    with open(last_file_path, "r", encoding="utf-8") as f:
                        last_data = json.load(f)
                        last_reports = last_data.get("reports", [])
                        last_raws = [r.get("raw_text") for r in last_reports if isinstance(r, dict)]
                        
                        if current_raws == last_raws:
                            print(f"[{icao}] Tekst depeszy bez zmian. Pomijam zapis duplikatu pliku {existing_files[-1]}.")
                            continue
                except Exception as e:
                    print(f"[{icao}] Uwaga: Błąd odczytu poprzedniego pliku JSON: {e}")
            
            filename = f"{icao}_{date_str}_{time_str}z.json"
            filepath = os.path.join(airport_dir, filename)
            
            payload = {
                "airport": icao,
                "fetch_time_utc": now_utc.strftime("%Y-%m-%d %H:%M:%S UTC"),
                "reports": parsed_reports
            }
            
            with open(filepath, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
                
            types_str = ", ".join([f"{r['type']} ({r['info_letter']})" for r in parsed_reports])
            print(f"[{icao}] ---> ZAPISANO NOWY PLIK: {filepath} [{types_str}]")
            
        except requests.exceptions.RequestException as e:
            print(f"[{icao}] Błąd połączenia ze stroną: {e}")
        except Exception as e:
            print(f"[{icao}] Wystąpił nieoczekiwany błąd: {e}")

if __name__ == "__main__":
    main()