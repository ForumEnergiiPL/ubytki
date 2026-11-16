
from __future__ import annotations

import argparse
import hashlib
import re
import sys
import time
from datetime import date, datetime, timedelta, timezone
import json
from pathlib import Path
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

import pandas as pd
import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT
PSE_MONTHS = DATA / "pse_jednostki"
TGE_MONTHS = DATA / "tge_migawki"
PSE_API = "https://api.raporty.pse.pl/api/"
TGE_URL = "https://gpi.tge.pl/pl/zestawienie-ubytkow"
WARSAW = ZoneInfo("Europe/Warsaw")
FIRST_DATE = date(2026, 11, 9)
PSE_RECHECK_DAYS = 10
TGE_BACK = 2
TGE_FORWARD = 5
PSE_PAGE_SIZE = 1200

PSE_FILES = {
    "his-bil-mocy": "pse_bilans.csv",
    "unav-pk5l": "pse_remonty.csv",
}

TGE_KEYS = [
    "jednostka", "moc", "ubytek",
    "typ", "poczatek", "koniec"
]

MONTHS_PL = {
    "styczeń": 1, "luty": 2, "marzec": 3,
    "kwiecień": 4, "maj": 5, "czerwiec": 6,
    "lipiec": 7, "sierpień": 8, "wrzesień": 9,
    "październik": 10, "listopad": 11, "grudzień": 12,
}

DETAIL_RE = re.compile(
    r"Jednostka\s+(?P<jednostka>.*?)\s+"
    r"Moc Osiągalna:\s*(?P<moc>[\d.,]+)\s*MW\s+"
    r"Wielkość ubytku:\s*(?P<ubytek>[\d.,]+)\s*MW\s+"
    r"Typ zdarzenia:\s*(?P<typ>Planowane|Nieplanowane)\s+"
    r"Początek zdarzenia:\s*(?P<poczatek>\d{2}-\d{2}-\d{4}\s+\d{2}:\d{2})\s+"
    r"Koniec zdarzenia:\s*(?P<koniec>\d{2}-\d{2}-\d{4}\s+\d{2}:\d{2})",
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
        low_memory=False
    )


def write_csv(path, frame):
    path.parent.mkdir(
        parents=True,
        exist_ok=True
    )

    temporary = path.with_name(
        path.name + ".tmp"
    )

    frame.to_csv(
        temporary,
        sep=";",
        index=False,
        encoding="utf-8-sig"
    )

    temporary.replace(path)


def merge_columns(old, new):
    return pd.concat(
        [old, new],
        ignore_index=True
    ).fillna("")


def numeric(value):
    try:
        return float(
            str(value).replace(",", ".")
        )
    except (TypeError, ValueError):
        return float("nan")


def normalized_event(row):
    result = {}

    for col in TGE_KEYS:
        value = str(
            row.get(col, "")
        ).strip()

        if col in ("moc", "ubytek"):
            value = str(
                float(value.replace(",", "."))
            )

        if col in ("poczatek", "koniec"):
            ts = pd.to_datetime(
                value,
                errors="coerce",
                dayfirst=False
            )

            if pd.isna(ts):
                raise ValueError(
                    f"Nieprawidlowa data TGE {value!r}"
                )

            value = ts.strftime(
                "%Y-%m-%d %H:%M:%S"
            )

        result[col] = value

    if (
        not result["jednostka"]
        or result["typ"].casefold()
        not in {"planowane", "nieplanowane"}
    ):
        raise ValueError(
            f"Niepoprawny rekord TGE: {result!r}"
        )

    if (
        numeric(result["ubytek"]) < 0
        or numeric(result["moc"]) <= 0
    ):
        raise ValueError(
            "Ubytek lub moc TGE poza zakresem"
        )

    if result["poczatek"] >= result["koniec"]:
        raise ValueError(
            "Koniec ubytku przed poczatkiem"
        )

    return result


def event_hash(row):
    norm = normalized_event(row)

    serialized = "\x1f".join(
        norm[col] for col in TGE_KEYS
    )

    return hashlib.sha256(
        serialized.encode("utf-8")
    ).hexdigest()[:24]


