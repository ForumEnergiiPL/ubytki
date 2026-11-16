
"""Konserwatywne uzgadnianie zgloszen TGE z PSE."""

from __future__ import annotations

import hashlib
import re
import unicodedata
from datetime import timedelta
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT

TGE = DATA / "tge_ubytki.csv"
PSE_UNAV = DATA / "pse_remonty.csv"
PSE_JW = DATA / "pse_jednostki"

MAX_GAP_TGE = pd.Timedelta(minutes=15)
CLOSE_TIME = pd.Timedelta(minutes=30)


def read_csv(path):
    if not path.exists() or path.stat().st_size == 0:
        return pd.DataFrame()

    return pd.read_csv(
        path,
        sep=";",
        dtype=str,
        keep_default_na=False,
        encoding="utf-8-sig"
    )


def write_csv(path, df):
    path.parent.mkdir(
        parents=True,
        exist_ok=True
    )

    temp = path.with_name(
        path.name + ".tmp"
    )

    df.to_csv(
        temp,
        sep=";",
        index=False,
        encoding="utf-8-sig"
    )

    temp.replace(path)


def clean(text):
    return "".join(
        c for c in unicodedata.normalize(
            "NFKD",
            str(text).lower()
        )
        if not unicodedata.combining(c)
    )


def unit_from_tge(name):
    n = clean(name)

    found = re.search(
        r"\bb\s*0?(\d{1,2})\b",
        n
    )

    if not found:
        found = re.search(
            r"\bblok\s*0?(\d{1,2})\b",
            n
        )

    if not found:
        return ""

    b = int(found.group(1))

    if "turow" in n and b in (
        1, 2, 3, 4, 5, 6, 11
    ):
        return (
            f"TUR "
            f"{1 if b == 1 else 4 if b == 11 else 2}"
            f"-{b:02d}"
        )

    if "belchatow" in n and 2 <= b <= 14:
        return (
            f"BEL "
            f"{2 if b <= 5 else 4}"
            f"-{b:02d}"
        )

    if n.startswith("opole ") and 1 <= b <= 6:
        return (
            f"OPL "
            f"{1 if b <= 2 else 4}"
            f"-{b:02d}"
        )

    if n.startswith("rybnik ") and b in (6, 7, 8):
        return (
            f"RYB "
            f"{2 if b == 6 else 4}"
            f"-{b:02d}"
        )

    if (
        n.startswith("jaworzno 3 ")
        and 1 <= b <= 6
    ):
        return (
            f"JW3_"
            f"{1 if b == 3 else 2}"
            f"S{b:02d}"
        )

    if (
        n.startswith("kozienice 1 ")
        and 1 <= b <= 8
    ):
        return f"KOZ12S{b:02d}"

    if (
        n.startswith("kozienice 2 ")
        and 9 <= b <= 11
    ):
        return f"KOZ24S{b:02d}"

    if (
        n.startswith("laziska 3 ")
        and b in (9, 10, 11, 12)
    ):
        return (
            f"LZA"
            f"{31 if b <= 10 else 32}"
            f"S{b:02d}"
        )

    if (
        n.startswith("polaniec ")
        and 2 <= b <= 7
    ):
        return (
            f"POL_"
            f"{2 if b <= 4 else 4}"
            f"S{b:02d}"
        )

    if (
        n.startswith("ostroleka b ")
        and b in (1, 2, 3)
    ):
        return (
            f"OSB_"
            f"{1 if b == 3 else 2}"
            f"S{b:02d}"
        )

    if (
        "gryfino" in n
        and "blok" in n
        and b in (9, 10)
    ):
        return f"EGF_4S{b:02d}"

    if n.startswith("patnow ") and b == 9:
        return "PAT24S09"

    if n.startswith("plock ") and b == 1:
        return "PLO_4S01"

    if n.startswith("ec wloclawek ") and b == 1:
        return "WLC_2S01"

    return ""


def when(value):
    try:
        x = pd.to_datetime(
            value,
            errors="coerce",
            format="mixed"
        )

        if pd.isna(x):
            return pd.NaT

        if x.tzinfo is not None:
            return (
                x.tz_convert("Europe/Warsaw")
                .tz_localize(None)
            )

        return x

    except (ValueError, TypeError):
        return pd.NaT


