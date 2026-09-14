# Chobits-Chii-ASR 部署实录

> 本文按家族惯例记录服务器部署全过程。

## 实测环境

- 服务器：腾讯云 CVM，Ubuntu 20.04 LTS，Tesla T4 16GB（驱动 525.105.17 / CUDA 12.0）
- Docker + NVIDIA Container Toolkit（nvidia-ctk 已装）
- 首验日期：2026-09-10

## 首验结论（2026-09-10 首验，2026-09-12 生产上线，Tesla T4）

批量转写与流式识别**全链路均已实测跑通**（引擎直连 + 经门面 9881，含鉴权/改写/关闭帧）：

- 批量：`POST /v1/audio/transcriptions` → `{"text":"伊吹は地位を拾ってくれた。"}`（4.2s 日语样本，CPU 约 2.5s）
- 流式：`start` → PCM16 帧 → `stop`，partial 逐步修正 → final「秀樹は地位を拾ってくれた。」
- 门面：Bearer 鉴权（未授权 401）、`model=chii-asr` → 引擎注册名改写、流式连接干净关闭

**2026-09-12 已在本机生产上线**（LLM 迁出后显存空出）：引擎容器 `--restart unless-stopped` +
门面 systemd（`chobits-chii-asr.service`，enabled）。本机拓扑（T4 16GB + TTS 常驻 3.7GB）：

- **批量转写跑 CPU**（`-e HTTP_DEVICE=cpu`）：实测 4.2s 音频约 2.5s，比实时还快，不占显存；
- **流式识别独占 GPU**（`-e WS_GPU_MEM_UTIL=0.40`，vLLM fp32 ~6GB + 音频组件 ~4GB）；
- 静态占用 ~13.3GB，留 ~3GB 推理工作余量。

若换显存更大的卡（或独占卡）：去掉 `HTTP_DEVICE=cpu` 让批量回 GPU，`WS_GPU_MEM_UTIL` 用镜像默认 0.55 即可。

## T4（及无 bf16 的老卡）适配要点

首验中踩过的坑，均已固化进 `docker/` 与 `tools/` 代码：

1. **funasr 1.4.x 服务栈基于 vLLM**：流式 `funasr-realtime-server` 硬依赖（无回退）；
   批量 `funasr-server` 失败回退 AutoModel。镜像需显式装 `vllm fastapi uvicorn python-multipart`
   （后三个 funasr 未声明为依赖）。
2. **dtype 只能 fp32**：T4（sm75）无 bf16 单元，vLLM 直接拒绝；funasr 又把 fp16 静默映射成
   bf16（`inference_vllm._resolve_vllm_dtype`），官方注释明确无 bf16 的卡用 fp32。权重显存因此翻倍。
3. **triton 需要 C 编译器**：vLLM 在 Turing 上 JIT 编译 ieee 精度 kernel，runtime 镜像无 gcc 必崩
   （`Failed to find C compiler`），镜像已装 gcc。
4. **批量侧 vLLM 尝试必须禁用**：`funasr._server_app` 硬编码 bf16 + 0.5 显存配比，T4 上必失败
   且每次失败残留数 GB 孤儿显存。镜像内补丁加 `FUNASR_SERVER_NO_VLLM` 开关，entrypoint 默认置 1
   （走 AutoModel 回退）；bf16 卡可 `-e FUNASR_SERVER_NO_VLLM=` 恢复。
5. **显存配比**：vLLM 的 `gpu_memory_utilization` 预算**含其他进程占用**。0.25/0.35 实测 KV cache
   不足（fp32 下 2048 长度需 ~0.88GiB），`WS_GPU_MEM_UTIL=0.55` 通过。
6. **funasr-server 参数**：`--model` 只接受别名（fun-asr-nano 等），模型 ID 要走 `--model-path`；
   此时引擎注册名为 `custom`，门面透传批量转写时把 `model` 改写为 `CHII_ASR_ENGINE_MODEL`（默认 custom）。
