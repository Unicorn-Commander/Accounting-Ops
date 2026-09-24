from pathlib import Path
import re


MIGRATION = (
    Path(__file__).parents[2]
    / "migrations/public/versions/0017_membership_audit.py"
)


def test_membership_audit_insert_columns_match_migration_columns():
    migration = MIGRATION.read_text()
    table_columns = re.search(
        r'op\.create_table\(\s*"membership_audit",(?P<body>.*?)\n\s*sa\.ForeignKeyConstraint',
        migration,
        re.S,
    ).group("body")
    migration_columns = re.findall(r'sa\.Column\(\s*"([^"]+)"', table_columns)
    insert_columns = re.search(
        r"INSERT INTO public\.membership_audit \((?P<columns>.*?)\) VALUES",
        (Path(__file__).parents[2] / "app/api/members.py").read_text(),
        re.S,
    ).group("columns")
    insert_columns = [column.strip() for column in insert_columns.split(",")]
    assert insert_columns == [
        column for column in migration_columns if column != "id" and column != "occurred_at"
    ]
