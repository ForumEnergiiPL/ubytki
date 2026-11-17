
import csv
import re
import json
import hashlib
import sys

from pathlib import Path
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup


# ==================================================
# 1. USTAWIENIA
# ==================================================

URL = "https://gpi.tge.pl/pl/zestawienie-ubytkow"

# Plik znajduje się w folderze kody/
# Dane CSV zapisujemy w głównym folderze repozytorium
ROOT = Path(__file__).resolve().parents[1]

PLIK = ROOT / "tge_ubytki.csv"
WIDOKI = ROOT / "tge_widoki.csv"
STATUS = ROOT / "stan_tge.json"

DZIS = datetime.now(
    ZoneInfo("Europe/Warsaw")
).date()

POLA = [
    "jednostka",
    "moc",
    "ubytek",
    "typ",
    "poczatek",
    "koniec"
]

KOLUMNY = POLA + [
    "widok_od",
    "zrodlo",
    "id_wersji",
    "pierwszy_odczyt_utc",
    "ostatni_odczyt_utc",
    "pobrano_utc"
]


# ==================================================
# 2. WZÓR ZGŁOSZEŃ TGE
# ==================================================

WZOR = re.compile(
    r"Jednostka\s+(?P<jednostka>.*?)\s+"
    r"Moc Osiągalna:\s*(?P<moc>[\d.,]+)\s*MW\s+"
    r"Wielkość ubytku:\s*(?P<ubytek>[\d.,]+)\s*MW\s+"
    r"Typ zdarzenia:\s*"
    r"(?P<typ>Planowane|Nieplanowane|Inne)\s+"
    r"Początek zdarzenia:\s*"
    r"(?P<poczatek>\d{2}-\d{2}-\d{4}\s+\d{2}:\d{2})\s+"
    r"Koniec zdarzenia:\s*"
    r"(?P<koniec>\d{2}-\d{2}-\d{4}\s+\d{2}:\d{2})",
    re.IGNORECASE
)


# ==================================================
# 3. FUNKCJE POMOCNICZE
# ==================================================

def moment_utc():
    return datetime.now(
        timezone.utc
    ).isoformat(timespec="seconds")


def data_iso(value):
    value = str(value).strip()

    for fmt in [
        "%d-%m-%Y %H:%M",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M"
    ]:
        try:
            return datetime.strptime(
                value, fmt
            ).strftime("%Y-%m-%d %H:%M:%S")

        except ValueError:
            continue

    raise ValueError(
        f"Nieprawidłowa data TGE: {value}"
    )


def klucz_zdarzenia(r):
    return (
        str(r["jednostka"]).strip(),
        str(float(str(r["moc"]).replace(",", "."))),
        str(float(str(r["ubytek"]).replace(",", "."))),
        str(r["typ"]).strip().lower(),
        data_iso(r["poczatek"]),
        data_iso(r["koniec"])
    )


def id_zdarzenia(r):
    tekst = "|".join(klucz_zdarzenia(r))

    return hashlib.sha256(
        tekst.encode("utf-8")
    ).hexdigest()[:24]


def wczytaj_csv(path):
    if not path.exists() or path.stat().st_size == 0:
        return [], []

    with path.open(
        "r",
        encoding="utf-8-sig",
        newline=""
    ) as f:

        reader = csv.DictReader(
            f,
            delimiter=";"
        )

        return (
            list(reader),
            list(reader.fieldnames or [])
        )


def zapisz_csv(path, rows, columns):
    path.parent.mkdir(
        parents=True,
        exist_ok=True
    )

    temp = path.with_name(
        path.name + ".tmp"
    )

    with temp.open(
        "w",
        encoding="utf-8-sig",
        newline=""
    ) as f:

        writer = csv.DictWriter(
            f,
            delimiter=";",
            fieldnames=columns,
            extrasaction="ignore"
        )

        writer.writeheader()
        writer.writerows(rows)

    temp.replace(path)


# ==================================================
# 4. POBIERANIE TGE - JEDNO ZAPYTANIE
# ==================================================

