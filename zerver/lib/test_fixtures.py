import json
import os
import re
import hashlib
import subprocess
import sys
from typing import Any, List, Set
from importlib import import_module
from io import StringIO
import glob
import time
import shutil

from django.db import connections, DEFAULT_DB_ALIAS, ProgrammingError, \
    connection
from django.db.utils import OperationalError
from django.apps import apps
from django.conf import settings
from django.core.management import call_command
from django.utils.module_loading import module_has_submodule

sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(__file__))))
from scripts.lib.zulip_tools import (
    get_dev_uuid_var_path, run, TEMPLATE_DATABASE_DIR,
    is_digest_obsolete, write_new_digest,
)

UUID_VAR_DIR = get_dev_uuid_var_path()
FILENAME_SPLITTER = re.compile(r'[\W\-_]')

def migration_paths() -> List[str]:
    return [
        *glob.glob('*/migrations/*.py'),
        'requirements/dev.txt',
    ]

class Database:
    def __init__(self, platform: str, database_name: str, settings: str):
        self.database_name = database_name
        self.settings = settings
        self.migration_status_file = 'migration_status_' + platform
        self.migration_status_path = os.path.join(
            UUID_VAR_DIR,
            self.migration_status_file
        )
        self.migration_digest_file = "migrations_hash_" + database_name

    def run_db_migrations(self) -> None:
        # We shell out to `manage.py` and pass `DJANGO_SETTINGS_MODULE` on
        # the command line rather than just calling the migration
        # functions, because Django doesn't support changing settings like
        # what the database is as runtime.
        # Also we export ZULIP_DB_NAME which is ignored by dev platform but
        # recognised by test platform and used to migrate correct db.
        env_prelude = [
            'env',
            'DJANGO_SETTINGS_MODULE=' + self.settings,
            'ZULIP_DB_NAME=' + self.database_name,
        ]

        run(env_prelude + [
            './manage.py', 'migrate', '--no-input',
        ])

        run(env_prelude + [
            './manage.py', 'get_migration_status', '--output='+self.migration_status_file,
        ])

    def what_to_do_with_migrations(self) -> str:
        status_fn = self.migration_status_path
        settings = self.settings

        if not os.path.exists(status_fn):
            return 'scrap'

        with open(status_fn) as f:
            previous_migration_status = f.read()

        current_migration_status = get_migration_status(settings=settings)
        all_curr_migrations = extract_migrations_as_list(current_migration_status)
        all_prev_migrations = extract_migrations_as_list(previous_migration_status)

        if len(all_curr_migrations) < len(all_prev_migrations):
            return 'scrap'

        for migration in all_prev_migrations:
            if migration not in all_curr_migrations:
                return 'scrap'

        if len(all_curr_migrations) == len(all_prev_migrations):
            return 'migrations_are_latest'

        return 'migrate'

    def database_exists(self) -> bool:
        try:
            connection = connections[DEFAULT_DB_ALIAS]

            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT 1 from pg_database WHERE datname='{}';".format(self.database_name)
                )
                return_value = bool(cursor.fetchone())
            connections.close_all()
            return return_value
        except OperationalError:
            return False

    def template_status(self) -> str:
        # This function returns a status string specifying the type of
        # state the template db is in and thus the kind of action required.
        database_name = self.database_name

        check_files = [
            'zilencer/management/commands/populate_db.py',
            'zerver/lib/bulk_create.py',
            'zerver/lib/generate_test_data.py',
            'zerver/lib/server_initialization.py',
            'tools/setup/postgres-init-test-db',
            'tools/setup/postgres-init-dev-db',
            'zerver/migrations/0258_enable_online_push_notifications_default.py',
        ]
        check_settings = [
            'REALM_INTERNAL_BOTS',
        ]

        # Construct a directory to store hashes named after the target database.
        status_dir = os.path.join(UUID_VAR_DIR, database_name + '_db_status')
        if not os.path.exists(status_dir):
            os.mkdir(status_dir)

        if not self.database_exists():
            # TODO: It's possible that `database_exists` will
            #       return `False` even though the database
            #       exists, but we just have the wrong password,
            #       probably due to changing the secrets file.
            #
            #       The only problem this causes is that we waste
            #       some time rebuilding the whole database, but
            #       it's better to err on that side, generally.
            return 'needs_rebuild'

        # To ensure Python evaluates all the hash tests (and thus creates the
        # hash files about the current state), we evaluate them in a
        # list and then process the result
        files_hash_status = all([check_file_hash(fn, status_dir) for fn in check_files])
        settings_hash_status = all([check_setting_hash(setting_name, status_dir)
                                    for setting_name in check_settings])
        hash_status = files_hash_status and settings_hash_status
        if not hash_status:
            return 'needs_rebuild'

        # Here we hash and compare our migration files before doing
        # the work of seeing what to do with them; if there are no
        # changes, we can safely assume we don't need to run
        # migrations without spending a few 100ms parsing all the
        # Python migration code.
        if not self.is_migration_digest_obsolete():
            return 'current'

        '''
        NOTE:
            We immediately update the digest, assuming our
            callers will do what it takes to run the migrations.

            Ideally our callers would just do it themselves
            AFTER the migrations actually succeeded, but the
            caller codepaths are kind of complicated here.
        '''
        self.write_new_migration_digest()

        migration_op = self.what_to_do_with_migrations()
        if migration_op == 'scrap':
            return 'needs_rebuild'

        if migration_op == 'migrate':
            return 'run_migrations'

        return 'current'

    def is_migration_digest_obsolete(self) -> bool:
        return is_digest_obsolete(
            self.migration_digest_file,
            migration_paths(),
        )

    def write_new_migration_digest(self) -> None:
        write_new_digest(
            self.migration_digest_file,
            migration_paths(),
        )

