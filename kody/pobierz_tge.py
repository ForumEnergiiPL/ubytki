
import csv
import hashlib
import json
import re

from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup


ROOT = Path(__file__).resolve().parents[1]

URL = "https://gpi.tge.pl/pl/zestawienie-ubytkow"

TODAY = datetime.now(
    ZoneInfo("Europe/Warsaw")
).date()

FIELDS = [
    "jednostka", "moc", "ubytek",
    "typ", "poczatek", "koniec"
]

ADDITIONAL = [
    "widok_od", "zrodlo", "id_wersji",
    "pierwszy_odczyt_utc",
    "ostatni_odczyt_utc",
    "pobrano_utc"
]

PATTERN = re.compile(
    r"Jednostka\s+(?P<jednostka>.*?)\s+"
    r"Moc Osiągalna:\s*(?P<moc>[\d.,]+)\s*MW\s+"
    r"Wielkość ubytku:\s*(?P<ubytek>[\d.,]+)\s*MW\s+"
    r"Typ zdarzenia:\s*"
    r"(?P<typ>Planowane|Nieplanowane)\s+"
    r"Początek zdarzenia:\s*"
    r"(?P<poczatek>\d{2}-\d{2}-\d{4}\s+\d{2}:\d{2})\s+"
    r"Koniec zdarzenia:\s*"
    r"(?P<koniec>\d{2}-\d{2}-\d{4}\s+\d{2}:\d{2})",
    re.I
)


def read(name):
    path = ROOT / name

    if not path.exists() or path.stat().st_size == 0:
        return [], []

    with path.open(
        encoding="utf-8-sig",
        newline=""
    ) as f:
        reader = csv.DictReader(
            f, delimiter=";"
        )
        return list(reader), list(reader.fieldnames or [])


