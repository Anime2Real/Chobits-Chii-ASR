#!/usr/bin/env python3
"""用 Chobits-Chii-Voice 数据集评测当前 ASR 后端的识别字错率 (CER)。

遍历数据集的 metadata.csv (文件名|文本), 逐条调用门面的批量转写接口,
按字符级编辑距离计算 CER (适合日语), 输出明细 CSV 与汇总到 outputs/。

环境变量:
    CHII_ASR_API_KEY   门面鉴权 key (必填)
    CHII_ASR_BASE_URL  门面地址, 默认 http://127.0.0.1:9881

用法:
    python3 tools/eval_chobits.py --voice /path/to/Chobits-Chii-Voice/dataset \
        [--limit 50] [--tag funasr-nano]

对比不同后端 (funasr/qwen3) 时用 --tag 区分报告文件名, 报告互不覆盖。
幂等: 同 tag 的报告已含某文件时跳过该条, 中断后可续跑。
"""

import argparse
import csv
import os
import sys
import time

import httpx

BASE_URL = os.environ.get("CHII_ASR_BASE_URL", "http://127.0.0.1:9881")
API_KEY = os.environ.get("CHII_ASR_API_KEY", "")
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def normalize(text: str) -> str:
    """CER 前归一化: 去全部空白字符 (日语无分词问题, 直接按字符算)。"""
    return "".join(text.split())


def edit_distance(a: str, b: str) -> int:
    """字符级 Levenshtein 距离 (滚动数组)。"""
    if len(a) < len(b):
        a, b = b, a
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1,
                           prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def transcribe(client: httpx.Client, wav_path: str) -> str:
    with open(wav_path, "rb") as f:
        resp = client.post(
            f"{BASE_URL}/v1/audio/transcriptions",
            headers={"Authorization": f"Bearer {API_KEY}"},
            files={"file": (os.path.basename(wav_path), f)},
            data={"model": "chii-asr"})
    resp.raise_for_status()
    return resp.json()["text"]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--voice", required=True,
                        help="Chobits-Chii-Voice 的 dataset/ 目录 (含 wavs/ 与 metadata.csv)")
    parser.add_argument("--limit", type=int, default=0, help="只评前 N 条 (0 = 全部)")
    parser.add_argument("--tag", default="funasr-nano", help="报告文件名后缀, 用于区分后端")
    args = parser.parse_args()

    if not API_KEY:
        sys.exit("[错误] 请设置 CHII_ASR_API_KEY")
    meta_path = os.path.join(args.voice, "metadata.csv")
    if not os.path.isfile(meta_path):
        sys.exit(f"[错误] 未找到 {meta_path}")

    with open(meta_path, encoding="utf-8") as f:
        rows = [line.rstrip("\n").split("|", 1) for line in f if "|" in line]
    if args.limit > 0:
        rows = rows[:args.limit]

    out_dir = os.path.join(REPO_ROOT, "outputs")
    os.makedirs(out_dir, exist_ok=True)
    report_path = os.path.join(out_dir, f"eval_{args.tag}.csv")

    done = set()
    if os.path.isfile(report_path):  # 续跑: 已评过的文件跳过
        with open(report_path, encoding="utf-8") as f:
            done = {r["file"] for r in csv.DictReader(f)}

    total_dist = total_chars = 0
    evaluated = 0
    with open(report_path, "a", encoding="utf-8", newline="") as f, \
            httpx.Client(timeout=300.0) as client:
        writer = csv.DictWriter(f, fieldnames=["file", "reference", "hypothesis",
                                               "distance", "ref_chars", "cer"])
        if not done:
            writer.writeheader()
        for name, ref in rows:
            if name in done:
                continue
            wav_path = os.path.join(args.voice, "wavs", f"{name}.wav")
            if not os.path.isfile(wav_path):
                wav_path = os.path.join(args.voice, "wavs", name)
            try:
                hyp = transcribe(client, wav_path)
            except httpx.HTTPError as e:
                print(f"[eval] {name}: 转写失败 ({type(e).__name__}), 跳过", file=sys.stderr)
                continue
            ref_n, hyp_n = normalize(ref), normalize(hyp)
            dist = edit_distance(ref_n, hyp_n)
            cer = dist / len(ref_n) if ref_n else 0.0
            writer.writerow({"file": name, "reference": ref, "hypothesis": hyp,
                             "distance": dist, "ref_chars": len(ref_n),
                             "cer": f"{cer:.4f}"})
            f.flush()
            total_dist += dist
            total_chars += len(ref_n)
            evaluated += 1
            print(f"[eval] {evaluated}/{len(rows) - len(done)} {name}: CER={cer:.4f}")

    if total_chars:
        print(f"\n[eval] 后端 {args.tag}: 整体 CER = "
              f"{total_dist / total_chars:.4f} ({total_dist}/{total_chars} 字符, "
              f"{evaluated} 条), 明细见 {report_path}")
    else:
        print("[eval] 没有可评测的条目")


if __name__ == "__main__":
    main()
