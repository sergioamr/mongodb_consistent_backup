import os, sys
import logging
import signal

from fabric.api import hide, settings, local
from math import floor
from multiprocessing import cpu_count
from signal import signal, SIGINT, SIG_IGN
from time import sleep

from mongodb_consistent_backup.Common import MongoUri
from mongodb_consistent_backup.Errors import Error, OperationError
from mongodb_consistent_backup.Oplog import OplogState

from MongodumpThread import MongodumpThread


class Mongodump:
    def __init__(self, manager, config, timer, base_dir, backup_dir, **kwargs):
        self.manager    = manager
        self.config     = config
        self.timer      = timer
        self.base_dir   = base_dir
        self.backup_dir = backup_dir
        self.binary     = self.config.backup.mongodump.binary
        self.user       = self.config.user
        self.password   = self.config.password
        self.authdb     = self.config.authdb
        self.verbose    = self.config.verbose

        try:
            self.replsets = kwargs['replsets']
        except KeyError:
            raise Error("'replsets' kwargs required!")

        self.sharding = None
        if 'sharding' in kwargs:
            self.sharding = kwargs['sharding']

        signal(SIGINT, SIG_IGN)
        signal(SIGINT, self.close)

        self.completed            = False
        self.timer_name           = self.__class__.__name__
        self.threads_per_dump_max = 16
        self.config_replset       = False
        self.cpu_count            = cpu_count()
        self.threads              = []
        self.states               = {}
        self._summary             = {}
        self._threads_per_dump    = None

        with hide('running', 'warnings'), settings(warn_only=True):
            self.version = local("%s --version|awk 'NR >1 {exit}; /version/{print $NF}'" % self.binary, capture=True)
        self.do_gzip = self.can_gzip()

        if not self.do_gzip and self.config.backup.mongodump.compression == 'gzip':
            logging.warning("mongodump gzip compression requested on binary that does not support gzip!")

        if not isinstance(self.replsets, dict):
            raise Error("Field 'replsets' must be a dictionary of mongodb_consistent_backup.Replication.Replset classes!")

    def can_gzip(self):
        if os.path.isfile(self.binary) and os.access(self.binary, os.X_OK):
            if tuple("3.2.0".split(".")) <= tuple(self.version.split(".")):
                return True
            return False
        else:
            logging.fatal("Cannot find or execute the mongodump binary file %s!" % self.binary)
            sys.exit(1)

    def is_compressed(self):
        return self.can_gzip()

    def summary(self):
        return self._summary

    # get oplog summaries from the queue
    def get_summaries(self):
        for shard in self.states:
            state = self.states[shard]
            host  = state.get('host')
            port  = state.get('port')
            self._summary[shard] = state.get().copy()

    def wait(self):
        completed = 0
        start_threads = len(self.threads)
        # wait for all threads to finish
        while len(self.threads) > 0:
            for thread in self.threads:
                if not thread.is_alive():
                    if thread.exitcode == 0:
                        completed += 1
                    self.threads.remove(thread)
            sleep(0.5)

        # sleep for 3 sec to fix logging order before gathering summaries
        sleep(3)
        self.get_summaries()

        # check if all threads completed
        if completed == start_threads:
            logging.info("All mongodump backups completed successfully")
            self.timer.stop(self.timer_name)
        else:
            raise OperationError("Not all mongodump threads completed successfully!")

    def threads_per_dump(self, threads=None):
        if threads:
            self._threads_per_dump = int(threads)
        elif not self._threads_per_dump:
            if tuple(self.version.split(".")) >= tuple("3.2.0".split(".")):
                self._threads_per_dump = 1
                if self.cpu_count > len(self.replsets):
                    self._threads_per_dump = int(floor(self.cpu_count / len(self.replsets)))
                    if self._threads_per_dump > self.threads_per_dump_max:
                        self._threads_per_dump = self.threads_per_dump_max
            else:
                logging.warn("Threading unsupported by mongodump version %s. Use mongodump 3.2.0 or greater to enable per-dump threading." % self.version)
        return self._threads_per_dump

    def run(self):
        self.timer.start(self.timer_name)

        # backup a secondary from each shard:
        for shard in self.replsets:
            secondary = self.replsets[shard].find_secondary()
            mongo_uri = secondary['uri']
            self.states[shard] = OplogState(self.manager, mongo_uri)
            thread = MongodumpThread(
                self.states[shard],
                mongo_uri,
                self.timer,
                self.user,
                self.password,
                self.authdb,
                self.backup_dir,
                self.binary,
                self.threads_per_dump(),
                self.do_gzip,
                self.verbose
            )
            self.threads.append(thread)

        if not len(self.threads) > 0:
            raise OperationError('No backup threads started!')

        logging.info(
            "Starting backups using mongodump %s (options: gzip=%s, threads_per_dump=%i)" % (self.version, str(self.do_gzip), self.threads_per_dump()))
        for thread in self.threads:
            thread.start()
        self.wait()

        # backup a single sccc/non-replset config server, if exists:
        if self.sharding:
            config_server = self.sharding.get_config_server()
            if config_server and isinstance(config_server, dict):
                logging.info("Using non-replset backup method for config server mongodump")
                mongo_uri = MongoUri(config_server['host'], 27019, 'configsvr')
                self.states['configsvr'] = OplogState(self.manager, mongo_uri)
                self.threads = [MongodumpThread(
                    self.states['configsvr'],
                    mongo_uri,
                    self.timer,
                    self.user,
                    self.password,
                    self.authdb,
                    self.backup_dir,
                    self.binary,
                    self.threads_per_dump(),
                    self.do_gzip,
                    self.verbose
                )]
                self.threads[0].start()
                self.wait()

        self.completed = True
        return self._summary

    def close(self):
        logging.info("Stopping all mongodump threads")
        if len(self.threads) > 0:
            for thread in self.threads:
                thread.terminate()
        try:
            self.timer.stop(self.timer_name)
        except:
            pass
        logging.info("Stopped all mongodump threads")
