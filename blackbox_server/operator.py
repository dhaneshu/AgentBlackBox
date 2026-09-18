from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import subprocess
import sys
from importlib.resources import files
from pathlib import Path
from urllib.parse import urlsplit


class OperatorError(RuntimeError):
    pass


def _postgres_env(database_url: str) -> tuple[dict[str, str], list[str]]:
    parsed = urlsplit(database_url.replace("postgresql+psycopg://", "postgresql://"))
    if parsed.scheme not in {"postgresql", "postgres"} or not parsed.hostname:
        raise OperatorError("a PostgreSQL URL is required")
    env = os.environ.copy()
    if parsed.password:
        env["PGPASSWORD"] = parsed.password
    args = ["--host", parsed.hostname, "--port", str(parsed.port or 5432)]
    if parsed.username:
        args += ["--username", parsed.username]
    args += ["--dbname", parsed.path.lstrip("/")]
    return env, args


def _run(command: list[str], env: dict[str, str] | None = None) -> None:
    executable = shutil.which(command[0])
    if not executable:
        raise OperatorError(f"{command[0]} is not installed")
    subprocess.run([executable, *command[1:]], env=env, check=True, shell=False)


def backup(database_url: str, output: Path) -> str:
    output.parent.mkdir(parents=True, exist_ok=True)
    env, connection = _postgres_env(database_url)
    _run(["pg_dump", *connection, "--format=custom", "--file", str(output)], env)
    digest = hashlib.sha256(output.read_bytes()).hexdigest()
    output.with_suffix(output.suffix + ".sha256").write_text(digest + "\n", encoding="ascii")
    return digest


def restore(database_url: str, backup_file: Path, *, clean: bool = False) -> None:
    digest_file = backup_file.with_suffix(backup_file.suffix + ".sha256")
    if digest_file.exists():
        expected = digest_file.read_text(encoding="ascii").strip()
        actual = hashlib.sha256(backup_file.read_bytes()).hexdigest()
        if expected != actual:
            raise OperatorError("backup checksum verification failed")
    env, connection = _postgres_env(database_url)
    flags = ["--clean", "--if-exists"] if clean else []
    _run(["pg_restore", *connection, *flags, "--exit-on-error", str(backup_file)], env)
    verify(database_url)


def migrate(config: str | None = None) -> None:
    from alembic import command
    from alembic.config import Config

    if config:
        alembic_config = Config(config)
    elif Path("alembic.ini").is_file():
        alembic_config = Config("alembic.ini")
    else:
        alembic_config = Config()
        alembic_config.set_main_option(
            "script_location", str(files("blackbox_server").joinpath("alembic"))
        )
        if database_url := os.getenv("BLACKBOX_SERVER_DATABASE_URL"):
            alembic_config.set_main_option("sqlalchemy.url", database_url)
        else:
            raise OperatorError("BLACKBOX_SERVER_DATABASE_URL is required")
    command.upgrade(alembic_config, "head")


def verify(database_url: str) -> None:
    env, connection = _postgres_env(database_url)
    _run(
        [
            "psql",
            *connection,
            "--no-psqlrc",
            "--tuples-only",
            "--command",
            "SELECT CASE WHEN version_num = '0001_phase6' "
            "AND to_regclass('public.events') IS NOT NULL "
            "AND to_regclass('public.jobs') IS NOT NULL THEN 'verified' "
            "ELSE CAST(1/0 AS text) END FROM alembic_version;",
        ],
        env,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="blackbox-operator")
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("backup", "restore", "verify"):
        item = commands.add_parser(name)
        item.add_argument("--database-url", default=os.getenv("BLACKBOX_SERVER_DATABASE_URL"))
        if name in {"backup", "restore"}:
            item.add_argument("--file", type=Path, required=True)
        if name == "restore":
            item.add_argument("--clean", action="store_true")
    migration = commands.add_parser("migrate")
    migration.add_argument("--config")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "migrate":
            migrate(args.config)
        elif not args.database_url:
            raise OperatorError("--database-url or BLACKBOX_SERVER_DATABASE_URL is required")
        elif args.command == "backup":
            print(backup(args.database_url, args.file))
        elif args.command == "restore":
            restore(args.database_url, args.file, clean=args.clean)
        else:
            verify(args.database_url)
    except (OperatorError, subprocess.CalledProcessError) as exc:
        print(f"operator error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
