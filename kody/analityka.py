
"""Dzienna analityka PSE/TGE oraz historia analiz."""

from __future__ import annotations

import json

from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

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
        low_memory=False
    )


def save(name, df):
    if df is None or len(df.columns) == 0:
        raise ValueError(
            f"Brak danych/kolumn: {name}"
        )

    path = ROOT / name
    tmp = path.with_name(path.name + ".tmp")

    df.to_csv(
        tmp,
        sep=";",
        index=False,
        encoding="utf-8-sig",
        na_rep=""
    )

    tmp.replace(path)

    print(
        f"ANALITYKA {name}: {len(df)} wierszy",
        flush=True
    )


def n(v):
    try:
        return float(str(v).replace(",", "."))
    except (ValueError, TypeError):
        return float("nan")


def sum_fields(r, cols):
    values = [
        n(r.get(c, ""))
        for c in cols
    ]

    if all(pd.notna(v) for v in values):
        return float(sum(values))

    return float("nan")


def loc_time(v):
    try:
        dt = pd.to_datetime(
            str(v),
            errors="coerce",
            format="mixed"
        )

        if pd.isna(dt):
            return pd.NaT

        if dt.tzinfo:
            return (
                dt.tz_convert(TZ)
                .tz_localize(None)
            )

        return dt

    except (ValueError, TypeError):
        return pd.NaT


def load_tge(df):
    if df.empty:
        return df

    required = {
        "jednostka",
        "typ",
        "poczatek",
        "koniec",
        "ubytek"
    }

    if not required.issubset(df):
        raise ValueError(
            f"TGE: brakuje kolumn "
            f"{required - set(df.columns)}"
        )

    df = df.copy()

    df["_start"] = pd.to_datetime(
        df.poczatek,
        format="mixed",
        errors="coerce"
    )

    df["_end"] = pd.to_datetime(
        df.koniec,
        format="mixed",
        errors="coerce"
    )

    df["_mw"] = pd.to_numeric(
        df.ubytek.str.replace(
            ",", ".", regex=False
        ),
        errors="coerce"
    )

    return df.dropna(
        subset=["_start", "_end", "_mw"]
    )


def power_at(df, moment):
    if df.empty:
        return {
            "suma": 0.0,
            "planowane": 0.0,
            "nieplanowane": 0.0,
            "inne": 0.0,
            "bloki": 0
        }

    active = df[
        (df._start <= moment)
        &
        (df._end > moment)
    ].copy()

    if active.empty:
        return {
            "suma": 0.0,
            "planowane": 0.0,
            "nieplanowane": 0.0,
            "inne": 0.0,
            "bloki": 0
        }

    active = active.sort_values(
        "_mw",
        ascending=False
    ).drop_duplicates("jednostka")

    kinds = (
        active.typ
        .str.casefold()
        .str.strip()
    )

    plan = float(
        active.loc[
            kinds == "planowane",
            "_mw"
        ].sum()
    )

    unplan = float(
        active.loc[
            kinds == "nieplanowane",
            "_mw"
        ].sum()
    )

    other = float(
        active.loc[
            ~kinds.isin([
                "planowane",
                "nieplanowane"
            ]),
            "_mw"
        ].sum()
    )

    return {
        "suma": plan + unplan + other,
        "planowane": plan,
        "nieplanowane": unplan,
        "inne": other,
        "bloki": len(active)
    }


def coverage_days():
    views = read("tge_widoki.csv")
    covered = set()

    if (
        "widok_od" not in views
        or "widok_do" not in views
    ):
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
    result = {}
    folder = ROOT / "tge_migawki"

    paths = (
        sorted(folder.glob("????-??.csv"))
        if folder.exists()
        else []
    )

    for p in paths:
        x = read(
            str(p.relative_to(ROOT))
        )

        if "data_obserwacji" not in x:
            continue

        for d, group in x.groupby(
            "data_obserwacji"
        ):
            result[str(d)] = load_tge(group)

    return result


def latest_unique_events():
    u = read("zdarzenia_unikalne.csv")

    if u.empty:
        return u

    required = {
        "id_zdarzenia",
        "poczatek",
        "koniec"
    }

    if not required.issubset(u):
        raise ValueError(
            "zdarzenia_unikalne.csv "
            "bez wymaganych kolumn"
        )

    u = u.drop_duplicates(
        "id_zdarzenia",
        keep="last"
    ).copy()

    u["_start"] = pd.to_datetime(
        u.poczatek,
        errors="coerce",
        format="mixed"
    )

    u["_end"] = pd.to_datetime(
        u.koniec,
        errors="coerce",
        format="mixed"
    )

    return u.dropna(
        subset=["_start", "_end"]
    )