def make_session():
    session = requests.Session()

    session.headers.update({
        "User-Agent": (
            "Mozilla/5.0 "
            "(compatible; public-energy-data-archive/1.0)"
        ),
        "Accept": "application/json,text/html;q=0.9,*/*"
    })

    retry = Retry(
        total=3,
        connect=3,
        read=2,
        backoff_factor=1,
        status_forcelist=[
            429, 500, 502, 503, 504
        ],
        allowed_methods=["GET"]
    )

    session.mount(
        "https://",
        HTTPAdapter(max_retries=retry)
    )

    return session


def get_pages(session, endpoint, day):
    url = PSE_API + endpoint

    params = {
        "$filter": (
            f"business_date eq '{day.isoformat()}'"
        ),
        "$first": PSE_PAGE_SIZE
    }

    visited = set()
    output = []

    while True:
        response = session.get(
            url,
            params=params,
            timeout=(12, 45)
        )

        response.raise_for_status()

        if (
            response.url in visited
            or len(visited) > 1000
        ):
            raise RuntimeError(
                f"Bledna paginacja w {endpoint}"
            )

        visited.add(response.url)

        payload = response.json()

        if (
            not isinstance(payload, dict)
            or not isinstance(payload.get("value"), list)
        ):
            raise ValueError(
                f"Nieoczekiwany JSON {endpoint}: "
                f"{str(payload)[:150]}"
            )

        batch = payload["value"]

        if not all(
            isinstance(row, dict)
            for row in batch
        ):
            raise ValueError(
                f"Rekordy {endpoint} nie sa obiektami"
            )

        output.extend(batch)

        next_url = (
            payload.get("nextLink")
            or payload.get("@odata.nextLink")
        )

        if not next_url:
            if len(batch) >= PSE_PAGE_SIZE:
                raise RuntimeError(
                    f"{endpoint}: pelna strona bez nextLink; "
                    "nie uznaje za kompletne"
                )

            return output

        url = urljoin(
            response.url,
            next_url
        )

        params = None


def status_days():
    status = read_csv(
        DATA / "pse_status.csv"
    )

    if (
        status.empty
        or not {"endpoint", "data"}.issubset(
            status.columns
        )
    ):
        return set()

    return set(
        zip(
            status["endpoint"],
            status["data"]
        )
    )


def mark_status(endpoint, day, amount):
    path = DATA / "pse_status.csv"

    previous = read_csv(path)

    record = pd.DataFrame([{
        "endpoint": endpoint,
        "data": day.isoformat(),
        "liczba": str(amount),
        "pobrano_utc": now_utc()
    }])

    result = merge_columns(
        previous,
        record
    )

    result = result.drop_duplicates(
        ["endpoint", "data"],
        keep="last"
    )

    write_csv(
        path,
        result.sort_values(
            ["endpoint", "data"]
        )
    )


def validate_pse_day(new, endpoint, day):
    if (
        "business_date" not in new
        or not new["business_date"]
        .astype(str)
        .str[:10]
        .eq(day.isoformat())
        .all()
    ):
        raise ValueError(
            f"{endpoint}: bledna data business_date"
        )

    expected = {
        "his-bil-mocy": [
            "peak",
            "peak_hour",
            "ub_sum"
        ],
        "pdwkseub": [
            "dtime_utc",
            "resource_code",
            "non_us_cap",
            "grid_lim"
        ],
        "unav-pk5l": [
            "unit_code",
            "start_dtime",
            "end_dtime",
            "reason",
            "state"
        ],
    }[endpoint]

    missing = [
        c for c in expected
        if c not in new
    ]

    if missing:
        raise ValueError(
            f"{endpoint}: brakuje kolumn {missing}"
        )

    if new[expected].isna().any().any():
        raise ValueError(
            f"{endpoint}: puste pola wymagane"
        )


