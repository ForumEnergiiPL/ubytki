
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

import json
import pandas as pd
import requests

from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


ROOT = Path(__file__).resolve().parents[1]

API = "https://api.raporty.pse.pl/api/"

TODAY = datetime.now(
    ZoneInfo("Europe/Warsaw")
).date()

FIRST_DATE = date(2026, 11, 9)
RECHECK_DAYS = 10

TARGETS = {
    "his-bil-mocy": (
        "pse_bilans.csv",
        ["business_date", "peak"]
    ),

    "pdwkseub": (
        "pse_jednostki.csv",
        ["resource_code", "dtime_utc"]
    ),

    "unav-pk5l": (
        "pse_remonty.csv",
        [
            "unit_code",
            "start_dtime",
            "end_dtime",
            "reason",
            "state",
            "publication_ts_utc"
        ]
    )
}


def read(name):
    path = ROOT / name

    if not path.exists() or path.stat().st_size == 0:
        return pd.DataFrame()

    return pd.read_csv(
        path,
        sep=";",
        dtype=str,
        keep_default_na=False,
        encoding="utf-8-sig"
    )


def save(name, df):
    target = ROOT / name
    temp = target.with_name(
        target.name + ".tmp"
    )

    df.to_csv(
        temp,
        sep=";",
        index=False,
        encoding="utf-8-sig"
    )

    temp.replace(target)


def session_new():
    s = requests.Session()

    s.headers.update({
        "User-Agent": "Mozilla/5.0",
        "Accept": "application/json"
    })

    retries = Retry(
        total=2,
        backoff_factor=1,
        status_forcelist=[
            429, 500, 502, 503, 504
        ]
    )

    s.mount(
        "https://",
        HTTPAdapter(max_retries=retries)
    )

    return s


def fetch(session, endpoint, day):
    url = API + endpoint

    params = {
        "$filter": (
            f"business_date eq '{day.isoformat()}'"
        ),
        "$first": 1000
    }

    seen = set()
    result = []

    while True:
        response = session.get(
            url,
            params=params,
            timeout=(12, 45)
        )

        response.raise_for_status()

        if response.url in seen:
            raise RuntimeError(
                "Zapętlona paginacja PSE"
            )

        seen.add(response.url)

        if len(seen) > 1000:
            raise RuntimeError(
                "Za dużo stron API PSE"
            )

        body = response.json()

        if (
            not isinstance(body, dict)
            or not isinstance(body.get("value"), list)
        ):
            raise ValueError(
                "Nieznana odpowiedź PSE"
            )

        result += body["value"]

        next_url = (
            body.get("nextLink")
            or body.get("@odata.nextLink")
        )

        if not next_url:
            if len(body["value"]) >= 1000:
                raise RuntimeError(
                    "Pełna strona bez nextLink"
                )

            return result

        url = urljoin(response.url, next_url)
        params = None


def update_source(session, endpoint):
    name, key = TARGETS[endpoint]

    old = read(name)
    status = read("pse_status.csv")

    completed = (
        set(zip(status["endpoint"], status["data"]))
        if {"endpoint", "data"}.issubset(status)
        else set()
    )

    present = (
        set(
            old["business_date"]
            .astype(str)
            .str[:10]
        )
        if "business_date" in old
        else set()
    )

    last = (
        TODAY
        if endpoint == "unav-pk5l"
        else TODAY - timedelta(days=1)
    )

    if last < FIRST_DATE:
        return 0

    failures = 0

    for i in range(
        (last - FIRST_DATE).days + 1
    ):
        day = FIRST_DATE + timedelta(days=i)
        d = day.isoformat()

        # Ostatnie dni pobieramy ponownie,
        # aby uwzględniać korekty.
        if day < TODAY - timedelta(
            days=RECHECK_DAYS
        ):
            if d in present or (
                endpoint == "unav-pk5l"
                and (endpoint, d) in completed
            ):
                continue

        try:
            records = fetch(
                session,
                endpoint,
                day
            )

            if not records and endpoint != "unav-pk5l":
                print(
                    f"{endpoint} {d}: brak danych, "
                    "ponowię później",
                    flush=True
                )
                continue

            if records:
                new = pd.DataFrame(
                    records
                ).fillna("")

                required = {
                    "his-bil-mocy": {
                        "business_date",
                        "peak",
                        "peak_hour",
                        "ub_sum"
                    },

                    "pdwkseub": {
                        "business_date",
                        "resource_code",
                        "dtime_utc",
                        "non_us_cap",
                        "grid_lim"
                    },

                    "unav-pk5l": {
                        "business_date",
                        "unit_code",
                        "start_dtime",
                        "end_dtime",
                        "reason",
                        "state"
                    }
                }[endpoint]

                if (
                    not required.issubset(new)
                    or not new["business_date"]
                    .astype(str)
                    .str[:10]
                    .eq(d)
                    .all()
                ):
                    raise ValueError(
                        f"Niepoprawne dane PSE: {endpoint}"
                    )

                new["pobrano_utc"] = (
                    datetime.now(
                        timezone.utc
                    ).isoformat(timespec="seconds")
                )

                if (
                    endpoint != "unav-pk5l"
                    and not old.empty
                ):
                    old = old[
                        old["business_date"]
                        .astype(str)
                        .str[:10] != d
                    ]

                old = pd.concat(
                    [old, new],
                    ignore_index=True
                ).fillna("")

                columns = [
                    c for c in key
                    if c in old
                ]

                expected_count = len(key) - (
                    "publication_ts_utc" not in old
                    and endpoint == "unav-pk5l"
                )

                if len(columns) != expected_count:
                    raise ValueError(
                        "Brak kolumn klucza PSE"
                    )

                old = old.drop_duplicates(
                    columns,
                    keep="last"
                )

                old = old.sort_values(
                    "business_date",
                    kind="stable"
                )

                save(name, old)

            mark = pd.DataFrame([{
                "endpoint": endpoint,
                "data": d,
                "liczba": str(len(records)),
                "pobrano_utc": datetime.now(
                    timezone.utc
                ).isoformat(timespec="seconds")
            }])

            status = pd.concat(
                [status, mark],
                ignore_index=True
            ).fillna("")

            status = status.drop_duplicates(
                ["endpoint", "data"],
                keep="last"
            )

            save(
                "pse_status.csv",
                status.sort_values(
                    ["endpoint", "data"]
                )
            )

            print(
                f"{endpoint} {d}: "
                f"{len(records)} rekordów",
                flush=True
            )

        except (
            requests.RequestException,
            ValueError,
            RuntimeError,
            KeyError
        ) as e:
            failures += 1

            print(
                f"BŁĄD {endpoint} {d}: {e}",
                flush=True
            )

    return failures


def main():
    errors = 0

    with session_new() as session:
        for endpoint in TARGETS:
            errors += update_source(
                session,
                endpoint
            )

    state = {
        "data": TODAY.isoformat(),
        "status": (
            "OK" if errors == 0 else "BLAD"
        ),
        "bledy": errors,
        "moment_utc": datetime.now(
            timezone.utc
        ).isoformat(timespec="seconds")
    }

    (ROOT / "stan_pse.json").write_text(
        json.dumps(
            state,
            ensure_ascii=False,
            indent=2
        ) + "\n",
        encoding="utf-8"
    )

    print("PSE:", state)

    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