def daily_analysis():
    pse = read("pse_bilans.csv")

    required = {
        "business_date",
        "peak",
        "peak_hour",
        "ub_sum"
    }

    if pse.empty or not required.issubset(pse):
        print(
            "Brak poprawnego pse_bilans.csv. "
            "Zachowuje dotychczasowa analityke."
        )

        return pd.DataFrame()

    if "publication_ts_utc" in pse:
        pse = pse.sort_values(
            "publication_ts_utc",
            kind="stable"
        )

    pse = pse.drop_duplicates(
        ["business_date", "peak"],
        keep="last"
    )

    tge = load_tge(
        read("tge_ubytki.csv")
    )

    covered = coverage_days()
    snapshots = load_snapshots()
    events = latest_unique_events()

    rows = []

    for r in pse.to_dict("records"):
        d = str(
            r.get("business_date", "")
        )[:10]

        try:
            day = date.fromisoformat(d)
        except ValueError:
            continue

        peak = str(
            r.get("peak", "")
        ).upper().strip()

        raw = str(
            r.get("peak_hour", "")
        )

        moment = loc_time(
            (d + " " + raw)
            if len(raw) <= 8
            else raw
        )

        status = (
            "OK_MIGAWKA"
            if d in snapshots
            else (
                "RETRO_WIDOK"
                if day in covered
                else "BRAK_WIDOKU"
            )
        )

        if (
            pd.isna(moment)
            or moment.date() != day
        ):
            status = "BRAK_POPRAWNEJ_GODZINY"

        source = (
            snapshots[d]
            if d in snapshots
            else tge
        )

        tpower = (
            power_at(source, moment)
            if status in (
                "OK_MIGAWKA",
                "RETRO_WIDOK"
            )
            else None
        )

        rem = sum_fields(
            r,
            [
                "rk_jg",
                "rk_prb",
                "rs_jg",
                "rs_prb",
                "rb_jg",
                "rb_prb"
            ]
        )

        aw = sum_fields(
            r,
            ["ra_jg", "ra_prb"]
        )

        overall = n(
            r.get("ub_sum", "")
        )

        rows.append({
            "data": d,
            "szczyt": peak,
            "czas_szczytu": (
                str(moment)
                if pd.notna(moment)
                else ""
            ),
            "pse_ubytki_ogolem_mw": overall,
            "pse_remonty_mw": rem,
            "pse_awarie_mw": aw,
            "pse_remonty_i_awarie_mw": rem + aw,
            "pse_ograniczenia_eksploatacyjne_mw":
                sum_fields(
                    r,
                    ["we_jg", "we_prb"]
                ),
            "tge_ubytek_mw": (
                tpower["suma"]
                if tpower is not None
                else float("nan")
            ),
            "tge_planowane_mw": (
                tpower["planowane"]
                if tpower is not None
                else float("nan")
            ),
            "tge_nieplanowane_mw": (
                tpower["nieplanowane"]
                if tpower is not None
                else float("nan")
            ),
            "tge_liczba_blokow": (
                tpower["bloki"]
                if tpower is not None
                else float("nan")
            ),
            "pse_minus_tge_mw": (
                overall - tpower["suma"]
                if tpower is not None
                else float("nan")
            ),
            "pse_remonty_awarie_minus_tge_mw": (
                rem + aw - tpower["suma"]
                if tpower is not None
                else float("nan")
            ),
            "status_tge": status
        })

    per_peak = pd.DataFrame(rows).sort_values(
        ["data", "szczyt"]
    )

    save(
        "porownanie_pse_tge.csv",
        per_peak
    )

    daily = []

    for day, grp in per_peak.groupby("data"):
        record = {"data": day}

        for r in grp.to_dict("records"):
            peak = r["szczyt"]

            columns = [
                "czas_szczytu",
                "pse_ubytki_ogolem_mw",
                "pse_remonty_mw",
                "pse_awarie_mw",
                "pse_remonty_i_awarie_mw",
                "tge_ubytek_mw",
                "tge_planowane_mw",
                "tge_nieplanowane_mw",
                "tge_liczba_blokow",
                "pse_minus_tge_mw",
                "status_tge"
            ]

            for col in columns:
                record[col + "_" + peak] = r[col]

        if not events.empty:
            today_starts = events[
                events._start.dt.strftime(
                    "%Y-%m-%d"
                ) == day
            ]

            record[
                "kandydaci_zdarzen_rozpoczetych"
            ] = len(today_starts)

            record["w_tym_z_tge"] = (
                int(
                    today_starts.zrodla
                    .str.contains(
                        "TGE",
                        na=False
                    ).sum()
                )
                if "zrodla" in today_starts
                else 0
            )

        daily.append(record)

    output = pd.DataFrame(daily).sort_values(
        "data"
    ).reset_index(drop=True)

    save(
        "analityka_dzienna.csv",
        output
    )

    return output


