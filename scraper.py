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
    """Wyciąga pojedyncze bloki depesz ATIS ze strony."""
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()

    full_text = soup.get_text(separator="\n")
    lines = [line.strip() for line in full_text.splitlines() if line.strip()]

    blocks = []
    current_block = []
    recording = False

    start_pattern = re.compile(rf'^{icao}\s+(?:ARR\s+|DEP\s+)?ATIS\s+[A-Z]\b', re.IGNORECASE)
    stop_pattern = re.compile(r'^(?:METAR\b|TAF\b|Arrival ATIS\b|Departure ATIS\b|Formatting\b|History\b)', re.IGNORECASE)

    for line in lines:
        if start_pattern.search(line):
            if current_block:
                blocks.append("\n".join(current_block))
                current_block = []
            recording = True
            current_block.append(line)
            continue

        if recording:
            if stop_pattern.search(line):
                blocks.append("\n".join(current_block))
                current_block = []
                recording = False
            else:
                current_block.append(line)
                if re.search(r'^(?:END OF\b|REPORT\s+[A-Z]\b|ACK(?:NOWLEDGE)?\s+INFO)', line, re.IGNORECASE):
                    blocks.append("\n".join(current_block))
                    current_block = []
                    recording = False

    if recording and current_block:
        blocks.append("\n".join(current_block))

    # Fallback regex
    if not blocks:
        regex_fallback = re.compile(
            rf'({icao}\s+(?:ARR\s+|DEP\s+)?ATIS\s+[A-Z][\s\S]+?)(?=(?:{icao}\s+(?:ARR\s+|DEP\s+)?ATIS\s+[A-Z]|METAR\b|TAF\b|\Z))',
            re.IGNORECASE
        )
        for m in regex_fallback.findall(full_text):
            cleaned = "\n".join([l.strip() for l in m.splitlines() if l.strip()])
            if len(cleaned) > 25:
                blocks.append(cleaned)

    return blocks

