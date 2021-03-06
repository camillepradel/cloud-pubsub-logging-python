# -*- coding: utf-8 -*-
# Copyright 2015 Google Inc. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Unit tests for pubsub_logging."""


import logging
import multiprocessing as mp
import os
import sys
import time
import unittest

import httplib2
import mock

from apiclient import errors
from oauth2client.client import GoogleCredentials

import pubsub_logging # flake8: noqa

from pubsub_logging.errors import RecoverableError
from pubsub_logging.utils import check_topic
from pubsub_logging.utils import compat_urlsafe_b64encode
from pubsub_logging.utils import get_pubsub_client
from pubsub_logging.utils import publish_body
from pubsub_logging.utils import PUBSUB_SCOPES


DEFAULT_TEST_PROJECT = 'pubsub-integration-test'
TEST_PROJECT_ENV = 'PUBSUB_LOGGING_TEST_PROJECT'


class CompatBase64Test(unittest.TestCase):
    """Test for compat_urlsafe_b64encode function."""

    def test_compat_urlsafe_b64encode(self):
        v = 'test'
        expected = 'dGVzdA=='
        result = compat_urlsafe_b64encode(v)
        self.assertEqual(expected, result)


class GetPubsubClientTest(unittest.TestCase):
    """Tests for utils.get_pubsub_client function.

    You have to set GOOGLE_APPLICATION_CREDENTIALS pointing to the
    json file of the service account.
    """
    RETRY = 3

    def test_get_pubsub_client_with_service_account(self):
        """Tests the client obtained by the service account method."""
        client = get_pubsub_client()
        project = ('projects/%s'
                   % os.environ.get(TEST_PROJECT_ENV, DEFAULT_TEST_PROJECT))
        client.projects().topics().list(project=project).execute(
            num_retries=self.RETRY)
        # Providing an Http object this time.
        self.assertIsNotNone(get_pubsub_client(http=httplib2.Http()))

    def test_get_pubsub_client_with_scoped_credentials(self):
        """Tests the client obtained by scoped credentials."""
        credentials = GoogleCredentials.get_application_default()
        credentials = credentials.create_scoped(PUBSUB_SCOPES)
        self.assertIsNotNone(get_pubsub_client(credentials=credentials))


class CheckTopicTest(unittest.TestCase):
    """Tests for utils.check_topic function.

    You have to set GOOGLE_APPLICATION_CREDENTIALS pointing to the
    json file of the service account.
    """
    RETRY = 3

    def setUp(self):
        self.client = get_pubsub_client()
        self.project = os.environ.get(TEST_PROJECT_ENV, DEFAULT_TEST_PROJECT)
        self.topic = 'projects/%s/topics/test-topic-%f' % (self.project,
                                                           time.time())
        self.nonexistence = 'projects/%s/topics/nonexistence' % self.project
        try:
            self.client.projects().topics().create(
                name=self.topic, body={}).execute()
        except errors.HttpError as e:
            if e.resp.status == 409:
                pass
            else:
                raise

    def tearDown(self):
        self.client.projects().topics().delete(topic=self.topic).execute()

    def test_check_topic(self):
        """Basic test for check_topic."""
        self.assertTrue(check_topic(self.client, self.topic, self.RETRY))

    def test_check_topic_failure(self):
        """Tests if the check_topic raises when getting a 404 error."""
        self.assertFalse(
            check_topic(self.client, self.nonexistence, self.RETRY))


