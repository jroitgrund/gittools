import os.path, re, threading, watchdog.events, watchdog.observers
from collections import defaultdict, namedtuple
from datetime import datetime, timedelta
from fnmatch import fnmatch
from lazy import lazy
from utils import first, LazyList, Sh, ShError

__all__ = [ 'revparse', 'getUpstreamBranch', 'git_dir', 'Branch', 'GitLockWatcher',
            'LazyGitFunction' ]

# wait(None) blocks signals like KeyboardInterrupt
# Use wait(99999) instead
INDEFINITELY = 99999

def fractionalSeconds(delta):
  return delta.total_seconds() + delta.microseconds / 10000000.0

@lazy
def git_dir():
  return revparse("--git-dir")

class GitLockWatcher(watchdog.events.FileSystemEventHandler):
  def __init__(self, latency = timedelta(seconds = 0.2)):
    self.lockfile = os.path.join(git_dir(), 'index.lock')
    self.latency = latency
    self.observer = None
    self._unlock_timestamp = datetime.utcfromtimestamp(0)
    self._unlocked = threading.Condition()

  @property
  def is_locked(self):
    assert self.observer is not None
    return self._unlock_timestamp is not None and self._unlock_timestamp <= datetime.utcnow()

  def await_unlocked(self):
    while True:
      timeout = (fractionalSeconds(self._unlock_timestamp - datetime.utcnow())
                 if self._unlock_timestamp else INDEFINITELY)
      if timeout is not None and timeout <= 0:
        return
      with self._unlocked:
        self._unlocked.wait(timeout)

  def _lock(self):
    self._unlock_timestamp = None

  def _unlock(self):
    with self._unlocked:
      self._unlock_timestamp = datetime.utcnow() + self.latency
      self._unlocked.notify_all()

  def __enter__(self):
    assert self.observer is None
    self.observer = watchdog.observers.Observer()
    self.observer.schedule(self, '.git', recursive = False)
    self.observer.start()
    if os.path.exists(self.lockfile):
      self._lock()
    return self

  def __exit__(self, type, value, traceback):
    self.observer.stop()
    self.observer.join()
    self.observer = None

  def on_created(self, event):
    if event.src_path == self.lockfile:
      self._lock()

  def on_deleted(self, event):
    if event.src_path == self.lockfile:
      self._unlock()

  def on_moved(self, event):
    if event.src_path == self.lockfile:
      self._unlock()
    if event.dest_path == self.lockfile:
      self._lock()

def revparse(*args):
  """Returns the result of `git rev-parse *args`."""
  try:
    return str(Sh("git", "rev-parse", *args)).strip()
  except ShError, e:
    raise ValueError(e)

def getUpstreamBranch(branch):
  """Returns the upstream of branch, or None if none is set."""
  try:
    return revparse("--abbrev-ref", branch + "@{upstream}")
  except ValueError:
    return None

RefLine = namedtuple('RefLine', 'timestamp hash')
Commit = namedtuple("Commit", "hash subject merges")

class LazyGitFunction(watchdog.events.FileSystemEventHandler):
  """
  Base class for functions that provide information about a git repository.

  Monitors root_dir (the current .git dir by default) and its subdirectories for
      file-system events.
  Events that match *any* include_glob will trigger an invalidation.
  If any exclude_globs are provided, events that *do not* match any of them will trigger an
      invalidation.
  If no globs are provided, *all* events trigger an invalidation.
  All globs are relative to root_dir.
  """
  def __init__(self,
               root_dir = None,
               exclude_globs = (),
               include_globs = ()):
    self._root_dir = os.path.abspath(root_dir or git_dir())
    self._exclude_globs = frozenset(exclude_globs)
    self._include_globs = frozenset(include_globs)
    self._recursive = bool(self._exclude_globs
                           or not self._include_globs
                           or any('*' in g for g in self._include_globs))

  def watch(self, callback):
    self._callback = callback
    self._observer = watchdog.observers.Observer()
    self._observer.schedule(self, self._root_dir, recursive = self._recursive)
    self._observer.start()

  def unwatch(self):
    self._observer.stop()
    #self._observer.join()

  def path_matches(self, rel_path):
    if not self._exclude_globs and not self._include_globs:
      return True
    any_included = any(fnmatch(rel_path, g) for g in self._include_globs)
    if any_included:
      return True
    any_excluded = any(fnmatch(rel_path, g) for g in self._exclude_globs)
    if self._exclude_globs and not any_excluded:
      return True
    return False

  def on_any_event(self, event):
    if event.is_directory:
      pass
    elif self.path_matches(os.path.relpath(event.src_path, self._root_dir)):
      self._callback()
    else:
      try:
        if self.path_matches(os.path.relpath(event.dest_path, self._root_dir)):
          self._callback()
      except AttributeError:
        pass

