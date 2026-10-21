"""`deploy/db-role-cutover.sh` moves the app off the RDS master, and nothing else.

Two layers, both offline (no AWS, no RDS):

- **The SQL** (`deploy/sql/db-role-{cluster,database}.sql`) runs against the
  test Postgres as a throwaway *master-like* role — LOGIN, CREATEDB, CREATEROLE,
  **not** a superuser, which is what RDS's master is — over throwaway databases,
  with throwaway role names (the files read them from `feoh.owner_role` /
  `feoh.app_role` / `feoh.iam_role`, so nothing here touches a real role). It
  pins: both roles' exact attributes and memberships; the master joining the
  owner but never the IAM-bearing login role (RDS would make it IAM-only);
  ownership of every kind of object moving to the owner; extension members
  staying put; a second run being a no-op; the master keeping every privilege
  (the rollback); the catalog-only search_path defeating a planted function;
  refusal as the app role, without the IAM role, on an unexpected membership,
  and on an object class the file does not know how to move; and that the app
  can then do what the app does — DDL, CREATE / DROP DATABASE (including
  WITH (FORCE) over its own open sessions) — and not what it must not.
- **The script** runs against a stub `docker` (the pattern of
  `tests/test_deploy_db_mode.py`): which databases it visits, that each gets the
  role settings and ON_ERROR_STOP, and its refusals before anything runs.

Docs: `docs/decisions.md` §305, `deploy/README.md` § Database role.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import uuid
from pathlib import Path

import asyncpg
import pytest

from app.config import settings
from app.db_engine import asyncpg_connect_kwargs

REPO_ROOT = Path(__file__).resolve().parents[2]
DEPLOY = REPO_ROOT / "deploy"
CLUSTER_SQL = (DEPLOY / "sql" / "db-role-cluster.sql").read_text()
DATABASE_SQL = (DEPLOY / "sql" / "db-role-database.sql").read_text()


# --------------------------------------------------------------------------- #
# The SQL, against a real server
# --------------------------------------------------------------------------- #


class Cluster:
    """Throwaway roles + databases on the test server, removed afterwards."""

    def __init__(self) -> None:
        tag = uuid.uuid4().hex[:8]
        self.master = f"cutover_master_{tag}"
        self.owner = f"cutover_owner_{tag}"
        self.app = f"cutover_app_{tag}"
        self.iam = f"cutover_iam_{tag}"
        self.control = f"cutover_ctl_{tag}"
        self.tenant = f"feoh_cutover_{tag}"
        self.other = f"feoh_cutover_other_{tag}"  # a feoh_* DB the master does NOT own
        self.password = uuid.uuid4().hex
        self.app_password = uuid.uuid4().hex

    def su(self, database: str = "postgres") -> dict:
        return asyncpg_connect_kwargs(settings.database_url, database=database)

    def as_role(self, role: str, password: str, database: str) -> dict:
        return {**self.su(database), "user": role, "password": password}

    def settings_sql(self, iam: str | None = None) -> str:
        return (
            f"SET feoh.owner_role = '{self.owner}'; SET feoh.app_role = '{self.app}'; "
            f"SET feoh.iam_role = '{iam or self.iam}';\n"
        )


async def _exec(kwargs: dict, sql: str) -> None:
    conn = await asyncpg.connect(**kwargs)
    try:
        await conn.execute(sql)
    finally:
        await conn.close()


async def _fetch(kwargs: dict, sql: str):
    conn = await asyncpg.connect(**kwargs)
    try:
        return await conn.fetch(sql)
    finally:
        await conn.close()


@pytest.fixture
async def cluster():
    c = Cluster()
    su = c.su()
    try:
        await _setup(c, su)
        yield c
    finally:
        for db in (c.control, c.tenant, c.other, f"{c.tenant}_new"):
            await _exec(su, f'DROP DATABASE IF EXISTS "{db}" WITH (FORCE)')
        for role in (c.app, c.owner, c.master, c.iam):
            await _exec(
                su,
                f"DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{role}') "
                f"THEN EXECUTE 'DROP OWNED BY {role}'; EXECUTE 'DROP ROLE {role}'; END IF; END $$",
            )


async def _setup(c: Cluster, su: dict) -> None:
    await _exec(
        su,
        f"""
        CREATE ROLE {c.iam} NOLOGIN;
        CREATE ROLE {c.master} LOGIN PASSWORD '{c.password}' CREATEDB CREATEROLE;
        -- RDS's master may grant rds_iam without being in it (it would be
        -- IAM-only otherwise); ADMIN alone is the nearest stand-in.
        GRANT {c.iam} TO {c.master} WITH ADMIN TRUE, INHERIT FALSE, SET FALSE;
        """,
    )
    await _exec(su, f'CREATE DATABASE "{c.control}" OWNER {c.master}')
    await _exec(su, f'CREATE DATABASE "{c.tenant}" OWNER {c.master}')
    await _exec(su, f'CREATE DATABASE "{c.other}"')  # owned by someone else
    # An extension whose member objects the master owns — how pgvector's look
    # on RDS, where the master creates it. Only a superuser can stage that here.
    await _exec(c.su(c.tenant), "CREATE EXTENSION IF NOT EXISTS citext")
    await _exec(c.su(c.tenant), f"ALTER FUNCTION citextin(cstring) OWNER TO {c.master}")
    # What the app's own databases hold, created the way production's were: by
    # the master.
    await _exec(
        c.as_role(c.master, c.password, c.tenant),
        """
        CREATE TABLE invoices (id serial PRIMARY KEY, n int GENERATED ALWAYS AS IDENTITY);
        CREATE TABLE parts (k int) PARTITION BY RANGE (k);
        CREATE TABLE parts_1 PARTITION OF parts FOR VALUES FROM (0) TO (10);
        CREATE SEQUENCE free_seq;
        CREATE TYPE invoice_state AS ENUM ('new', 'paid');
        CREATE TYPE pair AS (a int, b int);
        CREATE DOMAIN money_amount AS numeric(18, 2);
        CREATE VIEW invoice_view AS SELECT id FROM invoices;
        CREATE MATERIALIZED VIEW invoice_mv AS SELECT id FROM invoices;
        CREATE FUNCTION audit_guard() RETURNS trigger LANGUAGE plpgsql
            AS $$ BEGIN RAISE EXCEPTION 'append-only'; END $$;
        CREATE TRIGGER no_delete BEFORE DELETE ON invoices
            FOR EACH ROW EXECUTE FUNCTION audit_guard();
        CREATE SCHEMA side;
        CREATE TABLE side.t (id int);
        """,
    )


async def _cutover(c: Cluster) -> None:
    master_ctl = c.as_role(c.master, c.password, c.control)
    await _exec(master_ctl, c.settings_sql() + CLUSTER_SQL)
    for db in (c.control, c.tenant):
        await _exec(c.as_role(c.master, c.password, db), c.settings_sql() + DATABASE_SQL)


async def _master_owned(c: Cluster, db: str) -> list[str]:
    """Non-extension objects in `db` still owned by the master."""
    rows = await _fetch(
        c.su(db),
        f"""
        SELECT s.classid::regclass::text || ' ' || s.objid AS obj
        FROM pg_shdepend s
        WHERE s.dbid = (SELECT oid FROM pg_database WHERE datname = current_database())
          AND s.refobjid = (SELECT oid FROM pg_roles WHERE rolname = '{c.master}')
          AND s.deptype = 'o' AND s.classid <> 'pg_extension'::regclass
          AND NOT EXISTS (SELECT 1 FROM pg_depend d
                          WHERE d.classid = s.classid AND d.objid = s.objid AND d.deptype = 'e')
        """,
    )
    return [r["obj"] for r in rows]


async def test_the_cutover_hands_everything_but_extensions_to_the_app_role(cluster) -> None:
    c = cluster
    await _cutover(c)

    roles = {
        r["rolname"]: dict(r)
        for r in await _fetch(
            c.su(),
            f"SELECT rolname, rolcanlogin, rolcreatedb, rolcreaterole, rolsuper, rolreplication, "
            f"rolbypassrls, rolpassword IS NULL AS no_password FROM pg_authid "
            f"WHERE rolname IN ('{c.owner}', '{c.app}')",
        )
    }
    common = {"rolcreaterole": False, "rolsuper": False, "rolreplication": False}
    assert roles[c.owner] == {
        "rolname": c.owner,
        "rolcanlogin": False,
        "rolcreatedb": True,
        "rolbypassrls": False,
        "no_password": True,
        **common,
    }
    assert roles[c.app] == {
        "rolname": c.app,
        "rolcanlogin": True,
        "rolcreatedb": False,
        "rolbypassrls": False,
        "no_password": True,
        **common,
    }, "the login role signs in by IAM token only and creates nothing itself"

    memberships = {
        (r["member"], r["role"]): (r["inherit_option"], r["set_option"])
        for r in await _fetch(
            c.su(),
            "SELECT member::regrole::text AS member, roleid::regrole::text AS role, "
            "inherit_option, set_option FROM pg_auth_members "
            f"WHERE member::regrole::text IN ('{c.owner}', '{c.app}', '{c.master}') "
            f"AND roleid::regrole::text IN ('{c.owner}', '{c.app}', '{c.iam}')",
        )
    }
    assert memberships[(c.app, c.iam)] is not None
    assert memberships[(c.app, c.owner)] == (True, True)
    assert memberships[(c.master, c.owner)] == (True, True), "rollback needs the owner's privileges"
    # PG16 gives a role's creator an ADMIN-only grant; anything more would put
    # the master in the rds_iam chain, and RDS would stop taking its password.
    assert memberships.get((c.master, c.app), (False, False)) == (False, False)
    assert not any(member == c.owner for member, _ in memberships), "the owner joins nothing"

    defaults = await _fetch(
        c.su(),
        "SELECT unnest(setconfig) AS c FROM pg_db_role_setting "
        f"WHERE setrole = to_regrole('{c.app}') AND setdatabase = 0",
    )
    assert [r["c"] for r in defaults] == [f"role={c.owner}"]

    owners = {
        r["datname"]: r["owner"]
        for r in await _fetch(
            c.su(),
            "SELECT datname, pg_get_userbyid(datdba) AS owner FROM pg_database "
            f"WHERE datname IN ('{c.control}', '{c.tenant}', '{c.other}')",
        )
    }
    assert owners[c.control] == c.owner and owners[c.tenant] == c.owner
    assert owners[c.other] != c.owner, "a database the master does not own was taken"

    assert await _master_owned(c, c.tenant) == []
    assert await _master_owned(c, c.control) == []
    member = await _fetch(
        c.su(c.tenant),
        "SELECT pg_get_userbyid(proowner) AS owner FROM pg_proc WHERE proname = 'citextin'",
    )
    assert member[0]["owner"] == c.master, "an extension member object was moved"
    ext = await _fetch(
        c.su(c.tenant),
        "SELECT pg_get_userbyid(extowner) AS owner FROM pg_extension WHERE extname = 'citext'",
    )
    assert ext[0]["owner"] not in (c.app, c.owner)


async def test_a_second_run_is_a_no_op(cluster) -> None:
    c = cluster
    await _cutover(c)
    await _cutover(c)  # raises on any error (ON_ERROR_STOP is the DO block's)
    assert await _master_owned(c, c.tenant) == []


async def test_the_app_role_can_do_what_the_app_does_and_no_more(cluster) -> None:
    c = cluster
    await _cutover(c)
    # Production signs in with a token; locally a password stands in for it.
    await _exec(c.su(), f"ALTER ROLE {c.app} PASSWORD '{c.app_password}'")
    app_tenant = c.as_role(c.app, c.app_password, c.tenant)

    await _exec(
        app_tenant,
        """
        INSERT INTO invoices DEFAULT VALUES;
        ALTER TABLE invoices ADD COLUMN IF NOT EXISTS probe int;
        ALTER TYPE invoice_state ADD VALUE IF NOT EXISTS 'void';
        CREATE OR REPLACE FUNCTION audit_guard() RETURNS trigger LANGUAGE plpgsql
            AS $$ BEGIN RAISE EXCEPTION 'append-only'; END $$;
        REFRESH MATERIALIZED VIEW invoice_mv;
        SELECT nextval('free_seq');
        CREATE EXTENSION IF NOT EXISTS citext;
        """,
    )
    with pytest.raises(asyncpg.RaiseError, match="append-only"):
        await _exec(app_tenant, "DELETE FROM invoices")

    app_maint = c.as_role(c.app, c.app_password, "postgres")
    who = await _fetch(app_tenant, "SELECT session_user AS s, current_user AS c")
    assert (who[0]["s"], who[0]["c"]) == (c.app, c.owner), "the role default did not apply"

    # What signup / partner provisioning does, then what tenant deletion does —
    # over another of the app's own open sessions, which is the case FORCE is for.
    await _exec(app_maint, f'CREATE DATABASE "{c.tenant}_new"')
    owner = await _fetch(
        c.su(),
        f"SELECT pg_get_userbyid(datdba) AS o FROM pg_database WHERE datname = '{c.tenant}_new'",
    )
    assert owner[0]["o"] == c.owner
    holder = await asyncpg.connect(**c.as_role(c.app, c.app_password, f"{c.tenant}_new"))
    try:
        with pytest.raises(asyncpg.InsufficientPrivilegeError, match="terminate"):
            await _exec(app_maint, f'DROP DATABASE "{c.tenant}_new" WITH (FORCE)')
        # tenant_provisioning._drop_postgres_database's own sequence:
        dropper = await asyncpg.connect(**app_maint)
        try:
            await dropper.execute("SET ROLE NONE")
            await dropper.execute(f'DROP DATABASE "{c.tenant}_new" WITH (FORCE)')
        finally:
            await dropper.close()
    finally:
        await holder.close()
    with pytest.raises(asyncpg.InsufficientPrivilegeError):
        await _exec(app_maint, f"CREATE ROLE {c.app}_x")


async def test_rollback_the_master_keeps_every_privilege(cluster) -> None:
    """Pointing the app back at the master URL must just work."""
    c = cluster
    await _cutover(c)
    await _exec(
        c.as_role(c.master, c.password, c.tenant),
        "ALTER TABLE invoices ADD COLUMN IF NOT EXISTS rollback_probe int; "
        "INSERT INTO invoices DEFAULT VALUES; "
        "ALTER TYPE invoice_state ADD VALUE IF NOT EXISTS 'x';",
    )
    # ...and anything it created meanwhile goes back on the next run.
    await _exec(c.as_role(c.master, c.password, c.tenant), "CREATE TABLE made_in_rollback (i int)")
    assert await _master_owned(c, c.tenant) != []
    await _cutover(c)
    assert await _master_owned(c, c.tenant) == []


async def test_a_planted_function_never_runs_as_the_master(cluster) -> None:
    """The app owns `public` after the cutover. A better-matching `format`
    there would run with the master's privileges on the next re-run — unless
    the SQL resolves names from the catalog only."""
    c = cluster
    await _cutover(c)
    await _exec(c.su(), f"ALTER ROLE {c.app} PASSWORD '{c.app_password}'")
    await _exec(
        c.as_role(c.app, c.app_password, c.control),
        "CREATE FUNCTION public.format(text, text, text) RETURNS text LANGUAGE plpgsql "
        "AS $$ BEGIN RAISE EXCEPTION 'hijacked'; END $$",
    )
    # The same body WITHOUT the catalog-only search_path would call it.
    with pytest.raises(asyncpg.RaiseError, match="hijacked"):
        await _exec(
            c.as_role(c.master, c.password, c.control),
            c.settings_sql() + CLUSTER_SQL.replace("SET search_path = pg_catalog, pg_temp;", ""),
        )
    await _cutover(c)  # with it: untouched


async def test_refuses_an_unexpected_membership(cluster) -> None:
    c = cluster
    await _cutover(c)
    await _exec(c.su(), f"CREATE ROLE {c.owner}_extra NOLOGIN; GRANT {c.owner}_extra TO {c.app}")
    try:
        with pytest.raises(asyncpg.RaiseError, match="unexpected role membership"):
            await _exec(c.as_role(c.master, c.password, c.control), c.settings_sql() + CLUSTER_SQL)
    finally:
        await _exec(c.su(), f"DROP ROLE {c.owner}_extra")


async def test_refuses_a_third_member_of_the_owner(cluster) -> None:
    """Anyone else in feoh_owner would inherit every tenant's data."""
    c = cluster
    await _cutover(c)
    await _exec(c.su(), f"CREATE ROLE {c.owner}_peer NOLOGIN; GRANT {c.owner} TO {c.owner}_peer")
    try:
        with pytest.raises(asyncpg.RaiseError, match="unexpected role membership"):
            await _exec(c.as_role(c.master, c.password, c.control), c.settings_sql() + CLUSTER_SQL)
    finally:
        await _exec(c.su(), f"DROP ROLE {c.owner}_peer")


