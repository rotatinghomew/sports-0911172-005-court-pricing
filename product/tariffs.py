from .store import require, money

class Tariffs:
    def __init__(self, store):
        self.store = store

    def propose(self, id, prices, reason):
        require(bool(prices) and bool(reason),'价单为空')
        for slot, cents in prices.items():
            self.store.get('slot',slot)
            money(cents)
        return self.store.put('tariff',id,dict(prices=prices,reason=reason,state='draft'),create=True)

    def publish(self, id):
        with self.store.atomic():
            record = self.store.get('tariff',id)
            if record['state']=='published':
                return record
            require(record['state']=='draft','价单状态错误')
            for slot,cents in record['prices'].items():
                self.store.put('price',slot,dict(cents=cents,tariff=id))
            record['state']='published'
            return self.store.put('tariff',id,record)
