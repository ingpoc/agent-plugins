"""Owner CLI dispatch and serve entrypoint."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from context_ledger.contracts import LedgerError, canonical_json, fail_dict, ok
from context_ledger.store import Store, bind_ledger, init_ledger


def _print(payload: dict[str, Any], *, stream=None) -> int:
    target = stream or sys.stderr
    if payload.get("ok") is False:
        target.write(canonical_json(payload) + "\n")
        return 2
    target.write(canonical_json(payload) + "\n")
    return 0


def _flag(args: list[str], name: str) -> bool:
    return name in args


def _opt(args: list[str], name: str) -> str | None:
    if name not in args:
        return None
    i = args.index(name)
    if i + 1 >= len(args):
        raise LedgerError("INVALID_ARGUMENT")
    return args[i + 1]


def _req(args: list[str], name: str) -> str:
    value = _opt(args, name)
    if value is None:
        raise LedgerError("INVALID_ARGUMENT")
    return value


def dispatch(plugin_data: Path, command: str, args: list[str]) -> int:
    try:
        return _dispatch(plugin_data, command, args)
    except LedgerError as exc:
        return _print(fail_dict(exc))


def _dispatch(plugin_data: Path, command: str, args: list[str]) -> int:
    if command == "init":
        ledger_dir = _opt(args, "--ledger-dir")
        binding = init_ledger(
            plugin_data,
            Path(ledger_dir) if ledger_dir else None,
            _req(args, "--scope"),
            _req(args, "--actor"),
        )
        return _print(ok(binding))
    if command == "bind":
        binding = bind_ledger(
            plugin_data,
            Path(_req(args, "--ledger-dir")),
            _req(args, "--ledger-id"),
            _req(args, "--scope"),
            _req(args, "--actor"),
            replace=_flag(args, "--replace-binding"),
        )
        return _print(ok(binding))
    if command == "serve":
        from context_ledger.server import serve

        return serve(plugin_data)
    store = Store(plugin_data, actor_channel="owner_cli")
    if command == "doctor":
        return _print(ok(store.doctor()))
    if command == "scope-add":
        return _print(ok(store.scope_add(_req(args, "--scope"))))
    if command == "export":
        return _print(
            ok(
                store.export(
                    Path(_req(args, "--output")),
                    include_local_only=_flag(args, "--include-local-only"),
                )
            )
        )
    if command == "import":
        return _print(
            ok(
                store.import_file(
                    Path(_req(args, "--input")),
                    _req(args, "--source-ledger"),
                    _req(args, "--source-scope"),
                    include_local_only=_flag(args, "--include-local-only"),
                )
            )
        )
    if command == "attest":
        return _print(
            ok(
                store.attest(
                    _req(args, "--decision"),
                    int(_req(args, "--expected-revision")),
                    _req(args, "--request-id"),
                    _req(args, "--authority-ref"),
                    _req(args, "--reason"),
                )
            )
        )
    if command == "purge":
        return _print(
            ok(
                store.purge(
                    _req(args, "--decision"),
                    int(_req(args, "--expected-revision")),
                    _req(args, "--confirm-ledger"),
                )
            )
        )
    if command == "rebuild":
        return _print(ok(store.rebuild()))
    if command == "migrate":
        return _print(ok(store.migrate()))
    if command == "resume-maintenance":
        return _print(ok(store.resume_maintenance(_req(args, "--confirm-ledger"))))
    raise LedgerError("INVALID_ARGUMENT")


def main() -> int:
    from context_ledger.contracts import LedgerError as Err

    argv = sys.argv[1:]
    if not argv or argv[0] in {"-h", "--help"}:
        sys.stderr.write(
            "usage: python -m context_ledger --data DIR "
            "init|bind|serve|doctor|export|import|attest|purge|rebuild|migrate|"
            "resume-maintenance|scope-add|ensure-global-triggers ...\n"
        )
        return 0
    data = None
    rest = argv
    if rest[0] == "--data":
        if len(rest) < 3:
            return _print(fail_dict(Err("INVALID_ARGUMENT")))
        data = Path(rest[1])
        rest = rest[2:]
    if data is None or not rest:
        return _print(fail_dict(Err("INVALID_ARGUMENT")))
    return dispatch(data, rest[0], rest[1:])


if __name__ == "__main__":
    sys.exit(main())
