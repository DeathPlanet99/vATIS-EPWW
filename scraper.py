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

def clean_block_text(text: str) -> str:
    """Czyści linie bloku ATIS i ucina ewentualne stopki/kolejne sekcje."""
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    trimmed = []
    
    for l in lines:
        # Odcinamy nagłówki kolejnych sekcji strony
        if trimmed and re.search(r'^(?:Arrival ATIS|Departure ATIS|METAR|TAF|Formatting|History)\b', l, re.IGNORECASE):
            break
        trimmed.append(l)
        # Znaczniki jednoznacznego końca danej depeszy
        if re.search(r'^(?:END OF\b|REPORT\s+[A-Z]\b|ACK(?:NOWLEDGE)?\b)', l, re.IGNORECASE):
            break

    return "\n".join(trimmed).strip()

def extract_atis_sections(html: str, icao: str) -> list[dict]:
    """
    Wyszukuje sekcje ARRIVAL, DEPARTURE lub COMBINED na podstawie 
    dokładnych składni nagłówków poszczególnych lotnisk.
    """
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript", "svg"]):
        tag.decompose()

    full_text = soup.get_text(separator="\n")
    
    # 1. Szukamy pozycji nagłówka ARRIVAL: np. "EPKK ARR ATIS D" lub "EPWA ARR ATIS L"
    arr_match = re.search(rf'\b{icao}\s+ARR\s+ATIS(?:\s+INFO)?\s+([A-Z])\b', full_text, re.IGNORECASE)
    
    # 2. Szukamy pozycji nagłówka DEPARTURE: np. "EPKK DEP ATIS Z" lub "EPWA DEP ATIS Z"
    dep_match = re.search(rf'\b{icao}\s+DEP\s+ATIS(?:\s+INFO)?\s+([A-Z])\b', full_text, re.IGNORECASE)

    sections = []

    # Szukamy początku sekcji METAR lub TAF, aby wiedzieć gdzie kończy się ostatnia depesza
    metar_match = re.search(r'\b(?:METAR|TAF)\b', full_text)
    end_of_all = metar_match.start() if metar_match else len(full_text)

    # Scenariusz A: Lotnisko ma równolegle ARRIVAL i DEPARTURE (np. EPWA, EPKK, EPGD, EPPO)
    if arr_match and dep_match:
        if arr_match.start() < dep_match.start():
            arr_raw = full_text[arr_match.start():dep_match.start()]
            dep_raw = full_text[dep_match.start():end_of_all]
        else:
            dep_raw = full_text[dep_match.start():arr_match.start()]
            arr_raw = full_text[arr_match.start():end_of_all]

        sections.append({
            "type": "ARRIVAL",
            "info_letter": arr_match.group(1).upper(),
            "raw_text": clean_block_text(arr_raw)
        })
        sections.append({
            "type": "DEPARTURE",
            "info_letter": dep_match.group(1).upper(),
            "raw_text": clean_block_text(dep_raw)
        })

    # Scenariusz B: Tylko ARRIVAL
    elif arr_match:
        arr_raw = full_text[arr_match.start():end_of_all]
        sections.append({
            "type": "ARRIVAL",
            "info_letter": arr_match.group(1).upper(),
            "raw_text": clean_block_text(arr_raw)
        })

    # Scenariusz C: Tylko DEPARTURE
    elif dep_match:
        dep_raw = full_text[dep_match.start():end_of_all]
        sections.append({
            "type": "DEPARTURE",
            "info_letter": dep_match.group(1).upper(),
            "raw_text": clean_block_text(dep_raw)
        })

    # Scenariusz D: Pojedynczy ATIS COMBINED (np. EPKK ATIS B lub EPGD ATIS C)
    else:
        # Szukamy nagłówka niebędącego drugą linią czasu (nie poprzedzonego czasem Zulu)
        comb_match = re.search(rf'(?<!\d{{4}}Z\s)(?<!\d{{4}}Z\n)\b{icao}\s+ATIS(?:\s+INFO)?\s+([A-Z])\b', full_text, re.IGNORECASE)
        if comb_match:
            comb_raw = full_text[comb_match.start():end_of_all]
            sections.append({
                "type": "COMBINED",
                "info_letter": comb_match.group(1).upper(),
                "raw_text": clean_block_text(comb_raw)
            })

    return sections

def parse_atis_simple(raw_text: str, atis_type: str, info_letter: str) -> dict:
    """Parsuje kategorie ATIS do płaskich pól tekstowych."""
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

    # 1. Czas Zulu lub TIME
    m_z = re.search(r'\b(\d{4}Z)\b', raw_text)
    m_obs = re.search(r'\bTIME\s+(\d{4})\b', raw_text)
    if m_z:
        item["time"] = m_z.group(1)
    elif m_obs:
        item["time"] = m_obs.group(1) + "Z"

    # 2. Pasy w użyciu
    rwy = re.findall(r'\b(?:DEP\s+RWY|ARR\s+RWY|RWY|RUNWAY)\s+([^\.\n]+)', raw_text, re.IGNORECASE)
    if rwy:
        item["runway"] = ", ".join([r.strip().rstrip('.') for r in rwy])

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
        item["weather"] = " ".join(list(dict.fromkeys(wx)))

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
    rmk = re.findall(r'\b(CAUTION[^\.\n]+|REPORT\s+[A-Z]\b)', raw_text, re.IGNORECASE)
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
                print(f"[{icao}] Brak aktywnych depesz ATIS.")
                continue

            parsed_reports = [
                parse_atis_simple(s["raw_text"], s["type"], s["info_letter"])
                for s in raw_sections
            ]

            # Sygnatura uwzględnia typ (ARR/DEP), literę, czas i treść
            current_sig = [
                (r["type"], r["info_letter"], r["time"], r["raw_text"])
                for r in parsed_reports
            ]

            # Sprawdzenie poprzedniego pliku
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
                        if current_sig == last_sig:
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
            print(f"[{icao}] Utworzono plik: {filepath} ({summary})")

        except Exception as err:
            print(f"[{icao}] Błąd podczas pobierania {url}: {err}")

if __name__ == "__main__":
    run()