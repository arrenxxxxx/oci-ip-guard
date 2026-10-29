import base64
import hashlib
import hmac
import os
import time
from .common import request


class Notifier:
    def __init__(self):
        self.feishu = os.environ.get('FEISHU_WEBHOOK', '')
        self.signing = os.environ.get('FEISHU_SIGNING_SECRET', '')
        self.telegram = os.environ.get('TELEGRAM_BOT_TOKEN', '')
        self.chat = os.environ.get('TELEGRAM_CHAT_ID', '')
        self.channels = tuple(c for c, enabled in [('feishu', self.feishu),
                            ('telegram', self.telegram and self.chat)] if enabled)
        if self.feishu and not self.feishu.startswith('https://open.feishu.cn/open-apis/bot/v2/hook/'):
            raise ValueError('Unexpected Feishu webhook origin')

    def send(self, channel, message):
        if channel == 'feishu':
            payload = {'msg_type':'text','content':{'text':message}}
            if self.signing:
                ts = str(int(time.time()))
                payload.update(timestamp=ts, sign=base64.b64encode(hmac.new(
                    (ts+'\n'+self.signing).encode(), b'', hashlib.sha256).digest()).decode())
            result = request(self.feishu, payload)
            if result.get('code', result.get('StatusCode')) != 0:
                raise RuntimeError('Feishu rejected notification')
        elif channel == 'telegram':
            result = request('https://api.telegram.org/bot'+self.telegram+'/sendMessage',
                             {'chat_id':self.chat,'text':message})
            if result.get('ok') is not True:
                raise RuntimeError('Telegram rejected notification')

    def drain(self, engine):
        with engine.lock:
            rows = engine.db.execute('''SELECT o.event_id,o.channel,o.attempts,e.ts,e.kind,e.message
                FROM outbox o JOIN events e ON o.event_id=e.id
                WHERE delivered=0 AND next_at<=? ORDER BY event_id LIMIT 4''', (time.time(),)).fetchall()
        for event_id, channel, attempts, ts, kind, message in rows:
            if channel not in self.channels:
                continue
            try:
                self.send(channel, f'[OCI IP Guard] #{event_id} {kind}\n{message}\nUTC {time.strftime("%Y-%m-%d %H:%M:%S",time.gmtime(ts))}')
                delivered = 1
            except Exception as error:
                # Never print exception text: HTTP exceptions can contain webhook/token URLs.
                print(f'notification_error channel={channel} type={type(error).__name__}', flush=True)
                delivered = 0
            with engine.lock:
                engine.db.execute('UPDATE outbox SET delivered=?,attempts=attempts+1,next_at=? WHERE event_id=? AND channel=?',
                    (delivered, time.time()+min(3600,30*2**min(attempts,7)),event_id,channel))
                engine.db.commit()
