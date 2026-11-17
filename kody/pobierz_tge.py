
def pobierz_tge():

    # Alternatywne pobieranie przez Jina Reader.
    # GitHub laczy sie z r.jina.ai, a nie bezposrednio z GPI.

    adres = (
        "https://r.jina.ai/"
        "https://gpi.tge.pl/pl/zestawienie-ubytkow"
    )

    print(
        "Pobieram GPI przez alternatywny serwer...",
        flush=True
    )

    response = requests.get(
        adres,
        headers={
            "X-Respond-With": "html",
            "X-No-Cache": "true",
            "X-Locale": "pl-PL",
            "Accept": "text/html, text/plain, */*"
        },
        timeout=(15, 90)
    )

    response.raise_for_status()

    soup = BeautifulSoup(
        response.content,
        "html.parser"
    )

    tekst = soup.get_text(" ", strip=True)

    # Sprawdzamy, czy dostalismy faktyczne
    # zestawienie GPI, a nie strone bledu.

    fragmenty = re.split(
        r"Informacje o ubytku",
        tekst,
        flags=re.IGNORECASE
    )

    if len(fragmenty) < 2:
        raise RuntimeError(
            "Posrednik nie zwrocil szczegolow "
            "ubytkow GPI. Nie zapisuje danych. "
            f"Dlugosc odpowiedzi: {len(tekst)}"
        )

    # Sprawdzenie daty widoku.
    # Nie zapisujemy danych z nieaktualnego kalendarza.

    miesiace = {
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

    wzor_daty = (
        r"Moc\s+Osiągalna.{0,300}?\b("
        + "|".join(miesiace)
        + r")\s+(20\d{2})\s+"
        r"(?:Pn|Wt|Śr|Cz|Pt|So|N)\s+(\d{1,2})\b"
    )

    dopasowanie_daty = re.search(
        wzor_daty,
        fragmenty[0],
        re.IGNORECASE | re.DOTALL
    )

    if not dopasowanie_daty:
        raise RuntimeError(
            "Nie mozna potwierdzic daty widoku GPI. "
            "Nie zapisuje danych."
        )

    data_widoku = datetime(
        int(dopasowanie_daty.group(2)),
        miesiace[
            dopasowanie_daty.group(1).lower()
        ],
        int(dopasowanie_daty.group(3))
    ).date()

    if data_widoku != DZIS:
        raise RuntimeError(
            f"Nieaktualne dane GPI: {data_widoku}. "
            f"Oczekiwano {DZIS}. "
            "Nie zapisuje danych."
        )

    poczatek_dnia = datetime.combine(
        DZIS,
        datetime.min.time()
    )

    koniec_dnia = poczatek_dnia + timedelta(days=1)

    moment = moment_utc()
    wyniki = []

    for fragment in fragmenty[1:]:

        match = WZOR.search(fragment)

        if not match:
            raise RuntimeError(
                "Nie rozpoznano zgloszenia GPI: "
                + fragment[:150]
            )

        r = match.groupdict()

        r["poczatek"] = data_iso(r["poczatek"])
        r["koniec"] = data_iso(r["koniec"])

        r["moc"] = str(
            float(r["moc"].replace(",", "."))
        )

        r["ubytek"] = str(
            float(r["ubytek"].replace(",", "."))
        )

        start = datetime.fromisoformat(
            r["poczatek"]
        )

        end = datetime.fromisoformat(
            r["koniec"]
        )

        if end <= start:
            raise ValueError(
                "Niepoprawny przedzial ubytku GPI"
            )

        if not (
            start < koniec_dnia
            and end > poczatek_dnia
        ):
            continue

        r["id_wersji"] = id_zdarzenia(r)
        r["widok_od"] = DZIS.isoformat()
        r["zrodlo"] = ""
        r["pierwszy_odczyt_utc"] = moment
        r["ostatni_odczyt_utc"] = moment
        r["pobrano_utc"] = moment

        wyniki.append(r)

    print(
        "Zgloszenia w widoku GPI:",
        len(fragmenty) - 1
    )

    print(
        "Zgloszenia aktywne dzisiaj:",
        len(wyniki)
    )

    return wyniki
