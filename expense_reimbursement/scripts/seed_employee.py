"""Seed the one employee Stage 1 runs as (no login yet -- see
repository.get_or_create_seed_employee / server.get_current_employee)."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import repository
from db import get_sessionmaker


def main() -> None:
    session = get_sessionmaker()()
    try:
        employee = repository.get_or_create_seed_employee(session)
        print(f"employee: {employee.name} <{employee.email}> id={employee.id}")
    finally:
        session.close()


if __name__ == "__main__":
    main()
