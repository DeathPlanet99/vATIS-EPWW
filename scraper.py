import os
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
    
    # Próba odnalezienia głównego kontenera z depeszą / tekstem ATIS
    # Jeśli serwis zmieni strukturę, soup.get_text() zadziała jako bezpieczny fallback
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
                print(f"[{icao}] Brak tekstu do zapisania.")
                continue

            file_path = os.path.join(DATA_DIR, f"{icao}.txt")
            last_entry = ""

            # Sprawdzenie ostatniego zapisu, aby nie dublować wpisów
            if os.path.exists(file_path):
                with open(file_path, "r", encoding="utf-8") as f:
                    content = f.read().strip()
                    if "--- TREŚĆ ---" in content:
                        last_entry = content.split("--- TREŚĆ ---")[-1].strip()

            if current_text == last_entry:
                print(f"[{icao}] Brak zmian od ostatniego pobrania.")
                continue

            timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
            
            with open(file_path, "a", encoding="utf-8") as f:
                f.write(f"\n{'='*50}\n")
                f.write(f"LOTNISKO: {icao} | DATA: {timestamp}\n")
                f.write("--- TREŚĆ ---\n")
                f.write(current_text + "\n")

            print(f"[{icao}] Nowy stan zapisany ({timestamp}).")

        except Exception as err:
            print(f"[{icao}] Błąd podczas pobierania {url}: {err}")

if __name__ == "__main__":
    run()