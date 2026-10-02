"""安装 GPT-SoVITS 混合日英台词需要的 NLTK 数据，只写本机 TTS 环境。"""

import argparse
import hashlib
import json
import shutil
import sys
import zipfile
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--home", type=Path, required=True, help="本机 hiyori_tts 目录")
    parser.add_argument("--archives", type=Path, required=True, help="官方资源包、index.xml 对应的 packages.json 所在目录")
    args = parser.parse_args()
    home = args.home.expanduser().resolve()
    repo = Path(__file__).resolve().parents[1]
    if home.is_relative_to(repo):
        parser.error("发音数据必须放在仓库外")
    if not (home / "env" / "python.exe").is_file():
        parser.error("未找到独立 TTS Python 环境，请检查 --home")
    sys.path.insert(0, str(repo / "hiyoribot-backend"))
    from tts_worker import NLTK_RESOURCES, prepare_nltk_resources
    import nltk
    archives = args.archives.expanduser().resolve()
    packages = {row["id"]: row for row in json.loads((archives / "packages.json").read_text(encoding="utf-8"))}
    directory = home / "env" / "nltk_data"
    for name, resource in NLTK_RESOURCES.items():
        source = archives / f"{name}.zip"
        expected = packages[name].get("sha256_checksum")
        if not expected or hashlib.sha256(source.read_bytes()).hexdigest() != expected:
            raise RuntimeError(f"官方发音资源 SHA256 不符：{name}")
        destination = directory / resource.split("/")[0]
        destination.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(source) as archive:
            for entry in archive.infolist():
                target = (destination / entry.filename).resolve()
                if not target.is_relative_to(destination.resolve()) or not entry.filename.startswith(name + "/"):
                    raise RuntimeError(f"发音资源包路径无效：{name}")
            archive.extractall(destination)
        # g2p_en 导入时会检查 zip 文件，保留它以免再次尝试自动联网。
        shutil.copyfile(source, destination / source.name)
        print(f"已安装：{name}", flush=True)
    prepare_nltk_resources(home)
    # 实际加载词典与新版英语词性标注器，不能只检查文件名存在。
    from nltk.corpus import cmudict
    assert "hello" in cmudict.dict()
    assert nltk.pos_tag(["hello"])
    print("混合日英台词发音资源检查通过。", flush=True)


if __name__ == "__main__":
    main()
