#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# Copyright (C) 2016 Bitergia
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 59 Temple Place - Suite 330, Boston, MA 02111-1307, USA.
#
# Authors:
#     Luis Cañas-Díaz <lcanas@bitergia.com>
#     Alvaro del Castillo <acs@bitergia.com>
#

import logging
import queue
import sys
import threading
import time
import traceback

from datetime import datetime, timedelta

import redis
import requests

from arthur.common import Q_STORAGE_ITEMS


from mordred.config import Config
from mordred.error import DataCollectionError
from mordred.error import DataEnrichmentError
from mordred.task import Task
from mordred.task_collection import TaskRawDataCollection, TaskRawDataArthurCollection
from mordred.task_enrich import TaskEnrich
from mordred.task_identities import TaskIdentitiesExport, TaskIdentitiesLoad, TaskIdentitiesMerge, TaskInitSortingHat
from mordred.task_manager import TasksManager
from mordred.task_panels import TaskPanels, TaskPanelsAliases, TaskPanelsMenu
from mordred.task_projects import TaskProjects
from mordred.task_report import TaskReport
from mordred.task_track import TaskTrackItems

logger = logging.getLogger(__name__)


class Mordred:

    def __init__(self, config):
        """ config is a Config object """
        self.config = config
        self.conf = config.get_conf()

    def check_redis_access(self):
        redis_access = False

        redis_url = self.conf['es_collection']['redis_url']
        try:
            # Try to access REDIS items to check it is working
            conn = redis.StrictRedis.from_url(redis_url)
            pipe = conn.pipeline()
            pipe.lrange(Q_STORAGE_ITEMS, 0, 0)
            pipe.execute()[0]
            redis_access = True
        except redis.exceptions.ConnectionError:
            logging.error("Can not connect to raw items in redis %s", redis_url)

        return redis_access

    def check_arthur_access(self):

        arthur_access = False

        arthur_url = self.conf['es_collection']['arthur_url']
        try:
            res = requests.post(arthur_url + "/tasks")
            res.raise_for_status()
            arthur_access = True
        except requests.exceptions.ConnectionError as ex:
            logging.error("Can not connect to arthur %s", arthur_url)

        return arthur_access

    def check_es_access(self):

        # So far there is no way to distinguish between read and write permission

        es_access = True
        es_error = None

        def _ofuscate_server_uri(uri):
            if uri.rfind('@') > 0:
                pre, post = uri.split('@')
                char_from = pre.rfind(':')
                result = uri[0:char_from + 1] + '****@' + post
                return result
            else:
                return uri

        es = self.conf['es_collection']['url']
        try:
            r = requests.get(es, verify=False)
            r.raise_for_status()
        except Exception:
            es_access = False
            es_error = _ofuscate_server_uri(es)

        if (self.conf['phases']['enrichment'] or
           self.conf['es_enrichment']['studies']):
            es = self.conf['es_enrichment']['url']
            try:
                r = requests.get(es, verify=False)
                r.raise_for_status()
            except Exception:
                es_access = False
                es_error = _ofuscate_server_uri(es)

        if not es_access:
            logger.error('Can not connect to Elasticsearch: %s', es_error)

        return es_access

    def _get_repos_by_backend(self):
        #
        # return dict with backend and list of repositories
        #
        output = {}
        projects = TaskProjects.get_projects()

        for backend_section in Config.get_backend_sections():
            for pro in projects:
                backend = Task.get_backend(backend_section)
                if backend in projects[pro]:
                    if backend_section not in output:
                        output[backend_section] = projects[pro][backend]
                    else:
                        output[backend_section] += projects[pro][backend]

        # backend could be in project/repo file but not enabled in
        # mordred conf file
        enabled = {}
        for k in output:
            if k in self.conf:
                enabled[k] = output[k]

        # logger.debug('repos to be retrieved: %s ', enabled)
        return enabled

    def execute_tasks(self, tasks_cls):
        """
            Just a wrapper to the execute_batch_tasks method
        """
        self.execute_batch_tasks(tasks_cls)

    def execute_nonstop_tasks(self, tasks_cls):
        """
            Just a wrapper to the execute_batch_tasks method
        """
        self.execute_batch_tasks(tasks_cls,
                                 self.conf['sortinghat']['sleep_for'],
                                 self.conf['general']['min_update_delay'], False)

    def execute_batch_tasks(self, tasks_cls, big_delay=0, small_delay=0, wait_for_threads=True):
        """
        Start a task manager per backend to complete the tasks.

        :param task_cls: list of tasks classes to be executed
        :param big_delay: seconds before global tasks are executed, should be days usually
        :param small_delay: seconds before backend tasks are executed, should be minutes
        :param wait_for_threads: boolean to set when threads are infinite or
                                should be synchronized in a meeting point
        """

        def _split_tasks(tasks_cls):
            """
            we internally distinguish between tasks executed by backend
            and tasks executed with no specific backend. """
            backend_t = []
            global_t = []
            for t in tasks_cls:
                if t.is_backend_task(t):
                    backend_t.append(t)
                else:
                    global_t.append(t)
            return backend_t, global_t

        logger.debug('Tasks Manager starting .. ')

        backend_tasks, global_tasks = _split_tasks(tasks_cls)
        logger.debug('backend_tasks = %s' % (backend_tasks))
        logger.debug('global_tasks = %s' % (global_tasks))

        threads = []

        # stopper won't be set unless wait_for_threads is True
        stopper = threading.Event()

        # launching threads for tasks by backend
        if len(backend_tasks) > 0:
            repos_backend = self._get_repos_by_backend()
            # Init the shared dict to control if autorefresh must be done
            backends_autorefresh = {backend: False for backend in repos_backend}
            TasksManager.AUTOREFRESH_QUEUE.put(backends_autorefresh)
            logger.debug("Initial Autorefresh queue: %s", backends_autorefresh)
            # Init the shared dict to control the uuids to be autorefreshed in
            # each backend
            backends_autorefresh_uuids = {backend: [] for backend in repos_backend}
            TasksManager.UPDATED_UUIDS_QUEUE.put(backends_autorefresh_uuids)
            for backend in repos_backend:
                # Start new Threads and add them to the threads list to complete
                t = TasksManager(backend_tasks, backend, stopper, self.config, small_delay)
                threads.append(t)
                t.start()

        # launch thread for global tasks
        if len(global_tasks) > 0:
            # FIXME timer is applied to all global_tasks, does it make sense?
            # All tasks are executed in the same thread sequentially
            gt = TasksManager(global_tasks, "Global tasks", stopper, self.config, big_delay)
            threads.append(gt)
            gt.start()
            if big_delay > 0:
                when = datetime.now() + timedelta(seconds=big_delay)
                when_str = when.strftime('%a, %d %b %Y %H:%M:%S %Z')
                logger.info("%s will be executed on %s" % (global_tasks, when_str))

        if wait_for_threads:
            time.sleep(1)  # Give enough time create and run all threads
            stopper.set()  # All threads must stop in the next iteration
            logger.debug(" Waiting for all threads to complete. This could take a while ..")

        # Wait for all threads to complete
        for t in threads:
            t.join()

        # Checking for exceptions in threads to log them
        self.__check_queue_for_errors()

        # Empty the shared queues. They must have just one item.
        if TasksManager.AUTOREFRESH_QUEUE.qsize() != 1:
            logger.warning("AUTOREFRESH_QUEUE final size: %i",
                           TasksManager.AUTOREFRESH_QUEUE.qsize())
        if TasksManager.UPDATED_UUIDS_QUEUE.qsize() != 1:
            logger.warning("UPDATED_UUIDS_QUEUE final size: %i",
                           TasksManager.UPDATED_UUIDS_QUEUE.qsize())
        while not TasksManager.AUTOREFRESH_QUEUE.empty():
            TasksManager.AUTOREFRESH_QUEUE.get()
        while not TasksManager.UPDATED_UUIDS_QUEUE.empty():
            TasksManager.UPDATED_UUIDS_QUEUE.get()

        logger.debug(" Task manager and all its tasks (threads) finished!")

    def __check_queue_for_errors(self):
        try:
            exc = TasksManager.COMM_QUEUE.get(block=False)
        except queue.Empty:
            logger.debug("No exceptions in threads. Let's continue ..")
        else:
            exc_type, exc_obj, exc_trace = exc
            # deal with the exception
            logger.error(exc_type)
            raise exc_obj

    def __execute_initial_load(self):
        """
        Tasks that should be done just one time
        """

        if self.conf['phases']['panels']:
            tasks_cls = [TaskPanelsAliases, TaskPanels, TaskPanelsMenu]
            self.execute_tasks(tasks_cls)
        if self.conf['phases']['identities']:
            tasks_cls = [TaskInitSortingHat]
            self.execute_tasks(tasks_cls)

        logger.info("Loading projects")
        tasks_cls = [TaskProjects]
        self.execute_tasks(tasks_cls)
        logger.info("Done")

        return

    def start(self):
        """
        This method defines the workflow of Mordred. So it calls to:
        - initialize the databases
        - execute the different phases for the first iteration
          (collection, identities, enrichment)
        - start the collection and enrichment in parallel by data source
        - start also the Sorting Hat merge
        """

        # logger.debug("Starting Mordred engine ...")
        logger.info("")
        logger.info("----------------------------")
        logger.info("Starting Mordred engine ...")
        logger.info("- - - - - - - - - - - - - - ")

        # check we have access to the needed ES
        if not self.check_es_access():
            print('Can not access Elasticsearch service. Exiting mordred ...')
            sys.exit(1)

        # If arthur is configured check that it is working
        if self.conf['es_collection']['arthur']:
            if not self.check_redis_access():
                print('Can not access redis service. Exiting mordred ...')
                sys.exit(1)
            if not self.check_arthur_access():
                print('Can not access arthur service. Exiting mordred ...')
                sys.exit(1)

        # Initial round: panels and projects loading
        self.__execute_initial_load()

        # Tasks to be executed during updating process
        all_tasks_cls = []
        all_tasks_cls.append(TaskProjects)  # projects update is always needed
        if self.conf['phases']['collection']:
            if not self.conf['es_collection']['arthur']:
                all_tasks_cls.append(TaskRawDataCollection)
            else:
                all_tasks_cls.append(TaskRawDataArthurCollection)
        if self.conf['phases']['identities']:
            # load identities and orgs periodically for updates
            all_tasks_cls.append(TaskIdentitiesLoad)
            all_tasks_cls.append(TaskIdentitiesMerge)
            all_tasks_cls.append(TaskIdentitiesExport)
            # This is done in enrichement before doing the enrich
            # if self.conf['phases']['collection']:
            #     all_tasks_cls.append(TaskIdentitiesCollection)
        if self.conf['phases']['enrichment']:
            all_tasks_cls.append(TaskEnrich)
            # During enrich new indexes can be created a they need their aliases
            all_tasks_cls.append(TaskPanelsAliases)
        if self.conf['phases']['track_items']:
            all_tasks_cls.append(TaskTrackItems)
        if self.conf['phases']['report']:
            all_tasks_cls.append(TaskReport)

        # this is the main loop, where the execution should spend
        # most of its time

        while True:

            if not all_tasks_cls:
                logger.warning("No tasks to execute.")
                break

            try:
                if not self.conf['general']['update']:
                    self.execute_batch_tasks(all_tasks_cls,
                                             self.conf['sortinghat']['sleep_for'],
                                             self.conf['general']['min_update_delay'])
                    break
                else:
                    self.execute_nonstop_tasks(all_tasks_cls)

                # FIXME this point is never reached so despite the exception is
                # handled and the error is shown, the traceback is not printed

            except DataCollectionError as e:
                logger.error(str(e))
                var = traceback.format_exc()
                logger.error(var)

            except DataEnrichmentError as e:
                logger.error(str(e))
                var = traceback.format_exc()
                logger.error(var)

        logger.info("Finished Mordred engine ...")
