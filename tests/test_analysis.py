import json
import unittest
from pathlib import Path
from unittest import mock

from product.app import Application

DATA = Path(__file__).parent.parent / 'data'


def load(name):
    return json.loads((DATA / name).read_text())


class HistoryEvidenceTests(unittest.TestCase):
    """基于本地合成历史的证据评估：覆盖、截断、混淆与选择去重。"""

    def setUp(self):
        self.app = Application()
        self.app.replay(load('history.json'))
        self.review = self.app.execute(
            'analysis.review', {'id': 'rev1', 'hours': [18, 19, 20], 'reason': '晚间涨价评估'})

    def tearDown(self):
        self.app.store.close()

    def test_prices_and_cells_cover_only_observed_combinations(self):
        for hour in ('18', '19', '20'):
            group = self.review['hours'][hour]
            self.assertEqual(group['prices'], [8000, 10000, 12000])
            self.assertEqual(sorted(group['cells']), ['10000', '12000', '8000'])
            self.assertEqual(group['dates'], 36)

    def test_sold_out_slots_marked_as_censored_lower_bound(self):
        cell = self.review['hours']['18']['cells']['8000']
        self.assertGreater(cell['censored_dates'], 0)
        self.assertGreater(cell['waitlist_per_date'], 0)
        self.assertLessEqual(cell['fulfilled_per_date'], cell['capacity_max'])

    def test_rain_only_observed_at_lowest_price_is_visible(self):
        cells = self.review['hours']['18']['cells']
        self.assertGreater(cells['8000']['rain_dates'], 0)
        self.assertEqual(cells['10000']['rain_dates'], 0)
        self.assertEqual(cells['12000']['rain_dates'], 0)
        # 仍存在无雨可比日期，对照成立但仅作描述
        for contrast in self.review['hours']['18']['contrasts']:
            self.assertTrue(contrast['supported'])
            self.assertEqual(contrast['profile'], {'rain': False, 'event': False})

    def test_same_party_same_day_counts_as_one_choice(self):
        # 504 条报价合并为 252 次选择（7 组球友 × 36 天），跨时段占位不重复计
        self.assertEqual(self.review['lineage']['quotes'], 504)
        self.assertEqual(self.review['occasions_total'], 252)
        group = self.review['hours']['18']
        self.assertEqual(group['multi_slot_occasions'], group['occasions'])
        self.assertGreater(group['multi_booking_occasions'], 0)
        self.assertGreater(group['switched_occasions'], 0)

    def test_review_is_deterministic_for_same_state(self):
        again = self.app.execute(
            'analysis.review', {'id': 'rev2', 'hours': [18, 19, 20], 'reason': '晚间涨价评估'})
        self.assertEqual({k: v for k, v in again.items() if k not in ('id', 'revision')},
                         {k: v for k, v in self.review.items() if k not in ('id', 'revision')})

    def test_duplicate_review_id_rejected(self):
        with self.assertRaises(ValueError):
            self.app.execute(
                'analysis.review', {'id': 'rev1', 'hours': [18], 'reason': '重复编号'})