def pobierz_tge():

    print("Pobieram TGE - jedno zapytanie...", flush=True)

    response = requests.get(
        URL,
        headers={
            "User-Agent": "Mozilla/5.0",
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Language": "pl-PL,pl;q=0.9"
        },
        timeout=(8, 20)
    )

    response.raise_for_status()

    soup = BeautifulSoup(
        response.content,
        "html.parser"
    )

    tekst = soup.get_text(
        " ",
        strip=True
    )

    fragmenty = re.split(
        r"Informacje o ubytku",
        tekst,
        flags=re.IGNORECASE
    )[1:]

    if not fragmenty:
        raise RuntimeError(
            "Nie znaleziono zgłoszeń TGE. "
            "Stary CSV pozostaje bez zmian."
        )

    poczatek_dnia = datetime.combine(
        DZIS,
        datetime.min.time()
    )

    koniec_dnia = poczatek_dnia + timedelta(days=1)

    moment = moment_utc()
    wyniki = []

    for fragment in fragmenty:

        match = WZOR.search(fragment)

        if not match:
            raise RuntimeError(
                "Nie rozpoznano zgłoszenia TGE: "
                + fragment[:150]
            )

        r = match.groupdict()

        r["poczatek"] = data_iso(r["poczatek"])
        r["koniec"] = data_iso(r["koniec"])

        r["moc"] = str(
            float(r["moc"].replace(",", "."))
        )

        r["ubytek"] = str(
            float(r["ubytek"].replace(",", "."))
        )

        start = datetime.fromisoformat(
            r["poczatek"]
        )

        end = datetime.fromisoformat(
            r["koniec"]
        )

        if end <= start:
            raise ValueError(
                "Nieprawidłowy przedział zdarzenia TGE"
            )

        # Tylko zdarzenia aktywne dzisiaj
        if not (
            start < koniec_dnia
            and end > poczatek_dnia
        ):
            continue

        r["id_wersji"] = id_zdarzenia(r)
        r["widok_od"] = DZIS.isoformat()
        r["zrodlo"] = ""
        r["pierwszy_odczyt_utc"] = moment
        r["ostatni_odczyt_utc"] = moment
        r["pobrano_utc"] = moment

        wyniki.append(r)

    print(
        "Wszystkie zgłoszenia w widoku:",
        len(fragmenty)
    )

    print(
        "Zgłoszenia aktywne dzisiaj:",
        len(wyniki)
    )

    return wyniki


# ==================================================
# 5. AKTUALIZACJA BAZY CSV
# ==================================================

def aktualizuj_baze(nowe):

    stare, kolumny = wczytaj_csv(PLIK)

    if stare and not set(POLA).issubset(kolumny):
        raise ValueError(
            "Stary plik CSV nie ma wymaganych kolumn"
        )

    znane = {
        klucz_zdarzenia(r)
        for r in stare
    }

    dodane = []

    for r in nowe:

        klucz = klucz_zdarzenia(r)

        if klucz not in znane:
            znane.add(klucz)
            dodane.append(r)

    if dodane or not kolumny:

        wszystkie = stare + dodane

        for kol in KOLUMNY:
            if kol not in kolumny:
                kolumny.append(kol)

        zapisz_csv(
            PLIK,
            wszystkie,
            kolumny
        )

    print("Stare rekordy:", len(stare))
    print("Nowe rekordy:", len(dodane))
    print("Łącznie:", len(stare) + len(dodane))

    return len(dodane)


# ==================================================
# 6. HISTORIA POBIERANIA I MIGAWKI
# ==================================================

def zapisz_widok(nowe):

    stare, kolumny = wczytaj_csv(WIDOKI)

    stare = [
        r for r in stare
        if r.get("widok_od") != DZIS.isoformat()
    ]

    stare.append({
        "widok_od": DZIS.isoformat(),
        "widok_do": DZIS.isoformat(),
        "liczba_zgloszen": len(nowe),
        "pobrano_utc": moment_utc()
    })

    wymagane = [
        "widok_od",
        "widok_do",
        "liczba_zgloszen",
        "pobrano_utc"
    ]

    for kol in wymagane:
        if kol not in kolumny:
            kolumny.append(kol)

    zapisz_csv(
        WIDOKI,
        stare,
        kolumny
    )


def zapisz_migawke(nowe):

    folder = ROOT / "tge_migawki"

    path = folder / f"{DZIS:%Y-%m}.csv"

    stare, kolumny = wczytaj_csv(path)

    stare = [
        r for r in stare
        if r.get("data_obserwacji") != DZIS.isoformat()
    ]

    moment = moment_utc()

    for r in nowe:
        stare.append({
            **r,
            "data_obserwacji": DZIS.isoformat(),
            "moment_odczytu_utc": moment
        })

    wymagane = KOLUMNY + [
        "data_obserwacji",
        "moment_odczytu_utc"
    ]

    for kol in wymagane:
        if kol not in kolumny:
            kolumny.append(kol)

    zapisz_csv(
        path,
        stare,
        kolumny
    )


# ==================================================
# 7. URUCHOMIENIE
# ==================================================

def main():

    print("Data:", DZIS)
    print("Repozytorium:", ROOT)

    blad = None

    try:
        nowe = pobierz_tge()

        aktualizuj_baze(nowe)

        zapisz_widok(nowe)
        zapisz_migawke(nowe)

        print("\nTGE ZAKTUALIZOWANE POPRAWNIE!")

        status = "OK"

    except (
        requests.RequestException,
        RuntimeError,
        ValueError,
        KeyError,
        OSError
    ) as e:

        blad = f"{type(e).__name__}: {e}"

        print("\nBŁĄD TGE:")
        print(blad)

        print(
            "Wcześniejsze dane źródłowe nie są usuwane."
        )

        status = "BLAD"

    stan = {
        "data": DZIS.isoformat(),
        "status": status,
        "blad": blad,
        "moment_utc": moment_utc()
    }

    STATUS.write_text(
        json.dumps(
            stan,
            ensure_ascii=False,
            indent=2
        ) + "\n",
        encoding="utf-8"
    )

    return 0 if status == "OK" else 1


if __name__ == "__main__":
    sys.exit(main())
