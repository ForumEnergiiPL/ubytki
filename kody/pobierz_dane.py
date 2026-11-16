
from pathlib import Path
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from urllib.parse import urljoin
import argparse
import hashlib
import json
import re
import sys

import pandas as pd
import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


ROOT = Path(__file__).resolve().parents[1]

API = "https://api.raporty.pse.pl/api/"
TGE_URL = "https://gpi.tge.pl/pl/zestawienie-ubytkow"

TZ = ZoneInfo("Europe/Warsaw")
FIRST_DATE = date(2026, 11, 9)
RECHECK_DAYS = 10
PAGE_SIZE = 1200

TGE_FIELDS = [
    "jednostka",
    "moc",
    "ubytek",
    "typ",
    "poczatek",
    "koniec",
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
    "grudzień": 12,
}

TGE_PATTERN = re.compile(
    r"Jednostka\s+(?P<jednostka>.*?)\s+"
    r"Moc Osiągalna:\s*(?P<moc>[\d.,]+)\s*MW\s+"
    r"Wielkość ubytku:\s*(?P<ubytek>[\d.,]+)\s*MW\s+"
    r"Typ zdarzenia:\s*(?P<typ>Planowane|Nieplanowane)\s+"
    r"Początek zdarzenia:\s*"
    r"(?P<poczatek>\d{2}-\d{2}-\d{4}\s+\d{2}:\d{2})\s+"
    r"Koniec zdarzenia:\s*"
    r"(?P<koniec>\d{2}-\d{2}-\d{4}\s+\d{2}:\d{2})",
    re.IGNORECASE,
)


def log(*args):
    print(*args, flush=True)


def now_utc():
    return datetime.now(
        timezone.utc
    ).isoformat(timespec="seconds")


def read_csv(path):
    if not path.exists() or path.stat().st_size == 0:
        return pd.DataFrame()

    return pd.read_csv(
        path,
        sep=";",
        dtype=str,
        keep_default_na=False,
        encoding="utf-8-sig",
        low_memory=False,
    )


def write_csv(path, df):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")

    df.to_csv(
        temp,
        sep=";",
        index=False,
        encoding="utf-8-sig",
    )

    temp.replace(path)


def merge(old, new):
    return pd.concat(
        [old, new],
        ignore_index=True
    ).fillna("")


def make_session():
    session = requests.Session()

    session.headers.update({
        "User-Agent": "Mozilla/5.0",
        "Accept": "application/json,text/html,*/*",
    })

    retry = Retry(
        total=3,
        connect=3,
        read=2,
        backoff_factor=1,
        status_forcelist=[
            429, 500, 502, 503, 504
        ],
        allowed_methods=["GET"],
    )

    session.mount(
        "https://",
        HTTPAdapter(max_retries=retry),
    )

    return session


def pse_pages(session, endpoint, day):
    url = API + endpoint

    params = {
        "$filter": (
            f"business_date eq '{day.isoformat()}'"
        ),
        "$first": PAGE_SIZE,
    }

    rows = []
    visited = set()

    while True:
        response = session.get(
            url,
            params=params,
            timeout=(12, 45),
        )

        response.raise_for_status()

        if response.url in visited:
            raise RuntimeError(
                "Wykryto zapetlenie paginacji PSE"
            )

        visited.add(response.url)

        payload = response.json()

        if (
            not isinstance(payload, dict)
            or not isinstance(payload.get("value"), list)
        ):
            raise ValueError(
                f"Niepoprawna odpowiedz {endpoint}"
            )

        rows.extend(payload["value"])

        next_url = (
            payload.get("nextLink")
            or payload.get("@odata.nextLink")
        )

        if not next_url:
            if len(payload["value"]) >= PAGE_SIZE:
                raise RuntimeError(
                    "Pelna strona PSE bez nextLink"
                )

            return rows

        url = urljoin(response.url, next_url)
        params = None


def validate_pse(df, endpoint, day):
    required = {
        "his-bil-mocy": [
            "business_date",
            "peak",
            "peak_hour",
            "ub_sum",
        ],
        "unav-pk5l": [
            "business_date",
            "unit_code",
            "start_dtime",
            "end_dtime",
            "reason",
            "state",
        ],
        "pdwkseub": [
            "business_date",
            "resource_code",
            "dtime_utc",
            "non_us_cap",
            "grid_lim",
        ],
    }[endpoint]

    missing = [
        c for c in required
        if c not in df.columns
    ]

    if missing:
        raise ValueError(
            f"PSE {endpoint}: brak kolumn {missing}"
        )

    if not df["business_date"].astype(str).str[:10].eq(
        day.isoformat()
    ).all():
        raise ValueError(
            "PSE zwrocilo nieprawidlowa date"
        )


