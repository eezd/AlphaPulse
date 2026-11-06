"""在明确指定的隔离 Redis 上验证接入恢复与租约互斥。"""
import asyncio
import os
import uuid

def main():
    url = os.environ['AI_RECOVERY_TEST_REDIS_URL']
    from redis.asyncio import Redis
    from alphapulse.ai.recovery import SCRIPT

    async def run():
        redis = Redis.from_url(url, decode_responses=True)
        import json
        key = 'ap:ai:test:' + uuid.uuid4().hex
        # 仅替换时钟，实际执行生产 Lua，检查跨客户端的租约互斥。
        script = SCRIPT.replace("tonumber(redis.call('TIME')[1])", 'tonumber(ARGV[4])')
        async def call(op, identity='', epoch=-1, now=1000):
            return json.loads(await redis.eval(script, 1, key, op, identity, epoch, now))
        try:
            for _ in range(4):
                assert (await call('failure', 'same', 0))['level'] == 0
            # 四个并发请求先成功一个，其后其他三个失败必须正常计数。
            admissions = [await call('enter', name) for name in ('good', 'a', 'b', 'c')]
            epoch = admissions[0]['epoch']
            await call('success', 'good', epoch)
            assert (await call('read'))['epoch'] == epoch
            await call('failure', 'a', epoch, 1000)
            await call('failure', 'b', epoch, 1001)
            opened = await call('failure', 'c', epoch, 1002)
            assert opened['level'] == 1 and opened['retry_at'] == 1182
            assert not (await call('enter', 'early', now=1181))['admitted']
            claims = await asyncio.gather(*(call('enter', str(i), now=1182) for i in range(20)))
            assert sum(s['admitted'] for s in claims) == 1
            winner = str(next(i for i, s in enumerate(claims) if s['admitted']))
            epoch = claims[int(winner)]['epoch']
            # 旧失败不推进；失效的并发成功不解除当前故障。
            assert (await call('failure', 'late', epoch-1, 1183))['level'] == 1
            assert (await call('success', 'late', epoch-1, 1183))['level'] == 1
            second = await call('failure', winner, epoch, 1183)
            assert second['level'] == 2 and second['retry_at'] == 1783
            third = await call('enter', 'probe2', now=1783)
            state = await call('failure', 'probe2', third['epoch'], 1784)
            assert state['level'] == 3 and state['retry_at'] == 2684
            third = await call('enter', 'probe3', now=2684)
            state = await call('failure', 'probe3', third['epoch'], 2685)
            assert state['retry_at'] == 3585
            probe = await call('enter', 'crashed', now=3585)
            await call('heartbeat', 'crashed', probe['epoch'], 4000)
            assert not (await call('enter', 'other', now=4200))['admitted']
            recovered = await call('enter', 'other', now=4661)
            assert recovered['admitted']
            assert (await call('success', 'other', recovered['epoch'], 4662))['level'] == 0
            # 窗口边界：过期失败不积累，显式释放试探不冒充恢复成功。
            epoch = (await call('read'))['epoch']
            await call('failure', 'a', epoch, 5000)
            await call('failure', 'b', epoch, 5061)
            assert (await call('failure', 'c', epoch, 5062))['level'] == 0
            await call('failure', 'd', epoch, 5063)
            probe = await call('enter', 'cancelled', now=5243)
            assert probe['admitted']
            await call('release', 'cancelled', probe['epoch'], 5244)
            assert (await call('enter', 'replacement', now=5244))['admitted']
            # 另一个接入不受此故障影响。
            independent = await redis.eval(script, 1, key+'-other', 'enter', 'fresh', -1, 5244)
            assert json.loads(independent)['admitted']
        finally:
            await redis.delete(key)
            await redis.aclose()
    asyncio.run(run())



if __name__ == "__main__":
    main()
    print("AI recovery atomic acceptance passed")
