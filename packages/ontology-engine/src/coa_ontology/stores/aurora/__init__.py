# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Aurora PostgreSQL backend (``WORKBENCH_BACKEND=aurora_pgvector``).

Both stores talk to Aurora through the RDS Data API (``coa_common.aurora_data_api``),
so they need no Postgres driver and no VPC attachment:

- :mod:`coa_ontology.stores.aurora.graph` holds RDF quads in one table and
  answers multi-hop questions with recursive CTEs.
- :mod:`coa_ontology.stores.aurora.schema` checks (read-only) that the tables
  created by ``aurora-init.sql`` exist.

Embeddings live in :mod:`coa_ontology.stores.aurora_vector` (``search_doc``).

Import the concrete modules; nothing is imported here so that importing the
package never touches boto3 or the environment.
"""
