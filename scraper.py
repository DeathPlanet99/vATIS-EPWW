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

def extract_atis_sections(html: str, icao: str) -> list[dict]:
    """Przetwarza tekst linijka po linijce, omijając fałszywe powtórzenia nagłówków na liniach z czasem."""
    soup = BeautifulSoup(html, "html.parser")
    # Całkowicie usuwamy menu i stopki strony, by słowa kluczowe z UI nie zaburzały depeszy
    for tag in soup(["script", "style", "noscript", "svg", "nav", "header", "footer"]):
        tag.decompose()

    full_text = soup.get_text(separator="\n")
    lines = [line.strip() for line in full_text.splitlines() if line.strip()]
    
    sections = []
    current_section = []
    recording = False
    atis_type = "COMBINED"
    info_letter = ""
    
    # Wzorce łapiące główny nagłówek ATIS
    start_regex = re.compile(rf'\b{icao}\s+(?:(ARR|DEP)\s+)?ATIS(?:\s+INFO)?\s+([A-Z])\b', re.IGNORECASE)
    fallback_start_regex = re.compile(rf'\b{icao}\s+(ARR|DEP)\s+([A-Z])\b', re.IGNORECASE)
    
    for line in lines:
        # Czas Zulu (np. 1226Z) na początku linii oznacza, że to powtórzenie nazwy ATIS wewnątrz depeszy
        is_time_line = bool(re.search(r'^\d{4}Z', line, re.IGNORECASE))
        
        match = None
        if not is_time_line:
            match = start_regex.search(line) or fallback_start_regex.search(line)
            
        if match:
            if recording and current_section:
                sections.append({
                    "type": atis_type,
                    "info_letter": info_letter,
                    "raw_text": "\n".join(current_section).strip()
                })
            recording = True
            # Ucinamy cokolwiek, co mogło być w tej samej linii przed nagłówkiem
            current_section = [line[match.start():]]
            
            t = match.group(1)
            info_letter = match.group(2).upper()
            if t:
                atis_type = "ARRIVAL" if t.upper() == "ARR" else "DEPARTURE"
            else:
                atis_type = "COMBINED"
            continue
            
        if recording:
            # Słowa-klucze, które pojawiają się na stronie atis.guru PO właściwej depeszy
            if not is_time_line and re.search(r'\b(?:METAR\b|TAF\b|Arrival ATIS|Departure ATIS|Formatting\b|History\b)', line, re.IGNORECASE):
                sections.append({
                    "type": atis_type,
                    "info_letter": info_letter,
                    "raw_text": "\n".join(current_section).strip()
                })
                recording = False
                current_section = []
                continue
                
            current_section.append(line)
            
            # Znaczniki jednoznacznego zakończenia transmisji wewnątrz samej depeszy
            if re.search(r'^(?:END OF\b|REPORT\s+[A-Z]\b|ACK(?:NOWLEDGE)?\b)', line, re.IGNORECASE):
                sections.append({
                    "type": atis_type,
                    "info_letter": info_letter,
                    "raw_text": "\n".join(current_section).strip()
                })
                recording = False
                current_section = []
                continue

    if recording and current_section:
        sections.append({
            "type": atis_type,
            "info_letter": info_letter,
            "raw_text": "\n".join(current_section).strip()
        })

    # Czyszczenie śmieci i walidacja typów
    valid_sections = []
    for s in sections:
        txt = s["raw_text"]
        if len(txt) < 20:
            continue
        # Dodatkowe upewnienie się co do typu na podstawie samego tekstu
        if s["type"] == "COMBINED":
            if "ARR ATIS" in txt or f"{icao} ARR" in txt:
                s["type"] = "ARRIVAL"
            elif "DEP ATIS" in txt or f"{icao} DEP" in txt:
                s["type"] = "DEPARTURE"
        valid_sections.append(s)
        
    return valid_sections

def parse_atis_simple(raw_text: str, atis_type: str, info_letter: str) -> dict:
    """Konwertuje tekst na wybrane proste kategorie."""
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

    tr = re.search(