async def test_refuses_a_master_inheriting_the_login_role(cluster) -> None:
    """The design's key invariant: RDS makes any (inherited) rds_iam member
    IAM-only, so the master must never be a real member of the login role."""
    c = cluster
    await _cutover(c)
    await _exec(c.su(), f"GRANT {c.app} TO {c.master} WITH INHERIT TRUE")
    with pytest.raises(asyncpg.RaiseError, match="IAM-only"):
        await _exec(c.as_role(c.master, c.password, c.control), c.settings_sql() + CLUSTER_SQL)


async def test_refuses_to_run_as_the_app_role(cluster) -> None:
    c = cluster
    await _cutover(c)
    await _exec(c.su(), f"ALTER ROLE {c.app} PASSWORD '{c.app_password}'")
    with pytest.raises(asyncpg.RaiseError, match="run this as the RDS master"):
        await _exec(c.as_role(c.app, c.app_password, c.control), c.settings_sql() + CLUSTER_SQL)


async def test_refuses_without_the_iam_role(cluster) -> None:
    c = cluster
    with pytest.raises(asyncpg.RaiseError, match="IAM database authentication"):
        await _exec(
            c.as_role(c.master, c.password, c.control),
            c.settings_sql(iam=f"{c.iam}_missing") + CLUSTER_SQL,
        )
    gone = await _fetch(c.su(), f"SELECT 1 FROM pg_roles WHERE rolname = '{c.app}'")
    assert gone == [], "a refused run left the role behind"


