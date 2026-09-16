# data/

本目录用于存放评测用样本的说明，**音频数据不入库**（见根目录 `.gitignore`）。

评测数据直接复用 [Chobits-Chii-Voice](https://github.com/Anime2Real/Chobits-Chii-Voice)
数据集（487 条小叽日语台词，22.05 kHz 单声道，含人工校对文本），无需另备：

```bash
git clone https://huggingface.co/datasets/chenxin199305/Chobits-Chii-Voice
python3 tools/eval_chobits.py --voice /path/to/Chobits-Chii-Voice/dataset
```

评测报告输出到 `outputs/eval_<tag>.csv`（不入库）。
