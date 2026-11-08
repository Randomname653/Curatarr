"""Curatarr — what each Wrap-Up card says, in English and German.

Every card the miner makes (services/wrapup_insights) has a ``key``: what
kind of fact it is ("tempo_down", "wait_longest", "first_title"). Its claim
is rendered from that key and the card's ``slots``, so the same fact can be
said in the language of the story. And when the writer could not write a
slide (services/wrapup_writer._template), the slide says the claim with the
headline and the warm line that belong to THAT fact - not to its beat or
its kind: "you watched less than before" is not "you and the crowd,
happily apart" (UX design record, stage 3.7, review on real data).

A warm line is optional: where none fits a fact, there is none. Warm lines
judge the work, never the person, and know no streaks or guilt.

The English claims are exactly the miner's own sentences - the editor reads
them - so ``render(key, slots, "en")`` must not drift; the German ones
format numbers the German way ("8,4 Stunden", "44 %", "14.10.2026").
Pure: no I/O.
"""
import string
from typing import Optional

# headline, claim, warm line (None: none fits) - per key, per language.
# Format specs: n = number, p = share as percent, d = ISO date, m = YYYY-MM
# month, mn = month name of YYYY-MM, l = list of names, w / wp / wa / wA =
# a word from the language's word table (plural compound, adverb,
# capitalised adverb), dec / decd = a decade (nominative / dative plural).
TEXTS = {
    "tempo_up": {
        "en": ("More than last time", "{hours:n} hours, against {prev:n} in the same span before.",
               "There was a lot worth watching."),
        "de": ("Mehr als zuletzt", "{hours:n} Stunden, gegenüber {prev:n} im gleichen Zeitraum davor.",
               "Es gab viel Sehenswertes.")},
    "tempo_down": {
        "en": ("A quieter stretch", "{hours:n} hours, against {prev:n} in the same span before.", None),
        "de": ("Ruhiger als zuletzt", "{hours:n} Stunden, gegenüber {prev:n} im gleichen Zeitraum davor.", None)},
    "night_share": {
        "en": ("The late hours", "{share:p} of it after 23:00.", "The night belongs to the good stories."),
        "de": ("Die späten Stunden", "{share:p} davon nach 23 Uhr.", "Die Nacht gehört den guten Geschichten.")},
    "streak": {
        "en": ("On a run", "A {days}-day run without a day off.", "Something kept calling you back."),
        "de": ("Am Stück", "{days} Tage am Stück, ohne einen Tag Pause.", "Etwas hat dich immer wieder zurückgeholt.")},
    "big_day": {
        "en": ("The day it happened", "{date:d}: {hours:n} hours in one day, {plays} plays.",
               "A day worth remembering."),
        "de": ("Der große Tag", "{date:d}: {hours:n} Stunden an einem Tag, {plays:n} Wiedergaben.",
               "Ein Tag zum Erinnern.")},
    "top_title": {
        "en": ("Where the hours went", "{title} took {hours:n} hours, {share:p} of the time.", None),
        "de": ("Wohin die Stunden gingen", "{title}: {hours:n} Stunden, {share:p} der Zeit.", None)},
    "top_title_clear": {
        "en": ("Where the hours went", "{title} took {hours:n} hours, {share:p} of the time.",
               "Nothing else came close."),
        "de": ("Wohin die Stunden gingen", "{title}: {hours:n} Stunden, {share:p} der Zeit.",
               "Nichts anderes kam heran.")},
    "binge": {
        "en": ("One more episode", "{episodes} episodes of {series} in one sitting on {date:d}.",
               "It earned every one of them."),
        "de": ("Noch eine Folge", "{episodes} Folgen {series} am Stück, am {date:d}.",
               "Jede davon hat es sich verdient.")},
    "contrarian_rated": {
        "en": ("The twist", "You rated {title} {you:n}; the community gives it {community} ({votes:n} votes).",
               "You and the crowd, happily apart."),
        "de": ("Die Wendung", "Du gibst {title} eine {you:n}; die Community gibt {community:n} ({votes:n} Stimmen).",
               "Du und die anderen, angenehm verschieden.")},
    "contrarian_rewatched": {
        "en": ("The twist", "You kept rewatching {title}; the community gives it {community} ({votes:n} votes).",
               "You and the crowd, happily apart."),
        "de": ("Die Wendung", "Du hast {title} immer wieder geschaut; die Community gibt {community:n} "
                              "({votes:n} Stimmen).", "Du und die anderen, angenehm verschieden.")},
    "dismissal": {
        "en": ("Left behind", "You walked away from {title} ({how}); the community gives it {community}.",
               "Acclaim is not an obligation."),
        "de": ("Liegen gelassen", "Du hast {title} hinter dir gelassen ({how:w}); die Community gibt {community:n}.",
               "Lob ist keine Pflicht.")},
    "comfort_replays": {
        "en": ("The comfort title", "{title} again: {replays} replays.", "Some stories are a place to come home to."),
        "de": ("Der Wohlfühltitel", "{title}, schon wieder: {replays}-mal wiederholt.",
               "Manche Geschichten sind ein Zuhause.")},
    "court_keep": {
        "en": ("Before the court", "The judge was {score}% sure {title} should go. It stayed.",
               "Some titles earn a second chance."),
        "de": ("Vor Gericht", "Der Richter war sich zu {score} % sicher, dass {title} gehen sollte. Es blieb.",
               "Manche Titel verdienen eine zweite Chance.")},
    "court_debates": {
        "en": ("The debates", "Debates: you talked the curator out of {won}, it talked you into {lost}.",
               "Good arguments on both sides."),
        "de": ("Die Debatten", "Debatten: Du hast den Curator {won}-mal umgestimmt, er dich {lost}-mal.",
               "Gute Argumente auf beiden Seiten.")},
    "court_decided": {
        "en": ("The verdicts", "{decided} titles decided: {kept} kept, {deleted} deleted, "
                               "{overrides} over the curator's objection.", None),
        "de": ("Die Urteile", "{decided} Titel entschieden: {kept} behalten, {deleted} gelöscht, "
                              "{overrides} gegen den Einwand des Curators.", None)},
    "balance": {
        "en": ("In and out", "{gb_in:n} GB in, {gb_out:n} GB out — {verdict}.", None),
        "de": ("Rein und raus", "{gb_in:n} GB rein, {gb_out:n} GB raus – {verdict:w}.", None)},
    "wait_longest": {
        "en": ("The long wait", "{title} waited {days} days on the server before you pressed play.",
               "Good things keep."),
        "de": ("Die lange Wartezeit", "{title} wartete {days:n} Tage auf dem Server, bis du auf Play gedrückt hast.",
               "Gutes hält sich.")},
    "same_week": {
        "en": ("Fresh off the shelf", "{share:p} of the new titles played within a week of arriving.",
               "Straight from the shelf to the screen."),
        "de": ("Frisch eingetroffen", "{share:p} der neuen Titel liefen innerhalb einer Woche nach ihrer Ankunft.",
               "Direkt vom Regal auf den Bildschirm.")},
    "auteur_director": {
        "en": ("Your director", "Your director: {name}, {count} titles, {hours:n} hours ({titles:l}).",
               "Right up your street."),
        "de": ("Deine Regie", "Deine Regie: {name}, {count} Titel, {hours:n} Stunden ({titles:l}).",
               "Genau nach deinem Geschmack.")},
    "auteur_studio": {
        "en": ("Your studio", "Your studio: {name}, {count} titles, {hours:n} hours ({titles:l}).",
               "Right up your street."),
        "de": ("Dein Studio", "Dein Studio: {name}, {count} Titel, {hours:n} Stunden ({titles:l}).",
               "Genau nach deinem Geschmack.")},
    "auteur_actor": {
        "en": ("A familiar face", "Your face: {name}, {count} titles, {hours:n} hours ({titles:l}).",
               "Right up your street."),
        "de": ("Ein vertrautes Gesicht", "Dein Gesicht: {name}, {count} Titel, {hours:n} Stunden ({titles:l}).",
               "Genau nach deinem Geschmack.")},
    "drift": {
        "en": ("Something new", "Taste against your own past: {label}.", None),
        "de": ("Etwas Neues", "Dein Geschmack gegenüber früher: {label:w}.", None)},
    "taste_turn": {
        "en": ("The turn", "{month:m} was the month it turned: {titles:l}.", None),
        "de": ("Die Wende", "{month:m} war der Monat der Wende: {titles:l}.", None)},
    "discovery_new": {
        "en": ("New ground", "{share:p} of the time on titles new to you.", "Plenty worth discovering."),
        "de": ("Neues Terrain", "{share:p} der Zeit für Titel, die neu für dich waren.", "Viel Neues, das sich lohnte.")},
    "discovery_old": {
        "en": ("Old friends", "{share:p} of the time on titles new to you.", "Old favourites, well chosen."),
        "de": ("Alte Bekannte", "{share:p} der Zeit für Titel, die neu für dich waren.", "Alte Lieblinge, gut gewählt.")},
    "on_repeat": {
        "en": ("On repeat", "'{track}' by {artist}, {plays} times on {date:d}.", "Some songs earn it."),
        "de": ("In Dauerschleife", "„{track}“ von {artist}, {plays}-mal am {date:d}.", "Manche Songs verdienen das.")},
    "skipped": {
        "en": ("The skip button", "'{track}' by {artist}: skipped {skips} times.", None),
        "de": ("Die Skip-Taste", "„{track}“ von {artist}: {skips}-mal übersprungen.", None)},
    "moods_split": {
        "en": ("Two soundtracks", "{day:wA}s ran {day_mood}, {late}s {late_mood}.", None),
        "de": ("Zwei Soundtracks", "{day:wA} lief es {day_mood}, {late:wa} {late_mood}.", None)},
    "top_mood": {
        "en": ("The mood", "{share:p} of the tagged listening was {mood}.", None),
        "de": ("Die Stimmung", "{share:p} des getaggten Hörens war {mood}.", None)},
    "device_night": {
        "en": ("The late screen", "After 23:00 it was the {player}: {share:p} of {hours:n} night hours.", None),
        "de": ("Der späte Bildschirm", "Nach 23 Uhr lief es auf {player}: {share:p} von {hours:n} Nachtstunden.",
               None)},
    "decade": {
        "en": ("Back to the {decade}", "Your {decade}: {share:p} of the hours went to titles from the {decade}.", None),
        "de": ("Zurück in die {decade:dec}", "Deine {decade:dec}: {share:p} der Stunden gingen an Titel aus den "
                                             "{decade:decd}.", None)},
    "decade_titled": {
        "en": ("Back to the {decade}", "Your {decade}: {share:p} of the hours went to titles from the {decade} "
                                       "({titles:l}).", None),
        "de": ("Zurück in die {decade:dec}", "Deine {decade:dec}: {share:p} der Stunden gingen an Titel aus den "
                                             "{decade:decd} ({titles:l}).", None)},
    "ritual_title": {
        "en": ("A ritual", "{weekday} {daypart}s were {title}: {weeks} of {of_weeks} weeks.",
               "Some hours are spoken for."),
        "de": ("Ein Ritual", "{weekday:w}{daypart:wp} gehörten {title}: {weeks} von {of_weeks} Wochen.",
               "Manche Stunden sind fest vergeben.")},
    "ritual_kind": {
        "en": ("A ritual", "{weekday} {daypart}s were {kind:w}: {weeks} of {of_weeks} weeks.",
               "Some hours are spoken for."),
        "de": ("Ein Ritual", "{weekday:w}{daypart:wp} gehörten {kind:w}: {weeks} von {of_weeks} Wochen.",
               "Manche Stunden sind fest vergeben.")},
    "comfort_genre": {
        "en": ("The genre you came back to", "The genre you came back to: {genre}, {plays} plays across {titles:l}.",
               "A kind of story worth returning to."),
        "de": ("Dein Wohlfühl-Genre", "Das Genre, zu dem du zurückkamst: {genre}, {plays} Wiedergaben mit "
                                      "{titles:l}.", "Eine Art Geschichte, zu der man gern zurückkehrt.")},
    "of_the_month": {
        "en": ("Title of the month", "{month:mn} belonged to {title}: {share:p} of the month's hours, {hours:n} hours.",
               "Nothing else came close."),
        "de": ("Titel des Monats", "Der {month:mn} gehörte {title}: {share:p} der Stunden des Monats, "
                                   "{hours:n} Stunden.", "Nichts anderes kam heran.")},
    "night_shift": {
        "en": ("The night shift", "{title} was the after-midnight series: {night_plays} of its {plays} plays "
                                  "after 23:00.", "Some series are made for the late hours."),
        "de": ("Die Nachtschicht", "{title} war die Serie für die späten Stunden: {night_plays} von {plays} "
                                   "Wiedergaben nach 23 Uhr.", "Manche Serien sind für die späten Stunden gemacht.")},
    "first_title": {
        "en": ("How it began", "It started with {title}.", None),
        "de": ("Wie es anfing", "Es fing an mit {title}.", None)},
}

