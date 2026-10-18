"""Provision restricted roles in a dedicated, explicitly marked simulation DB."""
import secrets
import psycopg
from psycopg import sql
from sqlalchemy.engine import make_url
from .common import digest


def connection(url, **overrides):
    u = make_url(url)
    if u.get_backend_name() != 'postgresql':
        raise ValueError('A PostgreSQL administrator URL is required')
    params = dict(dbname=u.database or 'postgres', user=u.username, password=u.password,
                  host=u.query.get('host', u.host), port=u.query.get('port', u.port or 5432), connect_timeout=5)
    return psycopg.connect(**(params | overrides), autocommit=True)


class Postgres:
    def __init__(self, admin_url, control_id):
        self.url = admin_url
        self.control_id = control_id
        self.database = 'simdb_'+control_id[:24]
        self.marker = 'scm-simulation:'+control_id

    def initialize(self):
        # Trust authentication allows a child to impersonate the administrator.
        # Refuse it instead of claiming isolation based only on role names.
        try:
            unexpected = connection(self.url, password=secrets.token_urlsafe(32))
        except psycopg.OperationalError:
            pass
        else:
            unexpected.close()
            raise ValueError('Simulation PostgreSQL requires password authentication; trust/peer is not supported')
        with connection(self.url) as c:
            exists = c.execute('SELECT shobj_description(oid,\'pg_database\') FROM pg_database WHERE datname=%s', (self.database,)).fetchone()
            if exists:
                if exists[0] != self.marker:
                    raise ValueError('PostgreSQL control database marker mismatch')
            else:
                c.execute(sql.SQL('CREATE DATABASE {}').format(sql.Identifier(self.database)))
                c.execute(sql.SQL('COMMENT ON DATABASE {} IS {}').format(sql.Identifier(self.database), sql.Literal(self.marker)))
            c.execute(sql.SQL('REVOKE ALL ON DATABASE {} FROM PUBLIC').format(sql.Identifier(self.database)))
        with self.admin() as c:
            c.execute('REVOKE ALL ON SCHEMA public FROM PUBLIC')

    def admin(self):
        with connection(self.url) as c:
            found = c.execute('SELECT shobj_description(oid,\'pg_database\') FROM pg_database WHERE datname=%s', (self.database,)).fetchone()
            if not found or found[0] != self.marker:
                raise ValueError('Unknown or non-simulation database')
        return connection(self.url, dbname=self.database)

    def names(self, run_id):
        identity = digest([self.control_id, run_id])[:24]
        return 'sim_'+identity, 'simr_'+identity

    def provision(self, run_id):
        schema, role = self.names(run_id)
        password = secrets.token_urlsafe(32)
        marker = self.marker+':'+run_id
        with self.admin() as c, c.transaction():
            c.execute(sql.SQL('CREATE ROLE {} LOGIN PASSWORD {} NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS').format(sql.Identifier(role), sql.Literal(password)))
            c.execute(sql.SQL('GRANT CONNECT ON DATABASE {} TO {}').format(sql.Identifier(self.database), sql.Identifier(role)))
            c.execute(sql.SQL('CREATE SCHEMA {} AUTHORIZATION {}').format(sql.Identifier(schema), sql.Identifier(role)))
            c.execute(sql.SQL('REVOKE ALL ON SCHEMA {} FROM PUBLIC').format(sql.Identifier(schema)))
            c.execute(sql.SQL('COMMENT ON SCHEMA {} IS {}').format(sql.Identifier(schema), sql.Literal(marker)))
            c.execute(sql.SQL('COMMENT ON ROLE {} IS {}').format(sql.Identifier(role), sql.Literal(marker)))
            c.execute(sql.SQL('ALTER ROLE {} IN DATABASE {} SET search_path TO {}').format(sql.Identifier(role), sql.Identifier(self.database), sql.Identifier(schema)))
        url = make_url(self.url).set(username=role, password=password, database=self.database)
        return dict(database_url=url.render_as_string(hide_password=False), schema=schema, role=role)

    def verify(self, run_id, url):
        schema, role = self.names(run_id)
        with connection(url) as c:
            found = c.execute('SELECT current_user,current_schema(),current_database()').fetchone()
            if found != (role, schema, self.database):
                raise ValueError('PostgreSQL role/schema/database does not match the registered run')
            flags = c.execute('SELECT rolsuper,rolcreatedb,rolcreaterole,rolreplication,rolbypassrls FROM pg_roles WHERE rolname=current_user').fetchone()
            if any(flags) or c.execute('SELECT 1 FROM pg_auth_members WHERE member=(SELECT oid FROM pg_roles WHERE rolname=current_user)').fetchone():
                raise ValueError('Runtime role has privileges beyond its simulation schema')
            if c.execute("SELECT has_schema_privilege(current_user,'public','USAGE') OR has_schema_privilege(current_user,'public','CREATE')").fetchone()[0]:
                raise ValueError('Runtime role can access shared public schema')
        return True

    def destroy(self, run_id):
        schema, role = self.names(run_id)
        marker = self.marker+':'+run_id
        with self.admin() as c, c.transaction():
            found = c.execute("SELECT obj_description(oid,'pg_namespace'),pg_get_userbyid(nspowner) FROM pg_namespace WHERE nspname=%s", (schema,)).fetchone()
            if found != (marker, role):
                raise ValueError('Refusing cleanup of unknown PostgreSQL schema')
            role_marker = c.execute("SELECT shobj_description(oid,'pg_authid') FROM pg_roles WHERE rolname=%s", (role,)).fetchone()
            if role_marker != (marker,):
                raise ValueError('Runtime role marker mismatch')
            if c.execute('SELECT 1 FROM pg_stat_activity WHERE usename=%s', (role,)).fetchone():
                raise ValueError('Runtime has active database connections; stop it first')
            c.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(schema)))
            c.execute(sql.SQL('REVOKE ALL ON DATABASE {} FROM {}').format(sql.Identifier(self.database), sql.Identifier(role)))
            c.execute(sql.SQL('DROP ROLE {}').format(sql.Identifier(role)))