class Branch(object):
  _BRANCHES_BY_ID = { }
  _MERGE_PATTERN = re.compile(
      "Merge branch(?: '([^']+)'|es ('[^']+'(?:, '[^']+')*) and '([^']+)')")

  @staticmethod
  def _mergedBranches(comment):
    """If comment is a merge commit comment, returns the branches named in it."""
    branches = []
    m = Branch._MERGE_PATTERN.match(comment)
    if m:
      branches.extend(m.group(i) for i in (1,3) if m.group(i))
      if m.group(2):
        branches.extend(t[1:-1] for t in m.group(2).split(', '))
    return frozenset(branches)

  @classmethod
  def clear_cache(cls):
    for k, p in vars(cls).iteritems():
      if k == k.upper():
        try:
          p.invalidate()
        except AttributeError:
          pass

  @lazy
  def HEAD():
    """The current HEAD branch, or None if head is detached."""
    try:
      return Branch(revparse("--abbrev-ref", "HEAD"))
    except ValueError:
      return None

  @lazy
  def ALL():
    """The set of all (local) branches."""
    names = revparse("--abbrev-ref", "--branches").splitlines()
    return frozenset(Branch(name) for name in names)

  @lazy
  def REMOTES():
    """The set of all remote branches that have a local branch of the same name."""
    names = revparse("--abbrev-ref", "--remotes").splitlines()
    locals = frozenset(b.name for b in Branch.ALL)
    return frozenset(Branch(name) for name in names if name.split('/', 1)[-1] in locals)

  @lazy
  def _REF_LOGS():
    raw = {}
    for b in Branch.ALL:
      try:
        raw[b] = Sh("git", "log", "-g", "%s@{now}" % b.name, "--date=raw", "--format=%gd %H")
      except ShError:
        raw[b] = ()

    ref_logs = {}
    rx = re.compile("@[{](\\d+) .*[}] (\\w+)")
    for b in raw:
      ref_logs[b] = branch_logs = []
      try:
        for l in raw[b]:
          m = rx.search(l)
          if m:
            branch_logs.append(RefLine(int(m.group(1)), m.group(2)))
      except ShError:
        pass
    return ref_logs

  def __new__(cls, name):
    if name == 'HEAD':
      raise ValueError('HEAD is not a valid Branch name')
    if name not in cls._BRANCHES_BY_ID:
      cls._BRANCHES_BY_ID[name] = object.__new__(cls, name)
    return cls._BRANCHES_BY_ID[name]

  def __init__(self, name):
    self.name = name

  def __repr__(self):
    return "Branch('%s')" % self.name

  def __hash__(self):
    return hash(self.name)

  @lazy
  @property
  def _refLog(self):
    return type(self)._REF_LOGS.get(self, ())

  @lazy
  @property
  def allCommits(self):
    """All commits made to this branch, in reverse chronological order.

    Merges will only list commit hashes, not branches.

    """
    raw = Sh("git", "log", "--first-parent", "--format=%H:%P:%s", self.name)
    commits = (Commit(h, s.strip(), m.split(" ")[1:]) for h, m, s in
               (l.split(":", 2) for l in raw))
    return LazyList(commits)

  @lazy
  @property
  def latestCommit(self):
    """The latest commit made to this branch."""
    return self.allCommits[0]

  @lazy
  @property
  def upstream(self):
    """The branch set as this branch's 'upstream', or None if none is set."""
    upstreamName = getUpstreamBranch(self.name)
    return None if upstreamName is None else Branch(upstreamName)

  @lazy
  @property
  def upstreamCommit(self):
    """The most recent commit this branch shares with its upstream.

    `git log` and `git reflog` are used to detect rebases on the upstream
    branch, in similar fashion to `git pull`.

    """
    if self.upstream is None:
      return None
    commitHashes = set(c.hash for c in self.allCommits)
    firstUpstreamReference = first(h.hash for h in self.upstream._refLog if h.hash in commitHashes)
    upstreamCommitHashes = set(c.hash for c in self.upstream.allCommits)
    return first(c for c in self.allCommits
                 if c.hash in upstreamCommitHashes or c.hash == firstUpstreamReference)

  @lazy
  @property
  def commits(self):
    """All commits made to this branch since it left upstream, including merges."""
    def impl():
      for c in self.allCommits:
        if c == self.upstreamCommit:
          return
        mergedBranches = [Branch(name) for name in Branch._mergedBranches(c.subject)]
        if mergedBranches:
          yield Commit(c.hash, c.subject, mergedBranches)
        else:
          yield c
    return LazyList(impl())

  @lazy
  @property
  def parents(self):
    """All parents of this branch, whether upstream or merged."""
    if self.upstream is None:
      return frozenset()
    parents = [p for c in self.commits for p in c.merges if type(p) == Branch]
    parents.append(self.upstream)
    return frozenset(parents)

  @lazy
  @property
  def children(self):
    """All branches which have this branch as upstream or merged."""
    return frozenset(b for b in type(self).ALL if self in b.parents)

  @lazy
  @property
  def modtime(self):
    """The timestamp of the latest commit to this branch."""
    with Sh("git", "log", "-n5", "--format=%at", self.name, "--") as log:
      for line in log:
        modtime = int(line)
        if modtime != 1:
          return datetime.utcfromtimestamp(modtime)
    return None

  @lazy
  @property
  def unmerged(self):
    """The number of parent commits that have not been pulled to this branch."""
    if self.upstream is None:
      return 0
    allCommits = set(c.hash for c in self.allCommits)
    if len(self.parents) > 1:
      for c in self.allCommits:
        if c == self.upstreamCommit:
          break
        for rev in c.merges:
          allCommits.update(Sh("git", "log", "--first-parent", "--format=%H", rev))
    parentCommits = set()
    for p in self.parents:
      for c in p.allCommits:
        if c.hash in allCommits:
          break
        parentCommits.add(c.hash)
    return len(parentCommits)