async def test_an_object_class_it_cannot_move_stops_the_run(cluster) -> None:
    c = cluster
    await _exec(
        c.as_role(c.master, c.password, c.tenant),
        "CREATE COLLATION cutover_coll (provider = icu, locale = 'und')",
    )
    await _exec(c.as_role(c.master, c.password, c.control), c.settings_sql() + CLUSTER_SQL)
    with pytest.raises(asyncpg.RaiseError, match="does not know how to move"):
        await _exec(c.as_role(c.master, c.password, c.tenant), c.settings_sql() + DATABASE_SQL)


# --------------------------------------------------------------------------- #
# The script, against a stub docker
# --------------------------------------------------------------------------- #

DOCKER_STUB = """#!/bin/sh
n=$(cat "$STUB_DIR/n" 2>/dev/null || echo 0)
n=$((n + 1))
echo "$n" > "$STUB_DIR/n"
printf '%s\\n' "$@" > "$STUB_DIR/argv.$n"
env > "$STUB_DIR/env"
cat > "$STUB_DIR/stdin.$n"
case "$*" in
*"SELECT datname"*) printf 'feoh_acme\\nfeohledger\\n' ;;
*"session_user"*) printf '%s\\n' "${WHO:-feoh_app|feoh_owner|false}" ;;
esac
"""