class ConfoundingAndSwitchTests(unittest.TestCase):
    """手工构造的小样本：混淆对照不可断言，换时段不算新增客流。"""

    def setUp(self):
        self.app = Application()

    def tearDown(self):
        self.app.store.close()

    def test_price_drop_only_on_rainy_days_is_confounded(self):
        self.app.replay([
            ['capacity.open', {'id': '2027-02-01-18', 'count': 2}],
            ['capacity.open', {'id': '2027-02-02-18', 'count': 2}],
            ['tariffs.propose', {'id': 'ta', 'prices': {'2027-02-01-18': 8000}, 'reason': '雨天价'}],
            ['tariffs.publish', {'id': 'ta'}],
            ['tariffs.propose', {'id': 'tb', 'prices': {'2027-02-02-18': 10000}, 'reason': '常规价'}],
            ['tariffs.publish', {'id': 'tb'}],
            ['booking.quote', {'id': 'q1', 'person': 'p1', 'slot': '2027-02-01-18',
                               'expires': '2027-02-02', 'party': 'g1', 'rain': True}],
            ['booking.quote', {'id': 'q2', 'person': 'p2', 'slot': '2027-02-01-18',
                               'expires': '2027-02-02', 'party': 'g2', 'rain': True}],
            ['booking.quote', {'id': 'q3', 'person': 'p3', 'slot': '2027-02-02-18',
                               'expires': '2027-02-03', 'party': 'g3'}],
            ['booking.quote', {'id': 'q4', 'person': 'p4', 'slot': '2027-02-02-18',
                               'expires': '2027-02-03', 'party': 'g4'}],
        ])
        review = self.app.execute('analysis.review', {'id': 'r', 'hours': [18], 'reason': '核查'})
        (contrast,) = review['hours']['18']['contrasts']
        self.assertFalse(contrast['supported'])
        self.assertIn('rain', contrast['confounded_by'])

    def test_party_switching_slots_is_one_choice_not_new_demand(self):
        self.app.replay([
            ['capacity.open', {'id': '2027-01-01-18', 'count': 2, 'member_reserved': 1}],
            ['capacity.open', {'id': '2027-01-01-19', 'count': 2, 'member_reserved': 1}],
            ['tariffs.propose', {'id': 't1', 'prices': {'2027-01-01-18': 8000, '2027-01-01-19': 8000},
                                 'reason': '常规价'}],
            ['tariffs.publish', {'id': 't1'}],
            ['booking.quote', {'id': 'qW1', 'person': 'pW', 'slot': '2027-01-01-18',
                               'expires': '2027-01-02', 'party': 'gW'}],
            ['booking.quote', {'id': 'qW2', 'person': 'pW', 'slot': '2027-01-01-19',
                               'expires': '2027-01-02', 'party': 'gW'}],
            ['booking.reserve', {'id': 'bW1', 'quote': 'qW1', 'at': '2027-01-01'}],
            ['booking.cancel', {'id': 'bW1'}],
            ['booking.reserve', {'id': 'bW2', 'quote': 'qW2', 'at': '2027-01-01'}],
        ])
        review = self.app.execute('analysis.review', {'id': 'r', 'hours': [18, 19], 'reason': '核查'})
        self.assertEqual(review['occasions_total'], 1)
        # 同一组球友从 18 点换到 19 点：18 点有兴趣无成交，19 点计一次成交，总计一次需求
        self.assertEqual(review['hours']['18']['cells']['8000']['interested_per_date'], 1)
        self.assertEqual(review['hours']['18']['cells']['8000']['fulfilled_per_date'], 0)
        self.assertEqual(review['hours']['19']['cells']['8000']['fulfilled_per_date'], 1)

    def test_unparseable_slot_id_is_excluded_with_reason(self):
        self.app.replay(load('demo.json'))
        review = self.app.execute('analysis.review', {'id': 'r', 'reason': '核查'})
        self.assertEqual(review['hours'], {})
        (excluded,) = review['excluded_slots']
        self.assertEqual(excluded['id'], '19')
        self.assertIn('无法解析', excluded['reason'])


