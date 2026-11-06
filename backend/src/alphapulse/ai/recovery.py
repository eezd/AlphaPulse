"""跨进程模型接入熔断；只有持有租约的请求可以试探恢复。"""
import json
import logging
from redis.exceptions import RedisError
from alphapulse.core.redis import KEY_PREFIX, redis

# 时间以 Redis 为准。epoch 隔离旧请求；失败窗口只计不同逻辑请求。
SCRIPT = r'''
local raw = redis.call('GET', KEYS[1])
local s = raw and cjson.decode(raw) or {epoch=0, level=0, retry_at=0, failures={}}
local now = tonumber(redis.call('TIME')[1])
local op, id, epoch = ARGV[1], ARGV[2], tonumber(ARGV[3])
local function save()
 redis.call('SET', KEYS[1], cjson.encode(s), 'EX', 86400)
end
local function open()
 s.level = math.min(s.level+1, 3)
 s.retry_at = now + ({180,600,900})[s.level]
 s.epoch = s.epoch+1
 s.owner = nil; s.lease_until = nil; s.failures = {}
end
local admitted = false
if op == 'enter' then
 if s.level == 0 then admitted = true
 elseif now >= s.retry_at and (not s.owner or s.lease_until <= now) then
  s.owner=id; s.lease_until=now+660; admitted=true; save()
 end
elseif epoch == s.epoch then
 if op == 'success' then
  if s.level == 0 or s.owner == id then
   -- 正常成功只清空失败窗口，不使并发中的其他请求失效。
   -- 只有从整体故障恢复才切换代次，隔离旧试探结果。
   local next_epoch = s.epoch
   if s.level > 0 then next_epoch = next_epoch+1 end
   s={epoch=next_epoch,level=0,retry_at=0,failures={}}; save()
  end
 elseif op == 'failure' then
  if s.level > 0 then
   if s.owner == id then open(); save() end
  else
   for key, at in pairs(s.failures) do
    if at <= now-60 then s.failures[key]=nil end
   end
   s.failures[id]=now
   local count=0
   for _ in pairs(s.failures) do count=count+1 end
   if count >= 3 then open() end
   save()
  end
 elseif s.owner == id then
  if op == 'heartbeat' then s.lease_until=now+660
  elseif op == 'release' then s.owner=nil; s.lease_until=nil end
  save()
 end
end
return cjson.encode({epoch=s.epoch,level=s.level, retry_at=s.retry_at,
 probe=s.owner ~= nil, admitted=admitted, now=now})
'''


class Gate:
    def __init__(self, scope):
        self.key = KEY_PREFIX + 'ai:recovery:' + scope

    async def call(self, operation, request_id='', epoch=-1):
        return json.loads(await redis.eval(SCRIPT, 1, self.key, operation, request_id, epoch))


async def mark_waiting(identifier):
    import time
    await redis.zadd(KEY_PREFIX + 'ai:news-waiting', {identifier: time.time()+300})


async def clear_waiting(identifier):
    try:
        await redis.zrem(KEY_PREFIX + 'ai:news-waiting', identifier)
    except RedisError:
        # 展示计数有过期兜底，清理失败不能掩盖原始错误或取消。
        logging.getLogger(__name__).warning('无法清理模型等待计数')


async def waiting_count():
    import time
    await redis.zremrangebyscore(KEY_PREFIX + 'ai:news-waiting', '-inf', time.time())
    return await redis.zcard(KEY_PREFIX + 'ai:news-waiting')
