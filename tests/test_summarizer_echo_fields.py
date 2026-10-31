"""The summariser's title and year come from the source (2026-10-29).

The METADATA STRUCTURING template asked the model to write "title" and
"year" itself. With no year in the source it wrote ``"year": 19XX`` - invalid
JSON, 29 failed runs of one anime on the NAS - and a title with quotes in it
("Weird Al" Yankovic) came back unescaped. The template now carries both as
JSON, and the profile takes them from the source again.

Fakes only: no Ollama is called.

    python tests/test_summarizer_echo_fields.py
"""
import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.services import media_enricher as me  # noqa: E402

PASS = FAIL = 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}")


check("a year is a number, a missing or odd one is None",
      me._source_year({"year": "2010"}) == 2010 and me._source_year({"year": 1999}) == 1999
      and me._source_year({}) is None and me._source_year({"year": "Unknown"}) is None
      and me._source_year({"year": "19XX"}) is None)

prompts = []


class _Resp:
    status_code = 200

    def __init__(self, content):
        self._c = content

    def json(self):
        return {"message": {"content": self._c}}


class _Client:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, json=None):
        prompts.append(json["messages"][0]["content"])
        # What the model wrote for the echo fields before: invented and unescaped.
        return _Resp('{"title": "x", "year": 1987, "media_type": "anime", "plot_summary": "p", '
                     '"embedding_text": "e", "genres": [], "themes": [], "mood": [], "keywords": []}')


me.ollama_client = lambda timeout=None: _Client()
anime = {"title": "Elegy for the Henchmen: Fist of the North Star", "media_type": "anime",
         "overview": "o", "source": "mal"}
p = asyncio.run(me.summarize_with_small_llm(dict(anime)))
check("no source year: the template says null, and the profile keeps None, not the model's guess",
      '"year": null,' in prompts[-1] and p["year"] is None)
check("...the title is the source's", p["title"] == anime["title"])

asyncio.run(me.summarize_with_small_llm({**anime, "year": "1999"}))
check("a source year reaches the template as a number", '"year": 1999,' in prompts[-1])

artist = {"title": '"Weird Al" Yankovic', "media_type": "music", "genres": ["comedy"], "source": "mb"}
p = asyncio.run(me.summarize_with_small_llm(dict(artist)))
check("a title with quotes is escaped in the template",
      '"title": ' + json.dumps(artist["title"], ensure_ascii=False) + "," in prompts[-1])
check("...and the profile carries it as the source wrote it", p["title"] == artist["title"])

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
