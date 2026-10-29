"""flash只请求配置的DeepSeek接口，无本地模型或GPU依赖。"""
import json
from contextlib import contextmanager
from openai import OpenAI

def is_local(config):
    return False

@contextmanager
def gpu_session(config,job=None):
    if job:
        job.check()
    yield

def auxiliary_model(config):
    return config.model

def transport_options(config):
    return {}

def auxiliary_options(config):
    return {'temperature':0,'max_tokens':3072,'extra_body':{'thinking':{'type':'disabled'}}}

def fit_messages(messages,config,tools=None,*,output_tokens=1024):
    # 字符预算是保守上界近似；完整保留核心system和最新消息，按完整旧轮裁剪。
    budget=getattr(config,'input_budget_chars',48000)
    fitted=[{k:v for k,v in item.items() if k!='reasoning_content'} for item in messages]
    while len(json.dumps({'messages':fitted,'tools':tools or []},ensure_ascii=False))>budget:
        users=[i for i,m in enumerate(fitted) if m['role']=='user']
        if len(users)<2:
            raise ValueError('输入上下文超出预算，请缩短本条消息')
        del fitted[users[0]:users[1]]
    return fitted

def json_output(instructions,payload,schema,config,*,max_tokens=3072,timeout=120):
    messages=[{'role':'system','content':instructions+'\n输出符合以下schema的JSON：'+json.dumps(schema.model_json_schema(),ensure_ascii=False)},
              {'role':'user','content':json.dumps(payload,ensure_ascii=False)}]
    with OpenAI(api_key=config.api_key,base_url=config.base_url,timeout=timeout,max_retries=0) as client:
        response=client.chat.completions.create(model=config.model,messages=fit_messages(messages,config,output_tokens=3072),
            response_format={'type':'json_object'},**{**auxiliary_options(config),'max_tokens':max_tokens})
    choice=response.choices[0] if response.choices else None
    if choice is None or choice.finish_reason!='stop':
        raise ValueError('模型结构化输出不完整')
    return schema.model_validate_json(choice.message.content or '{}')
