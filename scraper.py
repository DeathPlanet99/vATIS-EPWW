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

def extract_atis_blocks(html: str) -> list[dict]:
    """Wyciąga bloki tekstowe ATIS (np. osobno Arrival i Departure) ze struktury strony."""
    soup = BeautifulSoup(html, "html.parser")
    blocks = []

    # Szukanie kart z nagłówkami ARRIVAL / DEPARTURE lub ogólnego kontenera
    cards = soup.find_all(lambda tag: tag.name in ["div", "section"] and any(
        kw in tag.get_text().upper() for kw in ["ARRIVAL ATIS", "DEPARTURE ATIS", "ATIS"]
    ))

    # Wyciąganie tekstu z podziałem na depesze za pomocą wyrażenia regularnego
    full_text = soup.get_text(separator="\n")
    
    # Wzorzec dopasowujący początek depeszy: np. "EPWA ARR ATIS I", "EPWA DEP ATIS P", "EPKK ATIS B"
    pattern = re.compile(
        r'([A-Z]{4}\s+(?:ARR\s+|DEP\s+)?ATIS\s+[A-Z][\s\S]+?(?:END OF [A-Z]{4}[^\n]*|REPORT [A-Z]|NOSIG|CAVOK[^\n]*|\n(?=[A-Z]{4}\s+(?:ARR|DEP)?\s*ATIS)|\Z))',
        re.IGNORECASE
    )

    matches = pattern.findall(full_text)
    if matches:
        for match in matches:
            cleaned = "\n".join([line.strip() for line in match.strip().splitlines() if line.strip()])
            if len(cleaned) > 20:  # Odrzucenie fałszywych dopasowań
                blocks.append(cleaned)
    else:
        # Fallback gdy strona ma niestandardowy układ
        for tag in soup.find_all(["pre", "p", "div"]):
            txt = tag.get_text(strip=True)
            if "ATIS" in txt and ("QNH" in txt or "INFO" in txt):
                blocks.append(tag.get_text(separator="\n", strip=True))

    # Usunięcie ewentualnych duplikatów bloków
    unique_blocks = []
    for b in blocks:
        if not any(b in existing or existing in b for existing in unique_blocks):
            unique_blocks.append(b)

    return unique_blocks

