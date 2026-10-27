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

def clean_block_lines(text: str) -> str:
    """Czyści surowy blok tekstu z menu i stopki strony."""
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    cleaned = []
    
    for l in lines:
        # Odcinamy nagłówki kolejnych kart i sekcji serwisu
        if cleaned and re.search(r'^(?:METAR\b|TAF\b|Arrival ATIS|Departure ATIS|Formatting\b|History\b)', l, re.IGNORECASE):
            break
        cleaned.append(l)
        # Znaczniki końca konkretnej depeszy ATIS
        if re.search(r'^(?:END OF\b|REPORT\s+[A-Z]\b|ACK(?:NOWLEDGE)?\b)', l, re.IGNORECASE):
            break

    return "\n".join(cleaned).strip()

def extract_atis_sections(html: str, icao: str) -> list[dict]:
    """Wyszukuje wszystkie depesze ATIS dla danego lotniska bez ryzyka ucięcia przez navbar."""
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript", "svg"]):
        tag.decompose()

    full_text = soup.get_text(separator="\n")

    # Wzorzec dopasowujący każdy nagłówek ATIS: np. EPWA ARR ATIS I, EPKK ATIS INFO B, EPGD ATIS C
    header_pattern = re.compile(
        rf'\b{icao}\s+(?:(ARR|DEP)\s+)?ATIS(?:\s+INFO)?\s+([A-Z])\b',
        re.IGNORECASE
    )

    matches = list(header_pattern.finditer(full_text))
    if not matches:
        # Fallback dla składni bez słowa ATIS (np. EPWA ARR I)
        header_pattern = re.compile(rf'\b{icao}\s+(ARR|DEP)\s+([A-Z])\b', re.IGNORECASE)
        matches = list(header_pattern.finditer(full_text))

    if not matches:
        return []

    sections = []
    for i, match in enumerate(matches):
        start_pos = match.start()
        
        # Koniec bloku to początek kolejnej depeszy lub sekcja METAR/TAF poniżej
        if i + 1 < len(matches):
            end_pos = matches[i + 1].start()
        else:
            post_text = full_text[start_pos:]
            # Szukamy słowa METAR lub TAF WYŁĄCZNIE po bieżącej depeszy (nie w navbarze)
            stop_match = re.search(r'\b(?:METAR\b|TAF\b|Arrival ATIS|Departure ATIS)', post_text[30:], re.IGNORECASE)
            end_pos = (start_pos + 30 + stop_match.start()) if stop_match else len(full_text)

        raw_segment = full_text[start_pos:end_pos]
        cleaned_text = clean_block_lines(raw_segment)

        if len(cleaned_text) < 25:
            continue

        raw_type = match.group(1)
        letter = match.group(2).upper()

        if raw_type:
            atis_type = "ARRIVAL" if raw_type.upper() == "ARR" else "DEPARTURE"
        else:
            if "ARR ATIS" in cleaned_text or f"{icao} ARR" in cleaned_text:
                atis_type = "ARRIVAL"
            elif "DEP ATIS" in cleaned_text or f"{icao} DEP" in cleaned_text:
                atis_type = "DEPARTURE"
            else:
                atis_type = "COMBINED"

        sections.append({
            "type": atis_type,
            "info_letter": letter,
            "raw_text": cleaned_text
        })

    return sections