7. **WS 引擎协议是纯文本命令**（非 JSON）：`START` / `STOP` / `LANGUAGE:<提示语>` / `HOTWORDS:a,b`；
   音频必须 16kHz PCM16；STOP 后引擎不关连接，回 `{"event":"stopped"}` 与 `is_final` 结果帧，
   由 `tools/backend_funasr.py` 适配并主动断流。语言提示语用「日本語」而非「日语」。
8. **构建网络**（国内机器）：PyPI/GitHub 直连慢或超时。构建用
   `--build-arg PIP_INDEX_URL/PIP_TRUSTED_HOST`（PyPI 镜像）与 `--build-arg APT_MIRROR`（apt 镜像）；
   流式服务直接用 pip 包自带的 `funasr-realtime-server`，不再从 GitHub 拉脚本。
9. **启动顺序竞争**：vLLM 按**启动那一刻的空闲显存**配比预算（`util × 总容量 ≤ 当前空闲`，
   否则拒绝启动）。两服务并发启动会互相看不见对方而超发，把后加载方挤到 OOM——entrypoint
   已改为批量先就绪（HTTP 200 探测）再起流式。
10. **小卡共存拓扑**：T4 16GB + TTS 3.7GB 常驻时，批量 AutoModel(GPU ~2.4GB) + 流式 vLLM(fp32)
    静态即占满，推理工作显存为 0（实测批量/流式双双 OOM）。解法：`HTTP_DEVICE=cpu` 批量上 CPU
    （8 核实测 4.2s 音频约 2.5s），流式独占 GPU（util 0.40），留 ~3GB 工作余量。
11. **TLS 私钥属主**：门面以 `User=ubuntu` 运行，`/etc/chobits-chii-asr.key` 必须
    `chown ubuntu:ubuntu`（openssl 以 sudo 生成默认 root:root 600，uvicorn 读不了直接起不来）。

## 1. 构建引擎镜像

```bash
# 海外机器直接 docker build -t chobits-chii-asr-engine docker/ 即可;
# 国内机器（本次实测）走内网镜像源:
docker build -t chobits-chii-asr-engine \
  --build-arg PIP_INDEX_URL=http://mirrors.tencentyun.com/pypi/simple \
  --build-arg PIP_TRUSTED_HOST=mirrors.tencentyun.com \
  --build-arg APT_MIRROR=mirrors.tencentyun.com \
  docker/
```

## 2. 启动引擎容器

```bash
# 模型权重首次启动经 ModelScope 下载 (~2.2GB), 挂载缓存卷避免重复下载
docker run -d --name chobits-chii-asr-engine \
  --gpus all --restart unless-stopped \
  -p 127.0.0.1:9001:9001 -p 127.0.0.1:10095:10095 \
  -v $HOME/.cache/modelscope:/root/.cache/modelscope \
  chobits-chii-asr-engine

# 本机 (T4 16GB + TTS 共卡) 生产实际使用:
#   增加 -e HTTP_DEVICE=cpu -e WS_GPU_MEM_UTIL=0.40   (理由见适配要点 9/10)

docker logs -f chobits-chii-asr-engine   # 等 "Uvicorn running on :9001" 与 "Server on ws://0.0.0.0:10095"
```

- 引擎只绑到 `127.0.0.1`，对外暴露统一由门面负责（鉴权/TLS 都在门面层）。
- 端口约定：容器内 `9001` = HTTP 批量转写，`10095` = WebSocket 流式。
- 镜像内默认值（均可 `-e` 覆盖）：`LANGUAGE=日本語`、`DTYPE=fp32`、`WS_GPU_MEM_UTIL=0.55`、
  `FUNASR_SERVER_NO_VLLM=1`。bf16 卡（A100/4090 等）建议 `-e DTYPE=bf16 -e FUNASR_SERVER_NO_VLLM=`。

## 3. 启动门面（宿主机）

```bash
export CHII_ASR_API_KEY=<随机密钥>   # 必填, 未设置拒绝启动
bash tools/start_asr_api.sh 9881     # 首次运行自动建 .venv 装依赖
```

