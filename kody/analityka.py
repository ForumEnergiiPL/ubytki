
from pathlib import Path
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo
import json

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
TZ = ZoneInfo("Europe/Warsaw")


def read(name):
    path = ROOT / name

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


def save(name, df):
    if df.empty and len(df.columns) == 0:
        print(f"Brak danych do zapisu: {name}")
        return

    path = ROOT / name
    temp = path.with_name(path.name + ".tmp")

    df.to_csv(
        temp,
        sep=";",
        index=False,
        encoding="utf-8-sig",
    )

    temp.replace(path)

    print(
        f"ANALITYKA {name}: {len(df)} wierszy",
        flush=True,
    )


def number(value):
    return pd.to_numeric(
        str(value).replace(",", "."),
        errors="coerce",
    )


def sum_fields(row, columns):
    values = [
        number(row.get(c, ""))
        for c in columns
    ]

    if not all(pd.notna(v) for v in values):
        return float("nan")

    return float(sum(values))


def local_time(value):
    try:
        ts = pd.to_datetime(
            value,
            errors="coerce",
            format="mixed",
        )

        if pd.isna(ts):
            return pd.NaT

        if ts.tzinfo is not None:
            return (
                ts.tz_convert(TZ)
                .tz_localize(None)
            )

        return ts

    except (TypeError, ValueError):
        return pd.NaT


def prepare_tge(df):
    if df.empty:
        return df

    required = {
        "jednostka",
        "typ",
        "poczatek",
        "koniec",
        "ubytek",
    }

    if not required.issubset(df.columns):
        raise ValueError(
            "TGE: brak wymaganych kolumn"
        )

    df = df.copy()

    df["_start"] = pd.to_datetime(
        df["poczatek"],
        errors="coerce",
        format="mixed",
    )

    df["_end"] = pd.to_datetime(
        df["koniec"],
        errors="coerce",
        format="mixed",
    )

    df["_mw"] = pd.to_numeric(
        df["ubytek"].str.replace(
            ",", ".", regex=False
        ),
        errors="coerce",
    )

    return df.dropna(
        subset=["_start", "_end", "_mw"]
    )


def tge_power_at(df, moment):
    if df.empty:
        return {
            "suma": 0.0,
            "planowane": 0.0,
            "nieplanowane": 0.0,
            "jednostki": 0,
        }

    active = df[
        (df["_start"] <= moment)
        &
        (df["_end"] > moment)
    ].copy()

    if active.empty:
        return {
            "suma": 0.0,
            "planowane": 0.0,
            "nieplanowane": 0.0,
            "jednostki": 0,
        }

    active = active.sort_values(
        "_mw",
        ascending=False,
    ).drop_duplicates("jednostka")

    kinds = (
        active["typ"]
        .str.lower()
        .str.strip()
    )

    planned = float(
        active.loc[
            kinds == "planowane",
            "_mw",
        ].sum()
    )

    unplanned = float(
        active.loc[
            kinds == "nieplanowane",
            "_mw",
        ].sum()
    )

    other = float(
        active.loc[
            ~kinds.isin(["planowane", "nieplanowane"]),
            "_mw",
        ].sum()
    )

    return {
        "suma": planned + unplanned + other,
        "planowane": planned,
        "nieplanowane": unplanned,
        "jednostki": len(active),
    }


def tge_coverage():
    views = read("tge_widoki.csv")
    covered = set()

    if not {
        "widok_od",
        "widok_do",
    }.issubset(views.columns):
        return covered

    for row in views.to_dict("records"):
        try:
            first = date.fromisoformat(
                str(row["widok_od"])[:10]
            )

            last = date.fromisoformat(
                str(row["widok_do"])[:10]
            )

            if first <= last <= first + timedelta(days=13):
                covered.update(
                    first + timedelta(days=i)
                    for i in range(
                        (last - first).days + 1
                    )
                )

        except ValueError:
            continue

    return covered


def load_snapshots():
    folder = ROOT / "tge_migawki"
    result = {}

    if not folder.exists():
        return result

    for path in sorted(
        folder.glob("????-??.csv")
    ):
        df = read(
            str(path.relative_to(ROOT))
        )

        if "data_obserwacji" not in df:
            continue

        for day, group in df.groupby(
            "data_obserwacji"
        ):
            result[str(day)] = prepare_tge(group)

    return result