def parse_atis_simple(raw_text: str, atis_type: str, info_letter: str) -> dict:
    """Parsuje kategorie z depeszy do płaskiego formatu tekstowego."""
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

    # 1. Czas
    m_z = re.search(r'\b(\d{4}Z)\b', raw_text)
    m_obs = re.search(r'\bTIME\s+(\d{4})\b', raw_text)
    if m_z:
        item["time"] = m_z.group(1)
    elif m_obs:
        item["time"] = m_obs.group(1) + "Z"

    # 2. Pasy w użyciu
    rwy = re.findall(r'\b(?:DEP\s+RWY|ARR\s+RWY|RWY|RUNWAY)\s+([0-9]{2}[LCR]?)', raw_text, re.IGNORECASE)
    if rwy:
        item["runway"] = ", ".join(dict.fromkeys(rwy))

    # 3. Podejście (Approach)
    apch = re.search(r'\b(EXP[^\.\n]+(?:APCH|ILS)[^\.\n]*)', raw_text, re.IGNORECASE)
    if apch:
        item["approach"] = apch.group(1).strip().rstrip('.')

    # 4. Poziom przejściowy (Transition Level)
    tl = re.search(r'\bTRANSITION\s+LEVEL\s+(\d+)\b', raw_text, re.IGNORECASE) or re.search(r'\bTL\s*(\d+)\b', raw_text)
    if tl:
        item["transition_level"] = tl.group(1)

    # 5. Wiatr
    w = re.search(r'\bWIND\s+([^\.\n]+)', raw_text, re.IGNORECASE)
    if w:
        item["wind"] = w.group(1).strip().rstrip('.')

    # 6. Widzialność i chmury
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

    # 7. Zjawiska pogody
    wx = re.findall(r'\b([+-]?(?:VC)?(?:DZ|RA|SN|SG|IC|PL|GR|GS|UP|BR|FG|FU|VA|DU|SA|HZ|PY|PO|SQ|FC|SS|DS|TSRA|TS|SHRA|SHSN))\b', raw_text)
    if wx:
        item["weather"] = " ".join(dict.fromkeys(wx))

    # 8. Temperatura i punkt rosy
    t = re.search(r'\bT\s*(-?\d+|M\d+)\b', raw_text)
    if t:
        item["temperature"] = t.group(1).replace("M", "-")

    dp = re.search(r'\bDP\s*(-?\d+|M\d+)\b', raw_text)
    if dp:
        item["dewpoint"] = dp.group(1).replace("M", "-")

    # 9. Ciśnienie (QNH)
    q = re.search(r'\b(QNH\s*\d{4}|Q\d{4}|A\d{4})\b', raw_text, re.IGNORECASE)
    if q:
        item["altimeter"] = q.group(1).upper()

    # 10. Prognoza trendu
    tr = re.search(r'\b(NOSIG|TEMPO[^\.\n]*|BECMG[^\.\n]*)', raw_text)
    if tr:
        item["trend"] = tr.group(1).strip().rstrip('.')

    # 11. Uwagi
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

            raw_sections = extract_atis_sections(response.text, icao)
            if not raw_sections:
                print(f"[{icao}] Brak aktywnych depesz na stronie atis.guru.")
                continue

            parsed_reports = [
                parse_atis_simple(s["raw_text"], s["type"], s["info_letter"])
                for s in raw_sections
            ]

            current_sig = [
                (r["type"], r["info_letter"], r["time"], r["raw_text"])
                for r in parsed_reports
            ]

            # Porównanie z ostatnim plikiem w folderze
            existing_files = sorted([f for f in os.listdir(airport_dir) if f.endswith(".json")])
            if existing_files:
                last_file_path = os.path.join(airport_dir, existing_files[-1])
                try:
                    with open(last_file_path, "r", encoding="utf-8") as f:
                        last_data = json.load(f)
                        last_reports = last_data.get("reports", [])
                        last_sig = [
                            (r.get("type"), r.get("info_letter"), r.get("time"), r.get("raw_text"))
                            for r in last_reports if isinstance(r, dict)
                        ]
                        # Jeśli stary plik miał błędny typ COMBINED, a teraz mamy ARR/DEP, wymuś aktualizację
                        had_wrong_combined = any(r.get("type") == "COMBINED" for r in last_reports) and any(r["type"] != "COMBINED" for r in parsed_reports)

                        if current_sig == last_sig and not had_wrong_combined:
                            print(f"[{icao}] ATIS bez zmian od pliku {existing_files[-1]}. Pomijam.")
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

            summary = ", ".join([f"{r['type']}: {r['info_letter']}" for r in parsed_reports])
            print(f"[{icao}] Zapisano nowy plik: {filepath} ({summary})")

        except Exception as err:
            print(f"[{icao}] Błąd podczas pobierania {url}: {err}")

if __name__ == "__main__":
    run()