# Words the facts carry in English, said in German.
_WORDS = {
    "en": {"w": {"movie": "films", "show": "series", "anime": "anime", "music": "music"},
           "wA": {"morning": "Morning", "afternoon": "Afternoon", "evening": "Evening", "night": "Night"}},
    "de": {
        "w": {"Monday": "Montag", "Tuesday": "Dienstag", "Wednesday": "Mittwoch", "Thursday": "Donnerstag",
              "Friday": "Freitag", "Saturday": "Samstag", "Sunday": "Sonntag",
              "movie": "Filmen", "show": "Serien", "anime": "Anime", "music": "der Musik",
              "dropped": "abgebrochen", "deleted": "gelöscht", "rated low": "schwach bewertet",
              "shifting": "im Wandel", "new direction": "eine neue Richtung",
              "a hoarder": "der Server wuchs", "a pruner": "der Server wurde leichter", "in balance": "im Gleichgewicht",
              "the server grew": "der Server wuchs", "the server got lighter": "der Server wurde leichter"},
        "wp": {"morning": "morgen", "afternoon": "nachmittage", "evening": "abende", "night": "nächte"},
        "wa": {"morning": "morgens", "afternoon": "nachmittags", "evening": "abends", "night": "nachts"},
        "wA": {"morning": "Morgens", "afternoon": "Nachmittags", "evening": "Abends", "night": "Nachts"}},
}
_MONTHS = {"en": ("January", "February", "March", "April", "May", "June", "July", "August", "September",
                  "October", "November", "December"),
           "de": ("Januar", "Februar", "März", "April", "Mai", "Juni", "Juli", "August", "September",
                  "Oktober", "November", "Dezember")}