class PublishBodyTest(unittest.TestCase):
    """Tests for utils.publish_body function."""
    RETRY = 3

    def setUp(self):
        self.mocked_client = mock.MagicMock()
        self.topic = 'projects/test-project/topics/test-topic'
        self.projects = self.mocked_client.projects.return_value
        self.topics = self.projects.topics.return_value
        self.topics_publish = self.topics.publish.return_value
        self.log_msg = 'Test message'
        self.expected_payload = compat_urlsafe_b64encode(
            self.log_msg)
        self.expected_body = {'messages': [{'data': self.expected_payload}]}
        self.r = logging.LogRecord('test', logging.INFO, None, 0, self.log_msg,
                                   [], None)

    def publish(self):
        publish_body(self.mocked_client, self.expected_body, self.topic,
                     self.RETRY)

    def test_publish_body(self):
        """Basic test for publish_body."""
        self.publish()
        self.topics.publish.assert_called_once_with(
            topic=self.topic, body=self.expected_body)
        self.topics_publish.execute.assert_called_with(num_retries=self.RETRY)

    def test_publish_body_raise_on_publish_404(self):
        """Tests if the flush method raises when publish gets a 404 error."""
        mocked_resp = mock.MagicMock()
        mocked_resp.status = 404
        mocked_resp.reason = 'Not Found'
        # 404 error
        self.topics_publish.execute.side_effect = [
            errors.HttpError(mocked_resp, 'Not found')
        ]
        self.assertRaises(errors.HttpError, self.publish)

    def test_flush_raise_on_publish_403(self):
        """Tests if the flush method raises when publish gets a 403 error."""
        mocked_resp = mock.MagicMock()
        mocked_resp.status = 403
        mocked_resp.reason = 'Access not allowed'
        # 403 error
        self.topics_publish.execute.side_effect = [
            errors.HttpError(mocked_resp, 'Access not allowed'),
        ]
        self.assertRaises(errors.HttpError, self.publish)

    def test_flush_ignore_recoverable(self):
        """Tests if we raise upon getting 503 error from Cloud Pub/Sub."""
        mocked_resp = mock.MagicMock()
        mocked_resp.status = 503
        mocked_resp.reason = 'Server Error'
        # 503 error
        self.topics_publish.execute.side_effect = [
            errors.HttpError(mocked_resp, 'Server Error'),
        ]
        self.assertRaises(RecoverableError, self.publish)
        self.topics.publish.assert_called_once_with(
            topic=self.topic, body=self.expected_body)
        self.topics_publish.execute.assert_called_once_with(
            num_retries=self.RETRY)


class CountPublishBody(object):
    """A simple counter that counts total number of messages."""
    def __init__(self, mock=None):
        """Initializes this mock.

        Args:
          mock: A mock object that we call before update the counter.
        """
        self.cnt = mp.Value('i', 0)
        self.lock = mp.Lock()
        self._mock = mock

    def __call__(self, client, body, topic, retry):
        if self._mock:
            self._mock(client, body, topic, retry)
        with self.lock:
            self.cnt.value += len(body['messages'])