def get_events():
    df = read("zdarzenia_unikalne.csv")

    if df.empty:
        return df

    required = {
        "id_zdarzenia",
        "poczatek",
        "koniec",
    }

    if not required.issubset(df.columns):
        return pd.DataFrame()

    df = df.drop_duplicates(
        "id_zdarzenia",
        keep="last",
    ).copy()

    df["_start"] = pd.to_datetime(
        df["poczatek"],
        errors="coerce",
        format="mixed",
    )

    df["_end"] = pd.to_datetime(
        df["koniec"],
        errors="coerce",
        format="mixed",
    )

    return df.dropna(
        subset=["_start", "_end"]
    )


def daily_analysis():
    pse = read("pse_bilans.csv")

    required = {
        "business_date",
        "peak",
        "peak_hour",
        "ub_sum",
    }

    if pse.empty or not required.issubset(pse.columns):
        print("Brak poprawnego bilansu PSE")
        return pd.DataFrame()

    if "publication_ts_utc" in pse:
        pse = pse.sort_values(
            "publication_ts_utc",
            kind="stable",
        )

    pse = pse.drop_duplicates(
        ["business_date", "peak"],
        keep="last",
    )

    tge = prepare_tge(
        read("tge_ubytki.csv")
    )

    covered = tge_coverage()
    snapshots = load_snapshots()
    events = get_events()

    rows = []

    for row in pse.to_dict("records"):
        day = str(
            row["business_date"]
        )[:10]

        try:
            day_date = date.fromisoformat(day)
        except ValueError:
            continue

        peak = str(
            row.get("peak", "")
        ).upper().strip()

        raw = str(
            row.get("peak_hour", "")
        )

        timestamp = (
            day + " " + raw
            if len(raw) <= 8
            else raw
        )

        moment = local_time(timestamp)

        if pd.isna(moment) or moment.date() != day_date:
            status = "BRAK_POPRAWNEJ_GODZINY"

        elif day in snapshots:
            status = "OK_MIGAWKA"

        elif day_date in covered:
            status = "RETRO_WIDOK"

        else:
            status = "BRAK_DANYCH_TGE"

        power = None

        if status in [
            "OK_MIGAWKA",
            "RETRO_WIDOK",
        ]:
            source = snapshots.get(day, tge)
            power = tge_power_at(
                source,
                moment,
            )

        rem = sum_fields(
            row,
            [
                "rk_jg",
                "rk_prb",
                "rs_jg",
                "rs_prb",
                "rb_jg",
                "rb_prb",
            ],
        )

        aw = sum_fields(
            row,
            ["ra_jg", "ra_prb"],
        )

        total = number(
            row.get("ub_sum", "")
        )

        tge_total = (
            power["suma"]
            if power is not None
            else float("nan")
        )

        rows.append({
            "data": day,
            "szczyt": peak,
            "czas_szczytu": (
                str(moment)
                if pd.notna(moment)
                else ""
            ),
            "pse_ubytki_ogolem_mw": total,
            "pse_remonty_mw": rem,
            "pse_awarie_mw": aw,
            "pse_remonty_i_awarie_mw": rem + aw,
            "tge_ubytek_mw": tge_total,
            "tge_planowane_mw": (
                power["planowane"]
                if power is not None
                else float("nan")
            ),
            "tge_nieplanowane_mw": (
                power["nieplanowane"]
                if power is not None
                else float("nan")
            ),
            "tge_liczba_jednostek": (
                power["jednostki"]
                if power is not None
                else float("nan")
            ),
            "pse_minus_tge_mw": total - tge_total,
            "status_tge": status,
        })

    if not rows:
        return pd.DataFrame()

    comparison = pd.DataFrame(rows)

    comparison = comparison.sort_values(
        ["data", "szczyt"]
    )

    save(
        "porownanie_pse_tge.csv",
        comparison,
    )

    daily_rows = []

    for day, group in comparison.groupby("data"):
        record = {"data": day}

        for row in group.to_dict("records"):
            peak = row["szczyt"]

            for col in [
                "czas_szczytu",
                "pse_ubytki_ogolem_mw",
                "pse_remonty_mw",
                "pse_awarie_mw",
                "pse_remonty_i_awarie_mw",
                "tge_ubytek_mw",
                "tge_planowane_mw",
                "tge_nieplanowane_mw",
                "tge_liczba_jednostek",
                "pse_minus_tge_mw",
                "status_tge",
            ]:
                record[f"{col}_{peak}"] = row[col]

        if not events.empty:
            started = events[
                events["_start"].dt.strftime(
                    "%Y-%m-%d"
                ) == day
            ]

            record[
                "kandydaci_zdarzen_rozpoczetych"
            ] = len(started)

            record["w_tym_z_tge"] = (
                int(
                    started["zrodla"]
                    .str.contains("TGE", na=False)
                    .sum()
                )
                if "zrodla" in started.columns
                else 0
            )

        daily_rows.append(record)

    daily = pd.DataFrame(
        daily_rows
    ).sort_values("data")

    save(
        "analityka_dzienna.csv",
        daily,
    )

    return daily


