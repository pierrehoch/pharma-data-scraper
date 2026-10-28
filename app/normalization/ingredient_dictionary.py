"""Validated dictionary imports and explicit public-reference synchronization."""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.core.http import HttpClientProtocol
from app.db.models import Ingredient, IngredientAlias, IngredientDictionary, IngredientIdentifier, IngredientOverride, Source
from app.normalization.text import fold

LANGUAGES = {"en", "fr", "de", "es", "it", "pt", "pt-br", "ja", "zh", "zh-cn", "zh-tw", "zh-hans", "zh-hant"}
RXNAV = "https://rxnav.nlm.nih.gov/REST"


class DictionaryAlias(BaseModel):
    model_config = ConfigDict(extra="forbid")
    label: str = Field(min_length=1, max_length=1024)
    language: str = Field(min_length=1, max_length=16)


class IngredientReference(BaseModel):
    model_config = ConfigDict(extra="forbid")
    namespace: str = Field(min_length=1, max_length=64)
    value: str = Field(min_length=1, max_length=128)


class DictionaryIngredient(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1)
    identifiers: dict[str, str] = Field(min_length=1)
    aliases: list[DictionaryAlias] = Field(default_factory=list)
    family: IngredientReference | None = None
    family_evidence: str | None = None
    evidence: str = Field(min_length=1)

    @field_validator("identifiers")
    @classmethod
    def validate_identifiers(cls, value: dict[str, str]) -> dict[str, str]:
        if any(not k.strip() or not v.strip() or len(k) > 64 or len(v) > 128 for k, v in value.items()):
            raise ValueError("Ingredient identifiers must have non-empty, bounded namespace and value")
        return value


class DictionaryOverride(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_id: str = Field(min_length=1)
    source_text: str = Field(min_length=1)
    ingredients: list[IngredientReference] = Field(min_length=1)
    evidence: str = Field(min_length=1)


class DictionaryDocument(BaseModel):
    model_config = ConfigDict(extra="forbid")
    provider: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    revision: str = Field(min_length=1)
    ingredients: list[DictionaryIngredient] = Field(default_factory=list)
    overrides: list[DictionaryOverride] = Field(default_factory=list)
    excluded_entities: list[str] = Field(default_factory=list)


def install_dictionary(session: Session, document: DictionaryDocument, *, cache_path: Path) -> dict[str, Any]:
    """Atomic at the caller's transaction boundary; reject identity conflicts instead of merging guesses."""
    if not document.ingredients and not document.overrides:
        raise ValueError("Dictionary import must contain ingredients or reviewed overrides")
    identifiers = {
        (r.namespace, r.value): r.ingredient_id
        for r in session.scalars(select(IngredientIdentifier))
    }
    by_id = {r.id: r for r in session.scalars(select(Ingredient))}
    records: list[tuple[DictionaryIngredient, Ingredient]] = []
    for record in document.ingredients:
        keys = list(record.identifiers.items())
        existing = {identifiers[key] for key in keys if key in identifiers}
        if len(existing) > 1:
            raise ValueError(f"Conflicting ingredient identities for {record.identifiers}")
        ingredient_id = next(iter(existing)) if existing else uuid.uuid5(
            uuid.NAMESPACE_URL, "pharma-ingredient:" + ":".join(sorted(keys)[0]),
        ).hex
        ingredient = by_id.get(ingredient_id)
        if ingredient is None:
            ingredient = Ingredient(id=ingredient_id, name=record.name)
            session.add(ingredient)
            by_id[ingredient_id] = ingredient
        records.append((record, ingredient))
        for namespace, value in keys:
            if (namespace, value) not in identifiers:
                session.add(IngredientIdentifier(
                    ingredient_id=ingredient_id, namespace=namespace, value=value, evidence=record.evidence,
                ))
                identifiers[(namespace, value)] = ingredient_id
    session.flush()
    prefix = document.provider + ":"
    session.execute(delete(IngredientAlias).where(IngredientAlias.evidence.startswith(prefix, autoescape=True)))
    seen_aliases: set[tuple[str, str, str, str]] = set()
    for record, ingredient in records:
        if record.family:
            if not record.family_evidence:
                raise ValueError("A family relationship requires explicit evidence")
            parent_id = identifiers.get((record.family.namespace, record.family.value))
            if parent_id is None:
                raise ValueError(f"Unknown family identity {record.family}")
            if ingredient.family_id and ingredient.family_id != parent_id:
                raise ValueError(f"Conflicting family relationship for {ingredient.name}")
            ingredient.family_id = parent_id
            ingredient.family_evidence = record.family_evidence
        for alias in record.aliases:
            normalized = fold(alias.label)
            if not normalized:
                raise ValueError("Ingredient alias must not normalize to an empty string")
            evidence = f"{prefix}{record.evidence}"[:512]
            key = (ingredient.id, normalized, alias.language, evidence)
            if key in seen_aliases:
                continue
            seen_aliases.add(key)
            session.add(IngredientAlias(
                ingredient_id=ingredient.id, label=alias.label, normalized=normalized,
                language=alias.language, evidence=evidence,
            ))
    for ingredient in by_id.values():
        visited = {ingredient.id}
        parent = ingredient.family_id
        while parent:
            if parent in visited:
                raise ValueError(f"Cyclic ingredient family for {ingredient.name}")
            visited.add(parent)
            parent = by_id[parent].family_id
    override_keys: set[tuple[str, str]] = set()
    for override in document.overrides:
        if session.get(Source, override.source_id) is None:
            raise ValueError(f"Unknown override source {override.source_id}")
        key = (override.source_id, override.source_text)
        if key in override_keys:
            raise ValueError(f"Duplicate source override {key}")
        override_keys.add(key)
        targets = []
        for ref in override.ingredients:
            target = identifiers.get((ref.namespace, ref.value))
            if target is None:
                raise ValueError(f"Unknown ingredient in override for {key}")
            targets.append(target)
        row = session.scalar(select(IngredientOverride).where(
            IngredientOverride.source_id == override.source_id, IngredientOverride.source_text == override.source_text,
        ))
        if row is None:
            row = IngredientOverride(source_id=override.source_id, source_text=override.source_text)
            session.add(row)
        row.ingredient_ids = sorted(set(targets))
        row.evidence = override.evidence
    version = hashlib.sha256(document.model_dump_json().encode()).hexdigest()
    summary = {
        "version": version, "revision": document.revision,
        "ingredients": len(records), "aliases": len(seen_aliases), "overrides": len(document.overrides),
        "excluded_entities": document.excluded_entities,
    }
    metadata = session.get(IngredientDictionary, document.provider)
    if metadata is None:
        metadata = IngredientDictionary(id=document.provider, provider=document.provider)
        session.add(metadata)
    metadata.retrieved_at = datetime.now(timezone.utc)
    metadata.cache_path = str(cache_path)
    metadata.summary = summary
    session.flush()
    return summary


def cache_json(directory: Path, provider: str, payload: Any) -> Path:
    content = json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2)
    digest = hashlib.sha256(content.encode()).hexdigest()
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{provider}-{digest}.json"
    if not path.exists():
        path.write_text(content, encoding="utf-8")
    return path


