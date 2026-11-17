import math
from datetime import date

from .store import require, money
from .tariffs import Tariffs

# 评估报告随附的固定说明，防止把描述性证据读成因果结论
REVIEW_NOTES = [
    '订满时段的成交与收款只是需求下限，候补与未成交报价尝试不计为完整需求',
    '价格差异与降雨或赛事同时出现时，对照仅作描述，不作因果断言',
    '历史未覆盖的价格与时段组合不给出预测，只能以显式假设进入方案',
    '同一球友组同一天的跨时段报价按一次选择计，多时段占位不重复计需求',
    '本评估为描述性证据整理，不自动改价，也不替代经营审批',
]

PLAN_NOTES = [
    '预测指标按观测证据或显式假设生成，每条预测在 forecasts 中标明 basis',
    '方案批准前不改动任何现行价；批准后发布价单仍是独立操作',
    '既有有效报价与已确认预约仍按原约定履行，方案不追溯改价',
]

RECON_CAVEAT = '预测净收益属于分析假设；生效价以 price 记录为准，到账以收款记录为准；预测收入不得当作到账收入'

# 对照用的天气赛事组合，按优先顺序尝试匹配
PROFILES = ((False, False), (True, False), (False, True), (True, True))


def _parse_slot(slot_id):
    """从时段编号解析 (日期, 小时)；无法解析时返回 (None, None)。"""
    head, sep, tail = str(slot_id).rpartition('-')
    if sep:
        try:
            hour = int(tail)
            date.fromisoformat(head)
        except ValueError:
            return None, None
        if 0 <= hour <= 23:
            return head, hour
    return None, None


def _mean(total, count):
    return round(total / count, 4) if count else 0.0


def _non_negative_int(value):
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