def history_analysis(daily):
    today = datetime.now(TZ).date()
    today_str = today.isoformat()

    state_file = ROOT / "stan_aktualizacji.json"
    run_state = {}

    if state_file.exists():
        try:
            run_state = json.loads(
                state_file.read_text(
                    encoding="utf-8"
                )
            )
        except (ValueError, OSError):
            pass

    events = get_events()
    tge = read("tge_ubytki.csv")
    views = read("tge_widoki.csv")

    snapshot_ok = False

    if {
        "widok_od",
        "widok_do",
    }.issubset(views.columns):
        snapshot_ok = bool(
            (
                (views["widok_od"] == today_str)
                &
                (views["widok_do"] == today_str)
            ).any()
        )

    new_versions = 0

    if (
        not tge.empty
        and "pierwszy_odczyt_utc" in tge
    ):
        dates = pd.to_datetime(
            tge["pierwszy_odczyt_utc"],
            utc=True,
            errors="coerce",
        )

        new_versions = int(
            (
                dates.dt.tz_convert(TZ)
                .dt.strftime("%Y-%m-%d")
                == today_str
            ).sum()
        )

    active_count = ""

    if not events.empty and snapshot_ok:
        moment = pd.Timestamp(
            datetime.now(TZ).replace(
                tzinfo=None
            )
        )

        active = events[
            (events["_start"] <= moment)
            &
            (events["_end"] > moment)
        ]

        if "zrodla" in active.columns:
            active_count = int(
                active["zrodla"]
                .str.contains(
                    "TGE",
                    na=False,
                ).sum()
            )

    latest = (
        daily.sort_values("data").iloc[-1]
        if not daily.empty
        else None
    )

    report = {
        "data_aktualizacji": today_str,
        "moment_odczytu_utc": datetime.now(
            timezone.utc
        ).isoformat(timespec="seconds"),
        "status_pobierania": run_state.get(
            "status",
            "BRAK_INFORMACJI",
        ),
        "bledy_pobierania": run_state.get(
            "liczba_bledow",
            "",
        ),
        "tge_pobrane_dzis": (
            "TAK" if snapshot_ok else "NIE"
        ),
        "tge_wersje_lacznie": len(tge),
        "tge_nowe_wersje_dzis": new_versions,
        "zdarzenia_kandydaci_lacznie": len(events),
        "tge_zdarzenia_aktywne_teraz": active_count,
        "ostatni_dzien_bilansu_pse": (
            latest["data"]
            if latest is not None
            else ""
        ),
        "pse_sr_ubytki_mw": (
            latest.get(
                "pse_ubytki_ogolem_mw_SR", ""
            )
            if latest is not None
            else ""
        ),
        "pse_sw_ubytki_mw": (
            latest.get(
                "pse_ubytki_ogolem_mw_SW", ""
            )
            if latest is not None
            else ""
        ),
        "tge_sr_ubytki_mw": (
            latest.get(
                "tge_ubytek_mw_SR", ""
            )
            if latest is not None
            else ""
        ),
        "tge_sw_ubytki_mw": (
            latest.get(
                "tge_ubytek_mw_SW", ""
            )
            if latest is not None
            else ""
        ),
    }

    old = read("historia_analityki.csv")

    updated = pd.concat(
        [
            old,
            pd.DataFrame([report]),
        ],
        ignore_index=True,
    )

    updated = updated.drop_duplicates(
        "data_aktualizacji",
        keep="last",
    )

    updated = updated.sort_values(
        "data_aktualizacji"
    )

    save(
        "historia_analityki.csv",
        updated,
    )


def main():
    daily = daily_analysis()
    history_analysis(daily)


if __name__ == "__main__":
    main()
