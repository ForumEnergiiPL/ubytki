
"""Jednorazowe pobieranie ubytków GPI TGE."""

import csv
import hashlib
import json
import os
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

TODAY = datetime.now(
    ZoneInfo("Europe/Warsaw")
).date()

URL = os.environ.get(
    "TGE_URL",
    "https://gpi.tge.pl/pl/zestawienie-ubytkow"
)

FIELDS = [
    "jednostka",
    "moc",
    "ubytek",
    "typ",
    "poczatek",
    "koniec"
]

COLUMNS = FIELDS + [
    "widok_od",
    "zrodlo",
    "id_wersji",
    "pierwszy_odczyt_utc",
    "ostatni_odczyt_utc",
    "pobrano_utc"
]

MONTHS = {
    "styczeń": 1,
    "luty": 2,
    "marzec": 3,
    "kwiecień": 4,
    "maj": 5,
    "czerwiec": 6,
    "lipiec": 7,
    "sierpień": 8,
    "wrzesień": 9,
    "październik": 10,
    "listopad": 11,
    "grudzień": 12
}

DETAIL = re.compile(
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

def utc_now():
    return datetime.now(
        timezone.utc
    ).isoformat(timespec="seconds")


def iso(value):
    formats = (
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%d-%m-%Y %H:%M"
    )

    for fmt in formats:
        try:
            return datetime.strptime(
                str(value).strip(),
                fmt
            ).strftime("%Y-%m-%d %H:%M:%S")

        except ValueError:
            pass

    raise ValueError(
        f"Nieprawidłowa data: {value}"
    )


def key(row):
    return (
        str(row["jednostka"]).strip(),
        str(float(
            str(row["moc"]).replace(",", ".")
        )),
        str(float(
            str(row["ubytek"]).replace(",", ".")
        )),
        str(row["typ"]).strip().lower(),
        iso(row["poczatek"]),
        iso(row["koniec"])
    )


def read_csv(path):
    if (
        not path.exists()
        or path.stat().st_size == 0
    ):
        return [], []

    with path.open(
        encoding="utf-8-sig",
        newline=""
    ) as file:

        reader = csv.DictReader(
            file,
            delimiter=";"
        )

        return (
            list(reader),
            list(reader.fieldnames or [])
        )


def save_csv(path, rows, cols):
    path.parent.mkdir(
        parents=True,
        exist_ok=True
    )

    tmp = path.with_name(
        path.name + ".tmp"
    )

    with tmp.open(
        "w",
        encoding="utf-8-sig",
        newline=""
    ) as file:

        writer = csv.DictWriter(
            file,
            fieldnames=cols,
            delimiter=";",
            extrasaction="ignore"
        )

        writer.writeheader()
        writer.writerows(rows)

    tmp.replace(path)


# ============================================
# PARSOWANIE GPI
# ============================================

def parse(html):
    text = BeautifulSoup(
        html,
        "html.parser"
    ).get_text(" ", strip=True)

    parts = re.split(
        r"Informacje o ubytku",
        text,
        flags=re.IGNORECASE
    )

    if len(parts) < 2:
        raise ValueError(
            "Brak szczegółów zgłoszeń GPI. "
            "Nie zapisuję danych."
        )

    # Sprawdzenie, czy widok obejmuje dzisiejszy dzień.
    header_re = re.compile(
        r"Moc\s+Osiągalna.*?\b("
        + "|".join(MONTHS)
        + r")\s+(20\d{2})\s+"
        r"(?:Pn|Wt|Śr|Cz|Pt|So|N)\s+"
        r"(\d{1,2})\b",
        re.IGNORECASE | re.DOTALL
    )

    m = header_re.search(parts[0])

    if not m:
        raise ValueError(
            "Nie potwierdzono daty widoku TGE."
        )

    view_start = datetime(
        int(m.group(2)),
        MONTHS[m.group(1).lower()],
        int(m.group(3))
    ).date()

    if not (
        view_start <= TODAY
        <= view_start + timedelta(days=13)
    ):
        raise ValueError(
            f"Nieaktualny widok: {view_start}. "
            f"Dzisiaj: {TODAY}"
        )

    start_day = datetime.combine(
        TODAY,
        datetime.min.time()
    )

    end_day = start_day + timedelta(days=1)

    retrieved = utc_now()
    result = []

    for part in parts[1:]:
        m = DETAIL.search(part)

        if not m:
            raise ValueError(
                "Nieznany format zgłoszenia: "
                + part[:150]
            )

        row = m.groupdict()

        row["poczatek"] = iso(row["poczatek"])
        row["koniec"] = iso(row["koniec"])

        row["moc"] = str(
            float(row["moc"].replace(",", "."))
        )

        row["ubytek"] = str(
            float(row["ubytek"].replace(",", "."))
        )

        a = datetime.fromisoformat(
            row["poczatek"]
        )

        b = datetime.fromisoformat(
            row["koniec"]
        )

        if b <= a:
            raise ValueError(
                "Niepoprawny okres zdarzenia."
            )

        # Tylko zdarzenia aktywne dzisiaj
        if a < end_day and b > start_day:

            identity = "\x1f".join(key(row))

            row.update({
                "widok_od": TODAY.isoformat(),
                "zrodlo": "",
                "id_wersji": hashlib.sha256(
                    identity.encode()
                ).hexdigest()[:24],
                "pierwszy_odczyt_utc": retrieved,
                "ostatni_odczyt_utc": retrieved,
                "pobrano_utc": retrieved
            })

            result.append(row)

    print(
        f"Zgłoszenia w widoku: {len(parts) - 1}",
        flush=True
    )

    print(
        f"Aktywne dzisiaj: {len(result)}",
        flush=True
    )

    return result


# ============================================
# POBIERANIE JEDNEGO WIDOKU
# ============================================

def fetch():
    print(
        f"Pobieram {URL}",
        flush=True
    )

    with requests.Session() as session:

        response = session.get(
            URL,
            headers={
                "User-Agent": "Mozilla/5.0",
                "Accept-Language": "pl-PL,pl;q=0.9",
                "Accept": (
                    "text/html,application/xhtml+xml"
                )
            },
            timeout=(30, 90),
            allow_redirects=True
        )

        response.raise_for_status()

        if "gpi.tge.pl" not in response.url:
            raise ValueError(
                "Nieoczekiwany adres docelowy: "
                + response.url
            )

        return parse(response.content)


# ============================================
# AKTUALIZACJA CSV
# ============================================

def update():

    # Najpierw pobierz i sprawdź odpowiedź.
    # Przy błędzie istniejące CSV pozostają bez zmian.
    fresh = fetch()

    target = ROOT / "tge_ubytki.csv"
    old, old_cols = read_csv(target)

    if (
        old_cols
        and not set(FIELDS).issubset(old_cols)
    ):
        raise ValueError(
            "Stary tge_ubytki.csv "
            "ma niezgodne kolumny."
        )

    seen = {
        key(row)
        for row in old
    }

    added = []

    for row in fresh:
        identity = key(row)

        if identity not in seen:
            seen.add(identity)
            added.append(row)

    # Dopisanie bez duplikatów
    if added or not old_cols:

        cols = old_cols + [
            c for c in COLUMNS
            if c not in old_cols
        ]

        save_csv(
            target,
            old + added,
            cols
        )

    # Historia udanych pobrań
    views = ROOT / "tge_widoki.csv"

    existing, cols = read_csv(views)

    existing = [
        r for r in existing
        if r.get("widok_od") != TODAY.isoformat()
    ]

    existing.append({
        "widok_od": TODAY.isoformat(),
        "widok_do": TODAY.isoformat(),
        "liczba_zgloszen": str(len(fresh)),
        "pobrano_utc": utc_now()
    })

    standard = [
        "widok_od",
        "widok_do",
        "liczba_zgloszen",
        "pobrano_utc"
    ]

    save_csv(
        views,
        existing,
        cols + [
            c for c in standard
            if c not in cols
        ]
    )

    # Migawka dzienna
    snapshot = (
        ROOT / "tge_migawki"
        / f"{TODAY:%Y-%m}.csv"
    )

    previous, cols = read_csv(snapshot)

    previous = [
        r for r in previous
        if r.get("data_obserwacji")
        != TODAY.isoformat()
    ]

    snapshot_rows = [
        {
            **r,
            "data_obserwacji": TODAY.isoformat(),
            "moment_odczytu_utc": utc_now()
        }
        for r in fresh
    ]

    standard = COLUMNS + [
        "data_obserwacji",
        "moment_odczytu_utc"
    ]

    save_csv(
        snapshot,
        previous + snapshot_rows,
        cols + [
            c for c in standard
            if c not in cols
        ]
    )

    print(
        f"Dopisano: {len(added)} nowych. "
        f"Łącznie: {len(old) + len(added)}",
        flush=True
    )


# ============================================
# URUCHOMIENIE
# ============================================

def main():

    try:
        update()
        state = "OK"
        error = ""

    except (
        requests.RequestException,
        ValueError,
        OSError,
        RuntimeError
    ) as exc:

        state = "BLAD"
        error = f"{type(exc).__name__}: {exc}"

        print(
            "BŁĄD TGE:",
            error,
            flush=True
        )

    (ROOT / "stan_tge.json").write_text(
        json.dumps(
            {
                "data": TODAY.isoformat(),
                "status": state,
                "blad": error,
                "moment_utc": utc_now(),
                "url": URL
            },
            ensure_ascii=False,
            indent=2
        ) + "\n",
        encoding="utf-8"
    )

    return 0 if state == "OK" else 1


if __name__ == "__main__":
    sys.exit(main())
