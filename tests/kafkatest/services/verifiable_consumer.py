# Licensed to the Apache Software Foundation (ASF) under one or more
# contributor license agreements.  See the NOTICE file distributed with
# this work for additional information regarding copyright ownership.
# The ASF licenses this file to You under the Apache License, Version 2.0
# (the "License"); you may not use this file except in compliance with
# the License.  You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import json
import os
import signal

from ducktape.services.background_thread import BackgroundThreadService
from ducktape.cluster.remoteaccount import RemoteCommandError

from kafkatest.directory_layout.kafka_path import KafkaPathResolverMixin
from kafkatest.services.kafka import TopicPartition
from kafkatest.version import TRUNK


class ConsumerState:
    Dead = 1
    Rebalancing = 3
    Joined = 2


class ConsumerEventHandler(object):

    def __init__(self, node):
        self.node = node
        self.state = ConsumerState.Dead
        self.revoked_count = 0
        self.assigned_count = 0
        self.assignment = []
        self.position = {}
        self.committed = {}
        self.total_consumed = 0

    def handle_shutdown_complete(self):
        self.state = ConsumerState.Dead
        self.assignment = []
        self.position = {}

    def handle_offsets_committed(self, event):
        if event["success"]:
            for offset_commit in event["offsets"]:
                topic = offset_commit["topic"]
                partition = offset_commit["partition"]
                tp = TopicPartition(topic, partition)
                offset = offset_commit["offset"]
                assert tp in self.assignment, "Committed offsets for a partition not assigned"
                assert self.position[tp] >= offset, "The committed offset was greater than the current position"
                self.committed[tp] = offset

    def handle_records_consumed(self, event):
        assert self.state == ConsumerState.Joined, "Consumed records should only be received when joined"

        for record_batch in event["partitions"]:
            tp = TopicPartition(topic=record_batch["topic"],
                                partition=record_batch["partition"])
            min_offset = record_batch["minOffset"]
            max_offset = record_batch["maxOffset"]

            assert tp in self.assignment, "Consumed records for a partition not assigned"
            assert tp not in self.position or self.position[tp] == min_offset, \
                "Consumed from an unexpected offset (%s, %s)" % (str(self.position[tp]), str(min_offset))
            self.position[tp] = max_offset + 1 

        self.total_consumed += event["count"]

    def handle_partitions_revoked(self, event):
        self.revoked_count += 1
        self.state = ConsumerState.Rebalancing
        self.position = {}

    def handle_partitions_assigned(self, event):
        self.assigned_count += 1
        self.state = ConsumerState.Joined
        assignment = []
        for topic_partition in event["partitions"]:
            topic = topic_partition["topic"]
            partition = topic_partition["partition"]
            assignment.append(TopicPartition(topic, partition))
        self.assignment = assignment

    def handle_kill_process(self, clean_shutdown):
        # if the shutdown was clean, then we expect the explicit
        # shutdown event from the consumer
        if not clean_shutdown:
            self.handle_shutdown_complete()

    def current_assignment(self):
        return list(self.assignment)

    def current_position(self, tp):
        if tp in self.position:
            return self.position[tp]
        else:
            return None

    def last_commit(self, tp):
        if tp in self.committed:
            return self.committed[tp]
        else:
            return None


