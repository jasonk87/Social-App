import os
import sqlite3
import tempfile
import pytest

import server

@pytest.fixture(autouse=True)
def setup_test_db(monkeypatch):
    # Create a temporary file for the database
    fd, temp_db_path = tempfile.mkstemp(suffix='.db')
    os.close(fd)

    # Override the DB_PATH in server module
    monkeypatch.setattr(server, 'DB_PATH', temp_db_path)

    # Reset the connection pool so it uses the new DB_PATH
    if server.DB_POOL is not None:
        # Drain the existing queue if any (though usually empty in tests until hit)
        while not server.DB_POOL.pool.empty():
            conn = server.DB_POOL.pool.get()
            conn.close()
    server.DB_POOL = server.ConnectionPool(temp_db_path)

    # Initialize the schema
    server.init_db()

    yield

    # Cleanup
    if server.DB_POOL is not None:
        while not server.DB_POOL.pool.empty():
            conn = server.DB_POOL.pool.get()
            conn.close()
        server.DB_POOL = None

    try:
        os.remove(temp_db_path)
        # SQLite WAL files
        if os.path.exists(temp_db_path + '-wal'):
            os.remove(temp_db_path + '-wal')
        if os.path.exists(temp_db_path + '-shm'):
            os.remove(temp_db_path + '-shm')
    except OSError:
        pass
