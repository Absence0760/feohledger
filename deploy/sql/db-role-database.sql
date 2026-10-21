-- The per-database half of deploy/db-role-cutover.sh (docs/decisions.md §305).
-- Run as the RDS MASTER user, connected to ONE database (the control plane, or a
-- feoh_* tenant), after db-role-cluster.sql. Idempotent: it only touches objects
-- the master still owns, so a second run finds nothing to do.
--
-- The caller sets the role name first:  SET feoh.owner_role = 'feoh_owner';
-- (feoh_owner is the NOLOGIN role that owns everything; the app signs in as
-- feoh_app, which acts as feoh_owner — db-role-cluster.sql says why.)
--
-- Explicit ALTERs, not REASSIGN OWNED BY <master>. REASSIGN is broader than
-- this needs and broader than is safe here: it also reassigns SHARED objects the
-- master owns (the `postgres` maintenance database, any database outside the
-- feoh_* set), and extension ownership, in one statement nobody can read the
-- scope of. These loops name what moves:
--
--   schemas, tables / partitions / views / materialized views / foreign tables,
--   free-standing sequences (one owned by a column moves with its table, and
--   ALTER SEQUENCE on it is an error), types (enum / domain / range /
--   standalone composite — array and multirange types follow their base, and a
--   table's row type follows the table), and routines (functions, procedures,
--   aggregates — the audit_log immutability trigger function among them).
--
-- What stays with the master, on purpose: EXTENSIONS and their member objects.
-- pgvector is not a trusted extension on RDS, so only the master
-- (rds_superuser) may create or update it; the app only uses its type and
-- operators, which needs no ownership. Ownership of an extension cannot be
-- transferred anyway (there is no ALTER EXTENSION ... OWNER).
--
-- It ends by listing anything ELSE the master still owns in this database
-- (pg_shdepend's owner records cover every object class), and fails if there
-- is any — a kind of object this file does not know about is a reason to stop
-- and look, not to finish green.

-- Catalog names only — see db-role-cluster.sql. The app owns `public` here.
SET search_path = pg_catalog, pg_temp;

DO $cutover$
DECLARE
    owner text := current_setting('feoh.owner_role');
    me  oid  := (SELECT oid FROM pg_roles WHERE rolname = current_user);
    r   record;
    leftover text;
BEGIN
    IF owner = '' THEN
        RAISE EXCEPTION 'feoh.owner_role is not set';
    END IF;
    IF NOT pg_has_role(current_user, owner, 'SET') OR current_user = owner THEN
        RAISE EXCEPTION 'connected as %, which cannot SET to % — run db-role-cluster.sql first, as the RDS master user', current_user, owner;
    END IF;

    -- Schemas (public is owned by pg_database_owner on PG15+ and follows the
    -- database; this catches any schema the master created or a restore set).
    FOR r IN
        SELECT n.nspname
        FROM pg_namespace n
        WHERE n.nspowner = me
          AND n.nspname NOT IN ('pg_catalog', 'information_schema')
          AND n.nspname NOT LIKE 'pg\_%'
          AND NOT EXISTS (SELECT 1 FROM pg_depend d
                          WHERE d.classid = 'pg_namespace'::regclass
                            AND d.objid = n.oid AND d.deptype = 'e')
    LOOP
        EXECUTE format('ALTER SCHEMA %I OWNER TO %I', r.nspname, owner);
    END LOOP;

    -- Relations. Indexes and TOAST tables follow their table.
    FOR r IN
        SELECT c.oid::regclass AS rel,
               CASE c.relkind
                   WHEN 'r' THEN 'TABLE' WHEN 'p' THEN 'TABLE'
                   WHEN 'v' THEN 'VIEW' WHEN 'm' THEN 'MATERIALIZED VIEW'
                   WHEN 'f' THEN 'FOREIGN TABLE' WHEN 'S' THEN 'SEQUENCE'
               END AS kind
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE c.relowner = me
          AND c.relkind IN ('r', 'p', 'v', 'm', 'f', 'S')
          AND n.nspname NOT IN ('pg_catalog', 'information_schema')
          AND n.nspname NOT LIKE 'pg\_%'
          AND NOT EXISTS (SELECT 1 FROM pg_depend d
                          WHERE d.classid = 'pg_class'::regclass
                            AND d.objid = c.oid AND d.deptype = 'e')
          -- a serial / identity sequence moves with its table
          AND NOT (c.relkind = 'S' AND EXISTS (
                SELECT 1 FROM pg_depend d
                WHERE d.classid = 'pg_class'::regclass AND d.objid = c.oid
                  AND d.refclassid = 'pg_class'::regclass
                  AND d.deptype IN ('a', 'i')))
        ORDER BY c.relkind <> 'S' DESC  -- tables first, so owned sequences have moved
    LOOP
        -- Re-checked: an owned sequence moved with its table earlier in the loop.
        IF (SELECT relowner FROM pg_class WHERE oid = r.rel) = me THEN
            EXECUTE format('ALTER %s %s OWNER TO %I', r.kind, r.rel, owner);
        END IF;
    END LOOP;

    -- Types the master owns that are not auto-generated companions.
    FOR r IN
        SELECT t.oid::regtype AS typ,
               CASE t.typtype WHEN 'd' THEN 'DOMAIN' ELSE 'TYPE' END AS kind
        FROM pg_type t
        JOIN pg_namespace n ON n.oid = t.typnamespace
        LEFT JOIN pg_class c ON c.oid = t.typrelid
        WHERE t.typowner = me
          AND n.nspname NOT IN ('pg_catalog', 'information_schema')
          AND n.nspname NOT LIKE 'pg\_%'
          AND (t.typtype IN ('e', 'd', 'r') OR (t.typtype = 'c' AND c.relkind = 'c'))
          AND NOT EXISTS (SELECT 1 FROM pg_depend d
                          WHERE d.classid = 'pg_type'::regclass
                            AND d.objid = t.oid AND d.deptype = 'e')
    LOOP
        EXECUTE format('ALTER %s %s OWNER TO %I', r.kind, r.typ, owner);
    END LOOP;

    -- Functions, procedures, aggregates.
    FOR r IN
        SELECT p.oid::regprocedure AS fn
        FROM pg_proc p
        JOIN pg_namespace n ON n.oid = p.pronamespace
        WHERE p.proowner = me
          AND n.nspname NOT IN ('pg_catalog', 'information_schema')
          AND n.nspname NOT LIKE 'pg\_%'
          AND NOT EXISTS (SELECT 1 FROM pg_depend d
                          WHERE d.classid = 'pg_proc'::regclass
                            AND d.objid = p.oid AND d.deptype = 'e')
    LOOP
        EXECUTE format('ALTER ROUTINE %s OWNER TO %I', r.fn, owner);
    END LOOP;

    -- Anything left: every owned object has an owner record in pg_shdepend.
    -- Extensions and their members stay with the master by design.
    SELECT string_agg(format('%s %s', s.classid::regclass, s.objid), ', ')
    INTO leftover
    FROM pg_shdepend s
    WHERE s.dbid = (SELECT oid FROM pg_database WHERE datname = current_database())
      AND s.refclassid = 'pg_authid'::regclass
      AND s.refobjid = me
      AND s.deptype = 'o'
      AND s.classid <> 'pg_extension'::regclass
      AND NOT EXISTS (SELECT 1 FROM pg_depend d
                      WHERE d.classid = s.classid AND d.objid = s.objid AND d.deptype = 'e');
    IF leftover IS NOT NULL THEN
        RAISE EXCEPTION 'database % still has objects owned by % that this cutover does not know how to move: % — move them by hand, then re-run',
            current_database(), current_user, leftover;
    END IF;
    RAISE NOTICE 'database %: everything outside extensions is owned by %', current_database(), owner;
END
$cutover$;