def update_pse_file(endpoint, day, new):
    new = new.copy()

    new["pobrano_utc"] = now_utc()

    if endpoint == "pdwkseub":
        path = DATA / "pse_jednostki.csv"

        old = read_csv(path)

        if not old.empty:
            old = old[
                old["business_date"]
                .astype(str)
                .str[:10] != day.isoformat()
            ]

        df = merge_columns(old, new)

        df = df.sort_values([
            "business_date",
            "dtime_utc",
            "resource_code"
        ])

        df = df.drop_duplicates(
            ["dtime_utc", "resource_code"],
            keep="last"
        )

    elif endpoint == "his-bil-mocy":
        path = DATA / PSE_FILES[endpoint]

        old = read_csv(path)

        if not old.empty:
            old = old[
                old["business_date"]
                .astype(str)
                .str[:10] != day.isoformat()
            ]

        df = merge_columns(old, new)

        df = df.drop_duplicates(
            ["business_date", "peak"],
            keep="last"
        )

        df = df.sort_values([
            "business_date",
            "peak"
        ])

    else:
        path = DATA / PSE_FILES[endpoint]
        old = read_csv(path)

        df = merge_columns(old, new)

        preferred = [
            "mrid_zas",
            "publication_ts_utc",
            "start_dtime",
            "end_dtime",
            "reason",
            "state",
            "direction"
        ]

        if "mrid_zas" not in df.columns:
            preferred = [
                "unit_code",
                "publication_ts",
                "start_dtime",
                "end_dtime",
                "reason",
                "state",
                "direction"
            ]

        key = [
            col for col in preferred
            if col in df.columns
        ]

        df = df.drop_duplicates(
            key,
            keep="last"
        )

        df = df.sort_values([
            "business_date",
            "unit_code"
        ])

    write_csv(path, df)
    return len(df)


def update_pse(session, endpoint, today):
    end = (
        today
        if endpoint == "unav-pk5l"
        else today - timedelta(days=1)
    )

    if end < FIRST_DATE:
        return 0

    completed = status_days()

    source_path = DATA / (
        "pse_jednostki.csv"
        if endpoint == "pdwkseub"
        else PSE_FILES[endpoint]
    )

    saved = read_csv(source_path)

    saved_dates = (
        set(
            saved["business_date"]
            .astype(str)
            .str[:10]
        )
        if not saved.empty
        and "business_date" in saved
        else set()
    )

    recheck = today - timedelta(
        days=PSE_RECHECK_DAYS
    )

    days = [
        FIRST_DATE + timedelta(days=i)
        for i in range(
            (end - FIRST_DATE).days + 1
        )
    ]

    days = [
        day for day in days
        if (
            day >= recheck
            or (
                day.isoformat() not in saved_dates
                and not (
                    endpoint == "unav-pk5l"
                    and (
                        endpoint,
                        day.isoformat()
                    ) in completed
                )
            )
        )
    ]

    log(
        f"PSE {endpoint}: "
        f"{len(days)} dni do sprawdzenia"
    )

    failures = 0
    consecutive = 0

    for day in days:
        try:
            records = get_pages(
                session,
                endpoint,
                day
            )

            if (
                not records
                and endpoint != "unav-pk5l"
            ):
                log(
                    f"  {day}: 0 rekordow, "
                    "ponowie probe jutro"
                )
                continue

            if records:
                frame = pd.DataFrame(records)

                validate_pse_day(
                    frame,
                    endpoint,
                    day
                )

                update_pse_file(
                    endpoint,
                    day,
                    frame
                )

            mark_status(
                endpoint,
                day,
                len(records)
            )

            log(
                f"  {day}: {len(records)} "
                "rekordow, zapisano"
            )

            consecutive = 0

        except (
            requests.RequestException,
            ValueError,
            RuntimeError,
            KeyError
        ) as exc:
            failures += 1
            consecutive += 1

            log(
                f"  BLAD {endpoint} {day}: "
                f"{type(exc).__name__}: {exc}"
            )

            if consecutive >= 3:
                log(
                    "  Trzy bledy z rzedu: "
                    "koncze to zrodlo; "
                    "zalegle dni zostana ponowione"
                )
                break

    return failures


def tge_params(today, index):
    if index == 0:
        return None

    anchor = today + timedelta(
        days=14 * (
            index - 1 if index > 0
            else index + 1
        )
    )

    return {
        "_gpicalendar_WAR_gpicalendarportlet_current":
            anchor.isoformat(),
        "_gpicalendar_WAR_gpicalendarportlet_direction":
            "1" if index > 0 else "-1",
        "p_p_id":
            "gpicalendar_WAR_gpicalendarportlet",
        "p_p_lifecycle": "0",
        "p_p_mode": "view",
        "p_p_state": "normal",
        "p_p_col_id": "column-1",
        "p_p_col_count": "1"
    }


