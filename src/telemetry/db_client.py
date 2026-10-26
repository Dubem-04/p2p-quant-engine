"""
Database connection pool manager and ORM abstraction layer for TimescaleDB/PostgreSQL.

Manages persistent connection pooling, schema migrations, and high-throughput
asynchronous writes for tick-level order book snapshots, active quote updates,
and trade execution telemetry. Implements hypertable optimizations and retention
policies for time-series operational data.

Classes:
    DatabaseClient: Async context manager handling connection checkout, transactional
        rollbacks, batch insertions, and automated query execution.
"""