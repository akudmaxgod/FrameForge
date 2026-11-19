"""One-shot paid-pass consent. No answer always resolves to the local model."""
import threading,time,uuid
from .domain import DomainError

class PaidPassPermit:
    def __init__(self,context):self.context=context;self.used=False;self.lock=threading.Lock()
    def consume(self,context):
        with self.lock:
            if self.used or context!=self.context:return False
            self.used=True;return True

class PaidPassGate:
    def __init__(self,timeout=60):self.timeout=timeout;self.items={};self.lock=threading.RLock()
    def request(self,scope,context,label):
        ident=uuid.uuid4().hex;event=threading.Event();item={'id':ident,'scope':scope,'context':context,'label':label,'deadline':time.monotonic()+self.timeout,'expires_at':time.time()+self.timeout,'event':event,'decision':None}
        with self.lock:self.items[ident]=item
        event.wait(self.timeout)
        with self.lock:
            decision=item['decision'] or 'local';self.items.pop(ident,None)
        return PaidPassPermit(context) if decision=='deepseek' else None
    def pending(self,scope):
        with self.lock:
            result=[]
            for item in self.items.values():
                if item['scope']!=scope:continue
                if time.monotonic()>=item['deadline'] and item['decision'] is None:item['decision']='local';item['event'].set()
                if item['decision'] is None:result.append({k:item[k] for k in ('id','label','expires_at')})
            return result
    def decide(self,scope,ident,decision):
        if decision not in ('local','deepseek'):raise DomainError('Выберите DeepSeek или локальную модель.')
        with self.lock:
            item=self.items.get(ident)
            if not item or item['scope']!=scope:raise DomainError('Ожидание уже завершено.',409)
            if item['decision'] is not None or time.monotonic()>=item['deadline']:
                item['decision']=item['decision'] or 'local';item['event'].set();raise DomainError('Время истекло. Выбрана локальная модель.',409)
            item['decision']=decision;item['event'].set()
        return {'ok':True,'decision':decision}

gate=PaidPassGate()