AWS_STUB = """#!/bin/sh
printf '%s\\n' "$@" > "$STUB_DIR/aws_argv"
[ "${AWS_STUB_FAIL:-0}" = 1 ] && exit 255
echo tok-verify
"""

MASTER_DOTENV = (
    "FEOH_DATABASE_URL=postgresql+asyncpg://postgres:pw@feoh.c1.us-east-1.rds.amazonaws.com"
    ":5432/feohledger\nPGSSLMODE=verify-full\nAWS_REGION=us-east-1\n"
)


class Script:
    def __init__(self, root: Path) -> None:
        self.dir = root / "deploy"
        (self.dir / "sql").mkdir(parents=True)
        for name in ("lib.sh", "db-role-cutover.sh"):
            shutil.copy2(DEPLOY / name, self.dir / name)
        for sql in (DEPLOY / "sql").glob("*.sql"):
            shutil.copy2(sql, self.dir / "sql" / sql.name)
        self.bin = root / "bin"
        self.bin.mkdir()
        (self.bin / "docker").write_text(DOCKER_STUB)
        (self.bin / "docker").chmod(0o755)
        (self.bin / "aws").write_text(AWS_STUB)
        (self.bin / "aws").chmod(0o755)
        self.stub = root

    def run(
        self, dotenv: str | None, *args: str, env: dict[str, str] | None = None
    ) -> subprocess.CompletedProcess[str]:
        if dotenv is not None:
            (self.dir / ".env").write_text(dotenv)
        return subprocess.run(
            ["bash", str(self.dir / "db-role-cutover.sh"), *args],
            env={
                "PATH": f"{self.bin}:{os.environ.get('PATH', '/usr/bin:/bin')}",
                "STUB_DIR": str(self.stub),
                **(env or {}),
            },
            capture_output=True,
            text=True,
        )

    def calls(self) -> list[tuple[list[str], str]]:
        n = int((self.stub / "n").read_text()) if (self.stub / "n").exists() else 0
        return [
            (
                (self.stub / f"argv.{i}").read_text().splitlines(),
                (self.stub / f"stdin.{i}").read_text(),
            )
            for i in range(1, n + 1)
        ]


