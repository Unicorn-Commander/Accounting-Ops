"""Keep the stack-bound RLS module visible to the unit-tier collector."""


def test_db_rls_roles_module_imports() -> None:
    """A stale integration module must fail loudly in the unit job."""
    import tests.integration.test_db_rls_roles  # noqa: F401