def parse_atis_simple(raw_text: str, icao: str) -> dict:
    """Parsuje czysty tekst z depeszy do płaskich kategorii tekstowych."""
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

    # 1. Typ depeszy (Arrival / Departure / Combined)
    if "ARR ATIS" in raw_text or f"{icao} ARR" in raw_text:
        item["type"] = "ARRIVAL"
    elif "DEP ATIS" in raw_text or f"{icao} DEP" in raw_text:
        item["type"] = "DEPARTURE"

    # 2. Litera informacji (np. "I", "P")
    match_letter = re.search(rf'\b{icao}\s+(?:ARR\s+|DEP\s+)?ATIS\s+([A-Z])\b', raw_text, re.IGNORECASE) or \
                   re.search(r'\b(?:ATIS|INFO(?:RMATION)?)\s+([A-Z])\b', raw_text, re.IGNORECASE)
    if match_letter:
        item["info_letter"] = match_letter.group(1).upper()

    # 3. Czas (np. "1431Z" lub "TIME 1430")
    match_zulu = re.search(r'\b(\d{4}Z)\b', raw_text)
    match_obs = re.search(r'\bTIME\s+(\d{4})\b', raw_text)
    if match_zulu:
        item["time"] = match_zulu.group(1)
    elif match_obs:
        item["time"] = match_obs.group(1)

    # 4. Pasy w użyciu
    rwy_matches = re.findall(r'\b(?:DEP\s+RWY|ARR\s+RWY|RWY|RUNWAY)\s+([^\.\n]+)', raw_text, re.IGNORECASE)
    if rwy_matches:
        item["runway"] = ", ".join([r.strip() for r in rwy_matches])

    # 5. Podejście (Approach)
    match_apch = re.search(r'\b(EXP[^\.\n]+APCH[^\.\n]*|EXP[^\.\n]+ILS[^\.\n]*)', raw_text, re.IGNORECASE)
    if match_apch:
        item["approach"] = match_apch.group(1).strip()

    # 6. Poziom przejściowy (Transition Level)
    match_tl = re.search(r'\bTRANSITION\s+LEVEL\s+(\d+)\b', raw_text, re.IGNORECASE)
    if match_tl:
        item["transition_level"] = match_tl.group(1)

    # 7. Wiatr
    match_wind = re.search(r'\bWIND\s+([^\.\n]+)', raw_text, re.IGNORECASE)
    if match_wind:
        item["wind"] = match_wind.group(1).strip().rstrip(".")

    # 8. Widzialność i chmury
    if "CAVOK" in raw_text:
        item["visibility"] = "CAVOK"
        item["clouds"] = "CAVOK"
    else:
        match_vis = re.search(r'\bVIS(?:IBILITY)?\s+([^\.\n]+)', raw_text, re.IGNORECASE)
        if match_vis:
            item["visibility"] = match_vis.group(1).strip().rstrip(".")

        cloud_matches = re.findall(r'\b((?:FEW|SCT|BKN|OVC|VV)\d{3}[A-Z]*|NSC|SKC|NCD)\b', raw_text)
        if cloud_matches:
            item["clouds"] = " ".join(cloud_matches)

    # 9. Zjawiska pogody
    wx = re.findall(r'\b([+-]?(?:VC)?(?:DZ|RA|SN|SG|IC|PL|GR|GS|UP|BR|FG|FU|VA|DU|SA|HZ|PY|PO|SQ|FC|SS|DS|TSRA|TS|SHRA|SHSN))\b', raw_text)
    if wx:
        item["weather"] = " ".join(list(dict.fromkeys(wx)))

    # 10. Temperatura i punkt rosy (np. T 12, DP 4)
    match_temp = re.search(r'\bT\s*(-?\d+|M\d+)\b', raw_text)
    if match_temp:
        item["temperature"] = match_temp.group(1).replace("M", "-")

    match_dp = re.search(r'\bDP\s*(-?\d+|M\d+)\b', raw_text)
    if match_dp:
        item["dewpoint"] = match_dp.group(1).replace("M", "-")

    # 11. Ciśnienie (QNH)
    match_qnh = re.search(r'\b(QNH\s*\d{4}|Q\d{4}|A\d{4})\b', raw_text, re.IGNORECASE)
    if match_qnh:
        item["altimeter"] = match_qnh.group(1).upper()

    # 12. Trend (NOSIG, TEMPO, BECMG)
    match_trend = re.search(r'\b(NOSIG|TEMPO[^\.\n]*|BECMG[^\.\n]*)', raw_text)
    if match_trend:
        item["trend"] = match_trend.group(1).strip().rstrip(".")

    # 13. Uwagi (Remarks / Caution)
    cautions = re.findall(r'\b(CAUTION[^\.\n]+|REPORT\s+[A-Z]\b)', raw_text, re.IGNORECASE)
    if cautions:
        item["remarks"] = "; ".join([c.strip().rstrip(".") for c in cautions])

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
                print(f"[{icao}] Brak aktywnych depesz.")
                continue

            parsed_reports = [parse_atis_simple(b, icao) for b in blocks]
            current_raw = [r["raw_text"] for r in parsed_reports]

            # Sprawdzenie ostatniego zapisanego pliku JSON dla danego lotniska
            existing_files = sorted([f for f in os.listdir(airport_dir) if f.endswith(".json")])
            if existing_files:
                last_file_path = os.path.join(airport_dir, existing_files[-1])
                try:
                    with open(last_file_path, "r", encoding="utf-8") as f:
                        last_data = json.load(f)
                        last_raw = [r.get("raw_text") for r in last_data.get("reports", [])]
                        if current_raw == last_raw:
                            print(f"[{icao}] ATIS bez zmian od pliku {existing_files[-1]}. Pomijam.")
                            continue
                except Exception:
                    pass

            filename = f"{icao}_{date_str}_{time_str}z.json"
            filepath = os.path.join(airport_dir, filename)

            payload = {
                "airport": icao,
                "fetch_time_utc": now_utc.strftime("%Y-%m-%d %H:%M:%S UTC"),
                "reports": parsed_reports
            }

            with open(filepath, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)

            info_letters = ", ".join([f"{r['type']}: {r['info_letter']}" for r in parsed_reports if r['info_letter']])
            print(f"[{icao}] Utworzono plik: {filepath} ({info_letters})")

        except Exception as err:
            print(f"[{icao}] Błąd: {err}")

if __name__ == "__main__":
    run()