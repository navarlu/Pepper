"""Czech docstrings for the experiment tools.

These get attached to copies of the English tool functions in
`tools_cs/__init__.py`. They mirror the structure of the English
docstrings 1:1 so the parameter-doc parser produces matching schema
entries (Czech text, identical parameter names).

Tool argument values like `emotion`, `activity`, `day`, and
`direction` keep their English enum values — those are API contracts
the LLM emits verbatim.
"""

from __future__ import annotations


SEND_MESSAGE_TO_USER = """\
Promluv k uživateli. TOTO JE JEDINÝ ZPŮSOB, JAK SE TVÁ SLOVA
DOSTANOU K UŽIVATELI. Běžný text asistenta se nikdy nevyslovuje —
pouze argument `text` tohoto nástroje.

Volání tohoto nástroje UKONČUJE aktuální tah. Po promluvě už nemůžeš
zavolat další nástroj, takže VŠECHNY informace si zjisti PŘEDTÍM, než
toto zavoláš. Používej právě jednou za uživatelův tah, jako poslední
akci.

text: slova, která Pepper řekne. TTS čte text doslova, takže piš
    prostou konverzační prózu — bez markdownu, bez JSONu, bez
    scénických pokynů v závorkách, bez názvů nástrojů.
emotion: řeč těla pro tuto promluvu — greet, think, explain, bow,
    happy, dont_know.
"""


LOOKUP_PERSON = """\
Vyhledej kontaktní údaje osoby (telefon, e-mail, místnost) v
veřejném seznamu zaměstnanců. Toleruje fonetické aproximace
neznámých příjmení — předej přesně to, co uživatel řekl, a nástroj
sám vyzkouší běžné varianty pravopisu.

Příjmení je skutečný vyhledávací klíč. Křestní jméno je VOLITELNÉ
a slouží jen k rozlišení, když více lidí sdílí stejné příjmení.
Pokud uživatel uvedl jen příjmení (nebo řekl „pan/paní <příjmení>“),
předej first_name="" — nástroj vrátí nejvýše postaveného člena a
ostatní vypíše, abys se rozhodl, zda se uživatele doptat, nebo
rovnou odpovědět.

first_name: křestní jméno osoby. Volitelné („" je v pořádku).
    Tituly se ignorují.
surname: pouze příjmení — bez titulů. Povinné.
emotion: řeč těla při vyhledávání. Výchozí 'think'.
request_heartbeat: True (výchozí), aby smyčka pokračovala a ty
    pak mohl/a promluvit přes send_message_to_user. False okamžitě
    smyčku ukončí — nastav False jen pokud opravdu chceš ukončit
    tah, aniž bys cokoliv řekl/a uživateli.
"""


FIND_PATH_TO_ROOM = """\
Získej cestu do místnosti v této budově. Uživatel s tebou už stojí
u hlavního vchodu.

Volej, když se uživatel ptá, jak se dostat do místnosti nebo kde
nějaká místnost je.

room: číslo místnosti přepiš DOSLOVA tak, jak ho uživatel řekl.
    „230“ uživatele je „230“ — ne „23“. Příklady: '101', 'A-205',
    'B-310'.
emotion: řeč těla při hledání trasy. Výchozí 'think'; vyber, co
    se hodí k situaci (např. 'happy', pokud se uživatel ptal
    nadšeně).
request_heartbeat: True (výchozí), aby smyčka pokračovala a ty
    mohl/a předat cestu přes send_message_to_user.
"""


MENSA_MENU = """\
Zjisti, co je v jídelně poblíž na jídelníčku.

Vrací všechny dny, které jsou aktuálně zveřejněné — obvykle tento
týden a příští týden. Každý den má seznam `dishes` a každé jídlo
má kategorii jako „soup“, „main“, „salad“, „vegetarian“.

Volej, když se uživatel ptá, co může jíst, co je k obědu, co je
na jídelníčku, nebo na jídelnu / menzu / bufet.

emotion: řeč těla při načítání. Výchozí 'think'.
request_heartbeat: True (výchozí), aby smyčka pokračovala a ty
    mohl/a předat jídelníček přes send_message_to_user.
"""


SUBJECT_SCHEDULE = """\
Vyhledej veřejný rozvrh pro předmět / kurz.

Použij, když se uživatel ptá, kdy nebo kde se koná přednáška,
laboratoř, cvičení nebo seminář nějakého předmětu.

Argument `subject` MUSÍ být krátký kód předmětu: 2 až 4 písmena
s volitelnou jednou koncovou číslicí (např. XYZ, AB, WXYZ, XYZ1,
AB2). Názvy předmětů ani dlouhé kódy jako A1B23CDE NEJSOU přijímány
— v takovém případě požádej uživatele přes send_message_to_user
o krátký kód.

activity: filtr TYPU akce. Předej „lecture“ / „exercise“ /
„laboratory“, pokud uživatel typ uvedl, jinak „".
day: volitelný filtr na den v týdnu — prázdné, Monday, …, Friday.

Vždy si přečti pole `instruction` ve výsledku a řiď se jím.

subject: krátký intranetový kód předmětu, 2 až 4 písmena s
    volitelnou číslicí.
activity: filtr typu akce nebo „".
day: volitelný filtr na den v týdnu.
emotion: řeč těla při načítání. Výchozí 'think'.
request_heartbeat: True (výchozí), aby smyčka pokračovala.
"""


GET_TIME = """\
Vrať aktuální místní čas. Používej jen tehdy, když se uživatel
výslovně ptá, kolik je hodin.

emotion: řeč těla při pohledu na hodiny. Výchozí 'think';
    klidně přepiš, pokud se hodí jiná nálada.
request_heartbeat: True (výchozí), aby smyčka pokračovala.
"""


ADJUST_VOLUME = """\
Nastav, aby Pepper mluvil hlasitěji nebo tišeji při dalších
odpovědích.

Každé volání posune hlasitost reproduktoru o 20 (omezeno na 0..100).
Změna se projeví zhruba do jedné sekundy — TVÁ DALŠÍ promluva přes
`send_message_to_user` bude na nové hlasitosti; aktuální promluva
zůstane beze změny.

Používej, když uživatel výslovně požádá („mluvte hlasitěji“, „prosím
nahlas“, „je to moc nahlas“, „můžete tišeji“). Sám tento nástroj
nenabízej.

direction: 'louder' pro zvýšení hlasitosti, 'quieter' pro snížení.
request_heartbeat: True (výchozí), abys pak mohl/a promluvit přes
    send_message_to_user.
"""
