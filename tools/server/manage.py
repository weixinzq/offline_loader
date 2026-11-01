"""Single-service release selection. Never installs dependencies or migrates data."""
from contextlib import closing, contextmanager
from datetime import datetime
from html.parser import HTMLParser
import importlib.metadata
import json
import os
from pathlib import Path
import re
import socket
import sqlite3
import subprocess
import sys
import time
from urllib.parse import unquote, urlsplit
from urllib.request import urlopen


UNIT = 'aola-offline-loader-root.service'
TERMINAL = {'succeeded', 'failed', 'cancelled', 'interrupted', 'skipped'}


class Assets(HTMLParser):
    def __init__(self):
        super().__init__()
        self.paths = []

    def handle_starttag(self, tag, attributes):
        attrs = dict(attributes)
        value = attrs.get('src') if tag == 'script' else attrs.get('href') if tag == 'link' and attrs.get('rel') == 'stylesheet' else None
        if value:
            parsed = urlsplit(value)
            if parsed.scheme or parsed.netloc:
                raise ValueError('Frontend entry must use local assets')
            self.paths.append(unquote(parsed.path))


class Manager:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.python = self.root / '.venv/bin/python'
        self.database = self.root / 'data/service/service.sqlite3'

    def command(self, *arguments):
        return subprocess.run(arguments, check=True, capture_output=True, text=True, timeout=55).stdout.strip()

    def property(self, name):
        return self.command('systemctl', 'show', UNIT, '-p', name, '--value')

    def owned_unit(self):
        entry = re.search(r'\bpath=([^ ;]+)', self.property('ExecStart'))
        if (not entry or entry.group(1) != str(self.root / 'run-service.sh')
                or self.property('WorkingDirectory') != str(self.root)):
            raise ValueError('Service does not use the unified root entry; migrate it first')

    def release(self, name):
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]*', name):
            raise ValueError('Use a release directory name, not a path')
        candidate = (self.root / 'releases' / name).resolve(strict=True)
        if candidate.parent != (self.root / 'releases').resolve() or not candidate.is_dir():
            raise ValueError('Release must be a direct child of releases')
        return candidate

    def selected(self):
        current = self.root / 'current'
        if not current.is_symlink():
            raise ValueError('current must be a symlink')
        target = current.resolve(strict=True)
        if target.parent != (self.root / 'releases').resolve():
            raise ValueError('current points outside releases')
        return self.release(target.name)

    def running(self):
        pid = int(self.property('MainPID') or 0)
        return Path('/proc/{}/cwd'.format(pid)).resolve(strict=True) if pid else None

    def api(self, endpoint):
        return json.loads(self.fetch('/api/v1/' + endpoint))

    def fetch(self, path):
        with urlopen('http://127.0.0.1:8765' + path, timeout=3) as response:
            return response.read()

    def idle(self):
        if self.api('health').get('status') != 'ok':
            raise ValueError('Backend is not healthy')
        if any(row['state'] not in TERMINAL for row in self.api('tasks')):
            raise ValueError('Unfinished tasks: finish or cancel them before switching/restarting')
        if any(row['state'] != 'offline' for row in self.api('sessions')):
            raise ValueError('Online or waiting accounts: disconnect them before switching/restarting')

    def assets(self, release):
        frontend = release / 'frontend/dist'
        parser = Assets()
        parser.feed((frontend / 'index.html').read_text(encoding='utf-8'))
        if not parser.paths:
            raise ValueError('Frontend has no built assets')
        for name in parser.paths:
            file = (frontend / name.lstrip('/')).resolve(strict=True)
            if not file.is_relative_to(frontend.resolve()) or not file.is_file():
                raise ValueError('Frontend asset is missing or outside dist')
        return parser.paths

    def check_environment(self, release):
        from pip._vendor.packaging.requirements import Requirement
        from pip._vendor.packaging.version import Version

        if sys.version_info[:2] != (3, 12):
            raise ValueError('Shared environment must use Python 3.12')
        checked = 0
        for line in (release / 'requirements.lock').read_text(encoding='utf-8').splitlines():
            if not line.strip() or line.lstrip().startswith('#'):
                continue
            requirement = Requirement(line)
            pins = list(requirement.specifier)
            if requirement.url or len(pins) != 1 or pins[0].operator != '==' or '*' in pins[0].version:
                raise ValueError('requirements.lock must contain exact package versions')
            if requirement.marker and not requirement.marker.evaluate():
                continue
            checked += 1
            if Version(importlib.metadata.version(requirement.name)) != Version(pins[0].version):
                raise ValueError('Shared dependency mismatch: ' + requirement.name)
        if not checked:
            raise ValueError('requirements.lock has no applicable pinned dependencies')
        self.command(str(self.python), '-m', 'pip', 'check')

    def check_schema(self, release):
        from alembic.script import ScriptDirectory

        migrations = (release / 'src/service/migrations').resolve(strict=True)
        if not migrations.is_relative_to(release):
            raise ValueError('Migrations point outside the release')
        heads = set(ScriptDirectory(str(migrations)).get_heads())
        with closing(sqlite3.connect(self.database.as_uri() + '?mode=ro', uri=True)) as database:
            versions = {row[0] for row in database.execute('select version_num from alembic_version')}
        if not heads or heads != versions:
            raise ValueError('Database revision differs; automatic upgrade/downgrade is not allowed')

    def check_release(self, release):
        for name in ('cli_main.py', 'backend_main.py', 'requirements.lock', 'frontend/dist/index.html'):
            file = release / name
            if not file.is_file() or not file.resolve().is_relative_to(release):
                raise ValueError('Missing or outside release: ' + name)
        self.assets(release)
        self.check_environment(release)
        self.check_schema(release)

    def select(self, release):
        temporary = self.root / '.current.pending'
        if temporary.exists() or temporary.is_symlink():
            if not temporary.is_symlink():
                raise ValueError('Unexpected .current.pending file')
            temporary.unlink()
        temporary.symlink_to(Path('releases') / release.name)
        os.replace(temporary, self.root / 'current')

    def port_free(self):
        with socket.socket() as probe:
            probe.settimeout(.2)
            return probe.connect_ex(('127.0.0.1', 8765)) != 0

    def stop(self):
        self.command('systemctl', 'stop', UNIT)
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if not int(self.property('MainPID') or 0) and self.port_free():
                return
            time.sleep(.2)
        raise ValueError('Service process or port 8765 did not stop; selection unchanged')

    def wait_healthy(self, release):
        deadline = time.monotonic() + 30
        assets = self.assets(release)
        while time.monotonic() < deadline:
            try:
                if (self.property('ActiveState') == 'active' and self.running() == release
                        and self.api('health').get('status') == 'ok'
                        and self.fetch('/') == (release / 'frontend/dist/index.html').read_bytes()
                        and all(self.fetch('/' + name.lstrip('/')) == (release / 'frontend/dist' / name.lstrip('/')).read_bytes() for name in assets)):
                    return
            except (OSError, ValueError, subprocess.SubprocessError):
                pass
            time.sleep(.5)
        raise ValueError('Release did not pass process, health and frontend checks within 30 seconds')

    def start(self):
        release = self.selected()
        self.check_release(release)
        if self.property('ActiveState') == 'active':
            self.wait_healthy(release)
            print('Service is already active: ' + release.name)
            return
        if int(self.property('MainPID') or 0) or not self.port_free():
            raise ValueError('Existing process or listener; refusing to start a second backend')
        self.command('systemctl', 'start', UNIT)
        self.wait_healthy(release)
        print('Started: ' + release.name)

    def audit(self, message):
        folder = self.root / 'logs'
        folder.mkdir(exist_ok=True, mode=0o700)
        with (folder / 'deployments.log').open('a', encoding='utf-8') as file:
            file.write(datetime.now().astimezone().isoformat() + ' ' + message + '\n')

    def switch(self, name):
        old, target = self.selected(), self.release(name)
        if self.property('ActiveState') != 'active' or self.running() != old:
            raise ValueError('Switch requires a healthy service running the selected release')
        self.wait_healthy(old)
        self.check_release(old)
        self.check_release(target)
        self.idle()
        if old == target:
            print('Already selected: ' + old.name)
            return
        self.audit('switch {} -> {} requested'.format(old.name, target.name))
        self.idle()
        self.stop()
        try:
            self.select(target)
            self.command('systemctl', 'start', UNIT)
            self.wait_healthy(target)
        except Exception as failure:
            self.audit('switch failed: ' + str(failure))
            try:
                self.stop()
                self.select(old)
                self.command('systemctl', 'start', UNIT)
                self.wait_healthy(old)
                self.audit('rollback succeeded: ' + old.name)
            except Exception as rollback:
                self.audit('rollback failed: ' + str(rollback))
                raise RuntimeError('Switch failed: {}; rollback failed: {}. Inspect logs/service.log'.format(failure, rollback)) from rollback
            raise RuntimeError('Switch failed: {}; restored {}'.format(failure, old.name)) from failure
        self.audit('switch succeeded: ' + target.name)
        print('Selected and running: ' + target.name)

    def restart(self):
        release = self.selected()
        self.check_release(release)
        if self.property('ActiveState') == 'active':
            self.wait_healthy(release)
            self.idle()
            self.idle()
        self.stop()
        self.start()

    def status(self):
        selected, running = self.selected(), self.running()
        print('Service: {} ({})'.format(self.property('ActiveState'), UNIT))
        print('Selected: ' + str(selected))
        print('Running: ' + str(running or 'stopped'))
        if running and running != selected:
            print('WARNING: selected and running releases differ; do not change current manually')

    def releases(self):
        selected, running = self.selected(), self.running()
        for folder in sorted((self.root / 'releases').iterdir()):
            try:
                release = self.release(folder.name)
            except (OSError, ValueError):
                continue
            marks = [label for label, target in [('current', selected), ('running', running)] if release == target]
            print(folder.name + (' [' + ', '.join(marks) + ']' if marks else ''))


