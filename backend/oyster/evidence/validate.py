"""JSON-schema validation against schemas/*.schema.json (the wire authority), then model parsing.

Incoming events, job results and targets are checked here before they reach the store. The same
functions back `oyster validate-schemas` and the contract tests, so schemas, examples and
contracts.py cannot drift apart unnoticed.
"""

from __future__ import annotations

import json
from datetime import datetime
from functools import cache, lru_cache
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker
from pydantic import BaseModel, ValidationError
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012

from oyster import contracts
from oyster.settings import REPO_DIR

SCHEMAS_DIR = REPO_DIR / "schemas"
EXAMPLES_DIR = SCHEMAS_DIR / "examples"
MAX_ERRORS = 5

# example file prefix -> (schema file, contracts model)
EXAMPLE_KINDS: dict[str, tuple[str, type[BaseModel]]] = {
    "event": ("event.schema.json", contracts.Event),
    "job": ("job.schema.json", contracts.Job),
    "job-result": ("job-result.schema.json", contracts.JobResult),
    "finding": ("finding.schema.json", contracts.Finding),
    "target": ("target-profile.schema.json", contracts.TargetProfile),
    "ai-action": ("ai-action.schema.json", contracts.AiAction),
    "run": ("run.schema.json", contracts.Run),
}


class SchemaValidationError(ValueError):
    def __init__(self, schema: str, errors: list[str]):
        super().__init__(f"{schema}: " + "; ".join(errors))
        self.schema = schema
        self.errors = errors


_format_checker = FormatChecker()


@_format_checker.checks("date-time", raises=ValueError)
def _is_datetime(value: object) -> bool:
    """RFC 3339 date-time with an explicit offset (jsonschema skips it without optional deps)."""
    if not isinstance(value, str):
        return True
    if len(value) < 20 or value[10] not in "Tt":
        return False
    text = value[:-1] + "+00:00" if value[-1] in "Zz" else value
    return datetime.fromisoformat(text).tzinfo is not None


@lru_cache(maxsize=1)
def _registry() -> tuple[Registry, dict[str, dict]]:
    by_name: dict[str, dict] = {}
    resources = []
    for path in sorted(SCHEMAS_DIR.glob("*.schema.json")):
        contents = json.loads(path.read_text(encoding="utf-8"))
        by_name[path.name] = contents
        resources.append(
            (contents["$id"], Resource.from_contents(contents, default_specification=DRAFT202012))
        )
    return Registry().with_resources(resources), by_name


@cache
def validator(schema_name: str) -> Draft202012Validator:
    registry, by_name = _registry()
    if schema_name not in by_name:
        raise KeyError(f"unknown schema {schema_name}")
    return Draft202012Validator(by_name[schema_name], registry=registry, format_checker=_format_checker)


def schema_errors(schema_name: str, instance: object) -> list[str]:
    errs = sorted(validator(schema_name).iter_errors(instance), key=lambda e: list(e.absolute_path))
    out = []
    for e in errs[:MAX_ERRORS]:
        where = "/".join(str(p) for p in e.absolute_path) or "(root)"
        out.append(f"{where}: {e.message}"[:200])
    return out


def check(schema_name: str, instance: object) -> None:
    errs = schema_errors(schema_name, instance)
    if errs:
        raise SchemaValidationError(schema_name, errs)


def _parse(schema_name: str, model: type[BaseModel], instance: object):
    check(schema_name, instance)
    try:
        return model.model_validate(instance)
    except ValidationError as e:
        raise SchemaValidationError(
            schema_name,
            [f"{'/'.join(map(str, x['loc']))}: {x['msg']}"[:200] for x in e.errors()[:MAX_ERRORS]],
        ) from None


def parse_event(instance: object) -> contracts.Event:
    return _parse("event.schema.json", contracts.Event, instance)


def parse_job_result(instance: object) -> contracts.JobResult:
    return _parse("job-result.schema.json", contracts.JobResult, instance)


def parse_target(instance: object) -> contracts.TargetProfile:
    return _parse("target-profile.schema.json", contracts.TargetProfile, instance)


def example_kind(path: Path) -> str | None:
    kind = path.name.split(".", 1)[0]
    return kind if kind in EXAMPLE_KINDS else None


def validate_example(path: Path) -> list[str]:
    """Problems with one example file: schema errors, model parse errors, or an unknown kind."""
    kind = example_kind(path)
    if kind is None:
        return [f"no schema mapping for example prefix {path.name.split('.', 1)[0]!r}"]
    schema_name, model = EXAMPLE_KINDS[kind]
    data = json.loads(path.read_text(encoding="utf-8"))
    problems = schema_errors(schema_name, data)
    try:
        obj = model.model_validate(data)
    except ValidationError as e:
        problems += [f"model {model.__name__}: {x['msg']}" for x in e.errors()[:MAX_ERRORS]]
    else:
        if isinstance(obj, contracts.AiAction):
            reason = contracts.validate_ai_action(obj)
            if reason:
                problems.append(f"validate_ai_action: {reason}")
    return problems


def validate_all() -> dict[str, list[str]]:
    """Every example plus the lab profile. Maps a relative path to its problems (empty = valid)."""
    results: dict[str, list[str]] = {}
    for path in sorted(EXAMPLES_DIR.glob("*.json")):
        results[f"schemas/examples/{path.name}"] = validate_example(path)
    lab = SCHEMAS_DIR / "lab-profile.json"
    results["schemas/lab-profile.json"] = schema_errors(
        "lab-profile.schema.json", json.loads(lab.read_text(encoding="utf-8"))
    )
    return results