def update_pse_file(endpoint, day, new):
    paths = {
        "his-bil-mocy": ROOT / "pse_bilans.csv",
        "unav-pk5l": ROOT / "pse_remonty.csv",
        "pdwkseub": ROOT / "pse_jednostki.csv",
    }

    path = paths[endpoint]
    old = read_csv(path)

    new = new.copy()
    new["pobrano_utc"] = now_utc()

    if endpoint == "his-bil-mocy":
        if not old.empty:
            old = old[
                old["business_date"].astype(str).str[:10]
                != day.isoformat()
            ]

        result = merge(old, new)

        result = result.drop_duplicates(
            ["business_date", "peak"],
            keep="last",
        )

        result = result.sort_values(
            ["business_date", "peak"]
        )

    elif endpoint == "pdwkseub":
        if not old.empty:
            old = old[
                old["business_date"].astype(str).str[:10]
                != day.isoformat()
            ]

        result = merge(old, new)

        result = result.drop_duplicates(
            ["resource_code", "dtime_utc"],
            keep="last",
        )

        result = result.sort_values(
            [
                "business_date",
                "dtime_utc",
                "resource_code",
            ]
        )

    else:
        result = merge(old, new)

        keys = [
            "unit_code",
            "start_dtime",
            "end_dtime",
            "reason",
            "state",
            "publication_ts_utc",
        ]

        keys = [
            k for k in keys
            if k in result.columns
        ]

        result = result.drop_duplicates(
            keys,
            keep="last",
        )

        result = result.sort_values(
            ["business_date", "unit_code"]
        )

    write_csv(path, result)


def get_status():
    status = read_csv(
        ROOT / "pse_status.csv"
    )

    if not {"endpoint", "data"}.issubset(status):
        return set()

    return set(zip(
        status["endpoint"],
        status["data"],
    ))


def save_status(endpoint, day, count):
    path = ROOT / "pse_status.csv"
    old = read_csv(path)

    new = pd.DataFrame([{
        "endpoint": endpoint,
        "data": day.isoformat(),
        "liczba": str(count),
        "pobrano_utc": now_utc(),
    }])

    result = merge(old, new)

    result = result.drop_duplicates(
        ["endpoint", "data"],
        keep="last",
    )

    write_csv(
        path,
        result.sort_values(["endpoint", "data"]),
    )


def update_pse(session, endpoint, today):
    last_day = (
        today
        if endpoint == "unav-pk5l"
        else today - timedelta(days=1)
    )

    if last_day < FIRST_DATE:
        return 0

    paths = {
        "his-bil-mocy": ROOT / "pse_bilans.csv",
        "unav-pk5l": ROOT / "pse_remonty.csv",
        "pdwkseub": ROOT / "pse_jednostki.csv",
    }

    saved = read_csv(paths[endpoint])
    completed = get_status()

    saved_dates = (
        set(
            saved["business_date"].astype(str).str[:10]
        )
        if not saved.empty
        and "business_date" in saved.columns
        else set()
    )

    recheck_from = today - timedelta(
        days=RECHECK_DAYS
    )

    days = [
        FIRST_DATE + timedelta(days=i)
        for i in range(
            (last_day - FIRST_DATE).days + 1
        )
    ]

    days_to_check = []

    for day in days:
        day_text = day.isoformat()

        if day >= recheck_from:
            days_to_check.append(day)

        elif day_text in saved_dates:
            continue

        elif (
            endpoint == "unav-pk5l"
            and (endpoint, day_text) in completed
        ):
            continue

        else:
            days_to_check.append(day)

    log(
        f"PSE {endpoint}: "
        f"{len(days_to_check)} dni do pobrania"
    )

    errors = 0
    consecutive_errors = 0

    for day in days_to_check:
        try:
            rows = pse_pages(
                session,
                endpoint,
                day,
            )

            if not rows and endpoint != "unav-pk5l":
                log(
                    f"PSE {endpoint} {day}: "
                    "brak danych, ponowie jutro"
                )
                continue

            if rows:
                df = pd.DataFrame(rows)

                validate_pse(
                    df,
                    endpoint,
                    day,
                )

                update_pse_file(
                    endpoint,
                    day,
                    df,
                )

            save_status(
                endpoint,
                day,
                len(rows),
            )

            log(
                f"PSE {endpoint} {day}: "
                f"{len(rows)} rekordow"
            )

            consecutive_errors = 0

        except (
            requests.RequestException,
            ValueError,
            RuntimeError,
            KeyError,
        ) as exc:
            errors += 1
            consecutive_errors += 1

            log(
                f"BLAD PSE {endpoint} {day}: "
                f"{type(exc).__name__}: {exc}"
            )

            if consecutive_errors >= 3:
                log(
                    "Trzy kolejne bledy PSE. "
                    "Przechodze do kolejnego raportu."
                )
                break

    return errors