def fetch_rxnorm(
    http: HttpClientProtocol, cache_dir: Path, *, substance_names: set[str] | None = None,
) -> tuple[DictionaryDocument, Path]:
    payload = {}
    ingredients = []
    for tty in ("IN", "PIN"):
        url = f"{RXNAV}/allconcepts.json?tty={tty}"
        data = http.get_json(url)
        payload[tty] = data
        concepts = data.get("minConceptGroup", {}).get("minConcept")
        if not isinstance(concepts, list) or not concepts:
            raise ValueError(f"RxNorm {tty}: expected a non-empty minConcept list")
        for concept in concepts:
            if concept.get("tty") != tty or not str(concept.get("rxcui", "")).isdigit() or not concept.get("name"):
                raise ValueError(f"RxNorm {tty}: malformed ingredient record")
            ingredients.append(DictionaryIngredient(
                name=concept["name"], identifiers={"rxnorm": concept["rxcui"]},
                aliases=[DictionaryAlias(label=concept["name"], language="en")], evidence=url,
            ))
    known_ids = {r.identifiers["rxnorm"] for r in ingredients}
    precise_ids = {c["rxcui"] for c in payload["PIN"]["minConceptGroup"]["minConcept"]}
    for record in ingredients:
        if record.identifiers["rxnorm"] not in precise_ids or fold(record.name) not in (substance_names or set()):
            continue
        url = f"{RXNAV}/rxcui/{record.identifiers['rxnorm']}/related.json?tty=IN"
        relations = http.get_json(url)
        payload[url] = relations
        group = relations.get("relatedGroup")
        if not isinstance(group, dict):
            raise ValueError("RxNorm ingredient relationship response is malformed")
        parents = {
            c["rxcui"] for g in group.get("conceptGroup", []) if g.get("tty") == "IN"
            for c in g.get("conceptProperties", []) if c.get("tty") == "IN" and c.get("rxcui") in known_ids
        }
        if len(parents) == 1:
            record.family = IngredientReference(namespace="rxnorm", value=next(iter(parents)))
            record.family_evidence = url
    raw_path = cache_json(cache_dir, "rxnorm-raw", payload)
    document = DictionaryDocument(provider="rxnorm", revision=raw_path.stem, ingredients=ingredients)
    return document, cache_json(cache_dir, "rxnorm", document.model_dump())


