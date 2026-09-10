# Chobits-Chii-ASR

[![Made with Love](https://img.shields.io/badge/Made%20with-Love-ff69b4.svg)](https://madewithlove.org.in)
[![GitHub](https://img.shields.io/badge/GitHub-Chobits--Chii--ASR-181717?logo=github)](https://github.com/chenxin199305/Chobits-Chii-ASR)
[![Dataset: Chobits-Chii-Voice](https://img.shields.io/badge/%F0%9F%A4%97%20Dataset-Chobits--Chii--Voice-yellow)](https://huggingface.co/datasets/chenxin199305/Chobits-Chii-Voice)
[![License: CC BY-NC-SA 4.0](https://img.shields.io/badge/License-CC%20BY--NC--SA%204.0-lightgrey.svg)](https://creativecommons.org/licenses/by-nc-sa/4.0/)
[![Language: Japanese](https://img.shields.io/badge/Language-Japanese-green.svg)]()

> ✅ 2026-09-10 仓库创建：Fun-ASR-Nano-2512 引擎镜像 + 门面服务（鉴权/限流/OpenAI 兼容/流式 WS）代码就绪，待服务器首验（见 [docs/deployment.md](docs/deployment.md)）。

《人形电脑天使心》(Chobits) 中 **小叽 (Chii / ちぃ)** 角色的 ASR（语音识别）服务项目。

本项目在服务器部署 [Fun-ASR-Nano-2512](https://www.modelscope.cn/models/FunAudioLLM/Fun-ASR-Nano-2512)（阿里通义实验室，0.8B，支持中/英/日及中文方言），提供 OpenAI 兼容的批量转写与 WebSocket 流式识别接口，并为未来切换到 [Qwen3-ASR](https://github.com/QwenLM/Qwen3-ASR) 预留了后端抽象。评测数据来自 [Chobits-Chii-Voice](https://github.com/chenxin199305/Chobits-Chii-Voice) 数据集。

> ⚠️ 注意：原始动画音频的版权归其权利方所有。本项目仅供学习与研究使用，请勿用于商业用途。

## 架构

```
客户端 ──TLS + API Key──> 门面 tools/server.py (宿主机 :9881)
                            │  POST /v1/audio/transcriptions ──> 引擎 HTTP :9001
                            │  WS   /v1/realtime (统一协议) ──> 后端适配层 ──> 引擎 WS :10095
                            ▼
                      CHII_ASR_BACKEND=funasr|qwen3 选择 backend_*.py

引擎 (Docker 容器, 单容器双进程):
  funasr-server          OpenAI 兼容 HTTP 批量转写
  serve_realtime_ws.py   Fun-ASR-Nano 官方流式 WebSocket 服务
```

与家族其他服务（[LLM](https://github.com/chenxin199305/Chobits-Chii-LLM) / [TTS](https://github.com/chenxin199305/Chobits-Chii-TTS)）一致的约定：推理引擎跑在 Docker 里，宿主机 Python 门面负责鉴权（`Authorization: Bearer`）、每 IP 限流与协议垫片，密钥经 `/etc/chobits-chii-asr.env` 注入，systemd 守护。

## 为切换 Qwen3-ASR 做的准备

- **HTTP 批量转写**：Fun-ASR-Nano（`funasr-server`）与 Qwen3-ASR（`qwen-asr-serve` / `vllm serve`）都原生暴露 `POST /v1/audio/transcriptions`，门面只做透传——切换时只需改三个环境变量，客户端零改动。
- **WS 流式**：门面定义了统一对外协议（`start`/音频帧/`stop` → `partial`/`final`），协议差异由 `tools/backend_funasr.py` / `tools/backend_qwen3.py` 吸收。Qwen3-ASR 侧流式 shim 见 Roadmap。

切换步骤（届时）：

```bash
# 1. 起 Qwen3-ASR 引擎 (官方镜像)
docker run -d --name chobits-chii-asr-engine-qwen3 --gpus all \
  -p 127.0.0.1:9001:8000 qwenllm/qwen3-asr:latest \
  qwen-asr-serve Qwen/Qwen3-ASR-1.7B --gpu-memory-utilization 0.8 --port 8000

# 2. /etc/chobits-chii-asr.env 改三行后重启门面:
#    CHII_ASR_BACKEND=qwen3
#    CHII_ASR_ENGINE_HTTP_URL=http://127.0.0.1:9001
#    CHII_ASR_ENGINE_WS_URL=ws://127.0.0.1:<qwen3 ws shim 端口>
```

## 仓库结构

```
Chobits-Chii-ASR/
├── README.md               # 本文件
├── LICENSE                 # CC BY-NC-SA 4.0
├── .gitignore
├── requirements.txt        # 门面依赖 (家族唯一: 门面需要 WebSocket 能力, 标准库不够)
├── docker/                 # 推理引擎镜像
│   ├── Dockerfile             # Fun-ASR-Nano-2512 引擎 (funasr + 官方流式脚本)
│   └── entrypoint.sh          # 单容器双进程: HTTP :9001 + WS :10095
├── tools/                  # 工具脚本
│   ├── server.py              # ASR 门面 (API Key 鉴权 + 限流 + OpenAI 垫片 + 流式 WS 网关)
│   ├── backend_funasr.py      # Fun-ASR 后端适配 (Nano START/STOP 协议翻译)
│   ├── backend_qwen3.py       # Qwen3-ASR 后端适配 (HTTP 现成; WS 留骨架, 见 Roadmap)
│   ├── start_asr_api.sh       # 启动门面 (自动建 .venv, 可直接运行或供 systemd 调用)
│   ├── client_example.py      # 调用示例: 批量转写 + 流式识别
│   └── eval_chobits.py        # 用 Chobits-Chii-Voice 数据集测 CER (幂等可续跑)
├── data/                   # 评测数据说明 (音频不入库, 复用 Chobits-Chii-Voice)
├── examples/               # 示例说明
├── docs/
│   └── deployment.md          # 服务器部署实录 (docker/systemd/TLS/验证)
└── outputs/                # 评测报告等产物 (不入库)
```

## 快速开始

推理只需要引擎镜像与门面环境，无需训练数据。完整服务器部署（含 systemd 与 TLS）见 [docs/deployment.md](docs/deployment.md)。

```bash
# 1. 构建并启动引擎容器 (模型权重首启自动经 ModelScope 下载)
docker build -t chobits-chii-asr-engine docker/
docker run -d --name chobits-chii-asr-engine --gpus all --restart unless-stopped \
  -p 127.0.0.1:9001:9001 -p 127.0.0.1:10095:10095 \
  -v $HOME/.cache/modelscope:/root/.cache/modelscope \
  chobits-chii-asr-engine

# 2. 启动门面 (0.0.0.0:9881, 首次自动建 .venv)
export CHII_ASR_API_KEY=<随机密钥>   # 必填, 未设置拒绝启动
bash tools/start_asr_api.sh 9881
```

调用（客户端 `baseUrl` 填 `http(s)://<服务器IP>:9881/v1`，`GET /v1/models` 固定返回 `chii-asr`）：

```bash
# 批量转写 (OpenAI 兼容协议)
curl -X POST http://<服务器IP>:9881/v1/audio/transcriptions \
  -H "Authorization: Bearer <API_KEY>" \
  -F "file=@sample.wav" -F "model=chii-asr"

# 流式识别 (统一 WS 协议: start → PCM16 帧 → stop, 返回 partial/final)
python3 tools/client_example.py stream sample.wav ja
```

其他环境变量：`CHII_ASR_RATE_LIMIT`（转写与流式每 IP 每分钟限流次数，默认 60，0 关闭）；
`CHII_ASR_BACKEND` / `CHII_ASR_ENGINE_HTTP_URL` / `CHII_ASR_ENGINE_WS_URL`（切换后端用）；
`CHII_ASR_SSL_CERTFILE` / `CHII_ASR_SSL_KEYFILE`（同时设置时以 HTTPS/WSS 启动）。

注意在云安全组放行 TCP 9881；引擎端口 9001/10095 不要对外开放。
对外提供服务须遵守 [CC BY-NC-SA 4.0](#许可协议)（非商业）。

## 评测

用 Chobits-Chii-Voice 数据集（487 条小叽日语台词，人工校对文本）测字错率：

```bash
git clone https://huggingface.co/datasets/chenxin199305/Chobits-Chii-Voice
python3 tools/eval_chobits.py --voice /path/to/Chobits-Chii-Voice/dataset --tag funasr-nano
# 切换 Qwen3-ASR 后用 --tag qwen3-asr 再跑一遍, 两份报告即可直接对比 CER
```

报告输出到 `outputs/eval_<tag>.csv`（整体 CER + 逐条明细，幂等可续跑）。

## Roadmap

- [ ] 服务器首验：镜像构建、Fun-ASR-Nano 批量/流式全链路（docs/deployment.md 补实测环境）
- [ ] Chobits-Chii-Voice 数据集上的日语 CER 基线（Fun-ASR-Nano vs Qwen3-ASR 对比）
- [ ] Qwen3-ASR 流式 WS shim（引擎容器内基于 qwen-asr streaming SDK，复用 Nano 协议）
- [ ] 日语识别热词支持（Fun-ASR-Nano 原生 hotwords，如角色名「秀樹」「ちぃ」）

## 许可协议

本项目的派生内容遵循数据集的 [CC BY-NC-SA 4.0](https://creativecommons.org/licenses/by-nc-sa/4.0/deed.zh-hans)（署名-非商业性使用-相同方式共享）协议：

- **署名 (BY)**：使用时须注明来源。
- **非商业性使用 (NC)**：不得用于商业目的。
- **相同方式共享 (SA)**：衍生作品须以相同协议发布。

上游模型 Fun-ASR-Nano-2512 与 Qwen3-ASR 均为 Apache-2.0，遵循其各自许可。

## 免责声明

- 本项目仅用于学术研究与个人学习，不构成对原作品版权的任何主张。
- 使用本项目处理或生成的内容，不得用于侵犯原作品及相关声优（田中理惠）权益的用途。
- 若权利方提出要求，本项目将被下架。

## 相关项目

- [Chobits-Chii-Voice](https://github.com/chenxin199305/Chobits-Chii-Voice) — 小叽语音数据集（本项目的评测数据来源）
- [Chobits-Chii-TTS](https://github.com/chenxin199305/Chobits-Chii-TTS) — 小叽声线 TTS（与本项目互补：一个合成声音，一个识别声音）
- [Chobits-Chii-LLM](https://github.com/chenxin199305/Chobits-Chii-LLM) — 小叽人格对话/翻译服务（可与本项目串联成语音对话链路）
- [Fun-ASR](https://github.com/QwenAudio/Fun-ASR) / [FunASR](https://github.com/modelscope/FunASR) — 底层语音识别引擎
- [Qwen3-ASR](https://github.com/QwenLM/Qwen3-ASR) — 备选语音识别引擎