@contextmanager
def management_lock(root):
    import fcntl

    with (Path(root) / '.manage.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError('Another management operation is in progress') from None
        yield


def main():
    os.umask(0o077)
    if os.geteuid() != 0:
        sys.exit('Run as the deployment owner (root)')
    manager = Manager(Path(__file__).resolve().parents[2])
    args = sys.argv[1:] or ['status']
    try:
        if args == ['check-current']:
            manager.check_release(manager.selected())
            return
        if args[0] not in {'start', 'stop', 'restart', 'status', 'logs', 'releases', 'switch'} or len(args) != (2 if args[0] == 'switch' else 1):
            raise ValueError('Usage: manage.sh {start|stop|restart|status|logs|releases|switch VERSION}')
        manager.owned_unit()
        if args[0] in {'start', 'stop', 'restart', 'switch'}:
            with management_lock(manager.root):
                if args[0] == 'switch':
                    manager.switch(args[1])
                else:
                    getattr(manager, args[0])()
        elif args[0] == 'logs':
            print(manager.command('tail', '-n', '100', str(manager.root / 'logs/service.log')))
        else:
            getattr(manager, args[0])()
    except Exception as error:
        print('Management refused/failed: ' + str(error), file=sys.stderr)
        sys.exit(1)


if __name__ == '__main__':
    main()
