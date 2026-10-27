import os
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

def clean_text(raw_html: str) -> str:
    soup = BeautifulSoup(raw_html, "html.parser")
    container = soup.find("main") or soup.find("div", class_="container") or soup.body
    if not container:
        return ""
    lines = [line.strip() for line in container.get_text(separator="\n").splitlines() if line.strip()]
    return "\n".join(lines)

def run():
    os.makedirs(DATA_DIR, exist_ok=True)

    for icao in AIRPORTS:
        url = BASE_URL.format(icao=icao)
        try:
            response = requests.get(url, headers=HEADERS, timeout=20)
            response.raise_for_status()

            current_text = clean_text(response.text)
            if not current_text:
                print(f"[{icao}] Brak tekstu do pobrania.")
                continue

            json_path = os.path.join(DATA_DIR, f"{icao}.json")
            history = []

            # Wczytanie dotychczasowej historii z pliku JSON
            if os.path.exists(json_path):
                try:
                    with open(json_path, "r", encoding="utf-8") as f:
                        history = json.load(f)
                except json.JSONDecodeError:
                    history = []

            # Sprawdzenie, czy treść różni się od ostatniego wpisu
            if history and history[-1].get("raw_text") == current_text:
                print(f"[{icao}] Brak zmian w ATIS od ostatniego sprawdzenia.")
                continue

            now_utc = datetime.now(timezone.utc)
            new_entry = {
                "airport": icao,
                "timestamp_utc": now_utc.strftime("%Y-%m-%d %H:%M:%S UTC"),
                "iso_timestamp": now_utc.isoformat(),
                "raw_text": current_text
            }

            history.append(new_entry)

            # Zapis z wcięciami, aby diff w Git był czytelny
            with open(json_path, "w", encoding="utf-8") as f:
                json.dump(history, f, ensure_ascii=False, indent=2)

            print(f"[{icao}] Dodano nowy wpis do {icao}.json.")

        except Exception as err:
            print(f"[{icao}] Błąd podczas przetwarzania {url}: {err}")

if __name__ == "__main__":
    run()