def validate_tge_header(text, expected):
    prefix = text.split(
        "Informacje o ubytku",
        1
    )[0]

    pattern = re.compile(
        r"Moc\s+Osiągalna.*?\b("
        + "|".join(MONTHS_PL)
        + r")\s+(20\d{2})\s+"
        r"(?:Pn|Wt|Śr|Cz|Pt|So|N)\s+(\d{1,2})\b",
        re.IGNORECASE | re.DOTALL
    )

    hit = pattern.search(prefix)

    if not hit:
        raise ValueError(
            "Nie odczytano pelnego naglowka "
            "dat TGE; nie oznaczam widoku "
            "jako poprawny"
        )

    found = date(
        int(hit.group(2)),
        MONTHS_PL[hit.group(1).lower()],
        int(hit.group(3))
    )

    if found != expected:
        raise ValueError(
            f"TGE zwrocilo widok {found} "
            f"zamiast {expected}"
        )


def parse_tge_page(html, expected):
    text = BeautifulSoup(
        html,
        "html.parser"
    ).get_text(
        " ",
        strip=True
    )

    validate_tge_header(
        text,
        expected
    )

    parts = text.split(
        "Informacje o ubytku"
    )[1:]

    if not parts:
        raise ValueError(
            "TGE nie zwrocilo szczegolowych ubytkow"
        )

    rows = []

    for part in parts:
        hit = DETAIL_RE.search(part)

        if hit is None:
            raise ValueError(
                f"Nieznany format zdarzenia TGE: "
                f"{part[:160]}"
            )

        raw = hit.groupdict()

        for field in ("poczatek", "koniec"):
            raw[field] = datetime.strptime(
                raw[field],
                "%d-%m-%Y %H:%M"
            ).strftime(
                "%Y-%m-%d %H:%M:%S"
            )

        normalized = normalized_event(raw)

        normalized["widok_od"] = (
            expected.isoformat()
        )

        normalized["zrodlo"] = ""
        normalized["id_wersji"] = (
            event_hash(normalized)
        )

        rows.append(normalized)

    return pd.DataFrame(rows)


def update_tge_master(new, now):
    path = DATA / "tge_ubytki.csv"

    old = read_csv(path)

    fresh = new.copy()

    fresh["pierwszy_odczyt_utc"] = now
    fresh["ostatni_odczyt_utc"] = now
    fresh["pobrano_utc"] = now

    if not old.empty:
        old = old.copy()

        if "id_wersji" not in old:
            old["id_wersji"] = old.apply(
                event_hash,
                axis=1
            )

        for col in (
            "pierwszy_odczyt_utc",
            "ostatni_odczyt_utc",
            "pobrano_utc"
        ):
            if col not in old:
                old[col] = ""

        old["pierwszy_odczyt_utc"] = (
            old["pierwszy_odczyt_utc"].where(
                old["pierwszy_odczyt_utc"] != "",
                old["pobrano_utc"]
            )
        )

    both = merge_columns(
        old,
        fresh
    )

    grouped_first = (
        both["pierwszy_odczyt_utc"]
        .astype(str)
        .replace("", pd.NA)
        .groupby(both["id_wersji"])
        .min()
    )

    both = both.sort_values(
        "ostatni_odczyt_utc",
        kind="stable"
    )

    both = both.drop_duplicates(
        "id_wersji",
        keep="last"
    )

    both["pierwszy_odczyt_utc"] = (
        both["id_wersji"]
        .map(grouped_first)
        .fillna("")
    )

    both = both.sort_values([
        "poczatek",
        "jednostka",
        "id_wersji"
    ])

    write_csv(path, both)

    return len(both)


def update_tge_view(expected, amount, fetched):
    path = DATA / "tge_widoki.csv"

    old = read_csv(path)

    new = pd.DataFrame([{
        "widok_od": expected.isoformat(),
        "widok_do": (
            expected + timedelta(days=13)
        ).isoformat(),
        "liczba_zgloszen": str(amount),
        "pobrano_utc": fetched
    }])

    combined = merge_columns(
        old,
        new
    )

    combined = combined.drop_duplicates(
        "widok_od",
        keep="last"
    )

    write_csv(
        path,
        combined.sort_values("widok_od")
    )