def observation_history(daily):
    today = datetime.now(TZ).date()

    run_status = {}
    state = ROOT / "stan_aktualizacji.json"

    if state.exists():
        try:
            run_status = json.loads(
                state.read_text(
                    encoding="utf-8"
                )
            )
        except ValueError:
            pass

    events = latest_unique_events()
    tge = read("tge_ubytki.csv")

    views = read("tge_widoki.csv")

    snapshot_today = bool(
        not views.empty
        and {
            "widok_od",
            "widok_do"
        }.issubset(views.columns)
        and (
            (
                (views["widok_od"] == today.isoformat())
                &
                (views["widok_do"] == today.isoformat())
            ).any()
        )
    )

    moment = pd.Timestamp(
        datetime.now(TZ).replace(
            tzinfo=None
        )
    )

    active = (
        events[
            (events._start <= moment)
            &
            (events._end > moment)
        ]
        if not events.empty
        else pd.DataFrame()
    )

    active_tge = (
        active[
            active.zrodla.str.contains(
                "TGE",
                na=False
            )
        ]
        if not active.empty and "zrodla" in active
        else pd.DataFrame()
    )

    new_tge_versions = 0

    if not tge.empty and "pierwszy_odczyt_utc" in tge:
        dates = pd.to_datetime(
            tge["pierwszy_odczyt_utc"],
            utc=True,
            errors="coerce"
        )

        new_tge_versions = int(
            (
                dates.dt.tz_convert(TZ)
                .dt.strftime("%Y-%m-%d")
                == today.isoformat()
            ).sum()
        )

    prior = read("historia_analityki.csv")

    last_day = (
        daily.sort_values("data").iloc[-1]
        if not daily.empty
        else None
    )

    report = {
        "data_aktualizacji": today.isoformat(),
        "moment_odczytu_utc": datetime.now(
            timezone.utc
        ).isoformat(timespec="seconds"),

        "status_pobierania": run_status.get(
            "status",
            "BRAK_INFORMACJI"
        ),

        "bledy_pobierania": run_status.get(
            "liczba_bledow",
            ""
        ),

        "tge_pelna_migawka_dzis": (
            "TAK" if snapshot_today else "NIE"
        ),

        "tge_liczba_roznych_wersji_lacznie": len(tge),

        "tge_nowe_wersje_zarejestrowane_dzis":
            new_tge_versions,

        "zdarzenia_kandydaci_lacznie": len(events),

        "zdarzenia_tge_aktywne_teraz_kandydaci": (
            len(active_tge)
            if snapshot_today
            else ""
        ),

        "ostatni_dzien_bilansu_pse": (
            last_day["data"]
            if last_day is not None
            else ""
        ),

        "pse_sr_ubytki_mw": (
            last_day.get(
                "pse_ubytki_ogolem_mw_SR", ""
            )
            if last_day is not None
            else ""
        ),

        "pse_sw_ubytki_mw": (
            last_day.get(
                "pse_ubytki_ogolem_mw_SW", ""
            )
            if last_day is not None
            else ""
        ),

        "tge_sr_ubytki_mw": (
            last_day.get(
                "tge_ubytek_mw_SR", ""
            )
            if last_day is not None
            else ""
        ),

        "tge_sw_ubytki_mw": (
            last_day.get(
                "tge_ubytek_mw_SW", ""
            )
            if last_day is not None
            else ""
        )
    }

    updated = pd.concat(
        [
            prior,
            pd.DataFrame([report])
        ],
        ignore_index=True
    )

    updated = updated.drop_duplicates(
        "data_aktualizacji",
        keep="last"
    )

    updated = updated.sort_values(
        "data_aktualizacji"
    ).reset_index(drop=True)

    save(
        "historia_analityki.csv",
        updated
    )


def main():
    daily = daily_analysis()
    observation_history(daily)


if __name__ == "__main__":
    main()