class PlanFlowTests(unittest.TestCase):
    """试价方案：假设纪律、会员保障、审批前重核与事后对账。"""

    def setUp(self):
        self.app = Application()
        self.ops = load('trial-demo.json')
        self.submit_at = next(i for i, (command, _) in enumerate(self.ops)
                              if command == 'analysis.submit')
        self.app.replay(self.ops[:self.submit_at + 1])

    def tearDown(self):
        self.app.store.close()

    def replay_rest(self):
        self.app.replay(self.ops[self.submit_at + 1:])

    def test_full_walkthrough_matches_hand_computed_figures(self):
        self.replay_rest()
        review = self.app.store.get('review', 'rev1')
        cell = review['hours']['18']['cells']['10000']
        self.assertEqual(cell['dates'], 3)
        self.assertEqual(cell['rain_dates'], 1)
        self.assertEqual(cell['fulfilled_per_date'], 1.6667)
        self.assertEqual(cell['censored_dates'], 1)
        plan = self.app.store.get('plan', 'plan1')
        self.assertEqual(plan['state'], 'approved')
        self.assertEqual(plan['totals']['trial']['net_cents'], 62000)
        self.assertEqual(plan['totals']['baseline']['net_cents'], 23000)
        self.assertEqual(plan['totals']['basis']['observed'], ['2026-12-04-18', '2026-12-04-19'])
        self.assertEqual(plan['totals']['basis']['assumption'], ['2026-12-04-20'])
        self.assertEqual(plan['approval']['honored_quotes'], 2)
        self.assertEqual(plan['approval']['honored_bookings'], 1)
        recon = self.app.store.get('recon', 'recon1')
        slot18 = recon['slots']['2026-12-04-18']
        self.assertTrue(slot18['effective_price']['matches_plan'])
        self.assertEqual(slot18['payments_before'], {'count': 1, 'net_cents': 10000})
        self.assertEqual(slot18['payments_after']['net_cents'], 12000)
        slot19 = recon['slots']['2026-12-04-19']
        # 审批前发出的有效报价仍按原价收款，不计入新价收入
        self.assertEqual(slot19['payments_after']['honored_old_net_cents'], 10000)
        self.assertEqual(recon['totals']['forecast_net_cents'], 62000)
        self.assertEqual(recon['totals']['after_net_cents'], 22000)
        self.assertIn('预测收入不得当作到账收入', recon['caveat'])

    def test_reconcile_never_writes_payments(self):
        self.replay_rest()
        before = self.app.store.snapshot()
        self.app.execute('analysis.reconcile', {'id': 'recon2', 'plan': 'plan1'})
        after = self.app.store.snapshot()
        self.assertEqual(before['payment'], after['payment'])
        self.assertEqual(before['price'], after['price'])

    def test_uncovered_price_requires_explicit_assumption(self):
        app = Application()
        try:
            app.replay(self.ops[:self.submit_at - 1])
            with self.assertRaisesRegex(ValueError, '显式假设'):
                app.execute('analysis.draft', {'id': 'px', 'review': 'rev1',
                                               'prices': {'2026-12-04-18': 14000}, 'reason': '试涨'})
        finally:
            app.store.close()

    def test_assumption_rejected_when_combination_already_observed(self):
        with self.assertRaisesRegex(ValueError, '不应再填假设'):
            self.app.execute('analysis.draft', {'id': 'px', 'review': 'rev1',
                                                'prices': {'2026-12-04-18': 12000},
                                                'assumptions': {'2026-12-04-18': {
                                                    'demand_per_date': 1, 'note': '多余'}},
                                                'reason': '试涨'})

    def test_assumption_must_match_plan_slots(self):
        with self.assertRaisesRegex(ValueError, '对不上试价时段'):
            self.app.execute('analysis.draft', {'id': 'px', 'review': 'rev1',
                                                'prices': {'2026-12-04-18': 12000},
                                                'assumptions': {'2026-12-04-20': {
                                                    'demand_per_date': 1, 'note': '无关'}},
                                                'reason': '试涨'})

    def test_capacity_change_before_approval_blocks_it(self):
        self.app.execute('capacity.block', {'id': '2026-12-04-20', 'count': 1, 'reason': '包场'})
        with self.assertRaisesRegex(ValueError, '容量或会员保障已变化'):
            self.app.execute('analysis.approve', {'id': 'plan1', 'tariff': 'eve-d4', 'at': '2026-12-03'})
        self.assertEqual(self.app.store.get('plan', 'plan1')['state'], 'submitted')
        # 容量恢复后与方案快照一致，可继续审批
        self.app.execute('capacity.block', {'id': '2026-12-04-20', 'count': 0, 'reason': '包场取消'})
        approved = self.app.execute('analysis.approve',
                                    {'id': 'plan1', 'tariff': 'eve-d4', 'at': '2026-12-03'})
        self.assertEqual(approved['state'], 'approved')

    def test_approval_creates_draft_tariff_without_changing_prices(self):
        before = self.app.store.snapshot()['price']
        self.app.execute('analysis.approve', {'id': 'plan1', 'tariff': 'eve-d4', 'at': '2026-12-03'})
        self.assertEqual(self.app.store.snapshot()['price'], before)
        tariff = self.app.store.get('tariff', 'eve-d4')
        self.assertEqual(tariff['state'], 'draft')
        self.assertEqual(tariff['prices'], {'2026-12-04-18': 12000, '2026-12-04-19': 12000,
                                            '2026-12-04-20': 13000})

    def test_approve_requires_submitted_state(self):
        app = Application()
        try:
            app.replay(self.ops[:self.submit_at])
            with self.assertRaisesRegex(ValueError, '方案状态错误'):
                app.execute('analysis.approve', {'id': 'plan1', 'tariff': 'eve-d4', 'at': '2026-12-03'})
        finally:
            app.store.close()

    def test_reject_flow(self):
        rejected = self.app.execute('analysis.reject', {'id': 'plan1', 'reason': '暂缓涨价'})
        self.assertEqual(rejected['state'], 'rejected')
        with self.assertRaisesRegex(ValueError, '方案状态错误'):
            self.app.execute('analysis.approve', {'id': 'plan1', 'tariff': 'eve-d4', 'at': '2026-12-03'})

    def test_member_protection_floor_enforced(self):
        with self.assertRaisesRegex(ValueError, '会员可获得时段低于保障下限'):
            self.app.execute('analysis.draft', {'id': 'px', 'review': 'rev1',
                                                'prices': {'2026-12-04-18': 12000},
                                                'member': {'accessible_floor': 2},
                                                'reason': '试涨'})
        with self.assertRaisesRegex(ValueError, '会员保障容量低于承诺下限'):
            self.app.execute('analysis.draft', {'id': 'py', 'review': 'rev1',
                                                'prices': {'2026-12-04-18': 12000},
                                                'member': {'reserved_floor': 2},
                                                'reason': '试涨'})

    def test_slot_outside_review_scope_rejected(self):
        self.app.execute('capacity.open', {'id': '2026-12-05-21', 'count': 2})
        self.app.execute('tariffs.propose', {'id': 't21', 'prices': {'2026-12-05-21': 9000},
                                             'reason': '常规价'})
        self.app.execute('tariffs.publish', {'id': 't21'})
        with self.assertRaisesRegex(ValueError, '不在评估范围'):
            self.app.execute('analysis.draft', {'id': 'px', 'review': 'rev1',
                                                'prices': {'2026-12-05-21': 10000}, 'reason': '试涨'})

    def test_reconcile_requires_approved_plan(self):
        with self.assertRaisesRegex(ValueError, '方案未批准'):
            self.app.execute('analysis.reconcile', {'id': 'rc', 'plan': 'plan1'})