注意：宿主机 `python3` 若为 ≤3.9（本机为 Ubuntu 20.04 自带 3.8），自动建的 .venv 无法运行
`server.py`（用到 3.10+ 注解语法），需用 ≥3.10 的解释器手动建 .venv：
`<新python> -m venv .venv && .venv/bin/pip install -r requirements.txt`。

## 4. systemd 守护（生产）

```ini
# /etc/systemd/system/chobits-chii-asr.service
[Unit]
Description=Chobits Chii ASR (Fun-ASR-Nano facade)
After=network-online.target docker.service
Wants=network-online.target

[Service]
Type=simple
User=ubuntu
ExecStart=/bin/bash /home/ubuntu/Github/Chobits-Chii-ASR/tools/start_asr_api.sh 9881
Restart=on-failure
RestartSec=5
LimitNOFILE=65536
EnvironmentFile=/etc/chobits-chii-asr.env

[Install]
WantedBy=multi-user.target
```

```bash
# /etc/chobits-chii-asr.env (chmod 600), 内容:
#   CHII_ASR_API_KEY=<随机密钥>
#   CHII_ASR_BACKEND=funasr                     (默认, 切 Qwen3-ASR 时改 qwen3)
#   CHII_ASR_ENGINE_HTTP_URL=http://127.0.0.1:9001
#   CHII_ASR_ENGINE_WS_URL=ws://127.0.0.1:10095
#   CHII_ASR_ENGINE_MODEL=custom                (默认, funasr --model-path 模式的注册名)
#   CHII_ASR_SSL_CERTFILE=/etc/chobits-chii-asr.crt   (可选, 见下方 TLS)
#   CHII_ASR_SSL_KEYFILE=/etc/chobits-chii-asr.key    (可选, 与上一条同时设置)
sudo systemctl daemon-reload && sudo systemctl enable --now chobits-chii-asr
journalctl -u chobits-chii-asr -f
```

> 2026-09-14 安全加固（均有 env 可调，默认值见 `deploy/chobits-chii-asr.env.example`）：
> - 批量转写上传上限 25MB（`CHII_ASR_MAX_UPLOAD_BYTES`，Content-Length 预检 + 读入累计兜底）；
> - WS 流式资源防护：每 IP / 全局并发上限（4 / 32）、会话最长 300s、空闲 60s 即断、
>   单会话音频总量 10MB——此前挂死连接即可耗尽引擎 GPU 会话；
> - WS 票据改为 `<exp>.<jti>.<sig>`，jti 核销、单次使用（与垫片同步更新）；
> - 限流改按真实客户端 IP：对端为本机（Caddy/垫片）时采信 XFF 末跳
>   （Caddy asr-ws 段与垫片透传均已配合覆盖/转发 XFF），直连不采信 XFF。

## 5. TLS（可选，公网强烈建议）

```bash
sudo openssl req -x509 -newkey rsa:2048 -nodes \
  -keyout /etc/chobits-chii-asr.key -out /etc/chobits-chii-asr.crt -days 3650 \
  -subj "/CN=chii-asr" -addext "subjectAltName=IP:<服务器IP>,IP:127.0.0.1"
sudo chmod 600 /etc/chobits-chii-asr.key
sudo chown ubuntu:ubuntu /etc/chobits-chii-asr.key /etc/chobits-chii-asr.crt   # 门面以 User=ubuntu 运行, 否则读 key 失败
# 在 /etc/chobits-chii-asr.env 中设置 CHII_ASR_SSL_CERTFILE / CHII_ASR_SSL_KEYFILE 后重启服务
```

## 6. 验证

```bash
# 批量转写 (未启用 TLS 时用 http 并去掉 -k)
curl -k -X POST https://<服务器IP>:9881/v1/audio/transcriptions \
  -H "Authorization: Bearer <API_KEY>" \
  -F "file=@sample.wav" -F "model=chii-asr"

# 流式识别
CHII_ASR_BASE_URL=https://<服务器IP>:9881 CHII_ASR_API_KEY=<API_KEY> \
  python3 tools/client_example.py stream sample.wav ja
```

注意在云安全组放行 TCP 9881；引擎端口 9001/10095 不要对外开放。