def save_tge_snapshot(all_views, today, fetched):
    if not all_views:
        return

    path = TGE_MONTHS / f"{today:%Y-%m}.csv"

    old = read_csv(path)

    new = pd.concat(
        all_views,
        ignore_index=True
    ).drop_duplicates("id_wersji")

    new["data_obserwacji"] = (
        today.isoformat()
    )

    new["moment_odczytu_utc"] = fetched

    if (
        not old.empty
        and "data_obserwacji" in old
    ):
        old = old[
            old["data_obserwacji"]
            != today.isoformat()
        ]

    df = merge_columns(
        old,
        new
    ).drop_duplicates(
        ["data_obserwacji", "id_wersji"],
        keep="last"
    )

    write_csv(
        path,
        df.sort_values([
            "data_obserwacji",
            "jednostka",
            "id_wersji"
        ])
    )


def update_tge(session, today):
    all_views = []
    failures = 0
    current = now_utc()

    for i in range(
        -TGE_BACK,
        TGE_FORWARD + 1
    ):
        expected = today + timedelta(
            days=14 * i
        )

        try:
            response = session.get(
                TGE_URL,
                params=tge_params(today, i),
                timeout=(12, 50)
            )

            response.raise_for_status()

            new = parse_tge_page(
                response.text,
                expected
            )

            n = update_tge_master(
                new,
                current
            )

            update_tge_view(
                expected,
                len(new),
                current
            )

            all_views.append(new)

            log(
                f"TGE {expected}: {len(new)} "
                f"wpisow; archiwum {n} wersji"
            )

        except (
            requests.RequestException,
            ValueError,
            RuntimeError,
            KeyError
        ) as exc:
            failures += 1

            log(
                f"BLAD TGE {expected}: "
                f"{type(exc).__name__}: {exc}"
            )

        time.sleep(0.2)

    if failures == 0:
        save_tge_snapshot(
            all_views,
            today,
            current
        )

        log(
            f"TGE: zapisana dzienna "
            f"migawka {today}"
        )

    else:
        log(
            "TGE: migawki dnia nie zapisano, "
            "bo nie pobrano wszystkich widokow"
        )

    return failures


def month_files():
    return sorted(
        PSE_MONTHS.glob("????-??.csv")
    )


def load_pse_jw():
    frames = [
        read_csv(p)
        for p in month_files()
    ]

    return (
        pd.concat(
            frames,
            ignore_index=True
        )
        if frames else pd.DataFrame()
    )


def parse_time(value):
    try:
        stamp = pd.to_datetime(
            value,
            errors="coerce"
        )

        if pd.isna(stamp):
            return pd.NaT

        if stamp.tzinfo is not None:
            return (
                stamp.tz_convert(WARSAW)
                .tz_localize(None)
            )

        return stamp

    except (ValueError, TypeError):
        return pd.NaT


def tge_at(tge, moment):
    if tge.empty:
        return 0.0, 0.0, 0.0, 0

    active = tge[
        (tge["_start"] <= moment)
        & (tge["_end"] > moment)
    ].copy()

    if active.empty:
        return 0.0, 0.0, 0.0, 0

    active = active.sort_values(
        "_mw",
        ascending=False
    ).drop_duplicates("jednostka")

    classification = active[
        "typ"
    ].str.casefold()

    planned = active.loc[
        classification == "planowane",
        "_mw"
    ].sum()

    unplanned = active.loc[
        classification == "nieplanowane",
        "_mw"
    ].sum()

    others = active.loc[
        ~classification.isin([
            "planowane",
            "nieplanowane"
        ]),
        "_mw"
    ].sum()

    return (
        float(planned),
        float(unplanned),
        float(others),
        len(active)
    )


def summed(row, cols):
    vals = [
        numeric(row.get(col, ""))
        for col in cols
    ]

    return (
        sum(vals)
        if all(pd.notna(v) for v in vals)
        else float("nan")
    )


def read_tge_snapshots():
    parts = [
        read_csv(p)
        for p in sorted(
            TGE_MONTHS.glob("????-??.csv")
        )
    ]

    return (
        pd.concat(
            parts,
            ignore_index=True
        )
        if parts else pd.DataFrame()
    )


