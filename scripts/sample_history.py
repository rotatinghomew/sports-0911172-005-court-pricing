import random
import json
from datetime import date, timedelta

def generate():
    rng = random.Random(117205)
    operations = []

    def add(command, **arguments):
        operations.append([command, arguments])

    def day(offset):
        return (date(2026, 7, 1) + timedelta(days=offset)).isoformat()
    for offset in range(36):
        slots = [f'{day(offset)}-{hour}' for hour in [18, 19, 20]]
        for key in slots:
            add('capacity.open', id=key, count=3, member_reserved=1)
        add('tariffs.propose', id=f't{offset}', prices={key: 8000 + 2000 * (offset % 3) for key in slots}, reason='人工周期价')
        add('tariffs.publish', id=f't{offset}')
        sold = {key: 0 for key in slots}
        for index in range(14):
            quote = f'q{offset}:{index}'
            add('booking.quote', id=quote, person=f'p{index % 60}', slot=slots[index % 3], expires=day(offset + 1), party=f'g{index // 2}', rain=offset % 6 == 0, event=offset % 7 == 3, member=index % 4 == 0)
            if rng.random() < 0.8:
                booking = f'b{offset}:{index}'
                add('booking.reserve', id=booking, quote=quote, at=day(offset))
                if sold[slots[index % 3]] < 3:
                    add('payments.capture', id=f'pay{offset}:{index}', booking=booking)
                    sold[slots[index % 3]] += 1
                    if index % 11 == 0:
                        add('payments.refund', id=f'pay{offset}:{index}', cents=2000)
        add('capacity.block', id=slots[0], count=0, reason='营业前核验')
    return operations

def main():
    import argparse
    parser = argparse.ArgumentParser(description='生成本地合成业务记录，不包含真实个人资料')
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    from pathlib import Path
    Path(args.output).write_text(json.dumps(generate(), ensure_ascii=False, indent=2) + '\n')

if __name__=='__main__':
    main()
