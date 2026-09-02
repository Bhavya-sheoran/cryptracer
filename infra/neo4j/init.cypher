// ===========================================================================
// SIH26183 : Neo4j constraints + indexes.
// Applied idempotently by the backend on startup (app/db/neo4j.py).
//
// Graph model summary (full prose version in docs/schema.md):
//   (:Address)-[:SENT]->(:Transaction)-[:RECEIVED_BY]->(:Address)
//   (:Address)-[:TRANSFERRED]->(:Address)      // denormalised, for fast tracing
//   (:Address)-[:MEMBER_OF]->(:Cluster)
//   (:Cluster)-[:ATTRIBUTED_TO]->(:Entity)
//   (:Address)-[:TAGGED_AS]->(:Entity)
//   (:Case)-[:REPORTED]->(:Address)
// ===========================================================================

// --- Node key constraints --------------------------------------------------
CREATE CONSTRAINT address_unique IF NOT EXISTS
FOR (a:Address) REQUIRE (a.chain, a.address_norm) IS UNIQUE;

CREATE CONSTRAINT transaction_unique IF NOT EXISTS
FOR (t:Transaction) REQUIRE (t.chain, t.txid) IS UNIQUE;

CREATE CONSTRAINT cluster_unique IF NOT EXISTS
FOR (c:Cluster) REQUIRE c.cluster_key IS UNIQUE;

CREATE CONSTRAINT entity_unique IF NOT EXISTS
FOR (e:Entity) REQUIRE e.entity_id IS UNIQUE;

CREATE CONSTRAINT case_unique IF NOT EXISTS
FOR (k:Case) REQUIRE k.case_id IS UNIQUE;

// --- Lookup indexes --------------------------------------------------------
CREATE INDEX address_norm_idx IF NOT EXISTS
FOR (a:Address) ON (a.address_norm);

CREATE INDEX address_entity_type_idx IF NOT EXISTS
FOR (a:Address) ON (a.entity_type);

CREATE INDEX transaction_ts_idx IF NOT EXISTS
FOR (t:Transaction) ON (t.timestamp);

CREATE INDEX entity_type_idx IF NOT EXISTS
FOR (e:Entity) ON (e.entity_type);

// --- Relationship property indexes (Neo4j 5.x) -----------------------------
CREATE INDEX transferred_ts_idx IF NOT EXISTS
FOR ()-[r:TRANSFERRED]-() ON (r.timestamp);

CREATE INDEX transferred_txid_idx IF NOT EXISTS
FOR ()-[r:TRANSFERRED]-() ON (r.txid);