def comparison():
    path = DATA / "porownanie_pse_tge.csv"

    pse = read_csv(
        DATA / "pse_bilans.csv"
    )

    tge = read_csv(
        DATA / "tge_ubytki.csv"
    )

    views = read_csv(
        DATA / "tge_widoki.csv"
    )

    snapshots = read_tge_snapshots()

    if pse.empty or "peak_hour" not in pse:
        log(
            "Porownanie: brak bilansu PSE; "
            "nie nadpisuje dotychczasowego pliku"
        )
        return

    if "publication_ts_utc" in pse:
        pub = pd.to_datetime(
            pse["publication_ts_utc"].replace(
                "",
                pd.NA
            ),
            utc=True,
            errors="coerce"
        )

        fallback = pd.to_datetime(
            pse.get(
                "publication_ts",
                pd.Series("", index=pse.index)
            ),
            errors="coerce"
        )

        pse["_pub"] = (
            pub.dt.tz_localize(None)
            .fillna(fallback)
        )

        pse = pse.sort_values(
            "_pub",
            kind="stable"
        ).drop(columns="_pub")

    pse = pse.drop_duplicates(
        ["business_date", "peak"],
        keep="last"
    )

    if not tge.empty:
        tge["_start"] = pd.to_datetime(
            tge["poczatek"],
            errors="coerce",
            format="mixed"
        )

        tge["_end"] = pd.to_datetime(
            tge["koniec"],
            errors="coerce",
            format="mixed"
        )

        tge["_mw"] = pd.to_numeric(
            tge["ubytek"].str.replace(
                ",",
                ".",
                regex=False
            ),
            errors="coerce"
        )

        tge = tge.dropna(
            subset=[
                "_start",
                "_end",
                "_mw"
            ]
        )

    coverage = set()

    if not views.empty:
        for raw in views["widok_od"]:
            try:
                start = date.fromisoformat(
                    str(raw)[:10]
                )

                coverage.update(
                    start + timedelta(days=i)
                    for i in range(14)
                )

            except ValueError:
                pass

    snapshot_by_day = {}

    if (
        not snapshots.empty
        and "data_obserwacji" in snapshots
    ):
        for day, group in snapshots.groupby(
            "data_obserwacji"
        ):
            sub = group.copy()

            sub["_start"] = pd.to_datetime(
                sub["poczatek"],
                errors="coerce",
                format="mixed"
            )

            sub["_end"] = pd.to_datetime(
                sub["koniec"],
                errors="coerce",
                format="mixed"
            )

            sub["_mw"] = pd.to_numeric(
                sub["ubytek"].str.replace(
                    ",",
                    ".",
                    regex=False
                ),
                errors="coerce"
            )

            snapshot_by_day[day] = sub.dropna(
                subset=[
                    "_start",
                    "_end",
                    "_mw"
                ]
            )

    rows = []

    for _, row in pse.iterrows():
        day = str(
            row["business_date"]
        )[:10]

        moment = parse_time(
            row.get("peak_hour", "")
        )

        if (
            pd.notna(moment)
            and moment.strftime("%Y-%m-%d") != day
        ):
            status = "NIEZGODNA_DATA_SZCZYTU"

        elif pd.isna(moment):
            status = "BRAK_GODZINY_PSE"

        elif moment.date() not in coverage:
            status = "BRAK_WIDOKU_TGE"

        else:
            status = (
                "OK_MIGAWKA_TGE"
                if day in snapshot_by_day
                else "ARCHIWALNY_WIDOK_TGE"
            )

        plan = unplan = others = tge_sum = (
            float("nan")
        )

        count = ""

        if status in (
            "OK_MIGAWKA_TGE",
            "ARCHIWALNY_WIDOK_TGE"
        ):
            source = snapshot_by_day.get(
                day,
                tge
            )

            plan, unplan, others, count = tge_at(
                source,
                moment
            )

            tge_sum = (
                plan + unplan + others
            )

        rem = summed(
            row,
            [
                "rk_jg", "rk_prb",
                "rs_jg", "rs_prb",
                "rb_jg", "rb_prb"
            ]
        )

        aw = summed(
            row,
            ["ra_jg", "ra_prb"]
        )

        pse_sum = numeric(
            row.get("ub_sum", "")
        )

        rows.append({
            "data": day,
            "szczyt": row.get("peak", ""),
            "czas_szczytu": (
                moment.strftime("%Y-%m-%d %H:%M:%S")
                if pd.notna(moment)
                else ""
            ),
            "pse_ubytki_ogolem_mw": pse_sum,
            "pse_remonty_mw": rem,
            "pse_awarie_mw": aw,
            "pse_remonty_awarie_mw": rem + aw,
            "pse_eksploatacyjne_mw": summed(
                row,
                ["we_jg", "we_prb"]
            ),
            "pse_cieplownictwo_mw": summed(
                row,
                ["ciep_jg", "ciep_prb"]
            ),
            "pse_inwestycyjne_mw": summed(
                row,
                ["inw_jg", "inw_prb"]
            ),
            "tge_ubytki_ogolem_mw": tge_sum,
            "tge_planowane_mw": plan,
            "tge_nieplanowane_mw": unplan,
            "tge_inne_mw": others,
            "tge_liczba_jednostek": count,
            "roznica_pse_minus_tge_mw": (
                pse_sum - tge_sum
            ),
            "roznica_remonty_awarie_minus_tge_mw":
                rem + aw - tge_sum,
            "status": status
        })

    df = pd.DataFrame(rows).sort_values(
        ["data", "czas_szczytu"]
    )

    write_csv(path, df)

    daily = []

    for day, group in df.groupby("data"):
        result = {"data": day}

        for _, item in group.iterrows():
            peak = (
                item["szczyt"]
                if item["szczyt"] in ("SR", "SW")
                else "INNY"
            )

            for col in (
                "pse_ubytki_ogolem_mw",
                "pse_remonty_awarie_mw",
                "tge_ubytki_ogolem_mw",
                "tge_planowane_mw",
                "tge_nieplanowane_mw",
                "status"
            ):
                result[f"{col}_{peak}"] = item[col]

        daily.append(result)

    write_csv(
        DATA / "agregacja_dzienna.csv",
        pd.DataFrame(daily).sort_values("data")
    )

    log(
        f"Porownanie: {len(df)} wierszy, "
        "OK z migawka: "
        f"{(df['status'] == 'OK_MIGAWKA_TGE').sum()}"
    )


