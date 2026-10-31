"""显式付费视觉预检：仅合成图＋临时数据库；不向QQ发送，不污染正式图库。"""
import argparse
import base64
import io
import json
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'hiyoribot-backend'))
from PIL import Image,ImageDraw
import main
import media_service
import storage


def main_cli():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live-model',action='store_true',required=True,help='明确允许实际视觉请求')
    parser.add_argument('--config',type=Path,required=True)
    parser.add_argument('--report',type=Path,required=True)
    args=parser.parse_args()
    report=args.report.resolve()
    if report.is_relative_to(ROOT):
        raise ValueError('模型输出报告必须位于仓库外')
    os.environ['HIYORI_CONFIG']=str(args.config)
    config=main.resolve_config()
    with tempfile.TemporaryDirectory(prefix='hiyori-vision-') as folder:
        database=Path(folder)/'test.sqlite3'
        config=config.model_copy(update={'sqlite_path':str(database),'media_home':str(Path(folder)/'media')})
        storage.configure(database)
        storage.init_db()
        if report.is_file():
            prior=json.loads(report.read_text(encoding='utf8'))
            with storage.connect(write=True) as c:
                for q in prior.get('quota',[]):
                    c.execute('INSERT INTO media_daily(day,attempts) VALUES(?,?)',(q['day'],q['attempts']))
        still=Image.new('RGB',(240,160),'skyblue')
        ImageDraw.Draw(still).text((25,60),'HIYORI',fill='black',font_size=26)
        tests=[]
        for suffix in ('PNG','GIF'):
            out=io.BytesIO()
            if suffix=='GIF':
                still.save(out,format='GIF',save_all=True,append_images=[Image.new('RGB',still.size,'pink'),
                    Image.new('RGB',still.size,'yellow')],duration=200,loop=0)
            else:
                still.save(out,format='PNG')
            with patch.object(media_service,'kick'):
                asset=media_service.ingest(media_service.Upload(bot_id=config.qq_bot_id,user_id=config.qq_owner_id,
                    message_id=str(-len(tests)-1),slot=0,data=base64.b64encode(out.getvalue()).decode()),config)
            media_service.work(config)
            with storage.connect() as c:
                row=c.execute('SELECT state,analysis,error,frames FROM media_asset WHERE id=?',(asset['asset_id'],)).fetchone()
            tests.append({'format':suffix,**row})
            if row['state']!='ready':break
        with storage.connect() as c:
            quota=c.execute('SELECT * FROM media_daily').fetchall()
        result={'model':config.vision_model or config.model,'tests':tests,'quota':quota,'ok':len(tests)==2 and all(t['state']=='ready' for t in tests)}
        report.parent.mkdir(parents=True,exist_ok=True)
        report.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf8')
        print(json.dumps({'ok':result['ok'],'model':result['model'],'tested_images':len(tests),
            'attempts':sum(r['attempts'] for r in quota)},ensure_ascii=False))
        return 0 if result['ok'] else 1


if __name__=='__main__':
    try:
        raise SystemExit(main_cli())
    except (ValueError,OSError):
        print('预检失败，请检查仓库外配置和报告路径。',file=sys.stderr)
        raise SystemExit(1)
