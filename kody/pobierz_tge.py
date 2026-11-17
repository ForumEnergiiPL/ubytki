
import csv
import hashlib
import json
import re
import sys

from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup


# ============================================
# USTAWIENIA
# ============================================

ROOT = Path(__file__).resolve().parents[1]

URL = (
    "https://r.jina.ai/"
    "https://gpi.tge.pl/pl/zestawienie-ubytkow"
)

DZIS = datetime.now(
    ZoneInfo("Europe/Warsaw")
).date()

POLA = [
    "jednostka", "moc", "ubytek",
    "typ", "poczatek", "koniec"
]

KOLUMNY = POLA + [
    "widok_od", "zrodlo", "id_wersji",
    "pierwszy_odczyt_utc",
    "ostatni_odczyt_utc", "pobrano_utc"
]

MIESIACE = {
    "styczeń": 1, "luty": 2, "marzec": 3,
    "kwiecień": 4, "maj": 5, "czerwiec": 6,
    "lipiec": 7, "sierpień": 8, "wrzesień": 9,
    "październik": 10, "listopad": 11,
    "grudzień": 12
}

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


# ============================================
# FUNKCJE POMOCNICZE
# ============================================

def teraz():
    return datetime.now(
        timezone.utc
    ).isoformat(timespec="seconds")


def data_iso(wartosc):
    for fmt in (
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%d-%m-%Y %H:%M"
    ):
        try:
            return datetime.strptime(
                str(wartosc).strip(), fmt
            ).strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            pass

    raise ValueError(
        f"Niepoprawna data: {wartosc}"
    )


def klucz(r):
    return (
        str(r["jednostka"]).strip(),
        str(float(str(r["moc"]).replace(",", "."))),
        str(float(str(r["ubytek"]).replace(",", "."))),
        str(r["typ"]).strip().lower(),
        data_iso(r["poczatek"]),
        data_iso(r["koniec"])
    )


def identyfikator(r):
    tekst = "\x1f".join(klucz(r))

    return hashlib.sha256(
        tekst.encode()
    ).hexdigest()[:24]


def czytaj_csv(sciezka):
    if (
        not sciezka.exists()
        or sciezka.stat().st_size == 0
    ):
        return [], []

    with sciezka.open(
        encoding="utf-8-sig",
        newline=""
    ) as f:
        reader = csv.DictReader(
            f, delimiter=";"
        )
        return list(reader), list(reader.fieldnames or [])


def zapisz_csv(sciezka, wiersze, kolumny):
    sciezka.parent.mkdir(
        parents=True, exist_ok=True
    )

    temp = sciezka.with_name(
        sciezka.name + ".tmp"
    )

    with temp.open(
        "w",
        encoding="utf-8-sig",
        newline=""
    ) as f:
        writer = csv.DictWriter(
            f,
            fieldnames=kolumny,
            delimiter=";",
            extrasaction="ignore"
        )
        writer.writeheader()
        writer.writerows(wiersze)

    temp.replace(sciezka)


# ============================================
# PARSOWANIE STRONY GPI
# ============================================

def parsuj(html):
    tekst = BeautifulSoup(
        html, "html.parser"
    ).get_text(" ", strip=True)

    fragmenty = re.split(
        r"Informacje o ubytku",
        tekst,
        flags=re.IGNORECASE
    )

    if len(fragmenty) < 2:
        raise RuntimeError(
            "Brak szczegółów ubytków "
            "w odpowiedzi pośrednika."
        )

    # Weryfikacja daty 14-dniowego widoku
    wzor_daty = re.compile(
        r"Moc\s+Osiągalna.*?\b("
        + "|".join(MIESIACE)
        + r")\s+(20\d{2})\s+"
        r"(?:Pn|Wt|Śr|Cz|Pt|So|N)\s+"
        r"(\d{1,2})\b",
        re.IGNORECASE | re.DOTALL
    )

    m = wzor_daty.search(fragmenty[0])

    if not m:
        raise RuntimeError(
            "Nie udało się potwierdzić daty widoku GPI."
        )

    widok_od = datetime(
        int(m.group(2)),
        MIESIACE[m.group(1).lower()],
        int(m.group(3))
    ).date()

    if not (
        widok_od <= DZIS
        <= widok_od + timedelta(days=13)
    ):
        raise RuntimeError(
            f"Nieaktualny widok: {widok_od}, "
            f"dzisiaj {DZIS}"
        )

    poczatek_dnia = datetime.combine(
        DZIS, datetime.min.time()
    )
    koniec_dnia = poczatek_dnia + timedelta(days=1)

    wyniki = []
    moment = teraz()

    for fragment in fragmenty[1:]:
        dopasowanie = WZOR.search(fragment)

        if not dopasowanie:
            raise RuntimeError(
                "Nieznany format zgłoszenia: "
                + fragment[:150]
            )

        r = dopasowanie.groupdict()

        r["poczatek"] = data_iso(r["poczatek"])
        r["koniec"] = data_iso(r["koniec"])
        r["moc"] = str(float(r["moc"].replace(",", ".")))
        r["ubytek"] = str(float(r["ubytek"].replace(",", ".")))

        start = datetime.fromisoformat(r["poczatek"])
        end = datetime.fromisoformat(r["koniec"])

        if end <= start:
            raise ValueError(
                "Koniec wyłączenia przed początkiem"
            )

        # Zachowujemy tylko aktywne dzisiaj
        if not (
            start < koniec_dnia
            and end > poczatek_dnia
        ):
            continue

        r.update({
            "widok_od": DZIS.isoformat(),
            "zrodlo": "",
            "id_wersji": identyfikator(r),
            "pierwszy_odczyt_utc": moment,
            "ostatni_odczyt_utc": moment,
            "pobrano_utc": moment
        })

        wyniki.append(r)

    print(
        f"Zgłoszeń w widoku: {len(fragmenty)-1}",
        flush=True
    )
    print(
        f"Aktywnych dzisiaj: {len(wyniki)}",
        flush=True
    )

    return wyniki


