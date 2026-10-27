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
    """Wyciąga pojedyncze bloki tekstowe ATIS ze strony, odrzucając menu i inne sekcje."""
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

    # Rezerwowe dopasowanie wyrażeniem regularnym w razie odmiennego układu strony
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

def parse_vatis_formatting(raw_text: str, icao: str) -> dict:
    """
    Parsuje parametry lotnicze zgodnie ze specyfikacją vATIS Formatting:
    https://vatis.app/docs/client/atis-configuration/formatting
    """
    data = {
        "observation": {
            "facility": icao,
            "atis_type": "COMBINED",
            "info_letter": None,
            "is_auto": False
        },
        "time": {
            "time_zulu": None,
            "time_observation": None
        },
        "runways_and_approaches": {
            "departure_runways": [],
            "arrival_runways": [],
            "all_runways": [],
            "approach": None
        },
        "transition_level": None,
        "wind": {
            "raw": None,
            "direction_deg": None,
            "speed_kt": None,
            "gust_kt": None,
            "variable_deg": None
        },
        "remark_wind": None,
        "visibility": {
            "raw": None,
            "is_cavok": False,
            "distance_meters": None
        },
        "runway_visual_range": {
            "raw": None,
            "details": []
        },
        "weather": [],
        "clouds": {
            "raw": None,
            "layers": []
        },
        "temperature_c": None,
        "dewpoint_c": None,
        "altimeter": {
            "qnh_hpa": None,
            "altimeter_inhg": None
        },
        "wind_shear": None,
        "runway_condition": None,
        "trend_forecast": None,
        "remarks": [],
        "raw_text": raw_text
    }

    # 1. Observation & Type
    if "ARR ATIS" in raw_text or f"{icao} ARR" in raw_text:
        data["observation"]["atis_type"] = "ARRIVAL"
    elif "DEP ATIS" in raw_text or f"{icao} DEP" in raw_text:
        data["observation"]["atis_type"] = "DEPARTURE"

    info_match = re.search(rf'\b{icao}\s+(?:ARR\s+|DEP\s+)?ATIS\s+([A-Z])\b', raw_text, re.IGNORECASE) or \
                 re.search(r'\b(?:ATIS|INFO(?:RMATION)?)\s+([A-Z])\b', raw_text, re.IGNORECASE)
    if info_match:
        data["observation"]["info_letter"] = info_match.group(1).upper()

    data["observation"]["is_auto"] = bool(re.search(r'\bAUTO\b', raw_text))

    # 2. Time
    zulu_match = re.search(r'\b(\d{4})Z\b', raw_text)
    if zulu_match:
        data["time"]["time_zulu"] = zulu_match.group(1) + "Z"

    obs_match = re.search(r'\bTIME\s+(\d{4})\b', raw_text)
    if obs_match:
        data["time"]["time_observation"] = obs_match.group(1)

    # 3. Runways & Approach
    dep_rwy = re.findall(r'\bDEP(?:ARTURE)?\s+RWY\s+(\d{2}[LCR]?)', raw_text, re.IGNORECASE)
    arr_rwy = re.findall(r'\bARR(?:IVAL)?\s+RWY\s+(\d{2}[LCR]?)', raw_text, re.IGNORECASE)
    all_rwy = re.findall(r'\b(?:RWY|RUNWAY)\s+(\d{2}[LCR]?)', raw_text, re.IGNORECASE)
    
    data["runways_and_approaches"]["departure_runways"] = list(dict.fromkeys(dep_rwy))
    data["runways_and_approaches"]["arrival_runways"] = list(dict.fromkeys(arr_rwy))
    data["runways_and_approaches"]["all_runways"] = list(dict.fromkeys(all_rwy))

    apch_match = re.search(r'\b(EXP[^\.\n]+APCH[^\.\n]*)', raw_text, re.IGNORECASE)
    if apch_match:
        data["runways_and_approaches"]["approach"] = apch_match.group(1).strip()

    # 4. Transition Level
    tl_match = re.search(r'\bTRANSITION\s+LEVEL\s+(\d+)\b', raw_text, re.IGNORECASE)
    if tl_match:
        data["transition_level"] = int(tl_match.group(1))

    # 5. Wind
    wind_match = re.search(r'\bWIND\s+([^\.\n]+)', raw_text, re.IGNORECASE)
    if wind_match:
        w_raw = wind_match.group(1).strip()
        data["wind"]["raw"] = w_raw

        deg_spd = re.search(r'(\d{3}|VRB)\s*(?:DEG|,)?\s*(\d+)\s*(?:G(\d+))?\s*KT', w_raw, re.IGNORECASE)
        if deg_spd:
            data["wind"]["direction_deg"] = "VRB" if deg_spd.group(1).upper() == "VRB" else int(deg_spd.group(1))
            data["wind"]["speed_kt"] = int(deg_spd.group(2))
            if deg_spd.group(3):
                data["wind"]["gust_kt"] = int(deg_spd.group(3))

        var_match = re.search(r'(\d{3})\s*V\s*(\d{3})', w_raw)
        if var_match:
            data["wind"]["variable_deg"] = {
                "from": int(var_match.group(1)),
                "to": int(var_match.group(2))
            }

    # 6. Remark Wind
    rmk_wind = re.search(r'\b(?:RMK\s+WIND|REMARK\s+WIND|WIND\s+RWY\s+\d+)\s+([^\.\n]+)', raw_text, re.IGNORECASE)
    if rmk_wind:
        data["remark_wind"] = rmk_wind.group(0).strip()

    # 7. Visibility
    if "CAVOK" in raw_text:
        data["visibility"]["raw"] = "CAVOK"
        data["visibility"]["is_cavok"] = True
        data["clouds"]["raw"] = "CAVOK"
    else:
        vis_match = re.search(r'\bVIS(?:IBILITY)?\s+([^\.\n]+)', raw_text, re.IGNORECASE)
        if vis_match:
            v_str = vis_match.group(1).strip()
            data["visibility"]["raw"] = v_str
            dist = re.search(r'(\d+)\s*(?:M|KM)?', v_str)
            if dist:
                val = int(dist.group(1))
                data["visibility"]["distance_meters"] = val * 1000 if "KM" in v_str else val

    # 8. Runway Visual Range (RVR)
    rvr_matches = re.findall(r'\b(RVR\s+[^\.\n]+|R\d{2}[LCR]?/[^\.\n]+)', raw_text, re.IGNORECASE)
    if rvr_matches:
        data["runway_visual_range"]["raw"] = "; ".join([r.strip() for r in rvr_matches])
        for r_item in rvr_matches:
            parsed_rvr = re.search(r'RWY\s*(\d{2}[LCR]?)\s*(\d+M?)', r_item, re.IGNORECASE)
            if parsed_rvr:
                data["runway_visual_range"]["details"].append({
                    "runway": parsed_rvr.group(1),
                    "rvr": parsed_rvr.group(2)
                })

    # 9. Present Weather
    wx_codes = re.findall(r'\b([+-]?(?:VC)?(?:DZ|RA|SN|SG|IC|PL|GR|GS|UP|BR|FG|FU|VA|DU|SA|HZ|PY|PO|SQ|FC|SS|DS|TSRA|TS|SHRA|SHSN))\b', raw_text)
    if wx_codes:
        data["weather"] = list(dict.fromkeys(wx_codes))

    # 10. Clouds
    if not data["visibility"]["is_cavok"]:
        cloud_layers = re.findall(r'\b(FEW|SCT|BKN|OVC|VV)(\d{3})(?:CB|TCU)?\b', raw_text)
        if cloud_layers:
            data["clouds"]["raw"] = " ".join([f"{c[0]}{c[1]}" for c in cloud_layers])
            for cl in cloud_layers:
                data["clouds"]["layers"].append({
                    "coverage": cl[0],
                    "altitude_ft": int(cl[1]) * 100
                })
        elif re.search(r'\b(NSC|SKC|NCD)\b', raw_text):
            data["clouds"]["raw"] = re.search(r'\b(NSC|SKC|NCD)\b', raw_text).group(1)

    # 11. Temperature & Dewpoint
    t_match = re.search(r'\bT\s*(-?\d+|M\d+)\b', raw_text)
    if t_match:
        val = t_match.group(1).replace("M", "-")
        data["temperature_c"] = int(val)

    dp_match = re.search(r'\bDP\s*(-?\d+|M\d+)\b', raw_text)
    if dp_match:
        val = dp_match.group(1).replace("M", "-")
        data["dewpoint_c"] = int(val)

    # 12. Altimeter (QNH)
    qnh_match = re.search(r'\bQ(?:NH)?\s*(\d{4})\b', raw_text, re.IGNORECASE)
    if qnh_match:
        qnh_val = int(qnh_match.group(1))
        data["altimeter"]["qnh_hpa"] = qnh_val
        data["altimeter"]["altimeter_inhg"] = round(qnh_val * 0.02953, 2)

    inhg_match = re.search(r'\bA(\d{4})\b', raw_text)
    if inhg_match:
        data["altimeter"]["altimeter_inhg"] = float(inhg_match.group(1)[:2] + "." + inhg_match.group(1)[2:])

    # 13. Wind Shear
    ws_match = re.search(r'\b(WS\s+[^\.\n]+|WIND\s+SHEAR[^\.\n]*)', raw_text, re.IGNORECASE)
    if ws_match:
        data["wind_shear"] = ws_match.group(1).strip()

    # 14. Runway Condition
    rc_match = re.search(r'\b(RUNWAY\s+CONDITION[^\.\n]*|RWY\s+CONDITION[^\.\n]*|SNOWTAM[^\.\n]*)', raw_text, re.IGNORECASE)
    if rc_match:
        data["runway_condition"] = rc_match.group(1).strip()

    # 15. Trend Forecast
    trend_match = re.search(r'\b(NOSIG|TEMPO[^\.\n]*|BECMG[^\.\n]*)', raw_text)
    if trend_match:
        data["trend_forecast"] = trend_match.group(1).strip()

    # 16. Remarks / Cautions
    cautions = re.findall(r'\b(CAUTION[^\.\n]+|REPORT\s+[A-Z]\b|ACK(?:NOWLEDGE)?\s+[^\.\n]+)', raw_text, re.IGNORECASE)
    if cautions:
        data["remarks"] = [c.strip() for c in cautions]

    return data

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
                print(f"[{icao}] Brak aktywnych depesz ATIS.")
                continue

            parsed_reports = [parse_vatis_formatting(b, icao) for b in blocks]
            current_raw = [r["raw_text"] for r in parsed_reports]

            # Sprawdzenie ostatniego zapisanego pliku JSON dla danego portu
            existing_files = sorted([f for f in os.listdir(airport_dir) if f.endswith(".json")])
            if existing_files:
                last_file_path = os.path.join(airport_dir, existing_files[-1])
                try:
                    with open(last_file_path, "r", encoding="utf-8") as f:
                        last_data = json.load(f)
                        last_raw = [r.get("raw_text") for r in last_data.get("reports", [])]
                        if current_raw == last_raw:
                            print(f"[{icao}] ATIS bez zmian od ostatniego pliku ({existing_files[-1]}).")
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

            info_letters = ", ".join([
                f"{r['observation']['atis_type']}: {r['observation']['info_letter']}" 
                for r in parsed_reports if r['observation']['info_letter']
            ])
            print(f"[{icao}] Zapisano nowy stan: {filepath} ({info_letters})")

        except Exception as err:
            print(f"[{icao}] Błąd: {err}")

if __name__ == "__main__":
    run()