# Model Graph Topology Contract

**Status:** PROVISIONAL - local Buildout 4 preparation, Windows extraction evidence pending.

## Source Of Truth

SQLite stores normalized context, extraction-run, asset, attribute, relationship, and provenance records. A `networkx.MultiGraph` is rebuilt from a persisted `GraphSnapshot` for graph operations. It is a disposable projection and is never written back as authoritative state.

Every graph record is scoped to the `ModelContext.configuration_key` and `ExtractionRevision`. The extraction fingerprint identifies captured structural content only; it is not a substitute for a dependency-scoped live-state fingerprint.

## Equipment Representation

Each asset is a graph node. Each relationship has a stable relationship ID and becomes a keyed `MultiGraph` edge, so parallel lines, switches, or other parallel equipment remain distinct. `GraphAsset.in_service`, `GraphRelationship.in_service`, and the required `GraphAsset.switch_closed` state for switches are explicit. Electrical-path operations exclude open switches and out-of-service assets or relationships; non-energized assets remain observable in the persisted topology.

Two- and three-winding transformers use one transformer equipment node joined by `TERMINAL_OF` relationships to each winding terminal. A two-winding transformer therefore has two terminal relationships; a three-winding transformer has three. This keeps transformer identity, provenance, switching state, and every winding visible without collapsing the three-winding case into invented pairwise branches or introducing an untyped hyperedge.

## Refresh And Query Rules

Full refresh writes a complete `GraphSnapshot`. Incremental refresh starts from the latest stored snapshot, only accepts changed assets already known to the context, advances the extraction revision, and writes a new complete immutable snapshot. A failed or unsupported incremental mapping falls back to full refresh.

All graph operations are typed and bounded. They return summaries, stable `AssetReference` values, and stable relationship IDs, never a raw NetworkX object or an unbounded subgraph. Supported operations are neighborhood, electrical path, area/zone membership, connected components, outage/change impact, and topology diff.

## Evidence Boundary

The representation and tests are portable preparation only. Real PowerFactory class mapping, switch semantics, out-of-service behavior, area/zone assignment, transformer terminal extraction, and configuration-change evidence remain `BLOCKED - Windows validation required` before Buildout 4 can be accepted.