DEV_DATABASE = Database(
    platform='dev',
    database_name='zulip',
    settings='zproject.settings',
)

TEST_DATABASE = Database(
    platform='test',
    database_name='zulip_test_template',
    settings='zproject.test_settings',
)

def update_test_databases_if_required(use_force: bool=False,
                                      rebuild_test_database: bool=False) -> None:
    """Checks whether the zulip_test_template database template, is
    consistent with our database migrations; if not, it updates it
    in the fastest way possible:

    * If all we need to do is add some migrations, just runs those
      migrations on the template database.
    * Otherwise, we rebuild the test template database from scratch.

    The default behavior is sufficient for the `test-backend` use
    case, where the test runner code will clone directly from the
    template database.

    The `rebuild_test_database` option (used by our Casper tests) asks
    us to drop and re-cloning the zulip_test database from the
    template so those test suites can run with a fresh copy.

    If use_force is specified, it will always do a full rebuild.
    """
    generate_fixtures_command = ['tools/setup/generate-fixtures']
    test_template_db_status = TEST_DATABASE.template_status()

    if use_force or test_template_db_status == 'needs_rebuild':
        generate_fixtures_command.append('--force')
        subprocess.check_call(generate_fixtures_command)
        return

    if test_template_db_status == 'run_migrations':
        TEST_DATABASE.run_db_migrations()
        subprocess.check_call(generate_fixtures_command)
        return

    if rebuild_test_database:
        subprocess.check_call(generate_fixtures_command)

def get_migration_status(**options: Any) -> str:
    verbosity = options.get('verbosity', 1)

    for app_config in apps.get_app_configs():
        if module_has_submodule(app_config.module, "management"):
            import_module('.management', app_config.name)

    app_label = options['app_label'] if options.get('app_label') else None
    db = options.get('database', DEFAULT_DB_ALIAS)
    out = StringIO()
    command_args = ['--list', ]
    if app_label:
        command_args.append(app_label)

    call_command(
        'showmigrations',
        *command_args,
        database=db,
        no_color=options.get('no_color', False),
        settings=options.get('settings', os.environ['DJANGO_SETTINGS_MODULE']),
        stdout=out,
        traceback=options.get('traceback', True),
        verbosity=verbosity,
    )
    connections.close_all()
    out.seek(0)
    output = out.read()
    return re.sub(r'\x1b\[(1|0)m', '', output)

