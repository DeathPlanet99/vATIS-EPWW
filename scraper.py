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

def extract_atis_blocks(html: str) -> list[str]:
    """Wyciąga pojedyncze bloki tekstowe ATIS ze strony."""
    soup = BeautifulSoup(html, "html.parser")
    full_text = soup.get_text(separator="\n")

    pattern = re.compile(
        r'([A-Z]{4}\s+(?:ARR\s+|DEP\s+)?ATIS(?:\s+INFO)?\s+[A-Z]\b[\s\S]+?)(?=(?:[A-Z]{4}\s+(?:ARR\s+|DEP\s+)?ATIS(?:\s+INFO)?\s+[A-Z]\b|METAR\b|TAF\b|\Z))',
        re.IGNORECASE
    )

    matches = pattern.findall(full_text)
    blocks = []
    if matches:
        for m in matches:
            cleaned = "\n".join([line.strip() for line in m.strip().splitlines() if line.strip()])
            if len(cleaned) > 25:
                blocks.append(cleaned)
    else:
        for tag in soup.find_all(["pre", "code", "p", "div"]):
            txt = tag.get_text(strip=True)
            if "ATIS" in txt and ("QNH" in txt or "INFO" in txt):
                cleaned = tag.get_text(separator="\n", strip=True)
                if len(cleaned) > 25 and cleaned not in blocks:
                    blocks.append(cleaned)

    unique_blocks = []
    for b in blocks:
        if not any(b in existing for existing in unique_blocks):
            unique_blocks.append(b)

    return unique_blocks

def parse_atis_text(raw_text: str) -> dict:
    """Parsuje poszczególne parametry z surowego tekstu ATIS."""
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

    if "ARR ATIS" in raw_text or "ARRIVAL" in raw_text.upper():
        parsed["type"] = "ARRIVAL"
    elif "DEP ATIS" in raw_text or "DEPARTURE" in raw_text.upper():
        parsed["type"] = "DEPARTURE"

    letter_match = re.search(r'\bATIS\s+(?:ARR\s+|DEP\s+)?([A-Z])\b', raw_text) or re.search(r'\bINFO(?:RMATION)?\s+([A-Z])\b', raw_text)
    if letter_match:
        parsed["info_letter"] = letter_match.group(1).upper()

    zulu_match = re.search(r'\b(\d{4})Z\b', raw_text)
    if zulu_match:
        parsed["time_zulu"] = zulu_match.group(1) + "Z"

    time_match = re.search(r'\bTIME\s+(\d{4})\b', raw_text)
    if time_match:
        parsed["time_obs"] = time_match.group(1)

    rwy_matches = re.findall(r'\b(?:RWY|RUNWAY)\s+(\d{2}[LCR]?)\b', raw_text)
    if rwy_matches:
        parsed["runways"] = list(dict.fromkeys(rwy_matches))

    apch_match = re.search(r'\b(EXP[^\.\n]+APCH[^\.\n]*)', raw_text)
    if apch_match:
        parsed["approach"] = apch_match.group(1).strip()

    trl_match = re.search(r'\bTRANSITION\s+LEVEL\s+(\d+)\b', raw_text, re.IGNORECASE)
    if trl_match:
        parsed["transition_level"] = int(trl_match.group(1))

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

    temp_match = re.search(r'\bT\s*(-?\d+)\b', raw_text)
    if temp_match:
        parsed["temperature_c"] = int(temp_match.group(1))

    dp_match = re.search(r'\bDP\s*(-?\d+)\b', raw_text)
    if dp_match:
        parsed["dewpoint_c"] = int(dp_match.group(1))

    qnh_match = re.search(r'\bQNH\s*(\d{4})\b', raw_text, re.IGNORECASE)
    if qnh_match:
        parsed["altimeter_qnh_hpa"] = int(qnh_match.group(1))

    trend_match = re.search(r'\b(NOSIG|TEMPO[^\.\n]*|BECMG[^\.\n]*)', raw_text)
    if trend_match:
        parsed["trend"] = trend_match.group(1).strip()

    caution_match = re.search(r'\b(CAUTION[^\.\n]+)', raw_text, re.IGNORECASE)
    if caution_match:
        parsed["remarks"] = caution_match.group(1).strip()

    return parsed

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

            blocks = extract_atis_blocks(response.text)
            if not blocks:
                print(f"[{icao}] Brak aktywnych depesz.")
                continue

            parsed_reports = [parse_atis_text(b) for b in blocks]
            current_raw = [r["raw_text"] for r in parsed_reports]

            # Sprawdzenie ostatniego zapisanego pliku JSON w katalogu tego lotniska
            existing_files = sorted([f for f in os.listdir(airport_dir) if f.endswith(".json")])
            if existing_files:
                last_file_path = os.path.join(airport_dir, existing_files[-1])
                try:
                    with open(last_file_path, "r", encoding="utf-8") as f:
                        last_data = json.load(f)
                        last_raw = [r["raw_text"] for r in last_data.get("reports", [])]
                        if current_raw == last_raw:
                            print(f"[{icao}] ATIS nie zmienił się od pliku {existing_files[-1]}. Pomijam.")
                            continue
                except Exception:
                    pass

            filename = f"{icao}_{date_str}_{time_str}z.json"
            filepath = os.path.join(airport_dir, filename)

            payload = {
                "airport": icao,
                "fetch_time_utc": now_utc.strftime("%Y-%m-%d %H:%M:%S UTC"),
                "iso_timestamp": now_utc.isoformat(),
                "reports_count": len(parsed_reports),
                "reports": parsed_reports
            }

            with open(filepath, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)

            letters = ", ".join([f"{r['type']}: {r['info_letter']}" for r in parsed_reports if r['info_letter']])
            print(f"[{icao}] Utworzono nowy plik: {filepath} ({letters})")

        except Exception as err:
            print(f"[{icao}] Błąd: {err}")

if __name__ == "__main__":
    run()