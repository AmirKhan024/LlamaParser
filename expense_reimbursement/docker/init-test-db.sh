#!/bin/sh
# Runs once, on first container startup only (postgres's own
# docker-entrypoint-initdb.d convention) -- creates the second database
# tests run against, alongside the main app database POSTGRES_DB already
# creates automatically.
set -e
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<-EOSQL
    CREATE DATABASE expense_test;
EOSQL
