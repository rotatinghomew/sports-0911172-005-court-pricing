from .store import require, money

class Capacity:
    def __init__(self, store):
        self.store = store

    def open(self, id, count, member_reserved=0):
        require(0<=member_reserved<=count,'保留容量无效')
        return self.store.put('slot',id,dict(count=count,blocked=0,member_reserved=member_reserved),create=True)

    def block(self, id, count, reason):
        record = self.store.get('slot',id)
        used = sum(item['state']=='reserved' and item['slot']==id for item in self.store.all('booking'))
        require(count>=0 and used+count<=record['count'],'维护与已承诺容量冲突')
        record.update(blocked=count,reason=reason)
        return self.store.put('slot',id,record)

    def free(self, id):
        record = self.store.get('slot',id)
        used = sum(item['state']=='reserved' and item['slot']==id for item in self.store.all('booking'))
        return dict(free=record['count']-record['blocked']-used,member_reserved=record['member_reserved'])
