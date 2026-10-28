"""Local, evidence-based ingredient resolution. Never performs network requests."""

from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict

from sqlalchemy import delete, select
from sqlalchemy.orm import Session, aliased

from app.core.types import IngredientComponent
from app.db.models import (
    IngredientAlias, IngredientDictionary, IngredientIdentifier, IngredientOverride,
    Product, ProductIngredient, ProductIngredientResolution,
)
from app.normalization.text import fold


class IngredientResolver:
    """One local dictionary snapshot per publication/backfill batch."""

    def __init__(self, session: Session):
        self.aliases: dict[str, set[str]] = defaultdict(set)
        for alias, ingredient_id in session.execute(select(IngredientAlias.normalized, IngredientAlias.ingredient_id)):
            self.aliases[alias].add(ingredient_id)
        self.identifiers = {
            (namespace, value): ingredient_id
            for namespace, value, ingredient_id in session.execute(
                select(IngredientIdentifier.namespace, IngredientIdentifier.value, IngredientIdentifier.ingredient_id)
            )
        }
        self.overrides = {
            (row.source_id, row.source_text): (list(row.ingredient_ids), row.evidence)
            for row in session.scalars(select(IngredientOverride))
        }
        versions = [
            f"{row.id}:{row.summary.get('version', '')}"
            for row in session.scalars(select(IngredientDictionary).order_by(IngredientDictionary.id))
        ]
        self.version = hashlib.sha256("|".join(versions).encode()).hexdigest() if versions else "uninstalled"

    def resolve(
        self, session: Session, product: Product, components: list[IngredientComponent] | None = None,
    ) -> None:
        previous = session.get(ProductIngredientResolution, product.id)
        # Backfill retains source-structured component boundaries captured during ingestion.
        if components is None and previous and previous.components and product.source_id != "es_cipm":
            expected = hashlib.sha256(json.dumps(
                [product.source_id, product.ingredient, previous.components], ensure_ascii=False, sort_keys=True,
            ).encode()).hexdigest()
            if expected == previous.fingerprint:
                components = [IngredientComponent(**value) for value in previous.components]
        # These adapters publish this separator only after joining structured active-ingredient rows.
        if components is None and product.ingredient and product.source_id in {"fr_bdpm", "ca_hc_dpd", "uk_dmd"}:
            components = [IngredientComponent(name) for name in product.ingredient.split(" + ")]
        if components is None and product.ingredient and product.source_id == "es_cipm":
            label = re.sub(
                r"^\s*[A-Z]\d{2}[A-Z0-9]{2,4}(?:\s*[-–]\s*|\s+)", "", product.ingredient, flags=re.IGNORECASE,
            )
            components = [IngredientComponent(label)]
        evidence_components = [
            {"name": c.name, "namespace": c.namespace, "identifier": c.identifier}
            for c in components or []
        ]
        fingerprint = hashlib.sha256(json.dumps(
            [product.source_id, product.ingredient, evidence_components], ensure_ascii=False, sort_keys=True,
        ).encode()).hexdigest()
        session.execute(delete(ProductIngredient).where(ProductIngredient.product_id == product.id))
        override = self.overrides.get((product.source_id, product.ingredient or ""))
        matches: dict[str, tuple[str, str, str]] = {}
        unresolved = 0
        ambiguous = False
        if override:
            for ingredient_id in override[0]:
                matches[ingredient_id] = (product.ingredient or "", "manual", override[1])
        else:
            # Without structured components only a complete dictionary alias is safe.
            candidates = components if components is not None else (
                [IngredientComponent(product.ingredient)] if product.ingredient else []
            )
            if not candidates:
                unresolved = 1
            for component in candidates:
                exact_id = (
                    self.identifiers.get((component.namespace, component.identifier))
                    if component.namespace and component.identifier else None
                )
                ids = {exact_id} if exact_id else self.aliases.get(fold(component.name), set())
                if len(ids) == 1:
                    ingredient_id = next(iter(ids))
                    method = "source_identifier" if exact_id else "exact_alias"
                    evidence = (
                        f"{component.namespace}:{component.identifier}" if exact_id
                        else f"Unambiguous complete dictionary alias: {component.name}"
                    )
                    matches[ingredient_id] = (component.name, method, evidence)
                else:
                    unresolved += 1
                    ambiguous |= len(ids) > 1
        for ingredient_id, (source_text, method, evidence) in matches.items():
            session.add(ProductIngredient(
                product_id=product.id, ingredient_id=ingredient_id, source_text=source_text,
                method=method, evidence=evidence, dictionary_version=self.version,
            ))
        status = "partial" if matches and unresolved else (
            "resolved" if matches else "ambiguous" if ambiguous else "unresolved"
        )
        reason = (
            "Reviewed source-specific mapping" if override else
            "All published components mapped" if matches and not unresolved else
            f"{unresolved} component(s) lack an unambiguous dictionary identity"
        )
        if not product.ingredient and not components and not override:
            reason = "Source publishes no structured ingredient evidence"
        resolution = previous or ProductIngredientResolution(product_id=product.id)
        resolution.status = status
        resolution.reason = reason
        resolution.fingerprint = fingerprint
        resolution.components = evidence_components
        resolution.dictionary_version = self.version
        session.add(resolution)
        if status == "unresolved" and product.source_id in {"ca_on_odb", "es_nomenclator"}:
            session.flush()
            reconcile_national_codes(session, source_id=product.source_id, product_id=product.id)


