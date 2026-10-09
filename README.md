<div align="center">
	<h1>Chobits-Chii-ASR</h1>
	<p><b>叽～</b> 小叽的语音识别服务</p>
	<p>OpenAI 兼容的批量转写服务（流式待 WS shim）：<b>Qwen3-ASR-1.7B</b> 驱动（分支 <code>qwen3-asr-1.7b</code>）。</p>
	<p>
		<a href="https://madewithlove.org.in"><img alt="Made with Love" src="https://img.shields.io/badge/Made%20with-Love-ff69b4.svg"></a>
		<a href="https://github.com/Anime2Real/Chobits-Chii-ASR"><img alt="GitHub" src="https://img.shields.io/badge/GitHub-Chobits--Chii--ASR-181717?logo=github"></a>
		<a href="https://huggingface.co/datasets/chenxin199305/Chobits-Chii-Voice"><img alt="Dataset: Chobits-Chii-Voice" src="https://img.shields.io/badge/%F0%9F%A4%97%20Dataset-Chobits--Chii--Voice-yellow"></a>
		<a href="https://creativecommons.org/licenses/by-nc-sa/4.0/"><img alt="License: CC BY-NC-SA 4.0" src="https://img.shields.io/badge/License-CC%20BY--NC--SA%204.0-lightgrey.svg"></a>
		<img alt="Language: Japanese" src="https://img.shields.io/badge/Language-Japanese-green.svg">
	</p>
</div>