def normalize_tge(row):
    result = {}

    for field in TGE_FIELDS:
        value = str(
            row.get(field, "")
        ).strip()

        if field in ("moc", "ubytek"):
            value = str(
                float(value.replace(",", "."))
            )

        elif field in ("poczatek", "koniec"):
            value = pd.to_datetime(
                value,
                errors="raise",
            ).strftime("%Y-%m-%d %H:%M:%S")

        result[field] = value

    return result


def tge_id(row):
    norm = normalize_tge(row)

    content = "\x1f".join(
        norm[field]
        for field in TGE_FIELDS
    )

    return hashlib.sha256(
        content.encode("utf-8")
    ).hexdigest()[:24]


def parse_tge(html, today):
    text = BeautifulSoup(
        html,
        "html.parser",
    ).get_text(" ", strip=True)

    header = text.split(
        "Informacje o ubytku",
        1,
    )[0]

    month_pattern = "|".join(MONTHS)

    pattern = re.compile(
        r"Moc\s+Osiągalna.*?\b("
        + month_pattern
        + r")\s+(20\d{2})\s+"
        r"(?:Pn|Wt|Śr|Cz|Pt|So|N)\s+(\d{1,2})\b",
        re.IGNORECASE | re.DOTALL,
    )

    hit = pattern.search(header)

    if not hit:
        raise ValueError(
            "Nie udalo sie ustalic daty widoku TGE"
        )

    view_date = date(
        int(hit.group(2)),
        MONTHS[hit.group(1).lower()],
        int(hit.group(3)),
    )

    if view_date != today:
        raise ValueError(
            f"TGE zwrocilo widok {view_date}, "
            f"oczekiwano {today}"
        )

    blocks = text.split(
        "Informacje o ubytku"
    )[1:]

    if not blocks:
        raise ValueError(
            "Brak szczegolow zgloszen TGE"
        )

    records = []

    for block in blocks:
        match = TGE_PATTERN.search(block)

        if not match:
            raise ValueError(
                "Nieznany format zgloszenia TGE: "
                + block[:150]
            )

        raw = match.groupdict()

        for field in ("poczatek", "koniec"):
            raw[field] = datetime.strptime(
                raw[field],
                "%d-%m-%Y %H:%M",
            ).strftime("%Y-%m-%d %H:%M:%S")

        row = normalize_tge(raw)
        row["id_wersji"] = tge_id(row)
        row["widok_od"] = today.isoformat()
        row["zrodlo"] = ""

        records.append(row)

    df = pd.DataFrame(records)

    if df.empty:
        return df

    start = pd.Timestamp(today)
    end = start + pd.Timedelta(days=1)

    starts = pd.to_datetime(
        df["poczatek"],
        errors="coerce",
    )

    ends = pd.to_datetime(
        df["koniec"],
        errors="coerce",
    )

    df = df.loc[
        (starts < end)
        & (ends > start)
    ].copy()

    return df


def update_tge_master(new, moment):
    path = ROOT / "tge_ubytki.csv"
    old = read_csv(path)

    new = new.copy()
    new["pierwszy_odczyt_utc"] = moment
    new["ostatni_odczyt_utc"] = moment
    new["pobrano_utc"] = moment

    if not old.empty:
        old = old.copy()

        if "id_wersji" not in old:
            old["id_wersji"] = old.apply(
                tge_id,
                axis=1,
            )

        for col in [
            "pierwszy_odczyt_utc",
            "ostatni_odczyt_utc",
            "pobrano_utc",
        ]:
            if col not in old:
                old[col] = ""

    combined = merge(old, new)

    combined = combined.drop_duplicates(
        "id_wersji",
        keep="last",
    )

    combined = combined.sort_values(
        [
            "poczatek",
            "jednostka",
            "id_wersji",
        ]
    )

    write_csv(path, combined)

    return len(combined)