# ============================================
# POBIERANIE - JEDNO ZAPYTANIE
# ============================================

def pobierz_tge():
    print(
        "Pobieram GPI przez Jina Reader...",
        flush=True
    )

    response = requests.get(
        URL,
        headers={
            "X-Respond-With": "html",
            "X-No-Cache": "true",
            "Accept": "text/html,text/plain,*/*"
        },
        timeout=(15, 90)
    )

    response.raise_for_status()

    return parsuj(response.content)


# ============================================
# AKTUALIZACJA PLIKÓW
# ============================================

def aktualizuj():

    # Faktycznie uruchamiamy pobieranie
    nowe = pobierz_tge()

    baza = ROOT / "tge_ubytki.csv"
    stare, kolumny = czytaj_csv(baza)

    if stare and not set(POLA).issubset(kolumny):
        raise ValueError(
            "Istniejący CSV ma niezgodne kolumny"
        )

    znane = {klucz(r) for r in stare}
    dopisane = []

    for r in nowe:
        k = klucz(r)

        if k not in znane:
            znane.add(k)
            dopisane.append(r)

    if dopisane or not kolumny:
        pelne_kolumny = kolumny + [
            c for c in KOLUMNY if c not in kolumny
        ]

        zapisz_csv(
            baza,
            stare + dopisane,
            pelne_kolumny
        )

    # Historia poprawnie pobranych widoków
    widoki = ROOT / "tge_widoki.csv"
    stare_widoki, kol_widoki = czytaj_csv(widoki)

    stare_widoki = [
        r for r in stare_widoki
        if r.get("widok_od") != DZIS.isoformat()
    ]

    stare_widoki.append({
        "widok_od": DZIS.isoformat(),
        "widok_do": DZIS.isoformat(),
        "liczba_zgloszen": str(len(nowe)),
        "pobrano_utc": teraz()
    })

    potrzebne = [
        "widok_od", "widok_do",
        "liczba_zgloszen", "pobrano_utc"
    ]

    zapisz_csv(
        widoki,
        stare_widoki,
        kol_widoki + [
            c for c in potrzebne if c not in kol_widoki
        ]
    )

    # Dzienna migawka
    migawka = (
        ROOT / "tge_migawki"
        / f"{DZIS:%Y-%m}.csv"
    )

    stare_migawki, kol_migawki = czytaj_csv(migawka)

    stare_migawki = [
        r for r in stare_migawki
        if r.get("data_obserwacji") != DZIS.isoformat()
    ]

    moment = teraz()

    snapshot = [
        dict(
            r,
            data_obserwacji=DZIS.isoformat(),
            moment_odczytu_utc=moment
        )
        for r in nowe
    ]

    potrzebne = KOLUMNY + [
        "data_obserwacji",
        "moment_odczytu_utc"
    ]

    zapisz_csv(
        migawka,
        stare_migawki + snapshot,
        kol_migawki + [
            c for c in potrzebne if c not in kol_migawki
        ]
    )

    print(
        f"Dopisano nowych: {len(dopisane)}",
        flush=True
    )
    print(
        f"Łącznie w bazie: {len(stare)+len(dopisane)}",
        flush=True
    )


# ============================================
# URUCHOMIENIE PROGRAMU
# ============================================

def main():
    try:
        aktualizuj()
        status = "OK"
        blad = ""

    except (
        requests.RequestException,
        RuntimeError,
        ValueError,
        KeyError,
        OSError
    ) as e:
        status = "BLAD"
        blad = f"{type(e).__name__}: {e}"

        print("BŁĄD TGE:", blad, flush=True)

    (ROOT / "stan_tge.json").write_text(
        json.dumps({
            "data": DZIS.isoformat(),
            "status": status,
            "blad": blad,
            "moment_utc": teraz()
        }, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8"
    )

    return 0 if status == "OK" else 1


if __name__ == "__main__":
    sys.exit(main())
