"""The discussion's Wikipedia read picks the right one of two same-name works
(media_enricher.fetch_wikipedia_summary): by IMDb id when there is one, else
by year. The 2004 animated "Teacher's Pet" had been read for the 1958
comedy, and the curator then refused a verdict on the mixed record.
"""
from _harness import check  # noqa: I001 - first: temp stores

import asyncio

from src.services import media_enricher as me  # noqa: E402
from src.services import wikidata  # noqa: E402

OLD = ("Teacher's Pet is a 2004 American animated musical comedy film about a dog "
       "who wants to be a boy. " * 3)
NEW = ("Teacher's Pet is a 1958 American romantic comedy film starring Clark Gable "
       "and Doris Day, about a hardboiled city editor. " * 3)
SHOW = ("Fringe is an American science fiction television series created by J. J. "
        "Abrams that aired on Fox. " * 3)
EXTRACTS = {"Teacher's Pet (2004 film)": OLD, "Teacher's Pet (1958 film)": NEW,
            "Teacher's Pet": OLD, "Fringe (TV series)": SHOW}


class _Resp:
    status_code = 200

    def __init__(self, body): self._b = body
    def json(self): return self._b


class _Client:
    hits: list = []
    searched: list = []

    def __init__(self, *a, **k): pass
    async def __aenter__(self): return self
    async def __aexit__(self, *a): return False

    async def get(self, url, params=None):
        if params.get("list") == "search":
            _Client.searched.append(params["srsearch"])
            return _Resp({"query": {"search": [{"title": t} for t in _Client.hits]}})
        page = params["titles"]
        return _Resp({"query": {"pages": {"1": {"extract": EXTRACTS.get(page, "")}}}})


async def _no_id(imdb_id, **k):
    return ""


me.httpx.AsyncClient = _Client
wikidata.resolve_enwiki_article = _no_id


def run(title, media_type="movie", **kw):
    _Client.searched = []
    return asyncio.run(me.fetch_wikipedia_summary(title, media_type, **kw))


_Client.hits = ["Teacher's Pet (2004 film)", "Teacher's Pet (1958 film)"]
check("the year in the disambiguator wins over the search order",
      (run("Teacher's Pet", year=1958) or "").startswith("Teacher's Pet is a 1958"))
check("the year goes into a film's search", _Client.searched and "1958" in _Client.searched[0])
check("a hit naming another year is never read",
      run("Teacher's Pet", year=1958) is not None and OLD[:40] not in run("Teacher's Pet", year=1958))

_Client.hits = ["Teacher's Pet"]
check("a film article without a year in its title has to name it in its opening",
      run("Teacher's Pet", year=1958) is None)
check("...and the year is the string the library stores, too", run("Teacher's Pet", year="1958") is None)
check("without a year the first name match is read, as before",
      (run("Teacher's Pet") or "").startswith("Teacher's Pet is a 2004"))

_Client.hits = ["Fringe (TV series)"]
check("a series keeps its article although the opening names no year",
      (run("Fringe", "show", year=2008) or "").startswith("Fringe is"))


async def _by_id(imdb_id, **k):
    return "Teacher's Pet (1958 film)" if imdb_id == "tt0052278" else ""


wikidata.resolve_enwiki_article = _by_id
_Client.hits = ["Teacher's Pet (2004 film)"]
got = run("Teacher's Pet", imdb_id="tt0052278")
check("an IMDb id resolves the article by identity, no search at all",
      (got or "").startswith("Teacher's Pet is a 1958") and _Client.searched == [])
check("an id Wikidata does not know falls back to the search",
      run("Teacher's Pet", imdb_id="tt9999999", year=2004) is not None and _Client.searched != [])

check.done()