> 💖 如果这个项目对你有帮助，欢迎在 [GitHub](https://github.com/Anime2Real/Chobits-Chii-ASR) 点个 Star —— 你的支持能让更多人发现小叽！

> 🔀 分支拓扑：本分支 `qwen3-asr-1.7b` 为 **Qwen3-ASR-1.7B** 的部署构建档案——引擎基于官方 `qwenllm/qwen3-asr` 镜像，`qwen-asr-serve` 单进程提供 OpenAI 兼容批量转写；**流式 `/v1/realtime` 暂不可用**（backend_qwen3 会明确报错，WS shim 见 Roadmap）。生产 T4 拓扑实录见 `fun-asr-nano-0.8b` 分支的 [docs/deployment.md](docs/deployment.md)。

《人形电脑天使心》(Chobits) 中 **小叽 (Chii / ちぃ)** 角色的 ASR（语音识别）服务项目。

本项目在服务器部署 [Qwen3-ASR-1.7B](https://github.com/QwenLM/Qwen3-ASR)（Qwen 团队，基于 Qwen3-Omni 后训练，52 种语言与方言含日/英/中，Apache-2.0），经官方 `qwenllm/qwen3-asr` 镜像提供 OpenAI 兼容的批量转写接口；流式识别待 WS shim 落地（见 Roadmap）。Fun-ASR-Nano-2512 方案见 `fun-asr-nano-0.8b` 分支。评测数据来自 [Chobits-Chii-Voice](https://github.com/Anime2Real/Chobits-Chii-Voice) 数据集。

> ⚠️ 注意：原始动画音频的版权归其权利方所有。本项目仅供学习与研究使用，请勿用于商业用途。

## 🏗️ 架构

```
客户端 ──TLS + API Key──> Caddy :443 ──> 门面 tools/server.py (127.0.0.1:9881)
                            │  POST /v1/audio/transcriptions ──> 引擎 HTTP :9001
                            │  WS   /v1/realtime (统一协议) ──> 后端适配层 ──> 引擎 WS :10095
                            ▼
                      CHII_ASR_BACKEND=funasr|qwen3 选择 backend_*.py

引擎 (Docker 容器, 官方 qwenllm/qwen3-asr 镜像, 单进程):
  qwen-asr-serve  OpenAI 兼容 HTTP 批量转写 (:8000→宿主 9001, vLLM)
  (无 WS 流式进程: /v1/realtime 由门面 backend_qwen3 明确报错)
```

与家族其他服务（[LLM](https://github.com/Anime2Real/Chobits-Chii-LLM) / [TTS](https://github.com/Anime2Real/Chobits-Chii-TTS)）一致的约定：推理引擎跑在 Docker 里，宿主机 Python 门面负责鉴权（`Authorization: Bearer`）、每 IP 限流与协议垫片，密钥经 `/etc/chobits-chii-asr.env` 注入，systemd 守护。

## 🔀 分支拓扑与后端抽象

- **HTTP 批量转写**：三分支门面代码一致，`CHII_ASR_BACKEND=funasr|qwen3` + 引擎地址环境变量切换，客户端零改动。
- **WS 流式**：门面定义了统一对外协议（`start`/音频帧/`stop` → `partial`/`final`），协议差异由 `tools/backend_funasr.py` / `tools/backend_qwen3.py` 吸收。Qwen3 侧流式 shim 尚未实现（`backend_qwen3.py` 现为报错骨架，连接即收到明确报错并断开），本分支流式不可用；Fun-ASR-Nano 分支流式已生产验证。
- 三个部署分支：`fun-asr-nano-0.8b`（Fun-ASR-Nano-2512，单容器双进程：批量 AutoModel/CPU + 流式 vLLM/GPU）、`qwen3-asr-0.6b`（本分支）、`qwen3-asr-1.7b`。门面 `tools/` 与 `tests/` 三分支完全一致，差异只在 `docker/` 与 `deploy/` 模板。

**会话语义（重要）**：一条 `/v1/realtime` 连接 = 一次聆听会话，可包含多句话。`start` 后持续推音频，每句话说定稿时服务端下推一条 `{"type":"final"}`（可能多条）；引擎句级 `is_final` 只是该句定稿的内部信号，不下发、也不结束连接。客户端发 `{"type":"stop"}`（或断连）即会话结束：服务端等引擎把最后的 final 吐完后主动关闭连接（close 1000）。客户端不应在收到第一条 final 后自行断连重开——换连接需要重新换票。

出错时服务端下发 `{"type":"error","code":"<CODE>","message":"<中文兜底>"}` 帧后随即关闭连接。`code` 为稳定枚举（X-6 协议，与 LLM/Mascot 门面同一集合；**机器可读单一事实源**见 [`docs/contracts/`](docs/contracts/) 的 `errors.schema.json` 与 `VERSION`，ASR 票据格式见同目录 `asr-ticket.schema.json`），**message 为兜底文案，客户端应按 code 本地化**：

| code | 适用 |
|---|---|
| `engine_error` | 引擎识别/处理错误（含引擎侧 PayloadTooBig 等异常 surfaced） |
| `engine_conn_failed` | 连不上引擎（accept 超时/拒绝） |
| `idle_timeout` | 空闲超时关闭 |
| `protocol_error` | 协议错误（首帧不是 start、start 参数非法等） |
| `frame_too_large` | 单帧超 `CHII_ASR_WS_MAX_FRAME_BYTES` |
| `audio_limit_exceeded` | 会话音频总量上限触顶（默认 10MB） |
| `internal_error` | 其他未归类 |

反向切换（回 Fun-ASR-Nano）：整分支切到 `fun-asr-nano-0.8b` 部署，门面 `CHII_ASR_BACKEND=funasr` 并恢复 `CHII_ASR_ENGINE_WS_URL`，客户端零改动。

## 🗂 仓库结构

```
Chobits-Chii-ASR/
├── README.md               # 本文件
├── LICENSE                 # CC BY-NC-SA 4.0
├── .gitignore
├── requirements.txt        # 门面依赖 (含 WebSocket 流式网关所需包, 标准库不够)
├── deploy/                 # 部署模板 (systemd unit + env 示例)
├── docker/                 # 推理引擎镜像
│   ├── Dockerfile             # Qwen3-ASR 引擎 (官方 qwenllm/qwen3-asr 镜像, qwen-asr-serve 单进程)
│   └── entrypoint.sh          # 单进程: qwen-asr-serve HTTP :8000→宿主 9001
├── tools/                  # 工具脚本
│   ├── server.py              # ASR 门面 (API Key 鉴权 + 限流 + OpenAI 垫片 + 流式 WS 网关)
│   ├── backend_funasr.py      # Fun-ASR 后端适配 (Nano START/STOP 协议翻译)
│   ├── backend_qwen3.py       # Qwen3-ASR 后端适配 (HTTP 现成; WS 留骨架, 见 Roadmap)
│   ├── start_asr_api.sh       # 启动门面 (自动建 .venv, 可直接运行或供 systemd 调用)
│   ├── client_example.py      # 调用示例: 批量转写 + 流式识别
│   └── eval_chobits.py        # 用 Chobits-Chii-Voice 数据集测 CER (幂等可续跑)
├── tests/                  # pytest 测试套件 (端点/WS 协议/后端适配, 引擎全 mock)
├── data/                   # 评测数据说明 (音频不入库, 复用 Chobits-Chii-Voice)
├── examples/               # 示例说明
├── docs/
│   ├── deployment.md          # 服务器部署实录 (docker/systemd/TLS/验证)
│   ├── hardware-adaptation.md   # 硬件适配记录 (T4 实测/V100 预留, 三分支同款)
│   └── contracts/             # 跨服务契约机器可读单一事实源 (X-6 错误码/ASR 票据 schema + VERSION)
└── outputs/                # 评测报告等产物 (不入库)
```

## 🚀 快速开始

推理只需要引擎镜像与门面环境，无需训练数据。完整服务器部署（含 systemd 与 TLS）见 [docs/deployment.md](docs/deployment.md)。各仓库用到的国内镜像/加速源汇总见 [Chobits-Chii-TTS docs/mirrors.md](https://github.com/Anime2Real/Chobits-Chii-TTS/blob/main/docs/mirrors.md)。

```bash
# 1. 构建并启动引擎容器 (模型权重首启自动下载, 挂缓存卷持久化)
docker build -t chobits-chii-asr-engine-qwen3 docker/
docker run -d --name chobits-chii-asr-engine --gpus all --restart unless-stopped \
  -p 127.0.0.1:9001:8000 \
  -e NVIDIA_DISABLE_REQUIRE=1 \
  -e VLLM_ATTENTION_BACKEND=TRITON_ATTN \
  -e HF_ENDPOINT=https://hf-mirror.com \
  -v $HOME/.cache/huggingface:/root/.cache/huggingface \
  chobits-chii-asr-engine-qwen3

# 2. 启动门面 (默认绑 127.0.0.1:9881, 首次自动建 .venv)
export CHII_ASR_API_KEY=<随机密钥>   # 必填, 未设置拒绝启动
bash tools/start_asr_api.sh 9881
```

> 引擎显存与 T4 适配（2026-10-09 实测）：vLLM 单进程，`GPU_MEM_UTIL=0.6`、`MAX_MODEL_LEN=32768`（默认 65536 的 KV 预算 ~7GB 在 T4 装不下）、`ENFORCE_EAGER=1`（CUDA graph 捕获期 OOM，entrypoint 已内置这三项默认值）。驱动 525（CUDA 12.0）低于镜像声明的 12.8，需 `NVIDIA_DISABLE_REQUIRE=1`；sm75（T4）上 flashinfer attention 后端启动卡死，需 `VLLM_ATTENTION_BACKEND=TRITON_ATTN`。权重首启经 HF 下载，国内配 `HF_ENDPOINT` 镜像。完整踩坑记录与后续硬件适配（V100 预留）见 [docs/hardware-adaptation.md](docs/hardware-adaptation.md)。

> 门面依赖兄弟仓库的共享库 [chii-facade-common](https://github.com/Anime2Real/Chobits-Chii-ServerDeploy/tree/cloud/tools/chii-facade-common)（鉴权/限流/env 解析等两门面公共逻辑的唯一真相源）。`start_asr_api.sh` 首次建 venv 时自动从同级目录 `../Chobits-Chii-ServerDeploy/tools/chii-facade-common` 以 editable 方式安装（兼容旧目录名）；单仓库 clone 需先同级 clone ServerDeploy 仓库**并检出 `cloud` 分支**——其 `main` 仅作分支索引、无 `tools/` 目录（`git clone -b cloud git@github.com:Anime2Real/Chobits-Chii-ServerDeploy.git ../Chobits-Chii-ServerDeploy`），或手动 `pip install -e ../Chobits-Chii-ServerDeploy/tools/chii-facade-common`。启动脚本已带前置检查：共享库缺失时打印上述指引并非零退出。改动共享库后须重启门面生效。

调用（在服务器本机验证用 `http://127.0.0.1:9881/v1`；公网由 Caddy 反代终结 TLS——
客户端经 443 由垫片转发到门面，见 [docs/deployment.md](docs/deployment.md)，`GET /v1/models` 固定返回 `chii-asr`）：

```bash
# 批量转写 (OpenAI 兼容协议)
curl -X POST http://127.0.0.1:9881/v1/audio/transcriptions \
  -H "Authorization: Bearer <API_KEY>" \
  -F "file=@sample.wav" -F "model=chii-asr"

# 流式识别 (本分支暂不可用, 连接即收明确报错; shim 见 Roadmap)
python3 tools/client_example.py stream sample.wav ja
```

其他环境变量：`CHII_ASR_BIND`（门面监听地址，默认 `127.0.0.1`；不经 Caddy 直接对外须配下方 SSL env，否则非回环绑定拒绝启动）；
`CHII_ASR_RATE_LIMIT`（转写与流式每 IP 每分钟限流次数，默认 60，0 关闭）；
`CHII_ASR_BACKEND` / `CHII_ASR_ENGINE_HTTP_URL` / `CHII_ASR_ENGINE_MODEL`（本分支默认 qwen3 / http://127.0.0.1:9001 / Qwen/Qwen3-ASR-1.7B）；`CHII_ASR_ENGINE_WS_URL` 本分支无需设置（流式不可用）；
`CHII_ASR_DEEP_PROBE_TTL` / `CHII_ASR_DEEP_PROBE_TIMEOUT`（`/healthz/deep` 探测结果缓存秒数 / 单次探测超时秒数，默认 30 / 15）；
`CHII_ASR_SSL_CERTFILE` / `CHII_ASR_SSL_KEYFILE`（同时设置时以 HTTPS/WSS 启动）。

健康检查：`GET /healthz` 为免鉴权浅探活（进程级，不触引擎、不暴露指纹）；
`GET /healthz/deep` 为深度检查——用一小段内置静音音频走引擎批量转写路径做一次真实转写探测（TTL 缓存防高频烧引擎，探测不占用 WS 流式会话），引擎不可用/超时/回包异常回 503，正常回 200（含 `ok`/`backend` 字段）。与其他端点一样须带 API key（监控端点免鉴权会形同虚设，与 TTS 门面 `/healthz/deep` 同语义）。

Caddy 架构（2026-09-12 起，见 [docs/deployment.md](docs/deployment.md)）下门面绑回环、公网只放行
TCP 443，安全组**不需要**放行 9881；引擎端口 9001/10095 不要对外开放。
对外提供服务须遵守 [CC BY-NC-SA 4.0](#-许可协议)（非商业）。

## 📊 评测

用 Chobits-Chii-Voice 数据集（487 条小叽日语台词，人工校对文本）测字错率：

```bash
git clone https://huggingface.co/datasets/chenxin199305/Chobits-Chii-Voice
python3 tools/eval_chobits.py --voice /path/to/Chobits-Chii-Voice/dataset --tag funasr-nano
# 切换 Qwen3-ASR 后用 --tag qwen3-asr 再跑一遍, 两份报告即可直接对比 CER
```

报告输出到 `outputs/eval_<tag>.csv`（整体 CER + 逐条明细，幂等可续跑）。

> **小叽提示 (・ω・)ノ**：CER 数字越小，说明小叽把台词听得越准哦。

## 🗺️ Roadmap

- [x] 服务器首验 + 生产部署：Fun-ASR-Nano 批量/流式全链路（Tesla T4，2026-09-12 上线，见 fun-asr-nano-0.8b 分支 docs/deployment.md）
- [ ] Chobits-Chii-Voice 数据集上的日语 CER 基线（Fun-ASR-Nano vs Qwen3-ASR 对比）
- [ ] Qwen3-ASR 流式 WS shim（引擎容器内基于 qwen-asr streaming SDK，复用 Nano 协议）
- [ ] 日语识别热词支持（Fun-ASR-Nano 原生 hotwords，如角色名「秀樹」「ちぃ」）

## 📄 许可协议

本项目的派生内容遵循数据集的 [CC BY-NC-SA 4.0](https://creativecommons.org/licenses/by-nc-sa/4.0/deed.zh-hans)（署名-非商业性使用-相同方式共享）协议：

- **署名 (BY)**：使用时须注明来源。
- **非商业性使用 (NC)**：不得用于商业目的。
- **相同方式共享 (SA)**：衍生作品须以相同协议发布。

上游模型 Fun-ASR-Nano-2512 与 Qwen3-ASR 均为 Apache-2.0，遵循其各自许可。

## ⚠️ 免责声明

- 本项目仅用于学术研究与个人学习，不构成对原作品版权的任何主张。
- 使用本项目处理或生成的内容，不得用于侵犯原作品及相关声优（田中理惠）权益的用途。
- 若权利方提出要求，本项目将被下架。

## 🙏 相关项目

- [Chobits-Chii-Voice](https://github.com/Anime2Real/Chobits-Chii-Voice) — 小叽语音数据集（本项目的评测数据来源）
- [Chobits-Chii-TTS](https://github.com/Anime2Real/Chobits-Chii-TTS) — 小叽声线 TTS（与本项目互补：一个合成声音，一个识别声音）
- [Chobits-Chii-LLM](https://github.com/Anime2Real/Chobits-Chii-LLM) — 小叽人格对话/翻译服务（可与本项目串联成语音对话链路）
- [Fun-ASR](https://github.com/QwenAudio/Fun-ASR) / [FunASR](https://github.com/modelscope/FunASR) — 底层语音识别引擎
- [Qwen3-ASR](https://github.com/QwenLM/Qwen3-ASR) — 备选语音识别引擎