class AtomicPublishTests(unittest.TestCase):
    """发布中断重试不得留下半套价。"""

    def setUp(self):
        self.app = Application()
        self.app.replay([
            ['capacity.open', {'id': '2027-03-01-18', 'count': 2}],
            ['capacity.open', {'id': '2027-03-01-19', 'count': 2}],
            ['capacity.open', {'id': '2027-03-01-20', 'count': 2}],
            ['tariffs.propose', {'id': 't0', 'prices': {'2027-03-01-18': 8000, '2027-03-01-19': 8000,
                                                        '2027-03-01-20': 8000}, 'reason': '常规价'}],
            ['tariffs.publish', {'id': 't0'}],
            ['tariffs.propose', {'id': 't1', 'prices': {'2027-03-01-18': 9000, '2027-03-01-19': 9000,
                                                        '2027-03-01-20': 9000}, 'reason': '试价'}],
        ])

    def tearDown(self):
        self.app.store.close()

    def test_interrupted_publish_leaves_no_partial_prices(self):
        before = self.app.store.snapshot()['price']
        original_put = self.app.store.put
        calls = []

        def flaky(kind, key, value, create=False):
            if kind == 'price':
                calls.append(key)
                if len(calls) == 2:
                    raise RuntimeError('模拟发布中断')
            return original_put(kind, key, value, create=create)

        with mock.patch.object(self.app.store, 'put', side_effect=flaky):
            with self.assertRaises(RuntimeError):
                self.app.execute('tariffs.publish', {'id': 't1'})
        self.assertEqual(self.app.store.snapshot()['price'], before)
        self.assertEqual(self.app.store.get('tariff', 't1')['state'], 'draft')
        # 重试后整套生效，价格版本一致
        self.app.execute('tariffs.publish', {'id': 't1'})
        prices = self.app.store.snapshot()['price']
        self.assertEqual({p['cents'] for p in prices}, {9000})
        self.assertEqual({p['tariff'] for p in prices}, {'t1'})
        again = self.app.execute('tariffs.publish', {'id': 't1'})
        self.assertEqual(again['state'], 'published')


if __name__ == '__main__':
    unittest.main()