def backfill(session: Session, *, source_id: str | None = None, batch_size: int = 1000) -> dict[str, int]:
    if batch_size <= 0:
        raise ValueError("batch_size must be positive")
    resolver = IngredientResolver(session)
    counts: dict[str, int] = defaultdict(int)
    last_id = 0
    while True:
        stmt = select(Product).where(Product.id > last_id).order_by(Product.id).limit(batch_size)
        if source_id:
            stmt = stmt.where(Product.source_id == source_id)
        products = list(session.scalars(stmt))
        if not products:
            break
        for product in products:
            resolver.resolve(session, product)
        session.flush()
        for product in products:
            resolution = session.get(ProductIngredientResolution, product.id)
            if resolution is None:
                raise RuntimeError(f"Missing ingredient resolution after processing product {product.id}")
            counts[resolution.status] += 1
        last_id = products[-1].id
        session.commit()
        session.expunge_all()
    reconcile_national_codes(session, source_id=source_id)
    session.commit()
    statement = select(ProductIngredientResolution.status, ProductIngredientResolution.product_id).join(
        Product, Product.id == ProductIngredientResolution.product_id,
    )
    if source_id:
        statement = statement.where(Product.source_id == source_id)
    counts.clear()
    for status, _ in session.execute(statement):
        counts[status] += 1
    return dict(counts)


def reconcile_national_codes(
    session: Session, *, source_id: str | None = None, product_id: int | None = None,
) -> None:
    """Transfer verified constituents only across documented exact same-country identifier systems."""
    target, reference = aliased(Product), aliased(Product)
    # DIN_PIN is intentionally excluded: a provincial PIN is not necessarily a Health Canada DIN.
    pairs = [
        ("ca_on_odb", "ca_hc_dpd", "CA", "DIN"),
        ("es_nomenclator", "es_cima", "ES", "CODIGO_NACIONAL"),
    ]
    for target_source, reference_source, country, code_type in pairs:
        if source_id and source_id != target_source:
            continue
        statement = (
            select(target.id, reference.id)
            .join(reference, reference.product_code == target.product_code)
            .join(ProductIngredientResolution, ProductIngredientResolution.product_id == reference.id)
            .where(
                target.source_id == target_source, reference.source_id == reference_source,
                target.country == country, reference.country == country,
                target.code_type == code_type, reference.code_type == code_type,
                ProductIngredientResolution.status == "resolved",
            )
        )
        if product_id is not None:
            statement = statement.where(target.id == product_id)
        rows = session.execute(statement)
        for target_id, reference_id in rows:
            resolution = session.get(ProductIngredientResolution, target_id)
            if resolution is None or resolution.status != "unresolved":
                continue
            links = list(session.scalars(select(ProductIngredient).where(ProductIngredient.product_id == reference_id)))
            if not links:
                continue
            for link in links:
                session.add(ProductIngredient(
                    product_id=target_id, ingredient_id=link.ingredient_id, source_text=link.source_text,
                    method="national_code",
                    evidence=f"Exact {country} {code_type} identity with {reference_source} product {reference_id}; {link.evidence}",
                    dictionary_version=link.dictionary_version,
                ))
            resolution.status = "resolved"
            resolution.reason = f"Exact {country} {code_type} link to {reference_source}; verified constituents"
