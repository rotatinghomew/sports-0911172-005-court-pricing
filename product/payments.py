from .store import require, money

class Payments:
    def __init__(self, store):
        self.store = store

    def capture(self, id, booking):
        original=self.store.get('booking',booking)
        require(original['state']=='reserved','未预约成功')
        require(not any(item['booking']==booking for item in self.store.all('payment')),'已经收款')
        return self.store.put('payment',id,dict(booking=booking,cents=original['cents'],refunded=0),create=True)

    def refund(self, id, cents):
        record=self.store.get('payment',id)
        require(record['refunded']+money(cents)<=record['cents'],'退款超额')
        record['refunded']+=cents
        return self.store.put('payment',id,record)