class AsyncPubsubHandlerTest(unittest.TestCase):
    """Tests for async_handler.AsyncPubsubHandler."""
    RETRY = 10

    def setUp(self):
        self.mocked_client = mock.MagicMock()
        self.topic = 'projects/test-project/topics/test-topic'

    @mock.patch('pubsub_logging.async_handler.check_topic')
    def test_fail_fast_when_topic_not_exist(self, check_topic):
        check_topic.return_value = False
        def create_handler():
            pubsub_logging.AsyncPubsubHandler(topic=self.topic,
                                              client=self.mocked_client,
                                              worker_num=1)
        self.assertRaises(EnvironmentError, create_handler)

    def test_single_message(self):
        """Tests if utils.publish_body is called with one message."""
        self.counter = CountPublishBody()
        self.handler = pubsub_logging.AsyncPubsubHandler(
            topic=self.topic, client=self.mocked_client, retry=self.RETRY,
            worker_num=1, publish_body=self.counter)
        log_msg = 'Test message'
        r = logging.LogRecord('test', logging.CRITICAL, None, 0, log_msg, [],
                              None)
        self.handler.emit(r)
        self.handler.close()
        with self.counter.lock:
            self.assertEqual(1, self.counter.cnt.value)

    def mock_publish_body(self, client, body, topic, retry):
        self.assertEqual(body, self.expected_body)

    def test_formated_message(self):
        """Tests if a formatter assigned to the handler is used."""
        self.log_msg = 'Test message'
        self.expected_payload = compat_urlsafe_b64encode(
            'Test Formatter - test - CRITICAL - ' + self.log_msg)
        self.expected_body = {'messages': [{'data': self.expected_payload}]}
        self.handler = pubsub_logging.AsyncPubsubHandler(
            topic=self.topic, client=self.mocked_client, retry=self.RETRY,
            worker_num=1, publish_body=self.mock_publish_body)
        self.handler.setFormatter(
            logging.Formatter(
                'Test Formatter - %(name)s - %(levelname)s - %(message)s'))
        r = logging.LogRecord('test', logging.CRITICAL, None, 0, self.log_msg, [],
                              None)
        self.handler.emit(r)
        self.handler.close()

    def test_handler_ignores_error(self):
        """Tests if the handler ignores errors and throws the logs away."""
        mock_publish_body = mock.MagicMock()
        mock_publish_body.side_effect = [RecoverableError(), mock.DEFAULT]
        self.counter = CountPublishBody(mock=mock_publish_body)
        # For suppressing the output.
        devnull = logging.Logger('devnull')
        devnull.addHandler(logging.NullHandler())
        self.handler = pubsub_logging.AsyncPubsubHandler(
            topic=self.topic, client=self.mocked_client, retry=self.RETRY,
            worker_num=1, publish_body=self.counter,
            stderr_logger=devnull)
        log_msg = 'Test message'
        r = logging.LogRecord('test', logging.CRITICAL, None, 0, log_msg, [],
                              None)

        # RecoverableError should be ignored, and retried.
        self.handler.emit(r)
        self.handler.close()
        with self.counter.lock:
            self.assertEqual(1, self.counter.cnt.value)

    def test_total_message_count(self):
        """Tests if utils.publish_body is called with 10000 message."""
        self.counter = CountPublishBody()
        self.handler = pubsub_logging.AsyncPubsubHandler(
            topic=self.topic, client=self.mocked_client, retry=self.RETRY,
            worker_num=10, publish_body=self.counter)
        log_msg = 'Test message'
        r = logging.LogRecord('test', logging.CRITICAL, None, 0, log_msg, [],
                              None)
        num = 10000
        for i in range(num):
            self.handler.emit(r)
        self.handler.close()
        with self.counter.lock:
            self.assertEqual(num, self.counter.cnt.value)


class PubsubHandlerTest(unittest.TestCase):
    """Tests for the emit method."""
    RETRY = 3
    BATCH_NUM = 2

    def setUp(self):
        self.mocked_client = mock.MagicMock()
        self.topic = 'projects/test-project/topics/test-topic'
        self.handler = pubsub_logging.PubsubHandler(
            topic=self.topic, client=self.mocked_client, retry=self.RETRY,
            capacity=self.BATCH_NUM)
        self.handler.flush = mock.MagicMock()

    @mock.patch('pubsub_logging.pubsub_handler.check_topic')
    def test_fail_fast_when_topic_not_exist(self, check_topic):
        check_topic.return_value = False
        def create_handler():
            pubsub_logging.PubsubHandler(topic=self.topic,
                                         client=self.mocked_client)
        self.assertRaises(EnvironmentError, create_handler)

    @mock.patch('pubsub_logging.pubsub_handler.get_pubsub_client')
    def test_constructor_without_client(self, get_pubsub_client):
        """Tests if the constructor create a new Pub/Sub client."""
        get_pubsub_client.return_value = self.mocked_client
        handler = pubsub_logging.PubsubHandler(
            topic=self.topic, client=None, retry=self.RETRY,
            capacity=self.BATCH_NUM)
        self.assertEqual(self.mocked_client, handler._client)

    def test_single_buff(self):
        """Tests if the log is stored in the internal buffer."""
        log_msg = 'Test message'
        r = logging.LogRecord('test', logging.INFO, None, 0, log_msg, [], None)

        self.handler.emit(r)
        self.assertEqual(1, len(self.handler.buffer))
        self.assertIs(r, self.handler.buffer[0])

    def test_critical_forces_flush(self):
        """Tests if a single CRITICAL level log forces flushing."""
        log_msg = 'Test message'
        r = logging.LogRecord('test', logging.CRITICAL, None, 0, log_msg, [],
                              None)

        self.handler.emit(r)
        self.handler.flush.assert_called_once()

    def test_custom_level_forces_flush(self):
        """Tests if a single INFO level log forces flushing."""
        self.handler._flush_level = logging.INFO
        log_msg = 'Test message'
        r = logging.LogRecord('test', logging.INFO, None, 0, log_msg, [], None)

        self.handler.emit(r)
        self.handler.flush.assert_called_once()

    def test_flush_when_full(self):
        """Tests if the flush is called when the buffer is full."""
        log_msg1 = 'Test message'
        log_msg2 = 'Test message2'
        r1 = logging.LogRecord('test', logging.INFO, None, 0, log_msg1, [],
                               None)
        r2 = logging.LogRecord('test', logging.INFO, None, 0, log_msg2, [],
                               None)

        self.handler.emit(r1)
        self.handler.flush.assert_not_called()

        self.handler.emit(r2)
        self.handler.flush.assert_called_once()


