"""Structural dependency boundaries shared by the architecture regressions."""
import ast


EXPOSURE_READS = {'proposal_priority', 'exposure_report', 'read_exposure_context'}
HTTP_SDK_ROOTS = {'httpx', 'openai', 'requests', 'urllib', 'http'}


def _literal_dynamic_imports(tree):
    loaders = {'__import__'}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for item in node.names:
                if (node.module, item.name) in {
                    ('importlib', 'import_module'), ('builtins', '__import__'),
                }:
                    loaders.add(item.asname or item.name)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        dynamic = (isinstance(node.func, ast.Name) and node.func.id in loaders) or (
            isinstance(node.func, ast.Attribute) and node.func.attr in {'import_module', '__import__'}
        )
        target = node.args[0] if node.args else next(
            (keyword.value for keyword in node.keywords if keyword.arg == 'name'), None
        )
        if dynamic and isinstance(target, ast.Constant) and isinstance(target.value, str):
            yield target.value


def assert_no_ledger_writes(source):
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            assert not any(item.name.split('.')[0] == 'ledger' for item in node.names)
        elif isinstance(node, ast.ImportFrom) and (node.module or '').split('.')[0] == 'ledger':
            assert node.module == 'ledger.exposure'
            assert {item.name for item in node.names} <= EXPOSURE_READS
    assert not any(name.split('.')[0] == 'ledger' for name in _literal_dynamic_imports(tree))


def assert_no_direct_http_or_llm(source):
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            assert not any(item.name.split('.')[0] in HTTP_SDK_ROOTS for item in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert (node.module or '').split('.')[0] not in HTTP_SDK_ROOTS
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            # Capturing this exact configuration key is not a provider call.
            if node.value != 'OPENAI_MODEL':
                assert 'httpx' not in node.value.lower() and 'openai' not in node.value.lower()
        elif isinstance(node, (ast.Name, ast.Attribute)):
            name = node.id if isinstance(node, ast.Name) else node.attr
            assert 'httpx' not in name.lower() and 'openai' not in name.lower()
    assert not any(name.split('.')[0] in HTTP_SDK_ROOTS for name in _literal_dynamic_imports(tree))
