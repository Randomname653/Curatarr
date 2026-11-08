"""The TrueNAS catalog app (deploy/truenas/catalog/curatarr) stays consistent.

- the files the catalog requires are there and parse;
- app.yaml: the name is the directory, the community train, semantic
  versions, a non-root run context;
- ix_values: the image is ours and its tag is the app_version; the data
  path is the image's own (/app/data);
- questions: the curatarr settings dict, the timezone, a web port with a
  default in TrueNAS' app range, a data store that defaults to a dataset;
- the template reads only consts that exist;
- the test values answer what the template reads;
- the release workflow runs on version tags and publishes the fixed tag.

    python tests/test_truenas_catalog.py
"""
from _harness import check  # noqa: I001 - first: temp stores

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "deploy" / "truenas" / "catalog" / "curatarr"
NEED = ["app.yaml", "ix_values.yaml", "questions.yaml", "README.md", "templates/docker-compose.yaml",
        "templates/test_values/basic-values.yaml"]
check("the files the catalog requires are there", all((APP / f).is_file() for f in NEED))


def load(rel):
    with open(APP / rel, encoding="utf-8") as fh:
        return yaml.safe_load(fh)


app, ixv, qs = load("app.yaml"), load("ix_values.yaml"), load("questions.yaml")
tv = load("templates/test_values/basic-values.yaml")
semver = re.compile(r"\d+\.\d+\.\d+")
check("app.yaml: name, train, semantic versions, a non-root run context",
      app["name"] == APP.name and app["train"] == "community" and bool(semver.fullmatch(app["version"]))
      and bool(semver.fullmatch(app["app_version"]))
      and all(c["uid"] != 0 and c["gid"] != 0 for c in app["run_as_context"]))
check("ix_values: our image, tagged with the app_version",
      ixv["images"]["image"]["repository"] == "ghcr.io/randomname653/curatarr"
      and str(ixv["images"]["image"]["tag"]).split("@")[0] == app["app_version"])
check("...and the image's own data path", ixv["consts"]["data_path"] == "/app/data")

top = {q["variable"]: q for q in qs["questions"]}
check("questions: the timezone, the curatarr settings, the user, network, storage, labels, resources",
      {"TZ", "curatarr", "run_as", "network", "storage", "labels", "resources"} <= set(top))


def attr(q, name):
    return next(a for a in q["schema"]["attrs"] if a["variable"] == name)


port = attr(attr(top["network"], "web_port"), "port_number")["schema"]
check("a web port defaulting into TrueNAS' app range", 30000 <= port["default"] <= 39999
      and port["min"] == 1 and port["max"] == 65535)
store = attr(top["storage"], "config")
check("the data store defaults to a dataset", attr(store, "type")["schema"]["default"] == "ix_volume")

with open(APP / "templates/docker-compose.yaml", encoding="utf-8") as fh:
    used = set(re.findall(r"values\.consts\.(\w+)", fh.read()))
check("the template reads only consts that exist", bool(used) and used <= set(ixv["consts"]))
answers = {"network": "web_port", "storage": "config", "curatarr": "additional_envs", "run_as": "user"}
check("the test values answer what the template reads",
      all(isinstance(tv.get(k), dict) and v in tv[k] for k, v in answers.items()))

with open(ROOT / ".github/workflows/docker.yml", encoding="utf-8") as fh:
    wf = yaml.safe_load(fh)
on = wf.get("on") or wf.get(True)        # YAML 1.1 reads a bare "on" key as True
steps = {s.get("name"): s for s in wf["jobs"]["image"]["steps"]}
check("the release workflow runs on version tags and publishes the fixed tag",
      on["push"]["tags"] == ["v*"] and {"Release tag matches the code's version",
                                        "Publish release tag to GHCR"} <= set(steps))

check.done()
