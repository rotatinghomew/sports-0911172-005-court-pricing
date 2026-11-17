from .store import require, money

from .capacity import Capacity

class Booking:
    def __init__(self, store):
        self.store = store

    def quote(self, id, person, slot, expires, party, rain=False, event=False, member=False):
        price = self.store.get('price',slot)
        return self.store.put('quote',id,dict(person=person,slot=slot,expires=expires,
            cents=price['cents'],tariff=price['tariff'],party=party,rain=rain,event=event,member=member),create=True)

    def reserve(self, id, quote, at):
        with self.store.atomic():
            original = self.store.get('quote',quote)
            require(at<=original['expires'],'报价已过期')
            require(not any(item['quote']==quote and item['state']=='reserved' for item in self.store.all('booking')),'报价已使用')
            free = Capacity(self.store).free(original['slot'])['free']
            return self.store.put('booking',id,dict(quote=quote,person=original['person'],
                slot=original['slot'],cents=original['cents'],at=at,
                state='reserved' if free>0 else 'waitlist'),create=True)

    def cancel(self, id):
        record=self.store.get('booking',id)
        require(record['state'] in ('reserved','waitlist'),'不能取消')
        record['state']='cancelled'
        return self.store.put('booking',id,record)
