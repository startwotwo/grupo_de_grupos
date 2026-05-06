import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

import unittest
import time
from broker import Broker
from common.models import Message, ControlMessageType


class TestBroker(unittest.TestCase):
    def setUp(self):
        self.broker = Broker(
            broker_id="test_broker",
            text_port=15556,
            control_port=15559,
            heartbeat_port=15561
        )
        self.broker.start()
        time.sleep(0.5)

    def tearDown(self):
        self.broker.stop()
        time.sleep(0.5)

    def test_broker_starts(self):
        self.assertTrue(self.broker.running)
        self.assertEqual(self.broker.broker_id, "test_broker")

    def test_broker_has_groups(self):
        self.assertEqual(len(self.broker.groups), 11)
        self.assertIn('A', self.broker.groups)
        self.assertIn('K', self.broker.groups)

    def test_message_cache_initialization(self):
        self.assertEqual(len(self.broker.message_cache), 0)


if __name__ == '__main__':
    unittest.main()