@pytest.fixture
def script(tmp_path: Path) -> Script:
    return Script(tmp_path)


def _database(argv: list[str]) -> str:
    return argv[argv.index("-d") + 1]


def test_applies_template1_then_the_cluster_then_every_database(script: Script) -> None:
    result = script.run(MASTER_DOTENV)
    assert result.returncode == 0, result.stderr
    calls = script.calls()
    sql_runs = [(argv, stdin) for argv, stdin in calls if "-c" not in argv]
    assert [_database(argv) for argv, _ in sql_runs] == [
        "template1",
        "feohledger",
        "feoh_acme",
        "feohledger",
    ]
    for argv, _ in sql_runs:
        assert "ON_ERROR_STOP=1" in argv and "PGUSER=postgres" in argv
        assert argv[argv.index("pgtools") + 1] == "psql"
    assert "CREATE EXTENSION IF NOT EXISTS vector" in sql_runs[0][1]
    settings_line = (
        "SET feoh.owner_role = 'feoh_owner'; SET feoh.app_role = 'feoh_app'; "
        "SET feoh.iam_role = 'rds_iam';"
    )
    assert sql_runs[1][1].startswith(settings_line)
    assert "CREATE ROLE %I NOLOGIN CREATEDB NOCREATEROLE" in sql_runs[1][1]
    for _, stdin in sql_runs[2:]:
        assert stdin.startswith(settings_line)
        assert "ALTER ROUTINE" in stdin
    # Every master query resolves names from the catalog only.
    for argv, _ in calls:
        assert "PGOPTIONS" in argv
    env = (script.stub / "env").read_text()
    assert "PGOPTIONS=-c search_path=pg_catalog,pg_temp" in env
    # The password travelled through the environment only.
    assert not any("pw" == a or ":pw@" in a for argv, _ in calls for a in argv)


