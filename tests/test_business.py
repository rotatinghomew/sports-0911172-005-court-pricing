import json
import unittest
from pathlib import Path
from product.app import Application


class BusinessBehaviorTests(unittest.TestCase):
    def setUp(self):
        self.app=Application()
        self.app.replay(json.loads((Path(__file__).parent.parent/'data'/'demo.json').read_text()))

    def tearDown(self):
        self.app.store.close()
    def test_new_price_does_not_change_valid_quote(self):
        self.assertEqual(self.app.store.get('price','19')['cents'],12000)
        self.assertEqual(self.app.store.get('quote','q1')['cents'],10000)

    def test_publish_retries_keep_price_version(self):
        before=self.app.store.get('price','19')
        self.app.execute('tariffs.publish',dict(id='new'))
        self.assertEqual(self.app.store.get('price','19'),before)
