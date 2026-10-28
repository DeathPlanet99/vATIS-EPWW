import os
import re
import json
import sys
from datetime import datetime, timezone

import requests
from bs4 import BeautifulSoup

AIRPORTS = ["EPKK", "EPWA", "EPPO", "EPGD"]
BASE_URL = "https://atis.guru/atis/{icao}"
DATA_DIR = "data"
DEBUG_DIR = "debug"
MODE = os.environ.get("SCRAPER_MODE", "browser").lower()

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
HEADERS = {"User-Agent": UA, "Accept-Language": "en-GB,en;q=0.9"}


# --------------------------------------------------------------------------
# POBIERANIE STRONY
# --------------------------------------------------------------------------

def flatten(text: str) -> str:
    """Zamienia dowolne białe znaki (w tym nowe linie) na pojedynczą spację."""
    return re.sub(r"\s+", " ", text).strip()


def html_to_text(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript", "svg"]):
        tag.decompose()
    return flatten(soup.get_text(separator=" "))


def fetch_static(icao: str) -> tuple[str, str]:
    """Zwraca (html, tekst) – bez JavaScriptu, więc dla Blazora zwykle pusto."""
    r = requests.get(BASE_URL.format(icao=icao), headers=HEADERS, timeout=20)
    r.raise_for_status()
    return r.text, html_to_text(r.text)


class BrowserFetcher:
    """Jedna instancja Chromium dla wszystkich lotnisk."""

    def __init__(self):
        from playwright.sync_api import sync_playwright

        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch()
        self._ctx = self._browser.new_context(user_agent=UA, locale="en-GB")

    def fetch(self, icao: str, wait_ms: int = 30000) -> tuple[str, str]:
        page = self._ctx.new_page()
        try:
            # NIE używamy "networkidle" – Blazor trzyma stałe połączenie
            # (SignalR/WebSocket) i sieć nigdy nie jest "idle".
            page.goto(BASE_URL.format(icao=icao), wait_until="domcontentloaded", timeout=45000)
            try:
                # czekamy aż pojawi się koniec depeszy ("END OF EPWA ARR J")
                page.wait_for_function(
                    "() => /END\\s+OF\\s+[A-Z]{4}/i.test(document.body.innerText)",
                    timeout=wait_ms,
                )
            except Exception:
                # brak depeszy w limicie czasu – i tak oddajemy co jest (do debugu)
                pass
            html = page.content()
            text = flatten(page.inner_text("body"))
            return html, text
        finally:
            page.close()

    def close(self):
        try:
            self._browser.close()
        finally:
            self._pw.stop()


# --------------------------------------------------------------------------
# WYODRĘBNIANIE DEPESZ Z TEKSTU STRONY
# --------------------------------------------------------------------------

def extract_blocks(text: str, icao: str) -> list[dict]:
    """
    Szuka na spłaszczonym tekście całych depesz w formacie:
        EPWA ARR ATIS J 1256Z ...... END OF EPWA ARR J
    Działa niezależnie od tego, jak strona dzieli tekst na elementy/linie
    (stara wersja czytała linia po linii i rozbijała depeszę na kawałki).
    Zwraca listę {"text": ..., "complete": bool}.
    """
    start = rf"\b{icao}\s+(?:(?:ARR|DEP)\s+)?ATIS(?:\s+(?:INFO|INFORMATION))?\s+([A-Z])\s+\d{{4}}\s*Z\b"
    end = rf"\bEND\s+OF\s+{icao}(?:\s+(?:ARR|DEP|ATIS))*\s+\1\b\.?"

    full = re.compile(f"{start}.*?{end}", re.IGNORECASE | re.DOTALL)
    blocks, seen = [], set()

    for m in full.finditer(text):
        t = m.group(0).strip()
        if t not in seen:
            seen.add(t)
            blocks.append({"text": t, "complete": True})

    # Fallback: jest nagłówek, ale brak "END OF ..." (np. strona ucięła tekst)
    if not blocks:
        starts = list(re.finditer(start, text, re.IGNORECASE))
        for i, m in enumerate(starts):
            stop = starts[i + 1].start() if i + 1 < len(starts) else min(len(text), m.start() + 1500)
            t = text[m.start():stop].strip()
            if t not in seen:
                seen.add(t)
                blocks.append({"text": t, "complete": False})

    return blocks


# --------------------------------------------------------------------------
# PARSER POSZCZEGÓLNYCH ELEMENTÓW DEPESZY
# --------------------------------------------------------------------------

def _num_sign(sign: str | None, digits: str) -> str:
    neg = sign and sign.upper() in ("M", "MS", "MINUS", "-")
    return f"-{int(digits)}" if neg else str(int(digits))


def parse_wind(s: str) -> dict:
    d = {"raw": s}
    m = re.search(r"\b(VRB|\d{3})\s*DEG\b[, ]*\s*(\d+)\s*KT", s, re.I)
    if m:
        d["direction_deg"] = None if m.group(1).upper() == "VRB" else int(m.group(1))
        d["variable_direction"] = m.group(1).upper() == "VRB"
        d["speed_kt"] = int(m.group(2))
    else:
        m = re.search(r"\b(\d{3})\s*/\s*(\d+)\s*KT", s, re.I)
        if m:
            d["direction_deg"], d["speed_kt"] = int(m.group(1)), int(m.group(2))
    g = re.search(r"\b(?:MAX|GUST[S]?)\s*(\d+)", s, re.I)
    if g:
        d["gust_kt"] = int(g.group(1))
    v = re.search(r"\bVAR(?:IABLE)?\s+BETWEEN\s+(\d{3})\s+AND\s+(\d{3})", s, re.I)
    if v:
        d["variable_from_deg"], d["variable_to_deg"] = int(v.group(1)), int(v.group(2))
    return d


def parse_visibility(s: str) -> dict:
    d = {"raw": s}
    m = re.search(r"(\d+(?:\.\d+)?)\s*KM", s, re.I)
    if m:
        d["meters"] = int(float(m.group(1)) * 1000)
    else:
        m = re.search(r"\b(\d{2,4})\s*M\b", s, re.I)
        if m:
            d["meters"] = int(m.group(1))
    d["or_more"] = bool(re.search(r"OR\s+MORE", s, re.I))
    return d


def parse_clouds(s: str) -> dict:
    d = {"raw": s, "layers": []}
    for m in re.finditer(r"\b(FEW|SCT|BKN|OVC)\s+(\d+)\s*FT(?:\s+(CB|TCU))?", s, re.I):
        layer = {"cover": m.group(1).upper(), "base_ft": int(m.group(2))}
        if m.group(3):
            layer["type"] = m.group(3).upper()
        d["layers"].append(layer)
    m = re.search(r"\bVV\s+(\d+)\s*FT", s, re.I)
    if m:
        d["vertical_visibility_ft"] = int(m.group(1))
    if re.search(r"\b(NSC|NCD|SKC|CAVOK)\b", s, re.I):
        d["no_significant_cloud"] = True
    return d


WX_WORDS = (
    r"(?:LIGHT|HEAVY|MODERATE|SLIGHT)?\s*"
    r"(?:RAIN|SNOW|FOG|MIST|HAZE|DRIZZLE|SHOWERS?|THUNDERSTORMS?|FREEZING|SLEET|HAIL|DUST|SAND|SMOKE|SQUALL)"
)
WX_CODE = r"[+-]?(?:VC)?(?:SH|TS|FZ|BC|MI|PR|DR|BL)?(?:DZ|RA|SN|SG|PL|GR|GS|BR|FG|FU|VA|DU|SA|HZ|SQ|FC|SS|DS)+"


def parse_atis(raw_text: str, icao: str, complete: bool = True) -> dict:
    flat = flatten(raw_text)

    # --- nagłówek: "EPWA ARR ATIS J 1256Z" ---
    hm = re.match(
        rf"\s*{icao}\s+(?:(ARR|DEP)\s+)?ATIS(?:\s+(?:INFO|INFORMATION))?\s+([A-Z])\s+(\d{{4}})\s*Z\b\.?",
        flat, re.I,
    )
    kind = hm.group(1).upper() if hm and hm.group(1) else None
    letter = hm.group(2).upper() if hm else "?"
    issue_time = f"{hm.group(3)}Z" if hm else None
    atis_type = {"ARR": "ARRIVAL", "DEP": "DEPARTURE"}.get(kind, "COMBINED")

    # --- treść bez nagłówka i bez "END OF ..." ---
    body = flat[hm.end():] if hm else flat
    body = re.sub(rf"\bEND\s+OF\s+{icao}(?:\s+(?:ARR|DEP|ATIS))*\s+[A-Z]\b\.?\s*$", "", body, flags=re.I).strip()

    # zdania rozdzielone ". " – kropka w "118.1" nie rozdziela
    sentences = [s.strip(" ,") for s in re.split(r"\.(?:\s+|$)", body) if s.strip(" ,.")]

    ident = re.compile(rf"^{icao}(?:\s+(?:ARR|DEP))?\s+[A-Z]$", re.I)

    details: dict = {}
    elements: list[dict] = []
    remarks: list[str] = []
    other: list[str] = []
    runways: list[str] = []
    approaches: list[str] = []
    seen_trend = False

    def add(key: str, text: str):
        elements.append({"key": key, "text": text})

    for s in sentences:
        u = s.upper()

        if ident.match(s):                                   # "EPWA ARR J"
            add("ident", s)
            continue

        m = re.match(r"TIME\s+(\d{4})\b", u)
        if m:
            details["observation_time"] = f"{m.group(1)}Z"
            add("time", s); continue

        if u.startswith("EXP") or re.search(r"\bAPCH\b", u):
            approaches.append(s)
            runways += re.findall(r"\bRWY\s+(\d{2}[LCR]?)", u)
            add("approach", s); continue

        if re.match(r"(?:(?:ARR|DEP|LDG|TKOF)\s+)?(?:RWY|RUNWAY)\s+\d{2}", u) or "RWY IN USE" in u or "RUNWAY IN USE" in u:
            runways += re.findall(r"\b(?:RWY|RUNWAY)\s+(\d{2}[LCR]?)", u)
            add("runway", s); continue

        m = re.match(r"TRANSITION\s+LEVEL\s+(\d+)", u)
        if m:
            details["transition_level"] = m.group(1)
            add("transition_level", s); continue

        m = re.match(r"TRANSITION\s+ALTITUDE\s+(\d+)", u)
        if m:
            details["transition_altitude_ft"] = int(m.group(1))
            add("transition_altitude", s); continue

        if re.match(r"(?:RWY\s+\S+\s+)?WIND\b", u):
            if "wind" not in details:
                details["wind"] = parse_wind(s)
            add("wind", s); continue

        if u == "CAVOK":
            details["cavok"] = True
            add("cavok", s); continue

        if re.match(r"VIS(?:IBILITY)?\b", u):
            details.setdefault("visibility", parse_visibility(s))
            add("visibility", s); continue

        if u.startswith("RVR"):
            details.setdefault("rvr", s)
            add("rvr", s); continue

        if re.match(r"(?:CLD|CLOUD)\b", u) or re.match(r"(?:NSC|NCD|SKC)\b", u) or re.match(r"VV\s+\d+", u):
            details.setdefault("clouds", parse_clouds(s))
            add("clouds", s); continue

        if re.match(r"(?:WX|WEATHER|PRESENT\s+WEATHER)\b", u) or re.match(rf"{WX_WORDS}\b", u) or re.fullmatch(WX_CODE, u):
            details.setdefault("weather", s)
            add("weather", s); continue

        m = re.fullmatch(r"T\s*(M|MS|MINUS|-)?\s*(\d+)", u)
        if m:
            details["temperature_c"] = int(_num_sign(m.group(1), m.group(2)))
            add("temperature", s); continue

        m = re.fullmatch(r"DP\s*(M|MS|MINUS|-)?\s*(\d+)", u)
        if m:
            details["dewpoint_c"] = int(_num_sign(m.group(1), m.group(2)))
            add("dewpoint", s); continue

        m = re.match(r"QNH\s*(\d{3,4})", u)
        if m:
            details["qnh_hpa"] = int(m.group(1))
            add("qnh", s); continue

        m = re.match(r"QFE\s*(\d{3,4})", u)
        if m:
            details["qfe_hpa"] = int(m.group(1))
            add("qfe", s); continue

        if re.match(r"(NOSIG|TEMPO|BECMG|TREND)\b", u):
            details["trend"] = s
            seen_trend = True
            add("trend", s); continue

        # wszystko co nierozpoznane: po trendzie = uwagi, przed = "other"
        if seen_trend or re.match(r"(CAUTION|WARNING|NOTAM|ACK|ACKNOWLEDGE|READ\s*BACK|ON\s+FIRST\s+CONTACT)", u):
            remarks.append(s)
            add("remark", s)
        else:
            other.append(s)
            add("other", s)

    runways = list(dict.fromkeys(runways))

    # --- pola płaskie (zgodne ze starym formatem JSON) + nowe ---
    wind = details.get("wind", {}).get("raw")
    vis = details.get("visibility", {}).get("raw")
    clouds = details.get("clouds", {}).get("raw")
    if details.get("cavok"):
        vis = vis or "CAVOK"
        clouds = clouds or "CAVOK"

    return {
        "icao": icao,
        "type": atis_type,
        "info_letter": letter,
        "issue_time": issue_time,
        "time": details.get("observation_time") or issue_time,
        "runway": ", ".join(runways) or None,
        "approach": "; ".join(approaches) or None,
        "transition_level": details.get("transition_level"),
        "wind": re.sub(r"^WIND\s+", "", wind, flags=re.I) if wind else None,
        "visibility": re.sub(r"^VIS(?:IBILITY)?\s+", "", vis, flags=re.I) if vis else None,
        "clouds": re.sub(r"^(?:CLD|CLOUD)\s+", "", clouds, flags=re.I) if clouds else None,
        "weather": details.get("weather"),
        "temperature": str(details["temperature_c"]) if "temperature_c" in details else None,
        "dewpoint": str(details["dewpoint_c"]) if "dewpoint_c" in details else None,
        "altimeter": f"QNH {details['qnh_hpa']}" if "qnh_hpa" in details else None,
        "trend": details.get("trend"),
        "remarks": "; ".join(remarks) or None,
        "complete": complete,
        "details": details,
        "elements": elements,
        "raw_text": flat,
    }


# --------------------------------------------------------------------------
# ZAPIS / DEDUPLIKACJA
# --------------------------------------------------------------------------

def latest_file(airport_dir: str) -> str | None:
    """
    Najnowszy plik wg NAZWY (ICAO_DDMMYYYY_HHMMz.json), a nie wg mtime.
    W GitHub Actions po checkoutcie wszystkie pliki mają praktycznie ten sam
    mtime, więc sortowanie po mtime wybierało praktycznie losowy plik.
    """
    best, best_key = None, None
    for f in os.listdir(airport_dir):
        m = re.match(r"^[A-Z]{4}_(\d{2})(\d{2})(\d{4})_(\d{4})z\.json$", f)
        if not m:
            continue
        dd, mm, yyyy, hhmm = m.groups()
        key = (yyyy, mm, dd, hhmm)
        if best_key is None or key > best_key:
            best, best_key = f, key
    return best


def save_debug(icao: str, html: str, text: str):
    os.makedirs(DEBUG_DIR, exist_ok=True)
    with open(os.path.join(DEBUG_DIR, f"{icao}.html"), "w", encoding="utf-8") as f:
        f.write(html)
    with open(os.path.join(DEBUG_DIR, f"{icao}.txt"), "w", encoding="utf-8") as f:
        f.write(text)


def run():
    now_utc = datetime.now(timezone.utc)
    date_str = now_utc.strftime("%d%m%Y")
    print(f"--- URUCHOMIENIE SKRYPTU: {now_utc.strftime('%Y-%m-%d %H:%M:%S UTC')} (tryb: {MODE}) ---")

    fetcher = None
    if MODE == "browser":
        try:
            fetcher = BrowserFetcher()
        except Exception as err:
            print(f"Uwaga: Playwright niedostępny ({err}) – używam trybu statycznego.")

    try:
        for icao in AIRPORTS:
            airport_dir = os.path.join(DATA_DIR, icao)
            os.makedirs(airport_dir, exist_ok=True)

            try:
                if fetcher:
                    html, text = fetcher.fetch(icao)
                else:
                    html, text = fetch_static(icao)

                blocks = extract_blocks(text, icao)
                print(f"[{icao}] Odczytano {len(blocks)} bloków ze strony.")

                if not blocks:
                    snippet = text[:160]
                    print(f"[{icao}] Podgląd tekstu strony: {snippet!r}")
                    save_debug(icao, html, text)
                    continue

                reports = [parse_atis(b["text"], icao, b["complete"]) for b in blocks]
                current_texts = [r["raw_text"] for r in reports]

                last = latest_file(airport_dir)
                if last:
                    try:
                        with open(os.path.join(airport_dir, last), "r", encoding="utf-8") as f:
                            last_data = json.load(f)
                        last_texts = [r.get("raw_text") for r in last_data.get("reports", []) if isinstance(r, dict)]
                        if current_texts == last_texts:
                            print(f"[{icao}] Brak zmian względem {last}. Skrypt pomija zapis.")
                            continue
                    except Exception as err:
                        print(f"[{icao}] Uwaga: Nie udało się wczytać starego pliku: {err}")

                t = reports[0].get("time") or ""
                digits = re.sub(r"\D", "", t)
                file_time = digits if len(digits) == 4 else now_utc.strftime("%H%M")
                filepath = os.path.join(airport_dir, f"{icao}_{date_str}_{file_time}z.json")

                payload = {
                    "airport": icao,
                    "fetch_time_utc": now_utc.strftime("%Y-%m-%d %H:%M:%S UTC"),
                    "reports": reports,
                }
                with open(filepath, "w", encoding="utf-8") as f:
                    json.dump(payload, f, ensure_ascii=False, indent=2)

                summary = ", ".join(f"{r['type']} {r['info_letter']} ({r['time']})" for r in reports)
                print(f"[{icao}] ---> ZAPISANO NOWY PLIK: {filepath} [{summary}]")

            except Exception as err:
                print(f"[{icao}] Błąd połączenia lub przetwarzania: {err}")
    finally:
        if fetcher:
            fetcher.close()


if __name__ == "__main__":
    run()