def test_check_changes_nothing(script: Script) -> None:
    result = script.run(MASTER_DOTENV, "--check")
    assert result.returncode == 0, result.stderr
    for argv, stdin in script.calls():
        assert "ALTER" not in stdin and "CREATE" not in stdin
        assert not any(a.lstrip().upper().startswith(("ALTER", "CREATE")) for a in argv)


def test_refuses_once_the_env_is_already_on_iam(script: Script) -> None:
    iam = (
        "FEOH_DATABASE_URL=postgresql+asyncpg://feoh_app@feoh.c1.us-east-1.rds.amazonaws.com"
        ":5432/feohledger\nFEOH_DATABASE_IAM_AUTH=true\nPGSSLMODE=verify-full\n"
        "AWS_REGION=us-east-1\n"
    )
    result = script.run(iam)
    assert result.returncode != 0
    assert "must run as the RDS master" in result.stderr
    assert script.calls() == []


def test_refuses_the_local_container(script: Script) -> None:
    result = script.run("POSTGRES_PASSWORD=x\n")
    assert result.returncode != 0
    assert "external database" in result.stderr
    assert script.calls() == []


def test_refuses_without_an_env(script: Script) -> None:
    result = script.run(None)
    assert result.returncode != 0
    assert script.calls() == []


def test_refuses_an_unknown_flag(script: Script) -> None:
    result = script.run(MASTER_DOTENV, "--force")
    assert result.returncode != 0
    assert "usage" in result.stderr