def extract_migrations_as_list(migration_status: str) -> List[str]:
    MIGRATIONS_RE = re.compile(r'\[[X| ]\] (\d+_.+)\n')
    return MIGRATIONS_RE.findall(migration_status)

def _get_hash_file_path(source_file_path: str, status_dir: str) -> str:
    basename = os.path.basename(source_file_path)
    filename = '_'.join(FILENAME_SPLITTER.split(basename)).lower()
    return os.path.join(status_dir, filename)

def _check_hash(source_hash_file: str, target_content: str) -> bool:
    """
    This function has a side effect of creating a new hash file or
    updating the old hash file.
    """
    target_hash_content = hashlib.sha1(target_content.encode('utf8')).hexdigest()

    if not os.path.exists(source_hash_file):
        source_hash_content = None
    else:
        with open(source_hash_file) as f:
            source_hash_content = f.read().strip()

    with open(source_hash_file, 'w') as f:
        f.write(target_hash_content)

    return source_hash_content == target_hash_content

def check_file_hash(target_file_path: str, status_dir: str) -> bool:
    source_hash_file = _get_hash_file_path(target_file_path, status_dir)

    with open(target_file_path) as f:
        target_content = f.read()

    return _check_hash(source_hash_file, target_content)

def check_setting_hash(setting_name: str, status_dir: str) -> bool:
    hash_filename = '_'.join(['settings', setting_name])
    source_hash_file = os.path.join(status_dir, hash_filename)

    target_content = json.dumps(getattr(settings, setting_name), sort_keys=True)

    return _check_hash(source_hash_file, target_content)

def destroy_leaked_test_databases(expiry_time: int = 60 * 60) -> int:
    """The logic in zerver/lib/test_runner.py tries to delete all the
    temporary test databases generated by test-backend threads, but it
    cannot guarantee it handles all race conditions correctly.  This
    is a catch-all function designed to delete any that might have
    been leaked due to crashes (etc.).  The high-level algorithm is to:

    * Delete every database with a name like zulip_test_template_*
    * Unless it is registered in a file under TEMPLATE_DATABASE_DIR as
      part of a currently running test-backend invocation
    * And that file is less expiry_time old.

    This should ensure we ~never break a running test-backend process,
    while also ensuring we will eventually delete all leaked databases.
    """
    files = glob.glob(os.path.join(UUID_VAR_DIR, TEMPLATE_DATABASE_DIR, "*"))
    test_databases = set()  # type: Set[str]
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT datname FROM pg_database;")
            rows = cursor.fetchall()
            for row in rows:
                if 'zulip_test_template_' in row[0]:
                    test_databases.add(row[0])
    except ProgrammingError:
        pass

    databases_in_use = set()  # type: Set[str]
    for file in files:
        if round(time.time()) - os.path.getmtime(file) < expiry_time:
            with open(file) as f:
                for line in f:
                    databases_in_use.add('zulip_test_template_{}'.format(line).rstrip())
        else:
            # Any test-backend run older than expiry_time can be
            # cleaned up, both the database and the file listing its
            # databases.
            os.remove(file)

    databases_to_drop = test_databases - databases_in_use

    if not databases_to_drop:
        return 0

    commands = "\n".join("DROP DATABASE IF EXISTS %s;" % (db,) for db in databases_to_drop)
    p = subprocess.Popen(["psql", "-q", "-v", "ON_ERROR_STOP=1", "-h", "localhost",
                          "postgres", "zulip_test"],
                         stdin=subprocess.PIPE)
    p.communicate(input=commands.encode())
    if p.returncode != 0:
        raise RuntimeError("Error cleaning up test databases!")
    return len(databases_to_drop)

def remove_test_run_directories(expiry_time: int = 60 * 60) -> int:
    removed = 0
    directories = glob.glob(os.path.join(UUID_VAR_DIR, "test-backend", "run_*"))
    for test_run in directories:
        if round(time.time()) - os.path.getmtime(test_run) > expiry_time:
            try:
                shutil.rmtree(test_run)
                removed += 1
            except FileNotFoundError:
                pass
    return removed