def num(x, lang: str = "en") -> str:
    """58.3 → '58.3' / '58,3'; 20.0 → '20'; 2100 → '2,100' / '2.100'."""
    x = float(x or 0)
    s = f"{x:,.1f}".rstrip("0").rstrip(".") if round(x % 1, 1) else f"{int(round(x)):,}"
    return s.translate(str.maketrans(",.", ".,")) if lang == "de" else s


def pct(share, lang: str = "en") -> str:
    return f"{round(float(share or 0) * 100)}{' %' if lang == 'de' else '%'}"


class _Formatter(string.Formatter):
    def __init__(self, lang: str):
        super().__init__()
        self.lang = lang

    def format_field(self, value, spec):
        lang = self.lang
        if spec == "n":
            return num(value, lang)
        if spec == "p":
            return pct(value, lang)
        if spec == "l":
            return ", ".join(str(v) for v in value or [])
        if spec == "d":
            if lang != "de" or len(str(value)) < 10:
                return str(value)
            y, m, d = str(value)[:10].split("-")
            return f"{int(d)}.{int(m)}.{y}"
        if spec in ("m", "mn"):
            y, m = str(value)[:7].split("-")
            name = _MONTHS.get(lang, _MONTHS["en"])[int(m) - 1]
            if spec == "mn":
                return name
            return str(value) if lang == "en" else f"{name} {y}"
        if spec in ("dec", "decd"):
            if lang != "de":
                return str(value)
            base = str(value).rstrip("s") + "er"
            return base + ("n" if spec == "decd" else "")
        if spec in ("w", "wp", "wa", "wA"):
            table = (_WORDS.get(lang) or {}).get(spec) or {}
            return table.get(value, str(value))
        return super().format_field(value, spec)


def _text(key: str, lang: str) -> Optional[tuple]:
    t = TEXTS.get(key)
    return (t.get(lang) or t["en"]) if t else None


def render(key: str, slots: dict, lang: str = "en") -> str:
    """The claim of a ``key`` card in ``lang``."""
    return _Formatter(lang).format(_text(key, lang)[1], **slots)


def claim(card: dict, lang: str = "en") -> str:
    """A card's claim in ``lang``; a card without a key (older) as stored."""
    if card.get("key") in TEXTS:
        return render(card["key"], card.get("slots") or {}, lang)
    return card.get("claim", "")


def headline(card: dict, lang: str = "en") -> Optional[str]:
    """The headline that belongs to this fact; None for a card without a key."""
    if card.get("key") not in TEXTS:
        return None
    return _Formatter(lang).format(_text(card["key"], lang)[0], **(card.get("slots") or {}))


def warm(card: dict, lang: str = "en") -> str:
    """The warm line that belongs to this fact, or '' where none fits."""
    t = _text(card.get("key") or "", lang)
    return (t[2] or "") if t else ""