def test_verify_login_signs_in_as_the_app_role_with_a_fresh_token(script: Script) -> None:
    """Run while .env is still the master's — the point is to prove the token
    path before prod.sops.yaml is switched."""
    result = script.run(MASTER_DOTENV, "--verify-login")
    assert result.returncode == 0, result.stderr
    [(argv, stdin)] = script.calls()
    assert "PGUSER=feoh_app" in argv and "PGPASSWORD" in argv
    assert not any("tok-verify" in a for a in argv), "the token reached an argv"
    env = (script.stub / "env").read_text()
    assert "PGPASSWORD=tok-verify" in env
    sql = argv[argv.index("-c") + 1]
    assert "ALTER" not in sql and "CREATE" not in sql
    aws = (script.stub / "aws_argv").read_text().splitlines()
    assert aws[:2] == ["rds", "generate-db-auth-token"] and "feoh_app" in aws


@pytest.mark.parametrize("mode", ["require", "verify-ca", "prefer"])
def test_verify_login_refuses_tls_short_of_verify_full(script: Script, mode: str) -> None:
    result = script.run(
        MASTER_DOTENV.replace("PGSSLMODE=verify-full", f"PGSSLMODE={mode}"), "--verify-login"
    )
    assert result.returncode != 0
    assert "verify-full" in result.stderr
    assert script.calls() == []


def test_verify_login_reports_a_failed_mint(script: Script) -> None:
    result = script.run(MASTER_DOTENV, "--verify-login", env={"AWS_STUB_FAIL": "1"})
    assert result.returncode != 0
    assert "rds-db:connect" in result.stderr
    assert script.calls() == []


@pytest.mark.parametrize(
    "who", ["feoh_app|feoh_app|false", "feoh_app|feoh_owner|true", "postgres|postgres|true"]
)
def test_verify_login_fails_unless_the_app_acts_as_the_owner(script: Script, who: str) -> None:
    """Signing in is not enough: the role default must have applied, and the
    role acted as must hold nothing privileged."""
    result = script.run(MASTER_DOTENV, "--verify-login", env={"WHO": who})
    assert result.returncode != 0
    assert "expected feoh_app|feoh_owner|false" in result.stderr


def test_verify_login_needs_a_region(script: Script) -> None:
    result = script.run(MASTER_DOTENV.replace("AWS_REGION=us-east-1\n", ""), "--verify-login")
    assert result.returncode != 0
    assert "AWS_REGION" in result.stderr