def parse_atis_text(raw_text: str) -> dict:
    """Parsuje poszczególne pola depeszy zgodnie ze standardem D-ATIS."""
    parsed = {
        "type": "COMBINED",
        "info_letter": None,
        "time_zulu": None,
        "time_obs": None,
        "runways": [],
        "approach": None,
        "transition_level": None,
        "wind": {
            "raw": None,
            "direction_deg": None,
            "speed_kt": None,
            "gust_kt": None
        },
        "visibility": None,
        "clouds": None,
        "temperature_c": None,
        "dewpoint_c": None,
        "altimeter_qnh_hpa": None,
        "trend": None,
        "remarks": None,
        "raw_text": raw_text
    }

    # 1. Typ depeszy (Arrival / Departure / Combined)
    if "ARR ATIS" in raw_text or "ARRIVAL" in raw_text.upper():
        parsed["type"] = "ARRIVAL"
    elif "DEP ATIS" in raw_text or "DEPARTURE" in raw_text.upper():
        parsed["type"] = "DEPARTURE"

    # 2. Litera informacji (np. "ATIS I", "ATIS P", "INFO B")
    letter_match = re.search(r'\bATIS\s+(?:ARR\s+|DEP\s+)?([A-Z])\b', raw_text) or re.search(r'\bINFO(?:RMATION)?\s+([A-Z])\b', raw_text)
    if letter_match:
        parsed["info_letter"] = letter_match.group(1).upper()

    # 3. Czas Zulu i czas obserwacji
    zulu_match = re.search(r'\b(\d{4})Z\b', raw_text)
    if zulu_match:
        parsed["time_zulu"] = zulu_match.group(1) + "Z"

    time_match = re.search(r'\bTIME\s+(\d{4})\b', raw_text)
    if time_match:
        parsed["time_obs"] = time_match.group(1)

    # 4. Pasy startowe i podejścia
    rwy_matches = re.findall(r'\b(?:RWY|RUNWAY)\s+(\d{2}[LCR]?)\b', raw_text)
    if rwy_matches:
        parsed["runways"] = list(dict.fromkeys(rwy_matches))

    apch_match = re.search(r'\b(EXP[^\.\n]+APCH[^\.\n]*)', raw_text)
    if apch_match:
        parsed["approach"] = apch_match.group(1).strip()

    # 5. Poziom przejściowy (Transition Level)
    trl_match = re.search(r'\bTRANSITION\s+LEVEL\s+(\d+)\b', raw_text, re.IGNORECASE)
    if trl_match:
        parsed["transition_level"] = int(trl_match.group(1))

    # 6. Wiatr
    wind_match = re.search(r'\bWIND\s+([^\.\n]+)', raw_text, re.IGNORECASE)
    if wind_match:
        wind_str = wind_match.group(1).strip()
        parsed["wind"]["raw"] = wind_str
        deg_spd = re.search(r'(\d{3})\s*(?:DEG|,)?\s*(\d+)\s*(?:G(\d+))?\s*KT', wind_str)
        if deg_spd:
            parsed["wind"]["direction_deg"] = int(deg_spd.group(1))
            parsed["wind"]["speed_kt"] = int(deg_spd.group(2))
            if deg_spd.group(3):
                parsed["wind"]["gust_kt"] = int(deg_spd.group(3))

    # 7. Widzialność i chmury
    if "CAVOK" in raw_text:
        parsed["visibility"] = "CAVOK"
        parsed["clouds"] = "CAVOK"
    else:
        vis_match = re.search(r'\bVIS(?:IBILITY)?\s+([^\.\n]+)', raw_text, re.IGNORECASE)
        if vis_match:
            parsed["visibility"] = vis_match.group(1).strip()
        cloud_match = re.search(r'\b(FEW|SCT|BKN|OVC|NSC|SKC)\d{3}[^\.\n]*', raw_text)
        if cloud_match:
            parsed["clouds"] = cloud_match.group(0).strip()

    # 8. Temperatura i punkt rosy (np. T 12, DP 4)
    temp_match = re.search(r'\bT\s*(-?\d+)\b', raw_text)
    if temp_match:
        parsed["temperature_c"] = int(temp_match.group(1))

    dp_match = re.search(r'\bDP\s*(-?\d+)\b', raw_text)
    if dp_match:
        parsed["dewpoint_c"] = int(dp_match.group(1))

    # 9. Ciśnienie QNH
    qnh_match = re.search(r'\bQNH\s*(\d{4})\b', raw_text, re.IGNORECASE)
    if qnh_match:
        parsed["altimeter_qnh_hpa"] = int(qnh_match.group(1))

    # 10. Prognoza trendu (NOSIG / BECMG / TEMPO)
    trend_match = re.search(r'\b(NOSIG|TEMPO[^\.\n]*|BECMG[^\.\n]*)', raw_text)
    if trend_match:
        parsed["trend"] = trend_match.group(1).strip()

    # 11. Uwagi / Remarks (np. CAUTION BIRD ACT)
    caution_match = re.search(r'\b(CAUTION[^\.\n]+)', raw_text, re.IGNORECASE)
    if caution_match:
        parsed["remarks"] = caution_match.group(1).strip()

    return parsed

def run():
    os.makedirs(DATA_DIR, exist_ok=True)

    for icao in AIRPORTS:
        url = BASE_URL.format(icao=icao)
        try:
            response = requests.get(url, headers=HEADERS, timeout=20)
            response.raise_for_status()

            blocks = extract_atis_blocks(response.text)
            if not blocks:
                print(f"[{icao}] Brak aktywnych depesz na stronie.")
                continue

            parsed_reports = [parse_atis_text(b) for b in blocks]

            json_path = os.path.join(DATA_DIR, f"{icao}.json")
            history = []

            if os.path.exists(json_path):
                try:
                    with open(json_path, "r", encoding="utf-8") as f:
                        history = json.load(f)
                except json.JSONDecodeError:
                    history = []

            # Sprawdzenie, czy treść różni się od ostatniego wpisu
            current_raw_payload = [r["raw_text"] for r in parsed_reports]
            if history:
                last_raw_payload = [r["raw_text"] for r in history[-1].get("reports", [])]
                if current_raw_payload == last_raw_payload:
                    print(f"[{icao}] Brak zmian w depeszach od ostatniego pobrania.")
                    continue

            now_utc = datetime.now(timezone.utc)
            new_entry = {
                "airport": icao,
                "timestamp_utc": now_utc.strftime("%Y-%m-%d %H:%M:%S UTC"),
                "iso_timestamp": now_utc.isoformat(),
                "reports_count": len(parsed_reports),
                "reports": parsed_reports
            }

            history.append(new_entry)

            with open(json_path, "w", encoding="utf-8") as f:
                json.dump(history, f, ensure_ascii=False, indent=2)

            letters = ", ".join([f"{r['type']}: {r['info_letter']}" for r in parsed_reports if r['info_letter']])
            print(f"[{icao}] Zapisano pomyślnie ({letters}).")

        except Exception as err:
            print(f"[{icao}] Błąd podczas pobierania {url}: {err}")

if __name__ == "__main__":
    run()