def save_tge_view(today, count, moment):
    path = ROOT / "tge_widoki.csv"
    old = read_csv(path)

    new = pd.DataFrame([{
        "widok_od": today.isoformat(),
        "widok_do": today.isoformat(),
        "liczba_zgloszen": str(count),
        "pobrano_utc": moment,
    }])

    combined = merge(old, new)

    combined = combined.drop_duplicates(
        "widok_od",
        keep="last",
    )

    write_csv(
        path,
        combined.sort_values("widok_od"),
    )


def save_tge_snapshot(today, events, moment):
    folder = ROOT / "tge_migawki"
    folder.mkdir(
        parents=True,
        exist_ok=True
    )

    path = folder / f"{today:%Y-%m}.csv"
    old = read_csv(path)

    new = events.copy()
    new["data_obserwacji"] = today.isoformat()
    new["moment_odczytu_utc"] = moment

    if not old.empty:
        old = old[
            old["data_obserwacji"]
            != today.isoformat()
        ]

    combined = merge(old, new)

    combined = combined.drop_duplicates(
        ["data_obserwacji", "id_wersji"],
        keep="last",
    )

    write_csv(
        path,
        combined.sort_values(
            ["data_obserwacji", "jednostka"]
        ),
    )


def update_tge(today):
    moment = now_utc()

    try:
        log("TGE: jedno zapytanie do biezacego widoku")

        with requests.Session() as session:
            session.headers.update({
                "User-Agent": "Mozilla/5.0",
                "Accept": "text/html,application/xhtml+xml",
                "Accept-Language": "pl-PL,pl;q=0.9",
            })

            response = session.get(
                TGE_URL,
                timeout=(6, 20),
            )

            response.raise_for_status()

        events = parse_tge(
            response.text,
            today,
        )

        if not events.empty:
            total = update_tge_master(
                events,
                moment,
            )

            save_tge_snapshot(
                today,
                events,
                moment,
            )

            log(
                f"TGE: {len(events)} zgloszen "
                f"aktywnych dzis, "
                f"{total} wersji w archiwum"
            )

        else:
            log(
                "TGE: brak zdarzen aktywnych dzis"
            )

        save_tge_view(
            today,
            len(events),
            moment,
        )

        return 0

    except (
        requests.RequestException,
        ValueError,
        RuntimeError,
        KeyError,
    ) as exc:
        log(
            f"UWAGA TGE: "
            f"{type(exc).__name__}: {exc}"
        )

        log(
            "TGE pominiete. "
            "PSE i analityka beda kontynuowane."
        )

        return 1


def main():
    parser = argparse.ArgumentParser()

    parser.add_argument(
        "--bez-pse",
        action="store_true",
    )

    parser.add_argument(
        "--bez-tge",
        action="store_true",
    )

    args = parser.parse_args()

    today = datetime.now(TZ).date()

    pse_errors = 0
    tge_errors = 0

    log("Aktualizacja:", today)
    log("Repozytorium:", ROOT)

    with make_session() as session:
        if not args.bez_pse:
            for endpoint in [
                "his-bil-mocy",
                "unav-pk5l",
                "pdwkseub",
            ]:
                pse_errors += update_pse(
                    session,
                    endpoint,
                    today,
                )

    if not args.bez_tge:
        tge_errors = update_tge(today)

    state = {
        "data_uruchomienia": today.isoformat(),
        "moment_utc": now_utc(),
        "status": (
            "OK"
            if pse_errors + tge_errors == 0
            else "CZESCIOWO_NIEUDANE"
        ),
        "liczba_bledow": pse_errors + tge_errors,
        "bledy_pse": pse_errors,
        "bledy_tge": tge_errors,
        "pobrano_pse": not args.bez_pse,
        "pobrano_tge": (
            not args.bez_tge
            and tge_errors == 0
        ),
    }

    (ROOT / "stan_aktualizacji.json").write_text(
        json.dumps(
            state,
            ensure_ascii=False,
            indent=2,
        ) + "\n",
        encoding="utf-8",
    )

    log(
        "Koniec. Bledy PSE:",
        pse_errors,
        "| Bledy TGE:",
        tge_errors,
    )

    return 1 if pse_errors else 0


if __name__ == "__main__":
    sys.exit(main())