class PubsubHandlerFlushTest(unittest.TestCase):
    """Tests for the flush method of PubsubHandler."""
    RETRY = 3
    BATCH_NUM = 2

    def setUp(self):
        self.mocked_client = mock.MagicMock()
        self.topic = 'projects/test-project/topics/test-topic'
        self.publish_body = mock.MagicMock()
        self.handler = pubsub_logging.PubsubHandler(
            topic=self.topic, client=self.mocked_client, retry=self.RETRY,
            capacity=self.BATCH_NUM, publish_body=self.publish_body)
        self.log_msg = 'Test message'
        self.expected_payload = compat_urlsafe_b64encode(
            self.log_msg)
        self.expected_body = {'messages': [{'data': self.expected_payload}]}
        self.r = logging.LogRecord('test', logging.INFO, None, 0, self.log_msg,
                                   [], None)

    def test_flush(self):
        """Tests if the flush method calls publish_body."""
        self.handler.emit(self.r)

        self.handler.flush()
        self.publish_body.assert_called_once_with(
            self.mocked_client, self.expected_body, self.topic, self.RETRY)
        self.assertEqual(0, len(self.handler.buffer))

    def test_flush_raise_on_publish_404(self):
        """Tests if the flush raises upon 404 error from publish_body."""
        self.handler.emit(self.r)
        mocked_resp = mock.MagicMock()
        mocked_resp.status = 404
        mocked_resp.reason = 'Not Found'
        # 404 error and None for atexit.
        self.publish_body.side_effect = [
            errors.HttpError(mocked_resp, 'Not found'),
            None]
        self.assertRaises(errors.HttpError, self.handler.flush)

    def test_flush_ignore_recoverable(self):
        """Tests if we ignore Recoverable error from publish_body."""
        self.handler.emit(self.r)
        self.publish_body.side_effect = RecoverableError()
        self.handler.flush()

        self.publish_body.assert_called_once_with(
            self.mocked_client, self.expected_body, self.topic, self.RETRY)
        self.assertEqual(1, len(self.handler.buffer))

    def test_cut_buffer(self):
        """Tests if we cut the buffer upon recoverale errors."""
        self.handler._buf_hard_limit = 0
        self.handler.emit(self.r)
        self.publish_body.side_effect = RecoverableError()
        self.handler.flush()

        self.publish_body.assert_called_once_with(
            self.mocked_client, self.expected_body, self.topic, self.RETRY)
        self.assertEqual(0, len(self.handler.buffer))
