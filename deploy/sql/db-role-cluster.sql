-- The cluster half of deploy/db-role-cutover.sh (docs/decisions.md §304).
-- Run as the RDS MASTER user, connected to the control-plane database, once per
-- cutover and again after anything that recreates objects as the master
-- (a rollback window, a restore). Every statement is idempotent; a second run
-- changes nothing.
--
-- The caller sets three custom settings first, so the role names are spelled
-- in one place and a test can use throwaway ones:
--   SET feoh.owner_role = 'feoh_owner'; -- owns everything; NOLOGIN, no rds_iam
--   SET feoh.app_role   = 'feoh_app';   -- signs in (IAM token only)
--   SET feoh.iam_role   = 'rds_iam';    -- RDS's IAM-auth role
--
-- Why two roles. RDS refuses a password sign-in for any member of rds_iam, and
-- counts inherited membership. The master must keep its password (it is the
-- rollback and the break-glass), yet it must also hold the owner's privileges
-- (so the rollback works) and be able to SET to the owner (a non-superuser may
-- only transfer ownership to a role it can SET to). So ownership sits on a role
-- with no rds_iam — feoh_owner — which the master joins, and the role that
-- signs in, feoh_app, joins it too and has `role` defaulted to it, so every
-- object the app creates is owned by feoh_owner as well. The master is never
-- made a member of feoh_app (PG16's automatic ADMIN grant to a role's creator
-- is the one link, and it is exactly the link AWS's own `CREATE USER …;
-- GRANT rds_iam …` procedure leaves).
--
-- feoh_owner: NOLOGIN, CREATEDB (self-service signup and partner provisioning
--   run CREATE DATABASE inside the API), member of nothing.
-- feoh_app: LOGIN, no password, member of rds_iam and feoh_owner and nothing
--   else; no CREATEROLE, not rds_superuser.
-- Then the control plane and every feoh_* database the master owns are handed
-- to feoh_owner. Objects inside them: db-role-database.sql.

-- Resolve every name below from the catalog, never from a schema the app owns
-- (it owns `public` in every one of these databases now): an app-planted
-- function or operator must not run with the master's privileges.
SET search_path = pg_catalog, pg_temp;

DO $cutover$
DECLARE
    owner text := current_setting('feoh.owner_role');
    app   text := current_setting('feoh.app_role');
    iam   text := current_setting('feoh.iam_role');
    db    text;
    extra text;
BEGIN
    IF owner = '' OR app = '' OR iam = '' THEN
        RAISE EXCEPTION 'feoh.owner_role / feoh.app_role / feoh.iam_role are not set';
    END IF;
    IF current_user IN (owner, app) THEN
        RAISE EXCEPTION 'connected as % — run this as the RDS master user (the URL before the cutover)', current_user;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = iam) THEN
        RAISE EXCEPTION 'role % does not exist — is this RDS with IAM database authentication enabled?', iam;
    END IF;

    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = owner) THEN
        EXECUTE format('CREATE ROLE %I NOLOGIN CREATEDB NOCREATEROLE', owner);
        RAISE NOTICE 'created role %', owner;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = app) THEN
        EXECUTE format('CREATE ROLE %I LOGIN NOCREATEDB NOCREATEROLE', app);
        RAISE NOTICE 'created role %', app;
    END IF;
    -- Re-asserted every run, so a hand edit is put back. (REPLICATION and
    -- BYPASSRLS are off by default and only a superuser can change them either
    -- way, so they are checked below, not set.)
    EXECUTE format('ALTER ROLE %I NOLOGIN CREATEDB NOCREATEROLE', owner);
    EXECUTE format('ALTER ROLE %I LOGIN NOCREATEDB NOCREATEROLE PASSWORD NULL', app);
    EXECUTE format('GRANT %I TO %I', iam, app);
    EXECUTE format('GRANT %I TO %I WITH INHERIT TRUE, SET TRUE', owner, app);
    EXECUTE format('GRANT %I TO %I WITH INHERIT TRUE, SET TRUE', owner, current_user);
    -- Every feoh_app session starts as feoh_owner, so what the app creates is
    -- owned by feoh_owner too. (After the master's grant above: Postgres checks
    -- the SET against the role running this, and warns otherwise.)
    EXECUTE format('ALTER ROLE %I SET role = %L', app, owner);

    -- Least privilege, checked rather than assumed: whoever else has been at
    -- these roles, they hold exactly what is listed above and nothing more.
    IF EXISTS (SELECT 1 FROM pg_roles
               WHERE rolname IN (owner, app)
                 AND (rolsuper OR rolreplication OR rolbypassrls OR rolcreaterole)) THEN
        RAISE EXCEPTION 'role % or % has SUPERUSER, REPLICATION, BYPASSRLS or CREATEROLE — remove it by hand before continuing', owner, app;
    END IF;
    SELECT string_agg(format('%s in %s', m.member::regrole, m.roleid::regrole), ', ')
    INTO extra
    FROM pg_auth_members m
    WHERE (m.member = to_regrole(app) AND m.roleid NOT IN (to_regrole(iam), to_regrole(owner)))
       OR (m.member = to_regrole(owner));
    IF extra IS NOT NULL THEN
        RAISE EXCEPTION 'unexpected role membership: % — revoke it by hand before continuing', extra;
    END IF;
    -- The master must stay out of the rds_iam chain, or RDS would refuse its
    -- password and take the rollback and break-glass paths with it.
    IF EXISTS (SELECT 1 FROM pg_auth_members
               WHERE member = to_regrole(current_user) AND roleid = to_regrole(app)
                 AND (inherit_option OR set_option)) THEN
        RAISE EXCEPTION '% is a member of % (INHERIT or SET) — revoke that: RDS would treat the master as IAM-only', current_user, app;
    END IF;

    FOR db IN
        SELECT datname FROM pg_database
        WHERE (datname = current_database() OR datname LIKE 'feoh\_%')
          AND datdba = to_regrole(current_user)
        ORDER BY datname
    LOOP
        EXECUTE format('ALTER DATABASE %I OWNER TO %I', db, owner);
        RAISE NOTICE 'database % now owned by %', db, owner;
    END LOOP;
END
$cutover$;
