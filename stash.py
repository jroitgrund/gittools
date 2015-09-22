import getpass, json, keyring, posixpath, re, requests, urlparse, warnings
from collections import Counter, defaultdict
from git import Branch
from itertools import count
from scheduling import NotDoneException, Poller, Scheduler
from lazy import lazy
from utils import Sh, ShError

class Stash(object):

  def __init__(self):
    serversByRemote = self._getServers()
    servers = frozenset(serversByRemote.values())
    branchesByCommit = defaultdict(set)
    commitsByServer = defaultdict(set)
    for remoteBranch in Branch.REMOTES:
      remote, branchName = remoteBranch.name.split('/', 1)
      if remote in serversByRemote:
        server = serversByRemote[remote]
        commit = remoteBranch.latestCommit.hash
        commitsByServer[server].add(commit)
        branchesByCommit[commit].add(branchName)
    self._branchesByCommit = branchesByCommit
    self._commitsByServer = commitsByServer
    self._auth = self._getAuth(servers)

    if servers:
      self._scheduler = Scheduler()
      self._futuresByServer = {
          server : lazy(Poller(self._scheduler, self._getServerStatsByBranch, server))
              for server in servers
      }
    else:
      self._scheduler = None
      self._futuresByServer = {}

  STASH_REGEX = re.compile('^git[@](stash[^:]*):.*$')

  PARAMS = {
    'verify' : False,
  }
  STATUS_MAP = {
    'successful' : 'green',
    'inProgress' : 'yellow',
    'failed' : 'red',
  }

  def _getServers(self):
    """Remote Stash servers, keyed by remote name."""
    try:
      raw = Sh('git', 'config', '--get-regexp', 'remote\..*\.url')
      remotes = {}
      for l in raw:
        key, server = l.split(' ', 1)
        name = key.split('.', 1)[-1].rsplit('.', 1)[0]
        hostname = Stash._getStashHostname(server)
        if hostname:
          remotes[name] = 'https://%s' % hostname
      return remotes
    except ShError, e:
      if e.returncode == 1:
        return {}
      raise

  @staticmethod
  def _getStashHostname(url):
    stash_match = Stash.STASH_REGEX.match(url)
    if stash_match:
      match = stash_match.group(1)
      if match == 'stash' or match.startswith('stash.'):
        return match
    url = urlparse.urlparse(url)
    if url.hostname:
      if url.hostname == 'stash' or url.hostname.startswith('stash.'):
        return url.hostname
    return None

  def _getWithAuth(self, url, auth):
    with warnings.catch_warnings():
      warnings.simplefilter(
          'ignore', requests.packages.urllib3.exceptions.InsecureRequestWarning)
      r = requests.get(url, auth = auth, **Stash.PARAMS)
      if r.status_code >= 400:
        raise RuntimeError('Got %d fetching %s as %s' % (r.status_code, url, auth[0]))
      return r.json()

  def _getAuth(self, servers):
    user = getpass.getuser()
    auth = {}
    for server in servers:
      password = keyring.get_password(server, user)
      if password is None:
        password = getpass.getpass('Password for %s? ' % server)
        # Verify the password
        self._getWithAuth(
            posixpath.join(server, 'rest/api/1.0/application-properties'),
            (user, password))
        keyring.set_password(server, user, password)
      auth[server] = (user, password)
    return auth

  def _getRawServerStats(self, server, commits):
    try:
      with warnings.catch_warnings():
        warnings.simplefilter(
            'ignore', requests.packages.urllib3.exceptions.InsecureRequestWarning)
        r = requests.post(posixpath.join(server, 'rest/build-status/1.0/commits/stats'),
                          data = json.dumps(list(commits)),
                          auth = self._auth[server],
                          headers = { 'content-type': 'application/json' },
                          **Stash.PARAMS)
      if r.status_code >= 400:
        raise RuntimeError('Got %d fetching %s as %s' % (r.status_code, url, auth[0]))
      return r.json()
    except IOError:
      return {}

  def _getServerStatsByBranch(self, server):
    statsByBranch = defaultdict(Counter)
    commits = self._commitsByServer[server]
    rawStats = self._getRawServerStats(server, commits)
    for commit, stats in rawStats.iteritems():
      colorCodedStats = { Stash.STATUS_MAP[k] : v for k, v in stats.iteritems()
                          if k in Stash.STATUS_MAP }
      for branch in self._branchesByCommit[commit]:
        statsByBranch[branch].update(colorCodedStats)
    return statsByBranch

  @lazy
  @property
  def _statsByBranch(self):
    statsByBranch = defaultdict(Counter)
    for serverStatsByBranch in self._futuresByServer.itervalues():
      try:
        for b, stats in serverStatsByBranch().iteritems():
          statsByBranch[b].update(stats)
      except NotDoneException:
        pass
    if self._scheduler:
      self._scheduler.release()
      self._scheduler = None
    return statsByBranch

  def ciStatus(self, branch):
    return dict(zip(count(0), self._statsByBranch[branch.name].elements()))