def fetch_wikidata(
    http: HttpClientProtocol, cache_dir: Path, entity_ids: list[str], known_rxnorm: set[str],
) -> tuple[DictionaryDocument, Path]:
    if not entity_ids or any(not re.fullmatch(r"Q[1-9]\d*", value) for value in entity_ids):
        raise ValueError("Supply one or more Wikidata entity IDs (e.g. Q57055)")
    records = []
    revisions = []
    excluded = []
    for entity_id in sorted(set(entity_ids)):
        url = f"https://www.wikidata.org/wiki/Special:EntityData/{entity_id}.json"
        data = http.get_json(url)
        cache_json(cache_dir, f"wikidata-{entity_id}-raw", data)
        entity = data.get("entities", {}).get(entity_id)
        if not entity or entity.get("type") != "item" or not entity.get("lastrevid"):
            raise ValueError(f"Wikidata {entity_id}: missing entity/revision")
        claims = entity.get("claims", {})

        def values(prop: str) -> list[str]:
            return [
                claim["mainsnak"]["datavalue"]["value"]
                for claim in claims.get(prop, [])
                if claim.get("rank") != "deprecated"
                and isinstance(claim.get("mainsnak", {}).get("datavalue", {}).get("value"), str)
            ]

        rx_ids = [value for value in values("P3345") if value in known_rxnorm]
        uniis = values("P652")
        if not rx_ids and not uniis:
            excluded.append(f"{entity_id}: no ingredient RxNorm or UNII identity; not imported")
            continue
        if len(set(rx_ids)) > 1 or len(set(uniis)) > 1:
            excluded.append(f"{entity_id}: ambiguous external substance identity; not imported")
            continue
        labels = entity.get("labels", {})
        name = labels.get("en", {}).get("value")
        if not name:
            raise ValueError(f"Wikidata {entity_id}: missing English canonical label")
        aliases = []
        for language in LANGUAGES:
            for value in [labels.get(language), *entity.get("aliases", {}).get(language, [])]:
                if value and value.get("value"):
                    aliases.append(DictionaryAlias(label=value["value"], language=language))
        identifiers = {"wikidata": entity_id}
        if rx_ids:
            identifiers["rxnorm"] = rx_ids[0]
        if uniis:
            identifiers["unii"] = uniis[0]
        revisions.append(f"{entity_id}:{entity['lastrevid']}")
        records.append(DictionaryIngredient(
            name=name, identifiers=identifiers, aliases=aliases,
            evidence=f"https://www.wikidata.org/wiki/{entity_id}?oldid={entity['lastrevid']}",
        ))
    if not records:
        raise ValueError(f"No verified ingredient entities found: {excluded}")
    document = DictionaryDocument(
        provider="wikidata", revision=";".join(revisions), ingredients=records, excluded_entities=excluded,
    )
    return document, cache_json(cache_dir, "wikidata", document.model_dump())


def discover_wikidata(http: HttpClientProtocol, *, limit: int) -> list[str]:
    """Bounded discovery, keyset-paginated by Wikidata entity URI."""
    if not 1 <= limit <= 10000:
        raise ValueError("Wikidata discovery limit must be between 1 and 10000")
    result: list[str] = []
    cursor = ""
    while len(result) < limit:
        size = min(200, limit - len(result))
        after = f'FILTER(STR(?item) > "{cursor}")' if cursor else ""
        query = f"SELECT DISTINCT ?item WHERE {{ ?item wdt:P3345 ?rx . {after} }} ORDER BY STR(?item) LIMIT {size}"
        url = "https://query.wikidata.org/sparql?" + urlencode({"query": query, "format": "json"})
        data = http.get_json(url, headers={"Accept": "application/sparql-results+json"})
        rows = data.get("results", {}).get("bindings")
        if not isinstance(rows, list):
            raise ValueError("Wikidata discovery returned malformed results")
        if not rows:
            break
        uris = [row["item"]["value"] for row in rows]
        if uris != sorted(set(uris)) or (cursor and uris[0] <= cursor):
            raise ValueError("Wikidata discovery cursor did not advance")
        result.extend(uri.rsplit("/", 1)[-1] for uri in uris)
        cursor = uris[-1]
    return result