def number(value):
    try:
        return float(
            str(value).replace(",", ".")
        )
    except (ValueError, TypeError):
        return float("nan")


def digest(*parts):
    return hashlib.sha256(
        "\x1f".join(
            map(str, parts)
        ).encode()
    ).hexdigest()[:18]


def load_tge():
    df = read_csv(TGE)

    required = {
        "jednostka",
        "typ",
        "poczatek",
        "koniec",
        "ubytek"
    }

    if df.empty:
        return []

    if not required.issubset(df.columns):
        raise ValueError(
            f"TGE: brak kolumn "
            f"{required - set(df.columns)}"
        )

    grouped = {}

    df = df.drop_duplicates(
        subset=list(required)
    )

    for r in df.to_dict("records"):
        a = when(r["poczatek"])
        b = when(r["koniec"])
        power = number(r["ubytek"])

        if (
            pd.isna(a)
            or pd.isna(b)
            or b <= a
            or pd.isna(power)
        ):
            continue

        unit = unit_from_tge(
            r["jednostka"]
        )

        identity = (
            unit if unit
            else "TGE:" + clean(r["jednostka"])
        )

        kind = clean(
            r["typ"]
        ).strip()

        grouped.setdefault(
            (identity, kind),
            []
        ).append(
            (a, b, power, r)
        )

    events = []

    for (identity, kind), periods in grouped.items():
        periods.sort(
            key=lambda x: (x[0], x[1])
        )

        group = None

        for a, b, mw, r in periods:
            if (
                group is not None
                and a <= group["end"] + MAX_GAP_TGE
            ):
                group["end"] = max(
                    group["end"],
                    b
                )

                group["mw"] = max(
                    group["mw"],
                    mw
                )

                group["fragments"] += 1

                if (
                    r.get("pierwszy_odczyt_utc")
                    and (
                        not group["seen"]
                        or r["pierwszy_odczyt_utc"]
                        < group["seen"]
                    )
                ):
                    group["seen"] = (
                        r["pierwszy_odczyt_utc"]
                    )

            else:
                if group is not None:
                    events.append(group)

                group = {
                    "unit": (
                        identity
                        if not identity.startswith("TGE:")
                        else ""
                    ),
                    "name": r["jednostka"],
                    "start": a,
                    "end": b,
                    "mw": mw,
                    "kind": kind,
                    "fragments": 1,
                    "seen": r.get(
                        "pierwszy_odczyt_utc",
                        ""
                    )
                }

        if group is not None:
            events.append(group)

    for ev in events:
        ev["id"] = "TGE-" + digest(
            ev["unit"] or ev["name"],
            ev["kind"],
            ev["start"]
        )

    return events


def load_unav():
    df = read_csv(PSE_UNAV)

    required = {
        "unit_code",
        "start_dtime",
        "end_dtime",
        "reason"
    }

    if df.empty:
        return []

    if not required.issubset(df.columns):
        raise ValueError(
            f"PSE unav: brak kolumn "
            f"{required - set(df.columns)}"
        )

    if "publication_ts_utc" in df.columns:
        df = df.sort_values(
            "publication_ts_utc",
            kind="stable"
        )

    keys = (
        [
            "unit_code",
            "start_dtime",
            "end_dtime",
            "reason",
            "state"
        ]
        if "state" in df
        else [
            "unit_code",
            "start_dtime",
            "end_dtime",
            "reason"
        ]
    )

    df = df.drop_duplicates(
        keys,
        keep="last"
    )

    events = []

    for r in df.to_dict("records"):
        a = when(r["start_dtime"])
        b = when(r["end_dtime"])

        if (
            pd.isna(a)
            or pd.isna(b)
            or b <= a
        ):
            continue

        unit = r.get(
            "unit_code",
            ""
        ).strip()

        reason = str(
            r.get("reason", "")
        ).strip().upper()

        events.append({
            "unit": unit,
            "name": r.get("power_plant", ""),
            "start": a,
            "end": b,
            "reason": reason,
            "state": r.get("state", ""),
            "published": r.get(
                "publication_ts_utc",
                ""
            ),
            "id": "PSE-" + digest(
                unit, a, b, reason
            )
        })

    return events


def intersection_seconds(a, b):
    return max(
        0.0,
        (
            min(a["end"], b["end"])
            -
            max(a["start"], b["start"])
        ).total_seconds()
    )