class VerifiableConsumer(KafkaPathResolverMixin, BackgroundThreadService):
    PERSISTENT_ROOT = "/mnt/verifiable_consumer"
    STDOUT_CAPTURE = os.path.join(PERSISTENT_ROOT, "verifiable_consumer.stdout")
    STDERR_CAPTURE = os.path.join(PERSISTENT_ROOT, "verifiable_consumer.stderr")
    LOG_DIR = os.path.join(PERSISTENT_ROOT, "logs")
    LOG_FILE = os.path.join(LOG_DIR, "verifiable_consumer.log")
    LOG4J_CONFIG = os.path.join(PERSISTENT_ROOT, "tools-log4j.properties")
    CONFIG_FILE = os.path.join(PERSISTENT_ROOT, "verifiable_consumer.properties")

    logs = {
        "verifiable_consumer_stdout": {
            "path": STDOUT_CAPTURE,
            "collect_default": False},
        "verifiable_consumer_stderr": {
            "path": STDERR_CAPTURE,
            "collect_default": False},
        "verifiable_consumer_log": {
            "path": LOG_FILE,
            "collect_default": True}
        }

    def __init__(self, context, num_nodes, kafka, topic, group_id,
                 max_messages=-1, session_timeout_sec=30, enable_autocommit=False,
                 assignment_strategy="org.apache.kafka.clients.consumer.RangeAssignor",
                 version=TRUNK, stop_timeout_sec=30):
        super(VerifiableConsumer, self).__init__(context, num_nodes)
        self.log_level = "TRACE"
        
        self.kafka = kafka
        self.topic = topic
        self.group_id = group_id
        self.max_messages = max_messages
        self.session_timeout_sec = session_timeout_sec
        self.enable_autocommit = enable_autocommit
        self.assignment_strategy = assignment_strategy
        self.prop_file = ""
        self.security_config = kafka.security_config.client_config(self.prop_file)
        self.prop_file += str(self.security_config)
        self.stop_timeout_sec = stop_timeout_sec

        self.event_handlers = {}
        self.global_position = {}
        self.global_committed = {}

        for node in self.nodes:
            node.version = version

    def _worker(self, idx, node):
        if node not in self.event_handlers:
            self.event_handlers[node] = ConsumerEventHandler(node)

        handler = self.event_handlers[node]
        node.account.ssh("mkdir -p %s" % VerifiableConsumer.PERSISTENT_ROOT, allow_fail=False)

        # Create and upload log properties
        log_config = self.render('tools_log4j.properties', log_file=VerifiableConsumer.LOG_FILE)
        node.account.create_file(VerifiableConsumer.LOG4J_CONFIG, log_config)

        # Create and upload config file
        self.logger.info("verifiable_consumer.properties:")
        self.logger.info(self.prop_file)
        node.account.create_file(VerifiableConsumer.CONFIG_FILE, self.prop_file)
        self.security_config.setup_node(node)
        cmd = self.start_cmd(node)
        self.logger.debug("VerifiableConsumer %d command: %s" % (idx, cmd))

        for line in node.account.ssh_capture(cmd):
            event = self.try_parse_json(line.strip())
            if event is not None:
                with self.lock:
                    name = event["name"]
                    if name == "shutdown_complete":
                        handler.handle_shutdown_complete()
                    if name == "offsets_committed":
                        handler.handle_offsets_committed(event)
                        self._update_global_committed(event)
                    elif name == "records_consumed":
                        handler.handle_records_consumed(event)
                        self._update_global_position(event)
                    elif name == "partitions_revoked":
                        handler.handle_partitions_revoked(event)
                    elif name == "partitions_assigned":
                        handler.handle_partitions_assigned(event)

    def _update_global_position(self, consumed_event):
        for consumed_partition in consumed_event["partitions"]:
            tp = TopicPartition(consumed_partition["topic"], consumed_partition["partition"])
            if tp in self.global_committed:
                # verify that the position never gets behind the current commit.
                assert self.global_committed[tp] <= consumed_partition["minOffset"], \
                    "Consumed position %d is behind the current committed offset %d" % (consumed_partition["minOffset"], self.global_committed[tp])

            # the consumer cannot generally guarantee that the position increases monotonically
            # without gaps in the face of hard failures, so we only log a warning when this happens
            if tp in self.global_position and self.global_position[tp] != consumed_partition["minOffset"]:
                self.logger.warn("Expected next consumed offset of %d, but instead saw %d" %
                                 (self.global_position[tp], consumed_partition["minOffset"]))

            self.global_position[tp] = consumed_partition["maxOffset"] + 1

    def _update_global_committed(self, commit_event):
        if commit_event["success"]:
            for offset_commit in commit_event["offsets"]:
                tp = TopicPartition(offset_commit["topic"], offset_commit["partition"])
                offset = offset_commit["offset"]
                assert self.global_position[tp] >= offset, \
                    "committed offset is ahead of the current partition"
                self.global_committed[tp] = offset

    def start_cmd(self, node):
        cmd = ""
        cmd += "export LOG_DIR=%s;" % VerifiableConsumer.LOG_DIR
        cmd += " export KAFKA_OPTS=%s;" % self.security_config.kafka_opts
        cmd += " export KAFKA_LOG4J_OPTS=\"-Dlog4j.configuration=file:%s\"; " % VerifiableConsumer.LOG4J_CONFIG
        cmd += self.path.script("kafka-run-class.sh", node) + " org.apache.kafka.tools.VerifiableConsumer" \
              " --group-id %s --topic %s --broker-list %s --session-timeout %s --assignment-strategy %s %s" % \
                                            (self.group_id, self.topic, self.kafka.bootstrap_servers(self.security_config.security_protocol),
               self.session_timeout_sec*1000, self.assignment_strategy, "--enable-autocommit" if self.enable_autocommit else "")
               
        if self.max_messages > 0:
            cmd += " --max-messages %s" % str(self.max_messages)

        cmd += " --consumer.config %s" % VerifiableConsumer.CONFIG_FILE
        cmd += " 2>> %s | tee -a %s &" % (VerifiableConsumer.STDOUT_CAPTURE, VerifiableConsumer.STDOUT_CAPTURE)
        return cmd

    def pids(self, node):
        try:
            cmd = "jps | grep -i VerifiableConsumer | awk '{print $1}'"
            pid_arr = [pid for pid in node.account.ssh_capture(cmd, allow_fail=True, callback=int)]
            return pid_arr
        except (RemoteCommandError, ValueError) as e:
            return []

    def try_parse_json(self, string):
        """Try to parse a string as json. Return None if not parseable."""
        try:
            return json.loads(string)
        except ValueError:
            self.logger.debug("Could not parse as json: %s" % str(string))
            return None

    def stop_all(self):
        for node in self.nodes:
            self.stop_node(node)

    def kill_node(self, node, clean_shutdown=True, allow_fail=False):
        if clean_shutdown:
            sig = signal.SIGTERM
        else:
            sig = signal.SIGKILL
        for pid in self.pids(node):
            node.account.signal(pid, sig, allow_fail)

        self.event_handlers[node].handle_kill_process(clean_shutdown)

    def stop_node(self, node, clean_shutdown=True):
        self.kill_node(node, clean_shutdown=clean_shutdown)

        stopped = self.wait_node(node, timeout_sec=self.stop_timeout_sec)
        assert stopped, "Node %s: did not stop within the specified timeout of %s seconds" % \
                        (str(node.account), str(self.stop_timeout_sec))

    def clean_node(self, node):
        self.kill_node(node, clean_shutdown=False)
        node.account.ssh("rm -rf " + self.PERSISTENT_ROOT, allow_fail=False)
        self.security_config.clean_node(node)

    def current_assignment(self):
        with self.lock:
            return { handler.node: handler.current_assignment() for handler in self.event_handlers.itervalues() }

    def current_position(self, tp):
        with self.lock:
            if tp in self.global_position:
                return self.global_position[tp]
            else:
                return None

    def owner(self, tp):
        for handler in self.event_handlers.itervalues():
            if tp in handler.current_assignment():
                return handler.node
        return None

    def last_commit(self, tp):
        with self.lock:
            if tp in self.global_committed:
                return self.global_committed[tp]
            else:
                return None

    def total_consumed(self):
        with self.lock:
            return sum(handler.total_consumed for handler in self.event_handlers.itervalues())

    def num_rebalances(self):
        with self.lock:
            return max(handler.assigned_count for handler in self.event_handlers.itervalues())

    def joined_nodes(self):
        with self.lock:
            return [handler.node for handler in self.event_handlers.itervalues()
                    if handler.state == ConsumerState.Joined]

    def rebalancing_nodes(self):
        with self.lock:
            return [handler.node for handler in self.event_handlers.itervalues()
                    if handler.state == ConsumerState.Rebalancing]

    def dead_nodes(self):
        with self.lock:
            return [handler.node for handler in self.event_handlers.itervalues()
                    if handler.state == ConsumerState.Dead]