def save(name, rows, columns):
    path = ROOT / name
    path.parent.mkdir(parents=True, exist_ok=True)

    temp = path.with_name(path.name + ".tmp")

    with temp.open(
        "w", encoding="utf-8-sig", newline=""
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


def iso(value):
    raw = str(value).strip()

    for fmt in (
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%d-%m-%Y %H:%M"
    ):
        try:
            return datetime.strptime(
                raw, fmt
            ).strftime("%Y-%m-%d %H:%M:%S")

        except ValueError:
            pass

    raise ValueError("Nieznana data TGE: " + raw)


def key(row):
    return (
        str(row["jednostka"]).strip(),
        str(float(str(row["moc"]).replace(",", "."))),
        str(float(str(row["ubytek"]).replace(",", "."))),
        str(row["typ"]).strip().lower(),
        iso(row["poczatek"]),
        iso(row["koniec"])
    )


def fetch():
    print("Pobieram TGE - jedno zapytanie...")

    response = requests.get(
        URL,
        headers={
            "User-Agent": "Mozilla/5.0",
            "Accept-Language": "pl-PL,pl;q=0.9"
        },
        timeout=(8, 20)
    )

    response.raise_for_status()

    text = BeautifulSoup(
        response.content,
        "html.parser"
    ).get_text(" ", strip=True)

    fragments = re.split(
        r"Informacje o ubytku",
        text,
        flags=re.I
    )[1:]

    if not fragments:
        raise ValueError(
            "Brak zgłoszeń TGE. "
            "Zachowuję poprzedni CSV."
        )

    start = datetime.combine(
        TODAY,
        datetime.min.time()
    )

    end = start + timedelta(days=1)

    now = datetime.now(
        timezone.utc
    ).isoformat(timespec="seconds")

    output = []

    for part in fragments:
        hit = PATTERN.search(part)

        if not hit:
            raise ValueError(
                "Nieznany format TGE: " + part[:120]
            )

        row = hit.groupdict()

        row["poczatek"] = iso(row["poczatek"])
        row["koniec"] = iso(row["koniec"])

        row["moc"] = str(
            float(row["moc"].replace(",", "."))
        )

        row["ubytek"] = str(
            float(row["ubytek"].replace(",", "."))
        )

        event_start = datetime.fromisoformat(
            row["poczatek"]
        )

        event_end = datetime.fromisoformat(
            row["koniec"]
        )

        # Tylko zdarzenia aktywne dzisiaj
        if not (
            event_start < end
            and event_end > start
        ):
            continue

        row.update({
            "widok_od": TODAY.isoformat(),
            "zrodlo": "",
            "pobrano_utc": now,
            "pierwszy_odczyt_utc": now,
            "ostatni_odczyt_utc": now,
            "id_wersji": hashlib.sha256(
                "\x1f".join(key(row)).encode()
            ).hexdigest()[:24]
        })

        output.append(row)

    print(
        f"TGE: {len(fragments)} zgłoszeń w widoku, "
        f"{len(output)} aktywnych dzisiaj"
    )

    return output


def update():
    incoming = fetch()

    old, columns = read("tge_ubytki.csv")

    if old and not set(FIELDS).issubset(columns):
        raise ValueError(
            "Stary CSV TGE ma niezgodne kolumny"
        )

    known = {
        key(row) for row in old
    }

    added = []

    for row in incoming:
        k = key(row)

        if k not in known:
            known.add(k)
            added.append(row)

    if added or not columns:
        new_columns = columns + [
            c for c in FIELDS + ADDITIONAL
            if c not in columns
        ]

        save(
            "tge_ubytki.csv",
            old + added,
            new_columns
        )

    # Potwierdzenie pobrania konkretnego dnia
    views, viewcols = read("tge_widoki.csv")

    views = [
        v for v in views
        if v.get("widok_od") != TODAY.isoformat()
    ]

    views.append({
        "widok_od": TODAY.isoformat(),
        "widok_do": TODAY.isoformat(),
        "liczba_zgloszen": len(incoming),
        "pobrano_utc": datetime.now(
            timezone.utc
        ).isoformat(timespec="seconds")
    })

    save(
        "tge_widoki.csv",
        views,
        viewcols + [
            c for c in [
                "widok_od", "widok_do",
                "liczba_zgloszen", "pobrano_utc"
            ]
            if c not in viewcols
        ]
    )

    # Migawka TGE dla historii
    if incoming:
        name = f"tge_migawki/{TODAY:%Y-%m}.csv"

        snapshots, snapcols = read(name)

        snapshots = [
            x for x in snapshots
            if x.get("data_obserwacji")
            != TODAY.isoformat()
        ]

        for row in incoming:
            snapshots.append({
                **row,
                "data_obserwacji": TODAY.isoformat(),
                "moment_odczytu_utc": datetime.now(
                    timezone.utc
                ).isoformat(timespec="seconds")
            })

        cols = FIELDS + ADDITIONAL + [
            "data_obserwacji",
            "moment_odczytu_utc"
        ]

        save(
            name,
            snapshots,
            snapcols + [
                c for c in cols
                if c not in snapcols
            ]
        )

    print(
        f"TGE: dopisano {len(added)} rekordów. "
        f"Łącznie {len(old) + len(added)}."
    )


def main():
    error = None

    try:
        update()
        ok = True

    except (
        requests.RequestException,
        ValueError,
        RuntimeError,
        KeyError
    ) as exc:
        ok = False
        error = f"{type(exc).__name__}: {exc}"

        print("BŁĄD TGE:", error)

    state = {
        "data": TODAY.isoformat(),
        "status": "OK" if ok else "BLAD",
        "blad": error,
        "moment_utc": datetime.now(
            timezone.utc
        ).isoformat(timespec="seconds")
    }

    (ROOT / "stan_tge.json").write_text(
        json.dumps(
            state,
            ensure_ascii=False,
            indent=2
        ) + "\n",
        encoding="utf-8"
    )

    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