def observed_pse_events():
    frame = load_pse_jw()

    required = {
        "dtime_utc",
        "resource_code",
        "non_us_cap",
        "grid_lim"
    }

    if (
        frame.empty
        or not required.issubset(frame)
    ):
        return []

    frame["_ts"] = pd.to_datetime(
        frame["dtime_utc"],
        utc=True,
        errors="coerce"
    )

    frame = frame.dropna(
        subset=["_ts"]
    )

    if "publication_ts_utc" in frame:
        frame["_pub"] = pd.to_datetime(
            frame["publication_ts_utc"],
            utc=True,
            errors="coerce"
        )

        frame = frame.sort_values(
            "_pub",
            kind="stable"
        )

    frame = frame.drop_duplicates(
        ["resource_code", "_ts"],
        keep="last"
    )

    frame = frame.sort_values([
        "resource_code",
        "_ts"
    ])

    events = []

    for unit, chunk in frame.groupby(
        "resource_code",
        sort=False
    ):
        plant = (
            str(chunk["power_plant"].iloc[-1])
            if "power_plant" in chunk
            else ""
        )

        for col, cause in (
            (
                "non_us_cap",
                "ubytek elektrowniany; przyczyna nieznana"
            ),
            (
                "grid_lim",
                "ograniczenie sieciowe"
            )
        ):
            segment = None

            def add_segment():
                if segment is not None:
                    events.append({
                        "zrodlo": "PSE pdwkseub",
                        "jednostka": plant,
                        "kod_jednostki": unit,
                        "poczatek": segment[0].isoformat(),
                        "koniec": segment[1].isoformat(),
                        "ubytek_mw": round(segment[2], 3),
                        "klasyfikacja": "obserwowany ubytek",
                        "przyczyna": cause,
                        "status": (
                            "przedzial obserwacji, "
                            "nie potwierdzone daty awarii"
                        ),
                        "czas": "UTC"
                    })

            for _, row in chunk.iterrows():
                ts = row["_ts"]
                mw = numeric(
                    row.get(col, "")
                )

                if pd.isna(mw) or mw <= 0:
                    add_segment()
                    segment = None
                    continue

                start = ts - pd.Timedelta(
                    minutes=15
                )

                if segment is None:
                    segment = [
                        start,
                        ts,
                        mw
                    ]

                elif start == segment[1]:
                    segment[1] = ts
                    segment[2] = max(
                        segment[2],
                        mw
                    )

                else:
                    add_segment()
                    segment = [
                        start,
                        ts,
                        mw
                    ]

            add_segment()

    return events


