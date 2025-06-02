-- init_db.sql

-- 1. Create a dedicated PostgreSQL user (adjust username/password as needed)
CREATE USER mcp2user WITH PASSWORD 'mcp2pass';

-- 2. Create the database owned by that user
CREATE DATABASE mcp2db OWNER mcp2user;

-- 3. Connect to the newly created database
\c mcp2db

-- 4. Create the context_entries table for ContextTool service
CREATE TABLE context_entries (
    context_key TEXT PRIMARY KEY,
    serialized_value BYTEA NOT NULL,
    metadata_json TEXT
);

-- 5. Grant privileges
GRANT ALL PRIVILEGES ON DATABASE mcp2db TO mcp2user;
GRANT ALL PRIVILEGES ON TABLE context_entries TO mcp2user;
