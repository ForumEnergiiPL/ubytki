#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Ręczne uzupełnianie TGE z GPI: dziś + 6 poprzednich dni.

Sposób użycia:
1. Na github.com/ForumEnergiiPL/ubytki pobierz osobno trzy pliki:
   tge_ubytki.csv, tge_widoki.csv, stan_tge.json.
   Z folderu GitHub tge_migawki pobierz także plik bieżącego miesiąca,
   np. 2026-11.csv (jeśli już istnieje w repozytorium).
2. Lokalnie umieść pobierz_tge.py i pobrane pliki W JEDNYM folderze,
   np. "Pobieranie dane TGE". W repozytorium skrypt może pozostać w
   folderze kody/, a pliki CSV i JSON powinny być poziom wyżej.
3. Uruchom pobierz_tge.py w Pythonie (np. Spyder F5 / Jupyter %run).
4. Po komunikacie SUKCES prześlij te same pliki z folderu na GitHub:
   trzy pliki główne do katalogu głównego, a YYYY-MM.csv do tge_migawki/.

UWAGA: aktualizacja dni wstecz jest RETROSPEKTYWNA. Dzisiejszy stan
GPI nie odtworzy informacji, które później usunięto albo zmieniono.
Starsze dni nie są zapisywane jako oryginalne migawki z tamtych dni.
"""

import csv
import hashlib
import json
import os
import re
import shutil
import sys
import time
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup


# -------------------- USTAWIENIA --------------------
# 7 dni = dziś + 6 poprzednich; zmień np. na 5, jeśli wolisz.
ILE_DNI = 7
if not (1 <= ILE_DNI <= 14):
    raise ValueError('ILE_DNI musi być od 1 do 14.')

# AUTOMATYCZNY WYBÓR FOLDERU DANYCH (nie trzeba nic ustawiać):
# A) Wygodniej lokalnie: pobierz_tge.py i pliki TGE w JEDNYM folderze.
# B) W repozytorium: kody/pobierz_tge.py i pliki TGE POZIOM WYŻEJ.
# Zawsze ustalamy lokalizację względem pliku .py, nie względem katalogu
# roboczego Spydera / terminala. W Jupyterze uruchom skrypt przez %run.
SKRYPT = Path(__file__).resolve().parent if '__file__' in globals() else Path.cwd()
if SKRYPT.name.casefold() == 'kody':
    FOLDER = SKRYPT.parent
else:
    FOLDER = SKRYPT
DZIS = datetime.now(ZoneInfo('Europe/Warsaw')).date()
OD_DNIA = DZIS - timedelta(days=ILE_DNI - 1)
URL = 'https://gpi.tge.pl/pl/zestawienie-ubytkow'
ADRESY = [URL, 'https://gpi.tge.pl/zestawienie-ubytkow', URL]

PODSTAWOWE = ['jednostka', 'moc', 'ubytek', 'typ', 'poczatek', 'koniec']
UBYTKI = PODSTAWOWE + [
    'widok_od', 'zrodlo', 'id_wersji', 'pierwszy_odczyt_utc',
    'ostatni_odczyt_utc', 'pobrano_utc'
]
WIDOKI = ['widok_od', 'widok_do', 'liczba_zgloszen',
          'pobrano_utc', 'rodzaj_odczytu']
MIGAWKI = UBYTKI + ['data_obserwacji', 'moment_odczytu_utc']

MIESIACE = {
    'styczeń': 1, 'luty': 2, 'marzec': 3, 'kwiecień': 4,
    'maj': 5, 'czerwiec': 6, 'lipiec': 7, 'sierpień': 8,
    'wrzesień': 9, 'październik': 10, 'listopad': 11, 'grudzień': 12,
}

SZCZEGOL = re.compile(
    r'Jednostka\s+(?P<jednostka>.*?)\s+'
    r'Moc Osiągalna:\s*(?P<moc>[\d.,]+)\s*MW\s+'
    r'Wielkość ubytku:\s*(?P<ubytek>[\d.,]+)\s*MW\s+'
    r'Typ zdarzenia:\s*(?P<typ>Planowane|Nieplanowane|Inne)\s+'
    r'Początek zdarzenia:\s*'
    r'(?P<poczatek>\d{2}-\d{2}-\d{4}\s+\d{2}:\d{2})\s+'
    r'Koniec zdarzenia:\s*'
    r'(?P<koniec>\d{2}-\d{2}-\d{4}\s+\d{2}:\d{2})',
    re.IGNORECASE,
)
DATA_WIDOKU = re.compile(
    r'Moc\s+Osiągalna.*?\b(' + '|'.join(MIESIACE) +
    r')\s+(20\d{2})\s+(?:Pn|Wt|Śr|Cz|Pt|So|N)\s+(\d{1,2})\b',
    re.IGNORECASE | re.DOTALL,
)


def czas_utc():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def numer(value):
    try:
        n = Decimal(str(value).replace(' ', '').replace(',', '.'))
        if not n.is_finite():
            raise ValueError('Nieprawidłowa liczba')
        return format(n.normalize(), 'f')
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f'Nieprawidłowa liczba: {value!r}') from exc


def data_iso(value):
    for fmt in ('%Y-%m-%d %H:%M:%S', '%Y-%m-%d %H:%M', '%d-%m-%Y %H:%M'):
        try:
            return datetime.strptime(str(value).strip(), fmt).strftime('%Y-%m-%d %H:%M:%S')
        except ValueError:
            pass
    raise ValueError(f'Nieprawidłowa data: {value!r}')


def klucz(row):
    return (
        str(row['jednostka']).strip(), numer(row['moc']), numer(row['ubytek']),
        str(row['typ']).strip().casefold(), data_iso(row['poczatek']),
        data_iso(row['koniec'])
    )


def odczytaj_csv(path, wymagane, *, opcjonalny=False):
    if not path.is_file():
        if opcjonalny:
            return [], []
        raise FileNotFoundError(
            f'Brak pliku {path.name}. Najpierw skopiuj najnowszy plik '
            'z GitHuba do folderu obok pobierz_tge.py.'
        )
    with path.open('r', encoding='utf-8-sig', newline='') as f:
        reader = csv.DictReader(f, delimiter=';')
        kol = list(reader.fieldnames or [])
        if not set(wymagane).issubset(kol):
            raise ValueError(f'{path.name}: brakuje kolumn {set(wymagane) - set(kol)}')
        rows = list(reader)
        if any(None in row for row in rows):
            raise ValueError(f'{path.name}: problem z separatorem CSV (wymagany średnik).')
    return rows, kol


def zapisz_csv(path, rows, cols):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + '.tmp')
    with tmp.open('w', encoding='utf-8-sig', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=cols, delimiter=';', extrasaction='ignore')
        writer.writeheader()
        writer.writerows(rows)
    os.replace(tmp, path)


def parse_html(html, moment):
    text = BeautifulSoup(html, 'html.parser').get_text(' ', strip=True)
    chunks = re.split(r'Informacje o ubytku', text, flags=re.IGNORECASE)
    if len(chunks) < 2:
        raise ValueError('Strona GPI nie ma szczegółów zgłoszeń. Nie zapisuję danych.')

    m = DATA_WIDOKU.search(chunks[0])
    if not m:
        raise ValueError('Nie znalazłem daty początku 14-dniowego widoku GPI.')
    start_view = date(int(m.group(2)), MIESIACE[m.group(1).lower()], int(m.group(3)))
    end_view = start_view + timedelta(days=13)

    rows, seen = [], set()
    for item in chunks[1:]:
        m = SZCZEGOL.search(item)
        if not m:
            raise ValueError(f'Nie rozpoznałem szczegółów zgłoszenia: {item[:150]}')
        row = m.groupdict()
        row['poczatek'] = data_iso(row['poczatek'])
        row['koniec'] = data_iso(row['koniec'])
        row['moc'] = numer(row['moc'])
        row['ubytek'] = numer(row['ubytek'])
        a, b = datetime.fromisoformat(row['poczatek']), datetime.fromisoformat(row['koniec'])
        if b <= a:
            raise ValueError('Zgłoszenie ma odwrócony zakres dat.')
        k = klucz(row)
        if k in seen:
            continue
        seen.add(k)
        row.update({
            'widok_od': start_view.isoformat(),
            'zrodlo': '',
            'id_wersji': hashlib.sha256('\x1f'.join(k).encode('utf-8')).hexdigest()[:24],
            'pierwszy_odczyt_utc': moment,
            'ostatni_odczyt_utc': moment,
            'pobrano_utc': moment,
        })
        rows.append(row)
    return start_view, end_view, rows


def pobierz_widok(rodzaj, params=None):
    with requests.Session() as session:
        for i, address in enumerate(ADRESY, 1):
            if i > 1:
                time.sleep(5 if i == 2 else 10)
            print(f'  {rodzaj}: próba {i}/3', flush=True)
            try:
                resp = session.get(
                    address, params=params,
                    headers={
                        'User-Agent': 'Mozilla/5.0',
                        'Accept-Language': 'pl-PL,pl;q=0.9',
                        'Accept': 'text/html,application/xhtml+xml',
                    },
                    timeout=(25, 75), allow_redirects=True,
                )
                resp.raise_for_status()
                if not resp.url.startswith('https://gpi.tge.pl/'):
                    raise ValueError('Nieoczekiwany adres końcowy: ' + resp.url)
                view_start, view_end, rows = parse_html(resp.content, czas_utc())
                return view_start, view_end, rows, resp.url
            except (requests.RequestException, ValueError) as exc:
                print(f'    Nieudane: {exc}', flush=True)
                if i == len(ADRESY):
                    raise RuntimeError(f'Nie udało się pobrać widoku: {rodzaj}') from exc
    raise RuntimeError('Brak pobierania')


def zachodzi_na_dzien(row, day):
    a = datetime.fromisoformat(row['poczatek'])
    b = datetime.fromisoformat(row['koniec'])
    day_start = datetime.combine(day, datetime.min.time())
    return a < day_start + timedelta(days=1) and b > day_start


def wykonaj():
    source_base = FOLDER / 'tge_ubytki.csv'
    source_views = FOLDER / 'tge_widoki.csv'
    source_status = FOLDER / 'stan_tge.json'

    # Pliki znajdują się obok skryptu (lokalnie) albo poziom wyżej (kody/).
    base, base_cols = odczytaj_csv(source_base, PODSTAWOWE)
    views, view_cols = odczytaj_csv(source_views, WIDOKI[:4])
    # Lokalnie plik miesięczny leży obok skryptu. W pełnym repozytorium
    # może znajdować się w podfolderze tge_migawki.
    plik_miesiaca = f'{DZIS:%Y-%m}.csv'
    flat_snapshot = FOLDER / plik_miesiaca
    nested_snapshot = FOLDER / 'tge_migawki' / plik_miesiaca
    if flat_snapshot.is_file():
        snapshot_file = flat_snapshot
    elif nested_snapshot.is_file():
        snapshot_file = nested_snapshot
    else:
        snapshot_file = flat_snapshot if FOLDER == SKRYPT else nested_snapshot
        print(f'UWAGA: brak pliku {plik_miesiaca}; tworzę nowy. '
              'Jeżeli na GitHubie jest starsza migawka, pobierz ją najpierw!')
    snap, snap_cols = odczytaj_csv(
        snapshot_file, PODSTAWOWE + ['data_obserwacji'], opcjonalny=True
    )
    known = {klucz(r) for r in base}

    print('\nTGE: uzupełnienie ostatnich dni')
    print(f'Folder: {FOLDER}')
    print(f'Zakres: {OD_DNIA} - {DZIS} ({ILE_DNI} dni)')
    print(f'Historycznych rekordów: {len(base)}')

    # Oficjalny link "Poprzednie" z GPI: bieżąca data i kierunek -1.
    params_previous = {
        '_gpicalendar_WAR_gpicalendarportlet_current': DZIS.isoformat(),
        '_gpicalendar_WAR_gpicalendarportlet_direction': '-1',
        'p_p_col_count': '1',
        'p_p_col_id': 'column-1',
        'p_p_id': 'gpicalendar_WAR_gpicalendarportlet',
        'p_p_lifecycle': '0',
        'p_p_mode': 'view',
        'p_p_state': 'normal',
    }

    old_view = None
    if ILE_DNI > 1:
        old_view = pobierz_widok('poprzedni widok', params_previous)
        start, end, _, _ = old_view
        yesterday = DZIS - timedelta(days=1)
        if not (start <= OD_DNIA <= yesterday <= end):
            raise ValueError(
                f'Poprzedni widok obejmuje {start} - {end}, '
                f'a potrzebujemy {OD_DNIA} - {yesterday}. Nic nie zapisano.'
            )

    now_view = pobierz_widok('bieżący widok')
    start_now, end_now, _, _ = now_view
    if not (start_now <= DZIS <= end_now):
        raise ValueError(
            f'Bieżący widok GPI obejmuje {start_now} - {end_now}, '
            f'bez dnia {DZIS}. Nic nie zapisano.'
        )

    candidates = []
    for view in [old_view, now_view]:
        if view is None:
            continue
        candidates.extend(view[2])

    # Ograniczamy historię do zdarzeń aktywnych w ostatnich 7 dniach.
    all_days = [OD_DNIA + timedelta(days=i) for i in range(ILE_DNI)]
    unique = {}
    for row in candidates:
        if any(zachodzi_na_dzien(row, d) for d in all_days):
            unique.setdefault(klucz(row), row)

    additions = []
    for k, row in unique.items():
        if k not in known:
            known.add(k)
            additions.append(row)

    # Migawką JEST TYLKO BIEŻĄCY odczyt dla dnia dzisiejszego.
    # Starsze dni uzupełniamy retrospektywnie; nie przypisujemy im
    # rzekomych momentów pobrania sprzed kilku dni.
    today_rows = [r for r in now_view[2] if zachodzi_na_dzien(r, DZIS)]
    now = czas_utc()
    if not today_rows:
        raise ValueError('Brak aktywnych zgłoszeń w bieżącym widoku - sprawdź GPI.')
    today_snapshot = [
        {**r, 'data_obserwacji': DZIS.isoformat(), 'moment_odczytu_utc': now}
        for r in today_rows
    ]
    earlier_snapshot = [r for r in snap if r.get('data_obserwacji') != DZIS.isoformat()]

    new_views = [
        r for r in views
        if not (
            r.get('pobrano_utc', '')[:10] == now[:10]
            and r.get('rodzaj_odczytu') in ('RETRO', 'BIEZACY')
        )
    ]
    if ILE_DNI > 1:
        prev_dates = all_days[:-1]
        count_retro = sum(
            any(zachodzi_na_dzien(r, d) for d in prev_dates)
            for r in unique.values()
        )
        new_views.append({
            'widok_od': OD_DNIA.isoformat(),
            'widok_do': (DZIS - timedelta(days=1)).isoformat(),
            'liczba_zgloszen': str(count_retro),
            'pobrano_utc': now,
            'rodzaj_odczytu': 'RETRO',
        })
    new_views.append({
        'widok_od': DZIS.isoformat(),
        'widok_do': DZIS.isoformat(),
        'liczba_zgloszen': str(len(today_rows)),
        'pobrano_utc': now,
        'rodzaj_odczytu': 'BIEZACY',
    })

    status = {
        'data': DZIS.isoformat(),
        'status': 'OK',
        'blad': '',
        'moment_utc': now,
        'url': now_view[3],
        'uzupelniono_od': OD_DNIA.isoformat(),
        'uzupelniono_do': DZIS.isoformat(),
        'uwaga': 'Dni sprzed dzisiaj odczytano retrospektywnie z GPI.',
    }

    # Backup przygotowanych plików, zanim nadpiszemy cokolwiek.
    backup = FOLDER / 'kopie_zapasowe' / datetime.now().strftime('%Y-%m-%d_%H-%M-%S_%f')
    backup.mkdir(parents=True, exist_ok=False)
    for item in (source_base, source_views, source_status, snapshot_file):
        if item.is_file():
            dest = backup / item.relative_to(FOLDER)
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(item, dest)

    if additions:
        zapisz_csv(source_base, base + additions, base_cols + [c for c in UBYTKI if c not in base_cols])
    zapisz_csv(source_views, new_views, view_cols + [c for c in WIDOKI if c not in view_cols])
    zapisz_csv(snapshot_file, earlier_snapshot + today_snapshot,
               snap_cols + [c for c in MIGAWKI if c not in snap_cols])
    source_status.write_text(json.dumps(status, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    (FOLDER / 'blad_tge.txt').unlink(missing_ok=True)

    print('\n' + '=' * 60)
    print('SUKCES - pobrano TGE i uzupełniono ostatnie dni!')
    print(f'Zakres analizowany: {OD_DNIA} - {DZIS}')
    print(f'Unikalnych zdarzeń z tego zakresu: {len(unique)}')
    print(f'Nowych wpisów dopisanych do tge_ubytki.csv: {len(additions)}')
    print(f'Łącznie rekordów w bazie: {len(base) + len(additions)}')
    print(f'Kopia zapasowa: {backup}')
    print('Na GitHuba prześlij: tge_ubytki.csv, tge_widoki.csv, stan_tge.json')
    print(f'  oraz plik {DZIS:%Y-%m}.csv do folderu tge_migawki na GitHubie.')
    print('Folderu kopie_zapasowe NIE wysyłaj.')
    print('=' * 60)


def main():
    try:
        wykonaj()
    except Exception as exc:
        print('\nBŁĄD! Nie wysyłaj nowych plików na GitHuba.')
        print(f'{type(exc).__name__}: {exc}', flush=True)
        (FOLDER / 'blad_tge.txt').write_text(
            f'{czas_utc()}\n{type(exc).__name__}: {exc}\n', encoding='utf-8'
        )
        return 1
    return 0


if __name__ == '__main__':
    wynik = main()
    # W Spyderze/Jupyterze nie zgłaszaj SystemExit po poprawnym wykonaniu.
    if wynik != 0:
        raise SystemExit(wynik)
