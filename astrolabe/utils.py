# Copyright 2020-present MongoDB, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import json
import logging
import os
import signal
import subprocess
import sys
from hashlib import sha256
from time import monotonic, sleep

import click
import junitparser

from pymongo import MongoClient

from astrolabe.exceptions import WorkloadExecutorError


LOGGER = logging.getLogger(__name__)


class ClickLogHandler(logging.Handler):
    """Handler for print log statements via Click's echo functionality."""
    def emit(self, record):
        try:
            msg = self.format(record)
            use_stderr = False
            if record.levelno >= logging.WARNING:
                use_stderr = True
            click.echo(msg, err=use_stderr)
        except Exception:
            self.handleError(record)


def assert_subset(dict1, dict2):
    """Utility that asserts that `dict2` is a subset of `dict1`, while
    accounting for nested fields."""
    for key, value in dict2.items():
        if key not in dict1:
            raise AssertionError("not a subset")
        if isinstance(value, dict):
            assert_subset(dict1[key], value)
        else:
            assert dict1[key] == value


class Timer:
    """Class to simplify timing operations."""
    def __init__(self):
        self._start = None
        self._end = None

    def reset(self):
        self.__init__()

    def start(self):
        self._start = monotonic()
        self._end = None

    def stop(self):
        self._end = monotonic()

    @property
    def elapsed(self):
        if self._end is None:
            return monotonic() - self._start
        return self._end - self._start


class SingleTestXUnitLogger:
    def __init__(self, *, output_directory):
        self._output_directory = os.path.realpath(os.path.join(
            os.getcwd(), output_directory))

        # Ensure folder exists.
        try:
            os.mkdir(self._output_directory)
        except FileExistsError:
            pass

    def write_xml(self, test_case, filename):
        filename += '.xml'
        xml_path = os.path.join(self._output_directory, filename)

        # Remove existing file if applicable.
        try:
            os.unlink(xml_path)
        except FileNotFoundError:
            pass

        # use filename as suitename
        suite = junitparser.TestSuite(filename)
        suite.add_testcase(test_case)

        xml = junitparser.JUnitXml()
        xml.add_testsuite(suite)
        xml.write(xml_path)


def get_test_name_from_spec_file(full_path):
    """Generate test name from a spec test file."""
    _, filename = os.path.split(full_path)
    test_name = os.path.splitext(filename)[0].replace('-', '_')
    return test_name


def get_cluster_name(test_name, name_salt):
    """Generate unique cluster name from test name and salt."""
    name_hash = sha256(test_name.encode('utf-8'))
    name_hash.update(name_salt.encode('utf-8'))
    return name_hash.hexdigest()[:10]


def load_test_data(connection_string, driver_workload):
    """Insert the test data into the cluster."""
    kwargs = {'w': "majority"}

    # TODO: remove this if...else block after BUILD-10841 is done.
    if (sys.platform in ("win32", "cygwin") and
            connection_string.startswith("mongodb+srv://")):
        import certifi
        kwargs['tlsCAFile'] = certifi.where()
    client = MongoClient(connection_string, **kwargs)

    coll = client.get_database(
        driver_workload.database).get_collection(
        driver_workload.collection)
    coll.drop()
    coll.insert_many(driver_workload.testData)


class DriverWorkloadSubprocessRunner:
    """Convenience wrapper to run a workload executor in a subprocess."""
    _PLACEHOLDER_EXECUTION_STATISTICS = {
        'numErrors': -1, 'numFailures': -1, 'numSuccesses': -1}

    def __init__(self):
        self.is_windows = False
        if sys.platform in ("win32", "cygwin"):
            self.is_windows = True
        self.workload_subprocess = None
        self.sentinel = os.path.join(
            os.path.abspath(os.curdir), 'results.json')

    @property
    def pid(self):
        return self.workload_subprocess.pid

    @property
    def returncode(self):
        return self.workload_subprocess.returncode

    def spawn(self, *, workload_executor, connection_string, driver_workload,
              startup_time=1):
        LOGGER.info("Starting workload executor subprocess")

        try:
            os.remove(self.sentinel)
            LOGGER.debug("Cleaned up sentinel file at {}".format(
                self.sentinel))
        except FileNotFoundError:
            pass

        _args = [workload_executor, connection_string, json.dumps(driver_workload)]
        if not self.is_windows:
            args = _args
            self.workload_subprocess = subprocess.Popen(
                args, preexec_fn=os.setsid)
        else:
            args = ['C:/cygwin/bin/bash']
            args.extend(_args)
            self.workload_subprocess = subprocess.Popen(
                args, creationflags=subprocess.CREATE_NEW_PROCESS_GROUP)

        LOGGER.debug("Subprocess argument list: {}".format(args))
        LOGGER.info("Started workload executor [PID: {}]".format(self.pid))

        try:
            # Wait for the workload executor to start.
            LOGGER.info("Waiting {} seconds for the workload executor "
                        "subprocess to start".format(startup_time))
            self.workload_subprocess.wait(timeout=startup_time)
        except subprocess.TimeoutExpired:
            pass
        else:
            # We end up here if TimeoutExpired was not raised. This means that
            # the workload executor has already quit which is incorrect.
            raise WorkloadExecutorError(
                "Workload executor quit without receiving termination signal")

        return self.workload_subprocess

    def terminate(self):
        LOGGER.info("Stopping workload executor [PID: {}]".format(self.pid))

        if not self.is_windows:
            os.killpg(self.workload_subprocess.pid, signal.SIGINT)
        else:
            os.kill(self.workload_subprocess.pid, signal.CTRL_BREAK_EVENT)

        t_wait = 10
        try:
            self.workload_subprocess.wait(timeout=t_wait)
            LOGGER.info("Stopped workload executor [PID: {}]".format(self.pid))
        except subprocess.TimeoutExpired:
            raise WorkloadExecutorError(
                "The workload executor did not terminate {} seconds "
                "after sending the termination signal".format(t_wait))

        # Workload executors wrapped in shell scripts can report that they've
        # terminated earlier than they actually terminate on Windows.
        if self.is_windows:
            sleep(2)

        try:
            LOGGER.info("Reading sentinel file {!r}".format(self.sentinel))
            with open(self.sentinel, 'r') as fp:
                stats = json.load(fp)
        except FileNotFoundError:
            LOGGER.error("Sentinel file not found")
            stats = self._PLACEHOLDER_EXECUTION_STATISTICS
        except json.JSONDecodeError:
            LOGGER.error("Sentinel file contains malformed JSON")
            stats = self._PLACEHOLDER_EXECUTION_STATISTICS

        return stats
