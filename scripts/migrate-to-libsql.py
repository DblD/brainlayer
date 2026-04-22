#!/usr/bin/env python3
"""Migrate BrainLayer from sqlite-vec to libSQL native vector search.

This script:
1. Reads all vectors from chunk_vectors (sqlite-vec) via APSW
2. Adds an embedding column (F32_BLOB) to chunks table via libSQL
3. Copies vectors into the new column
4. Creates a DiskANN index
5. Verifies the migration
6. Optionally drops the old chunk_vectors table

Usage:
    python scripts/migrate-to-libsql.py                    # dry run
    python scripts/migrate-to-libsql.py --execute          # do it
    python scripts/migrate-to-libsql.py --execute --drop   # do it + remove old tables
"""

import argparse
import os
import shutil
import struct
import sys
import time


def main():
    parser = argparse.ArgumentParser(description="Migrate BrainLayer vectors to libSQL native format")
    parser.add_argument("--execute", action="store_true", help="Actually perform the migration (default: dry run)")
    parser.add_argument("--drop", action="store_true", help="Drop old chunk_vectors table after migration")
    parser.add_argument("--db", default=os.path.expanduser("~/.local/share/brainlayer/brainlayer.db"), help="DB path")
    args = parser.parse_args()

    db_path = args.db
    backup_path = f"{db_path}.pre-libsql-backup"

    print(f"Database: {db_path}")
    print(f"Size: {os.path.getsize(db_path) / (1024**2):.1f} MB")
    print(f"Mode: {'EXECUTE' if args.execute else 'DRY RUN'}")
    print()

    # Step 1: Extract vectors via APSW (sqlite-vec)
    print("Step 1: Reading vectors from chunk_vectors (sqlite-vec)...")
    import apsw
    import sqlite_vec

    conn_apsw = apsw.Connection(db_path, flags=apsw.SQLITE_OPEN_READONLY)
    conn_apsw.setbusytimeout(30000)
    conn_apsw.enable_load_extension(True)
    sqlite_vec.load(conn_apsw)

    t0 = time.time()
    vectors = conn_apsw.cursor().execute("SELECT chunk_id, embedding FROM chunk_vectors").fetchall()
    t1 = time.time()
    conn_apsw.close()

    print(f"  Extracted {len(vectors)} vectors in {t1-t0:.1f}s")

    # Validate dimensions
    dims = len(vectors[0][1]) // 4
    print(f"  Dimensions: {dims}")
    assert dims == 1024, f"Expected 1024 dimensions, got {dims}"

    if not args.execute:
        print("\nDry run complete. Run with --execute to migrate.")
        return

    # Step 2: Backup
    print(f"\nStep 2: Backing up to {backup_path}...")
    shutil.copy2(db_path, backup_path)
    print(f"  Backup: {os.path.getsize(backup_path) / (1024**2):.1f} MB")

    # Step 3: Add embedding column and migrate via libSQL
    print("\nStep 3: Adding embedding column to chunks table...")
    import libsql_experimental as libsql

    conn = libsql.connect(db_path)

    # Check if column already exists
    cols = conn.execute("PRAGMA table_info(chunks)").fetchall()
    col_names = [c[1] for c in cols]
    if "embedding" in col_names:
        print("  WARNING: embedding column already exists — skipping ALTER TABLE")
    else:
        conn.execute("ALTER TABLE chunks ADD COLUMN embedding F32_BLOB(1024)")
        print("  Column added")

    # Step 4: Copy vectors into chunks.embedding
    print(f"\nStep 4: Migrating {len(vectors)} vectors...")
    t0 = time.time()
    batch_size = 500
    for i in range(0, len(vectors), batch_size):
        batch = vectors[i:i + batch_size]
        for chunk_id, emb_bytes in batch:
            conn.execute("UPDATE chunks SET embedding = ? WHERE id = ?", (emb_bytes, chunk_id))
        conn.execute("COMMIT")
        done = min(i + batch_size, len(vectors))
        if done % 5000 == 0 or done == len(vectors):
            elapsed = time.time() - t0
            rate = done / elapsed if elapsed > 0 else 0
            print(f"  {done}/{len(vectors)} ({rate:.0f}/s)")
    t1 = time.time()
    print(f"  Migration complete in {t1-t0:.1f}s")

    # Step 5: Create DiskANN index
    print("\nStep 5: Creating DiskANN index...")
    t0 = time.time()
    conn.execute("CREATE INDEX IF NOT EXISTS chunks_embedding_idx ON chunks(libsql_vector_idx(embedding))")
    t1 = time.time()
    print(f"  Index created in {t1-t0:.1f}s")

    # Step 6: Verify
    print("\nStep 6: Verifying...")
    null_count = conn.execute("SELECT COUNT(*) FROM chunks WHERE embedding IS NULL").fetchone()[0]
    total = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
    print(f"  Total chunks: {total}")
    print(f"  With embeddings: {total - null_count}")
    print(f"  Missing: {null_count}")

    # Test vector search
    query_vec = struct.pack(f'{1024}f', *([0.1] * 1024))
    results = conn.execute(
        "SELECT id, vector_distance_cos(embedding, ?) AS dist FROM chunks WHERE embedding IS NOT NULL ORDER BY dist LIMIT 3",
        (query_vec,)
    ).fetchall()
    print(f"  Test search: {len(results)} results")
    for r in results:
        print(f"    {r[0][:60]}... (dist: {r[1]:.6f})")

    # Step 7: Optionally drop old tables
    if args.drop:
        print("\nStep 7: Dropping old sqlite-vec tables...")
        # These are the sqlite-vec internal tables
        for table in ["chunk_vectors", "chunk_vectors_chunks", "chunk_vectors_info",
                       "chunk_vectors_rowids", "chunk_vectors_vector_chunks00"]:
            try:
                conn.execute(f"DROP TABLE IF EXISTS {table}")
                print(f"  Dropped {table}")
            except Exception as e:
                print(f"  Skip {table}: {e}")

    conn.close()

    final_size = os.path.getsize(db_path) / (1024**2)
    print(f"\nDone. DB size: {final_size:.1f} MB")
    print(f"Backup at: {backup_path}")


if __name__ == "__main__":
    main()