def rebuild_events():
    tge = read_csv(
        DATA / "tge_ubytki.csv"
    )

    rem = read_csv(
        DATA / "pse_remonty.csv"
    )

    result = []

    for row in tge.to_dict("records"):
        result.append({
            "zrodlo": "TGE",
            "jednostka": row.get("jednostka", ""),
            "kod_jednostki": "",
            "poczatek": row.get("poczatek", ""),
            "koniec": row.get("koniec", ""),
            "ubytek_mw": row.get("ubytek", ""),
            "klasyfikacja": row.get("typ", ""),
            "przyczyna": "",
            "status": "wersja zgloszenia",
            "czas": "Europe/Warsaw"
        })

    if not rem.empty:
        if "publication_ts_utc" in rem:
            rem = rem.sort_values(
                "publication_ts_utc",
                kind="stable"
            )

        event_key = [
            c for c in (
                "unit_code",
                "start_dtime",
                "end_dtime",
                "reason",
                "state"
            )
            if c in rem
        ]

        rem = rem.drop_duplicates(
            event_key,
            keep="last"
        )

        for row in rem.to_dict("records"):
            result.append({
                "zrodlo": "PSE unav-pk5l",
                "jednostka": row.get(
                    "power_plant",
                    ""
                ),
                "kod_jednostki": row.get(
                    "unit_code",
                    ""
                ),
                "poczatek": row.get(
                    "start_dtime",
                    ""
                ),
                "koniec": row.get(
                    "end_dtime",
                    ""
                ),
                "ubytek_mw": "",
                "klasyfikacja": "planowany postoj",
                "przyczyna": row.get(
                    "reason",
                    ""
                ),
                "status": row.get("state", ""),
                "czas": "Europe/Warsaw"
            })

    result.extend(
        observed_pse_events()
    )

    if result:
        df = pd.DataFrame(
            result
        ).fillna("").drop_duplicates()

        write_csv(
            DATA / "zdarzenia_jednostek.csv",
            df.sort_values([
                "poczatek",
                "jednostka"
            ])
        )

        log(
            f"Zdarzenia jednostek: "
            f"{len(df)} wpisow"
        )


def main():
    parser = argparse.ArgumentParser()

    parser.add_argument(
        "--bez-pse",
        action="store_true"
    )

    parser.add_argument(
        "--bez-tge",
        action="store_true"
    )

    args = parser.parse_args()

    DATA.mkdir(
        parents=True,
        exist_ok=True
    )

    today = datetime.now(
        WARSAW
    ).date()

    errors = 0

    log(
        "Pobieranie PSE/TGE:",
        today,
        "folder",
        DATA
    )

    if not args.bez_pse or not args.bez_tge:
        with make_session() as session:
            if not args.bez_pse:
                for endpoint in (
                    "his-bil-mocy",
                    "unav-pk5l",
                    "pdwkseub"
                ):
                    errors += update_pse(
                        session,
                        endpoint,
                        today
                    )

            if not args.bez_tge:
                errors += update_tge(
                    session,
                    today
                )

    state = {
        "data_uruchomienia": today.isoformat(),
        "moment_utc": now_utc(),
        "status": (
            "OK"
            if errors == 0
            else "CZESCIOWO_NIEUDANE"
        ),
        "liczba_bledow": errors,
        "pobrano_pse": not args.bez_pse,
        "pobrano_tge": not args.bez_tge
    }

    (
        DATA / "stan_aktualizacji.json"
    ).write_text(
        json.dumps(
            state,
            ensure_ascii=False,
            indent=2
        ) + "\n",
        encoding="utf-8"
    )

    log(
        "Koniec pobierania; bledy:",
        errors
    )

    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
