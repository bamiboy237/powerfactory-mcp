"""Durable product-identity to immutable PowerFactory-locator bindings."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Callable
from uuid import uuid4

from powerfactory_agent.domain import (
    IdentityLifecycleRecord,
    IdentityLifecycleState,
    IdentityTombstone,
    LocatorRebind,
    PowerFactoryLocator,
    ProductIdentity,
)
from powerfactory_agent.serialization import canonical_digest, canonical_json, from_json

from .database import SQLiteDatabase


class IdentityNotFoundError(LookupError):
    pass


class IdentityConflictError(RuntimeError):
    pass


class IdentityAmbiguousError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class IdentityBinding:
    product_identity: ProductIdentity
    lifecycle_state: IdentityLifecycleState
    current_locator: PowerFactoryLocator
    first_evidence_reference: str
    last_evidence_reference: str
    created_at: datetime
    updated_at: datetime


class IdentityStore:
    """Small fail-closed registry; no method persists a vendor object handle."""

    def __init__(
        self,
        database: SQLiteDatabase,
        *,
        identity_factory: Callable[[], str] | None = None,
    ) -> None:
        self._database = database
        self._identity_factory = identity_factory or (lambda: str(uuid4()))

    def create(
        self,
        locator: PowerFactoryLocator,
        *,
        evidence_reference: str,
        product_identity: ProductIdentity | None = None,
    ) -> IdentityBinding:
        identity = product_identity or ProductIdentity(self._identity_factory())
        signature = _locator_signature(locator)
        lifecycle = IdentityLifecycleRecord(
            identity,
            IdentityLifecycleState.ACTIVE,
            locator.observed_at,
            evidence_reference,
        )
        with self._database.transaction(immediate=True) as connection:
            existing = connection.execute(
                """SELECT p.product_identity FROM identity_locators l
                JOIN identity_products p ON p.product_identity = l.product_identity
                WHERE l.locator_signature = ? AND p.lifecycle_state != ?""",
                (signature, IdentityLifecycleState.TOMBSTONED.value),
            ).fetchall()
            if existing:
                raise IdentityConflictError("locator evidence is already bound to a live product identity")
            try:
                connection.execute(
                    """INSERT INTO identity_products(
                    product_identity, installation_id, profile_id, project_key,
                    object_class, lifecycle_state, current_locator_version_id,
                    first_evidence_reference, last_evidence_reference, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        identity.value,
                        locator.project_provenance.installation_id,
                        locator.project_provenance.profile_id,
                        locator.project_provenance.project_key,
                        locator.object_class,
                        IdentityLifecycleState.ACTIVE.value,
                        locator.locator_version_id,
                        evidence_reference,
                        evidence_reference,
                        locator.observed_at.isoformat(),
                        locator.observed_at.isoformat(),
                    ),
                )
            except Exception as exc:
                raise IdentityConflictError("product identity or locator binding already exists") from exc
            self._insert_locator(connection, identity, locator)
            self._insert_lifecycle(connection, lifecycle)
        return self.get(identity)

    def get(self, product_identity: ProductIdentity) -> IdentityBinding:
        with self._database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM identity_products WHERE product_identity = ?",
                (product_identity.value,),
            ).fetchone()
            if row is None:
                raise IdentityNotFoundError("product identity was not found")
            locator_row = connection.execute(
                "SELECT locator_json FROM identity_locators WHERE locator_version_id = ?",
                (row["current_locator_version_id"],),
            ).fetchone()
        if locator_row is None:
            raise IdentityConflictError("identity has no current locator evidence")
        return IdentityBinding(
            product_identity,
            IdentityLifecycleState(row["lifecycle_state"]),
            from_json(PowerFactoryLocator, locator_row["locator_json"]),
            row["first_evidence_reference"],
            row["last_evidence_reference"],
            datetime.fromisoformat(row["created_at"]),
            datetime.fromisoformat(row["updated_at"]),
        )

    def resolve_exact(self, locator: PowerFactoryLocator) -> IdentityBinding:
        signature = _locator_signature(locator)
        with self._database.connect() as connection:
            rows = connection.execute(
                """SELECT p.product_identity FROM identity_locators l
                JOIN identity_products p ON p.product_identity = l.product_identity
                WHERE l.locator_signature = ? AND p.lifecycle_state = ?""",
                (signature, IdentityLifecycleState.ACTIVE.value),
            ).fetchall()
        if not rows:
            raise IdentityNotFoundError("exact active locator binding was not found")
        identities = {row["product_identity"] for row in rows}
        if len(identities) != 1:
            raise IdentityAmbiguousError("exact locator evidence resolves to multiple product identities")
        return self.get(ProductIdentity(next(iter(identities))))

    def mark_unresolved(
        self,
        product_identity: ProductIdentity,
        *,
        observed_at: datetime,
        evidence_reference: str,
    ) -> IdentityBinding:
        return self._transition(
            product_identity,
            IdentityLifecycleState.UNRESOLVED,
            observed_at,
            evidence_reference,
        )

    def rebind(self, rebind: LocatorRebind, *, observed_at: datetime) -> IdentityBinding:
        current = self.get(rebind.product_identity)
        if current.lifecycle_state is IdentityLifecycleState.TOMBSTONED:
            raise IdentityConflictError("tombstoned product identities cannot be rebound")
        if current.current_locator != rebind.prior_locator:
            raise IdentityConflictError("rebind prior locator is not current")
        lifecycle = IdentityLifecycleRecord(
            rebind.product_identity,
            IdentityLifecycleState.ACTIVE,
            observed_at,
            rebind.evidence_reference,
        )
        with self._database.transaction(immediate=True) as connection:
            self._insert_locator(connection, rebind.product_identity, rebind.replacement_locator)
            connection.execute(
                """UPDATE identity_products
                SET lifecycle_state = ?, current_locator_version_id = ?,
                    last_evidence_reference = ?, updated_at = ?
                WHERE product_identity = ?""",
                (
                    IdentityLifecycleState.ACTIVE.value,
                    rebind.replacement_locator.locator_version_id,
                    rebind.evidence_reference,
                    observed_at.isoformat(),
                    rebind.product_identity.value,
                ),
            )
            self._insert_lifecycle(connection, lifecycle)
        return self.get(rebind.product_identity)

    def tombstone(self, tombstone: IdentityTombstone) -> IdentityBinding:
        current = self.get(tombstone.product_identity)
        if current.lifecycle_state is IdentityLifecycleState.TOMBSTONED:
            return current
        lifecycle = IdentityLifecycleRecord(
            tombstone.product_identity,
            IdentityLifecycleState.TOMBSTONED,
            tombstone.tombstoned_at,
            tombstone.reason,
        )
        with self._database.transaction(immediate=True) as connection:
            connection.execute(
                "INSERT INTO identity_tombstones(product_identity, tombstone_json, tombstoned_at) VALUES (?, ?, ?)",
                (
                    tombstone.product_identity.value,
                    canonical_json(tombstone),
                    tombstone.tombstoned_at.isoformat(),
                ),
            )
            connection.execute(
                """UPDATE identity_products SET lifecycle_state = ?,
                last_evidence_reference = ?, updated_at = ? WHERE product_identity = ?""",
                (
                    IdentityLifecycleState.TOMBSTONED.value,
                    tombstone.reason,
                    tombstone.tombstoned_at.isoformat(),
                    tombstone.product_identity.value,
                ),
            )
            self._insert_lifecycle(connection, lifecycle)
        return self.get(tombstone.product_identity)

    def lifecycle(self, product_identity: ProductIdentity) -> tuple[IdentityLifecycleRecord, ...]:
        with self._database.connect() as connection:
            rows = connection.execute(
                """SELECT event_json FROM identity_lifecycle_events
                WHERE product_identity = ? ORDER BY sequence""",
                (product_identity.value,),
            ).fetchall()
        return tuple(from_json(IdentityLifecycleRecord, row["event_json"]) for row in rows)

    def _transition(
        self,
        product_identity: ProductIdentity,
        state: IdentityLifecycleState,
        observed_at: datetime,
        evidence_reference: str,
    ) -> IdentityBinding:
        current = self.get(product_identity)
        if current.lifecycle_state is IdentityLifecycleState.TOMBSTONED:
            raise IdentityConflictError("tombstoned product identities cannot transition")
        lifecycle = IdentityLifecycleRecord(product_identity, state, observed_at, evidence_reference)
        with self._database.transaction(immediate=True) as connection:
            connection.execute(
                """UPDATE identity_products SET lifecycle_state = ?,
                last_evidence_reference = ?, updated_at = ? WHERE product_identity = ?""",
                (state.value, evidence_reference, observed_at.isoformat(), product_identity.value),
            )
            self._insert_lifecycle(connection, lifecycle)
        return self.get(product_identity)

    @staticmethod
    def _insert_locator(connection: object, identity: ProductIdentity, locator: PowerFactoryLocator) -> None:
        connection.execute(  # type: ignore[attr-defined]
            """INSERT INTO identity_locators(
            locator_version_id, product_identity, locator_signature, locator_json, observed_at
            ) VALUES (?, ?, ?, ?, ?)""",
            (
                locator.locator_version_id,
                identity.value,
                _locator_signature(locator),
                canonical_json(locator),
                locator.observed_at.isoformat(),
            ),
        )

    @staticmethod
    def _insert_lifecycle(connection: object, record: IdentityLifecycleRecord) -> None:
        connection.execute(  # type: ignore[attr-defined]
            """INSERT INTO identity_lifecycle_events(
            product_identity, lifecycle_state, evidence_reference, observed_at, event_json
            ) VALUES (?, ?, ?, ?, ?)""",
            (
                record.product_identity.value,
                record.state.value,
                record.evidence_reference,
                record.observed_at.isoformat(),
                canonical_json(record),
            ),
        )


def _locator_signature(locator: PowerFactoryLocator) -> str:
    return canonical_digest(
        {
            "installation_id": locator.project_provenance.installation_id,
            "profile_id": locator.project_provenance.profile_id,
            "project_key": locator.project_provenance.project_key,
            "object_class": locator.object_class,
            "locator_kind": locator.locator_kind,
            "native_field": locator.native_field,
            "native_value": locator.native_value,
            "canonical_path": locator.canonical_path,
            "evidence_schema": locator.evidence_schema,
        },
        kind="locator-signature",
    )


__all__ = [
    "IdentityAmbiguousError",
    "IdentityBinding",
    "IdentityConflictError",
    "IdentityNotFoundError",
    "IdentityStore",
]