def assess(tge, pse):
    intersect = intersection_seconds(
        tge,
        pse
    )

    if (
        not intersect
        or not tge["unit"]
        or tge["unit"] != pse["unit"]
    ):
        return "", 0.0

    total = (
        max(tge["end"], pse["end"])
        -
        min(tge["start"], pse["start"])
    ).total_seconds()

    overlap = (
        intersect / total
        if total else 0.0
    )

    kind = tge["kind"]
    reason = pse["reason"]

    compatible = (
        (
            reason in {"RK", "RS", "RB"}
            and kind == "planowane"
        )
        or (
            reason == "RA"
            and kind == "nieplanowane"
        )
    )

    near = (
        abs(
            tge["start"] - pse["start"]
        ) <= CLOSE_TIME
        and
        abs(
            tge["end"] - pse["end"]
        ) <= CLOSE_TIME
    )

    high_overlap = (
        overlap >= 0.85
        and intersect >= 3600
    )

    if near:
        return "ZGODNE_TERMINY", overlap

    if compatible and high_overlap:
        return "PEWNE_PODOBIENSTWO", overlap

    return "DO_WERYFIKACJI", overlap


def load_jw():
    legacy = DATA / "pse_jednostki.csv"

    paths = (
        [legacy]
        if legacy.exists()
        else (
            sorted(PSE_JW.glob("????-??.csv"))
            if PSE_JW.exists()
            else []
        )
    )

    frames = [
        read_csv(p) for p in paths
    ]

    frames = [
        f for f in frames
        if not f.empty
    ]

    if not frames:
        return pd.DataFrame(columns=[
            "resource_code",
            "_end",
            "_mw"
        ])

    df = pd.concat(
        frames,
        ignore_index=True
    )

    required = {
        "resource_code",
        "dtime_utc",
        "non_us_cap"
    }

    if not required.issubset(df.columns):
        return pd.DataFrame(columns=[
            "resource_code",
            "_end",
            "_mw"
        ])

    if "publication_ts_utc" in df:
        df = df.sort_values(
            "publication_ts_utc",
            kind="stable"
        )

    df = df.drop_duplicates(
        ["resource_code", "dtime_utc"],
        keep="last"
    )

    df["_end"] = (
        pd.to_datetime(
            df["dtime_utc"],
            errors="coerce",
            utc=True
        )
        .dt.tz_convert("Europe/Warsaw")
        .dt.tz_localize(None)
    )

    df["_mw"] = pd.to_numeric(
        df["non_us_cap"].str.replace(
            ",", ".", regex=False
        ),
        errors="coerce"
    )

    return df.dropna(
        subset=["_end", "_mw"]
    )[[
        "resource_code",
        "_end",
        "_mw"
    ]]