class Analysis:
    """可审查试价分析：证据评估、试价方案、审批核对与事后对账。

    只读历史记录生成描述性证据；方案不自动改价，批准后仍走 tariffs 的
    原子发布。预测、生效价与到账分别记录，互不混用。
    """

    def __init__(self, store):
        self.store = store

    # ---------- 证据评估 ----------

    def review(self, id, hours=None, slots=None, reason=''):
        require(bool(str(reason).strip()), '需要说明评估事由')
        require(not (hours and slots), '时段筛选只能二选一')
        payload = self._build_review(hours=hours, slots=slots, reason=reason)
        return self.store.put('review', id, payload, create=True)

    def _build_review(self, hours, slots, reason):
        slot_records = {r['id']: r for r in self.store.all('slot')}
        quotes = self.store.all('quote')
        bookings = self.store.all('booking')
        payments = self.store.all('payment')
        pay_by_booking = {p['booking']: p for p in payments}
        bookings_by_quote = {}
        for record in bookings:
            bookings_by_quote.setdefault(record['quote'], []).append(record)
        bookings_by_slot = {}
        for record in bookings:
            bookings_by_slot.setdefault(record['slot'], []).append(record)
        quotes_by_slot = {}
        for record in quotes:
            quotes_by_slot.setdefault(record['slot'], []).append(record)

        occasions = self._choice_occasions(quotes, bookings_by_quote)

        if hours:
            wanted = {str(int(h)) for h in hours}
            chosen = sorted(sid for sid in slot_records
                            if _parse_slot(sid)[1] is not None and str(_parse_slot(sid)[1]) in wanted)
        elif slots:
            for sid in slots:
                require(sid in slot_records, f'不存在的时段: {sid}')
            chosen = sorted(set(slots))
        else:
            chosen = sorted(sid for sid in slot_records if _parse_slot(sid)[1] is not None)

        chosen_set = set(chosen)
        excluded = []
        for sid in sorted(slot_records):
            if _parse_slot(sid)[1] is None:
                excluded.append({'id': sid, 'reason': '时段编号无法解析日期与小时，无法归入小时组'})
            elif sid not in chosen_set:
                excluded.append({'id': sid, 'reason': '不在评估范围'})
        in_scope = [(sid, day, hour) for sid in chosen
                    for day, hour in [_parse_slot(sid)] if hour is not None]

        hour_data = {}
        for sid, day, hour in in_scope:
            facts = self._slot_facts(sid, slot_records[sid], quotes_by_slot.get(sid, []),
                                     bookings_by_slot.get(sid, []), pay_by_booking, occasions)
            bucket = hour_data.setdefault(str(hour), {'slots': [], 'daily': {}, 'mixed_price_slots': [],
                                                      'unknown_price_slots': [], 'price_changed_slots': []})
            bucket['slots'].append(sid)
            if facts['mixed_price']:
                bucket['mixed_price_slots'].append(sid)
                continue
            if facts['price'] is None:
                bucket['unknown_price_slots'].append(sid)
                continue
            if facts['current_price'] is not None and facts['quoted_prices'] \
                    and facts['current_price'] not in facts['quoted_prices']:
                bucket['price_changed_slots'].append(sid)
            cell = bucket['daily'].setdefault(facts['price'], {})
            cell[day] = facts

        hours_payload = {}
        for hour_key in sorted(hour_data, key=lambda h: int(h)):
            data = hour_data[hour_key]
            cells = {str(price): self._summarize_cell(daily)
                     for price, daily in sorted(data['daily'].items())}
            contrasts = []
            prices = sorted(data['daily'])
            for i, low in enumerate(prices):
                for high in prices[i + 1:]:
                    contrasts.append(self._contrast(low, high, data['daily'][low], data['daily'][high]))
            hour_occasions = [o for o in occasions
                              if any(_parse_slot(s)[1] is not None and str(_parse_slot(s)[1]) == hour_key
                                     for s in o['slots'])]
            hours_payload[hour_key] = {
                'slots': sorted(data['slots']),
                'dates': len({day for daily in data['daily'].values() for day in daily}),
                'prices': prices,
                'cells': cells,
                'contrasts': contrasts,
                'occasions': len(hour_occasions),
                'multi_slot_occasions': sum(1 for o in hour_occasions if len(o['slots']) > 1),
                'multi_booking_occasions': sum(1 for o in hour_occasions if o['multi_booking']),
                'switched_occasions': sum(1 for o in hour_occasions
                                          if o['outcome'] == 'fulfilled' and len(o['slots']) > 1),
                'mixed_price_slots': sorted(data['mixed_price_slots']),
                'unknown_price_slots': sorted(data['unknown_price_slots']),
                'price_changed_slots': sorted(data['price_changed_slots']),
            }

        return {
            'reason': reason,
            'scope': {'hours': sorted(hour_data, key=int)},
            'lineage': {'slots': len(slot_records), 'quotes': len(quotes),
                        'bookings': len(bookings), 'payments': len(payments)},
            'hours': hours_payload,
            'excluded_slots': excluded,
            'occasions_total': len(occasions),
            'notes': list(REVIEW_NOTES),
        }

    def _choice_occasions(self, quotes, bookings_by_quote):
        """同一球友组同一天的所有报价合并为一次选择，互斥时段不重复计需求。"""
        groups = {}
        for quote in quotes:
            day, _ = _parse_slot(quote['slot'])
            key = (quote['party'], day if day is not None else f"slot:{quote['slot']}")
            groups.setdefault(key, []).append(quote)
        occasions = []
        for (party, day), owned in sorted(groups.items()):
            records = [b for quote in owned for b in bookings_by_quote.get(quote['id'], [])]
            reserved = [b for b in records if b['state'] == 'reserved']
            waitlisted = [b for b in records if b['state'] == 'waitlist']
            cancelled = [b for b in records if b['state'] == 'cancelled']
            if reserved:
                outcome = 'fulfilled'
            elif waitlisted:
                outcome = 'unmet'
            elif cancelled:
                outcome = 'withdrawn'
            else:
                outcome = 'open'
            occasions.append({
                'party': party,
                'day': day,
                'slots': sorted({q['slot'] for q in owned}),
                'member': any(q['member'] for q in owned),
                'outcome': outcome,
                'fulfilled_slot': min((b['slot'] for b in reserved), default=None),
                'multi_booking': len(reserved) > 1,
                'waitlist_slots': sorted({b['slot'] for b in waitlisted}),
            })
        return occasions

    def _slot_facts(self, sid, slot, slot_quotes, slot_bookings, pay_by_booking, occasions):
        capacity = slot['count'] - slot['blocked']
        quoted_prices = sorted({q['cents'] for q in slot_quotes})
        try:
            current = self.store.get('price', sid)
            revisions = len(self.store.history('price', sid))
        except ValueError:
            current, revisions = None, 0
        mixed_price = len(quoted_prices) > 1
        if len(quoted_prices) == 1:
            price = quoted_prices[0]
        elif mixed_price:
            price = None
        elif current is not None and revisions == 1:
            price = current['cents']
        else:
            price = None
        reserved = [b for b in slot_bookings if b['state'] == 'reserved']
        waitlisted = [b for b in slot_bookings if b['state'] == 'waitlist']
        cancelled = [b for b in slot_bookings if b['state'] == 'cancelled']
        captured = sum(pay_by_booking[b['id']]['cents'] for b in reserved if b['id'] in pay_by_booking)
        refunded = sum(pay_by_booking[b['id']]['refunded'] for b in reserved if b['id'] in pay_by_booking)
        return {
            'price': price,
            'quoted_prices': quoted_prices,
            'current_price': current['cents'] if current else None,
            'mixed_price': mixed_price,
            'capacity': capacity,
            'rain': any(q['rain'] for q in slot_quotes) if slot_quotes else None,
            'event': any(q['event'] for q in slot_quotes) if slot_quotes else None,
            'interested': sum(1 for o in occasions if sid in o['slots']),
            'fulfilled': sum(1 for o in occasions if o['fulfilled_slot'] == sid),
            'unmet': sum(1 for o in occasions if o['outcome'] == 'unmet' and sid in o['waitlist_slots']),
            'reserved': len(reserved),
            'waitlisted': len(waitlisted),
            'cancelled': len(cancelled),
            'captured': captured,
            'refunded': refunded,
            'censored': len(reserved) >= capacity or bool(waitlisted),
        }

    def _summarize_cell(self, daily):
        dates = len(daily)
        entries = list(daily.values())
        return {
            'dates': dates,
            'capacity_min': min(e['capacity'] for e in entries),
            'capacity_max': max(e['capacity'] for e in entries),
            'rain_dates': sum(1 for e in entries if e['rain'] is True),
            'event_dates': sum(1 for e in entries if e['event'] is True),
            'unknown_condition_dates': sum(1 for e in entries if e['rain'] is None),
            'interested_per_date': _mean(sum(e['interested'] for e in entries), dates),
            'fulfilled_per_date': _mean(sum(e['fulfilled'] for e in entries), dates),
            'unmet_per_date': _mean(sum(e['unmet'] for e in entries), dates),
            'reserved_per_date': _mean(sum(e['reserved'] for e in entries), dates),
            'waitlist_per_date': _mean(sum(e['waitlisted'] for e in entries), dates),
            'cancelled_per_date': _mean(sum(e['cancelled'] for e in entries), dates),
            'captured_per_date': _mean(sum(e['captured'] for e in entries), dates),
            'refunded_per_date': _mean(sum(e['refunded'] for e in entries), dates),
            'censored_dates': sum(1 for e in entries if e['censored']),
        }

    def _contrast(self, low, high, daily_low, daily_high):
        for profile in PROFILES:
            matched_low = [e for e in daily_low.values() if (e['rain'], e['event']) == profile]
            matched_high = [e for e in daily_high.values() if (e['rain'], e['event']) == profile]
            if matched_low and matched_high:
                return {
                    'low': low, 'high': high, 'supported': True,
                    'profile': {'rain': profile[0], 'event': profile[1]},
                    'low_dates': len(matched_low), 'high_dates': len(matched_high),
                    'low_interested_per_date': _mean(sum(e['interested'] for e in matched_low), len(matched_low)),
                    'high_interested_per_date': _mean(sum(e['interested'] for e in matched_high), len(matched_high)),
                    'low_fulfilled_per_date': _mean(sum(e['fulfilled'] for e in matched_low), len(matched_low)),
                    'high_fulfilled_per_date': _mean(sum(e['fulfilled'] for e in matched_high), len(matched_high)),
                    'note': '同条件日期的描述性对照，样本有限，不等于因果效应',
                }
        rain_low = {e['rain'] for e in daily_low.values() if e['rain'] is not None}
        rain_high = {e['rain'] for e in daily_high.values() if e['rain'] is not None}
        event_low = {e['event'] for e in daily_low.values() if e['event'] is not None}
        event_high = {e['event'] for e in daily_high.values() if e['event'] is not None}
        confounded = []
        if rain_low and rain_high and rain_low.isdisjoint(rain_high):
            confounded.append('rain')
        if event_low and event_high and event_low.isdisjoint(event_high):
            confounded.append('event')
        if not (rain_low or rain_high) and not (event_low or event_high):
            return {'low': low, 'high': high, 'supported': False, 'confounded_by': ['unknown'],
                    'note': '两档价格的天气赛事条件均未知，无法构造可比对照'}
        if not confounded:
            confounded = ['profile']
            note = '两档价格的天气赛事组合不重叠，无法构造可比对照'
        else:
            note = '价格差异与降雨或赛事同时出现，对照受混淆，不作因果断言'
        return {'low': low, 'high': high, 'supported': False,
                'confounded_by': confounded, 'note': note}

    # ---------- 试价方案 ----------

    def draft(self, id, review, prices, member=None, assumptions=None, reason=''):
        require(bool(str(reason).strip()), '需要说明方案事由')
        require(bool(prices), '试价范围为空')
        review_doc = self.store.get('review', review)
        member = dict(member or {})
        assumptions = dict(assumptions or {})
        reserved_floor = member.get('reserved_floor')
        accessible_floor = member.get('accessible_floor')
        if reserved_floor is not None:
            require(_non_negative_int(reserved_floor), '会员保障容量下限无效')
        if accessible_floor is not None:
            require(_non_negative_int(accessible_floor), '会员可获得时段下限无效')
        require(set(assumptions) <= set(prices), '假设对不上试价时段')

        forecasts = {}
        capacity_snapshot = {}
        floors = {}
        for slot_id, cents in sorted(prices.items()):
            money(cents)
            slot = self.store.get('slot', slot_id)
            day, hour = _parse_slot(slot_id)
            require(hour is not None and str(hour) in review_doc['hours'],
                    f'时段不在评估范围: {slot_id}')
            try:
                current = self.store.get('price', slot_id)
            except ValueError:
                raise ValueError(f'时段尚无现行价，无法比较: {slot_id}')
            cells = review_doc['hours'][str(hour)]['cells']
            capacity = slot['count'] - slot['blocked']
            floor = reserved_floor if reserved_floor is not None else slot['member_reserved']
            require(slot['member_reserved'] >= floor, f'会员保障容量低于承诺下限: {slot_id}')
            floors[slot_id] = floor

            trial_cell = cells.get(str(cents))
            if trial_cell is None:
                asm = assumptions.get(slot_id)
                require(isinstance(asm, dict) and 'demand_per_date' in asm
                        and bool(str(asm.get('note', '')).strip()),
                        f'历史未覆盖的价格时段组合需要显式假设: {slot_id}')
                demand = asm['demand_per_date']
                require(isinstance(demand, (int, float)) and not isinstance(demand, bool)
                        and math.isfinite(demand) and demand >= 0, '假设需求量无效')
                refund_per = asm.get('refund_per_fulfillment', 0)
                require(_non_negative_int(refund_per), '假设退款额无效')
                fulfilled = min(float(demand), float(capacity))
                unmet = max(0.0, float(demand) - capacity)
                net = fulfilled * cents - fulfilled * refund_per
                basis = 'assumption'
                lower_bound = False
            else:
                require(slot_id not in assumptions, f'该组合已有观测依据，不应再填假设: {slot_id}')
                fulfilled = trial_cell['fulfilled_per_date']
                unmet = trial_cell['unmet_per_date']
                net = fulfilled * cents - trial_cell['refunded_per_date']
                basis = 'observed'
                lower_bound = trial_cell['censored_dates'] > 0

            base_cell = cells.get(str(current['cents']))
            if base_cell is not None:
                baseline = {
                    'fulfilled_per_date': base_cell['fulfilled_per_date'],
                    'unmet_per_date': base_cell['unmet_per_date'],
                    'net_cents': int(round(base_cell['fulfilled_per_date'] * current['cents']
                                           - base_cell['refunded_per_date'])),
                }
            else:
                baseline = None

            reserved_now = sum(1 for b in self.store.all('booking')
                               if b['slot'] == slot_id and b['state'] == 'reserved')
            free_now = capacity - reserved_now
            baseline_open = None if baseline is None else capacity - baseline['fulfilled_per_date'] > 0
            trial_open = capacity - fulfilled > 0
            forecasts[slot_id] = {
                'slot_hour': str(hour),
                'trial_price': cents,
                'basis': basis,
                'trial': {
                    'fulfilled_per_date': round(fulfilled, 4),
                    'unmet_per_date': round(unmet, 4),
                    'net_cents': int(round(net)),
                    'lower_bound': lower_bound,
                },
                'baseline_price': current['cents'],
                'baseline': baseline,
                'member': {
                    'floor': floor,
                    'free_now': free_now,
                    'baseline_open': baseline_open,
                    'trial_open': trial_open,
                    'baseline_protected': None if baseline_open is None
                        else baseline_open and slot['member_reserved'] >= floor,
                    'trial_protected': trial_open and slot['member_reserved'] >= floor,
                },
            }
            capacity_snapshot[slot_id] = {
                'count': slot['count'], 'blocked': slot['blocked'],
                'member_reserved': slot['member_reserved'],
                'capacity': capacity, 'free': free_now,
            }

        def total(field):
            values = [f['trial'][field] for f in forecasts.values()]
            return round(sum(values), 4) if field != 'net_cents' else sum(values)

        base_known = all(f['baseline'] is not None for f in forecasts.values())
        baseline_totals = {
            'net_cents': sum(f['baseline']['net_cents'] for f in forecasts.values()) if base_known else None,
            'unmet_per_date': round(sum(f['baseline']['unmet_per_date'] for f in forecasts.values()), 4) if base_known else None,
            'open_slots': sum(1 for f in forecasts.values() if f['member']['baseline_open']) if base_known else None,
            'protected_slots': sum(1 for f in forecasts.values() if f['member']['baseline_protected']) if base_known else None,
        }
        trial_totals = {
            'net_cents': total('net_cents'),
            'unmet_per_date': total('unmet_per_date'),
            'open_slots': sum(1 for f in forecasts.values() if f['member']['trial_open']),
            'protected_slots': sum(1 for f in forecasts.values() if f['member']['trial_protected']),
        }
        if accessible_floor is not None:
            floor_access = accessible_floor
        else:
            floor_access = baseline_totals['protected_slots'] or 0
        require(trial_totals['protected_slots'] >= floor_access, '会员可获得时段低于保障下限')

        delta = None
        if base_known:
            delta = {
                'net_cents': trial_totals['net_cents'] - baseline_totals['net_cents'],
                'unmet_per_date': round(trial_totals['unmet_per_date'] - baseline_totals['unmet_per_date'], 4),
                'open_slots': trial_totals['open_slots'] - baseline_totals['open_slots'],
                'protected_slots': trial_totals['protected_slots'] - baseline_totals['protected_slots'],
            }

        payload = {
            'state': 'draft',
            'review': review,
            'reason': reason,
            'prices': dict(sorted(prices.items())),
            'member': {'reserved_floor': floors, 'accessible_floor': floor_access},
            'assumptions': {sid: assumptions[sid] for sid in sorted(assumptions)},
            'forecasts': forecasts,
            'totals': {
                'baseline': baseline_totals,
                'trial': trial_totals,
                'delta': delta,
                'basis': {
                    'observed': sorted(s for s, f in forecasts.items() if f['basis'] == 'observed'),
                    'assumption': sorted(s for s, f in forecasts.items() if f['basis'] == 'assumption'),
                },
            },
            'capacity': capacity_snapshot,
            'notes': list(PLAN_NOTES),
        }
        return self.store.put('plan', id, payload, create=True)

    def submit(self, id, at=None):
        record = self.store.get('plan', id)
        require(record['state'] == 'draft', '方案状态错误')
        record['state'] = 'submitted'
        if at is not None:
            record['submitted_at'] = at
        return self.store.put('plan', id, record)

    def approve(self, id, tariff, at):
        require(bool(str(at).strip()), '需要审批日期')
        require(bool(str(tariff).strip()), '需要价单编号')
        record = self.store.get('plan', id)
        require(record['state'] == 'submitted', '方案状态错误')
        changed = []
        weakened = []
        for slot_id, snap in record['capacity'].items():
            slot = self.store.get('slot', slot_id)
            if slot['count'] - slot['blocked'] != snap['capacity'] \
                    or slot['member_reserved'] != snap['member_reserved']:
                changed.append(slot_id)
            if slot['member_reserved'] < record['member']['reserved_floor'][slot_id]:
                weakened.append(slot_id)
        require(not changed, f'审批前容量或会员保障已变化，需重新评估方案: {sorted(changed)}')
        require(not weakened, f'会员保障容量低于方案承诺: {sorted(weakened)}')
        targets = set(record['prices'])
        honored_quotes = [q for q in self.store.all('quote')
                          if q['slot'] in targets and str(q['expires']) >= str(at)]
        honored_bookings = [b for b in self.store.all('booking')
                            if b['slot'] in targets and b['state'] == 'reserved']
        Tariffs(self.store).propose(tariff, record['prices'],
                                    f'试价方案{id}批准后生成，发布仍为独立操作')
        record['state'] = 'approved'
        record['approval'] = {
            'at': at,
            'tariff': tariff,
            'capacity_recheck': 'passed',
            'honored_quotes': len(honored_quotes),
            'honored_quote_cents': sum(q['cents'] for q in honored_quotes),
            'honored_bookings': len(honored_bookings),
            'honored_booking_cents': sum(b['cents'] for b in honored_bookings),
        }
        return self.store.put('plan', id, record)

    def reject(self, id, reason=''):
        require(bool(str(reason).strip()), '需要说明驳回原因')
        record = self.store.get('plan', id)
        require(record['state'] == 'submitted', '方案状态错误')
        record['state'] = 'rejected'
        record['rejection'] = reason
        return self.store.put('plan', id, record)

    # ---------- 事后对账 ----------

    def reconcile(self, id, plan):
        record = self.store.get('plan', plan)
        require(record['state'] == 'approved', '方案未批准，无生效价可核对')
        tariff_id = record['approval']['tariff']
        bookings = {b['id']: b for b in self.store.all('booking')}
        payments = self.store.all('payment')
        slots = {}
        totals = {'forecast_net_cents': 0, 'before_net_cents': 0, 'after_net_cents': 0}
        for slot_id in sorted(record['prices']):
            price = self.store.get('price', slot_id)
            history = self.store.history('price', slot_id)
            sequences = [h['sequence'] for h in history if h['payload'].get('tariff') == tariff_id]
            cut = min(sequences) if sequences else None
            before = {'count': 0, 'net_cents': 0}
            after = {'count': 0, 'net_cents': 0, 'honored_old_count': 0, 'honored_old_net_cents': 0}
            for pay in payments:
                booking = bookings.get(pay['booking'])
                if booking is None or booking['slot'] != slot_id:
                    continue
                sequence = self.store.history('payment', pay['id'])[0]['sequence']
                net = pay['cents'] - pay['refunded']
                if cut is not None and sequence > cut:
                    after['count'] += 1
                    after['net_cents'] += net
                    if pay['cents'] != price['cents']:
                        after['honored_old_count'] += 1
                        after['honored_old_net_cents'] += net
                else:
                    before['count'] += 1
                    before['net_cents'] += net
            forecast = record['forecasts'][slot_id]
            floor = record['member']['reserved_floor'][slot_id]
            slot = self.store.get('slot', slot_id)
            slots[slot_id] = {
                'effective_price': {'cents': price['cents'], 'tariff': price['tariff'],
                                    'matches_plan': price['tariff'] == tariff_id},
                'plan_tariff_published': cut is not None,
                'forecast_net_cents': forecast['trial']['net_cents'],
                'forecast_basis': forecast['basis'],
                'payments_before': before,
                'payments_after': after,
                'member_reserved': {'current': slot['member_reserved'], 'floor': floor,
                                    'ok': slot['member_reserved'] >= floor},
            }
            totals['forecast_net_cents'] += forecast['trial']['net_cents']
            totals['before_net_cents'] += before['net_cents']
            totals['after_net_cents'] += after['net_cents']
        payload = {
            'plan': plan,
            'tariff': tariff_id,
            'slots': slots,
            'totals': totals,
            'caveat': RECON_CAVEAT,
        }
        return self.store.put('recon', id, payload)