def reconcile():
    tges = load_tge()
    pses = load_unav()
    measured = load_jw()

    candidates = []

    for i, t in enumerate(tges):
        if not t["unit"]:
            continue

        for j, p in enumerate(pses):
            if (
                t["unit"] == p["unit"]
                and intersection_seconds(t, p) > 0
            ):
                level, score = assess(t, p)

                candidates.append({
                    "i": i,
                    "j": j,
                    "level": level,
                    "score": score
                })

    confident = [
        c for c in candidates
        if c["level"] in {
            "ZGODNE_TERMINY",
            "PEWNE_PODOBIENSTWO"
        }
    ]

    freq_i = pd.Series([
        c["i"] for c in confident
    ]).value_counts().to_dict()

    freq_j = pd.Series([
        c["j"] for c in confident
    ]).value_counts().to_dict()

    paired = {
        c["i"]: c["j"]
        for c in confident
        if (
            freq_i[c["i"]] == 1
            and freq_j[c["j"]] == 1
        )
    }

    joined_pse = set(
        paired.values()
    )

    review = []

    for c in candidates:
        if paired.get(c["i"]) == c["j"]:
            continue

        t = tges[c["i"]]
        p = pses[c["j"]]

        review.append({
            "id_tge": t["id"],
            "id_pse": p["id"],
            "jednostka": t["name"],
            "kod_jednostki": t["unit"],
            "poczatek_tge": t["start"],
            "koniec_tge": t["end"],
            "poczatek_pse": p["start"],
            "koniec_pse": p["end"],
            "typ_tge": t["kind"],
            "przyczyna_pse": p["reason"],
            "wspolczynnik_nakladania": round(
                c["score"], 3
            ),
            "powod": (
                "wiele mozliwych dopasowan"
                if c["level"] in {
                    "ZGODNE_TERMINY",
                    "PEWNE_PODOBIENSTWO"
                }
                else (
                    "rozne przyczyny lub "
                    "niezgodne terminy"
                )
            )
        })

    events = []

    for i, t in enumerate(tges):
        p = (
            pses[paired[i]]
            if i in paired
            else None
        )

        events.append({
            "id_zdarzenia": t["id"],
            "jednostka": t["name"],
            "kod_jednostki": t["unit"],
            "poczatek": t["start"],
            "koniec": t["end"],
            "ubytek_tge_mw_max": round(
                t["mw"], 3
            ),
            "typ_tge": t["kind"],
            "przyczyna_pse": (
                p["reason"] if p else ""
            ),
            "status_pse": (
                p["state"] if p else ""
            ),
            "id_raportu_pse": (
                p["id"] if p else ""
            ),
            "liczba_wpisow_tge": t["fragments"],
            "data_pierwszej_obserwacji_utc":
                t["seen"],
            "zrodla": (
                "TGE + PSE unav-pk5l"
                if p else "TGE"
            ),
            "status_dopasowania": (
                "dopasowane_z_duzym_prawdopodobienstwem"
                if p else "tylko_TGE"
            )
        })

    for j, p in enumerate(pses):
        if j in joined_pse:
            continue

        events.append({
            "id_zdarzenia": p["id"],
            "jednostka": p["name"],
            "kod_jednostki": p["unit"],
            "poczatek": p["start"],
            "koniec": p["end"],
            "ubytek_tge_mw_max": "",
            "typ_tge": "",
            "przyczyna_pse": p["reason"],
            "status_pse": p["state"],
            "id_raportu_pse": p["id"],
            "liczba_wpisow_tge": 0,
            "data_pierwszej_obserwacji_utc": "",
            "zrodla": "PSE unav-pk5l",
            "status_dopasowania": "tylko_PSE_unav"
        })

    observed = {}

    if not measured.empty:
        for code, chunk in measured[
            measured["_mw"] > 0
        ].groupby("resource_code"):
            observed[code] = chunk

    for event in events:
        ev_start = when(
            event["poczatek"]
        )

        ev_end = when(
            event["koniec"]
        )

        group = observed.get(
            event["kod_jednostki"]
        )

        if group is None:
            event["pse_obserwacje_15min"] = 0
            event[
                "pse_maks_zaobserwowany_ubytek_mw"
            ] = ""

            continue

        sub = group[
            (group["_end"] > ev_start)
            &
            (
                group["_end"]
                - pd.Timedelta(minutes=15)
                < ev_end
            )
        ]

        event["pse_obserwacje_15min"] = len(sub)

        event[
            "pse_maks_zaobserwowany_ubytek_mw"
        ] = (
            round(sub["_mw"].max(), 3)
            if not sub.empty
            else ""
        )

        if not sub.empty:
            event["zrodla"] += (
                " + PSE pdwkseub (pomiar)"
            )

    unique = pd.DataFrame(events)

    checks = pd.DataFrame(
        review,
        columns=[
            "id_tge",
            "id_pse",
            "jednostka",
            "kod_jednostki",
            "poczatek_tge",
            "koniec_tge",
            "poczatek_pse",
            "koniec_pse",
            "typ_tge",
            "przyczyna_pse",
            "wspolczynnik_nakladania",
            "powod"
        ]
    )

    if not unique.empty:
        unique = unique.sort_values([
            "poczatek",
            "jednostka",
            "id_zdarzenia"
        ])

    write_csv(
        DATA / "zdarzenia_unikalne.csv",
        unique
    )

    write_csv(
        DATA / "zdarzenia_do_weryfikacji.csv",
        checks
    )

    print(
        "Zdarzenia (kandydaci po ostroznym "
        f"grupowaniu): {len(unique)}"
    )

    print(
        f"TGE: {len(tges)}, "
        f"raporty PSE unav: {len(pses)}, "
        f"sparowane 1:1: {len(paired)}"
    )

    print(
        "Mozliwe dodatkowe dopasowania "
        f"do recznej weryfikacji: {len(checks)}"
    )

    return unique, checks


if __name__ == "__main__":
    reconcile()
