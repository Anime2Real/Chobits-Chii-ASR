# Chobits-Chii-ASR 部署实录

> ⚠️ 本分支 `qwen3-asr-0.6b` 说明：本文是 **fun-asr-nano-0.8b 分支**（Fun-ASR-Nano 双进程方案）的部署流程实录（构建 / 容器 / systemd / TLS / 验证），供对照参考；Qwen3-ASR 引擎的构建与运行见本分支 README「快速开始」（`docker/` 与 `deploy/chobits-chii-asr.env.example` 已按 qwen3 单进程方案改写）。

> 本文按家族惯例记录服务器部署全过程（镜像构建 / 容器 / 门面 / systemd / TLS / 验证）。
> **硬件适配、踩坑与调参经验已统一收录到 [docs/hardware-adaptation.md](hardware-adaptation.md)**
> （三个分支同款，T4 已实测、V100 预留），本文不再重复。

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

> 已迁至 [docs/hardware-adaptation.md](hardware-adaptation.md)（T4 节，按引擎分小节；
> 三个分支同款，避免双份漂移）。此处只留部署流程。

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
#   增加 -e HTTP_DEVICE=cpu -e WS_GPU_MEM_UTIL=0.40   (理由见 docs/hardware-adaptation.md T4 节)

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

unit 以仓库 [deploy/chobits-chii-asr.service](../deploy/chobits-chii-asr.service) 为唯一事实源——
本文不再内嵌全文（避免双份漂移，改动只改 deploy/ 那一处）：

```bash
sudo cp deploy/chobits-chii-asr.service /etc/systemd/system/chobits-chii-asr.service
```

unit 要点：`Restart=always` + `RestartSec=3`（崩溃/启动失败快速拉起）、
`NoNewPrivileges=true` / `PrivateTmp=true` 加固、`EnvironmentFile` 注入密钥、
`LimitNOFILE=65536`、`After=docker.service` 保证引擎容器先就绪。

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
> - WS 流式资源防护：每客户端身份 / 全局并发上限（4 / 32）、会话最长 300s、空闲 60s 即断、
>   单会话音频总量 10MB——此前挂死连接即可耗尽引擎 GPU 会话；
> - WS 并发按票据身份计数（`CHII_ASR_WS_MAX_PER_CLIENT`，旧名 `CHII_ASR_WS_MAX_PER_IP`
>   兼容读取）：票据携带垫片签入的调用方身份（guest:<installId> / acct:<邮箱>，
>   被 HMAC 签名覆盖），同一 NAT/出口 IP 下各客户端独立配额；旧票据与 API key
>   直连无身份，回退按 IP 计数；
> - WS 票据改为 `<exp>.<jti>.<sig>`，jti 核销、单次使用（与垫片同步更新）；
>   2026-09-14 起扩展为 `<exp>.<jti>.<idb64>.<sig>`（idb64 = base64url(身份)），
>   旧格式票据验签仍兼容；
> - 限流改按真实客户端 IP：对端为本机（Caddy/垫片）时采信 XFF 末跳
>   （Caddy asr-ws 段与垫片透传均已配合覆盖/转发 XFF），直连不采信 XFF。

> 2026-09-24 新增 `GET /healthz/deep` 深度健康检查（2026-09-24 门面 pytest 套件
> 验证通过，真机引擎探测待部署后按 §6 命令补验）：用 0.5s 内置静音 WAV 走引擎批量
> 转写 HTTP 路径发一次真实转写探测（不经 WS 票据，直连引擎层；探测不占用流式 GPU
> 会话），结果缓存 `CHII_ASR_DEEP_PROBE_TTL` 秒（默认 30，防监控高频烧引擎），
> 单次探测超时 `CHII_ASR_DEEP_PROBE_TIMEOUT`（默认 15s）——引擎忙/不可达/回包
> 异常回 503（`{"ok":false,"status":"degraded",...}`），正常回 200（`ok:true` +
> `backend` 字段）。须带 API key（与 TTS 门面 `/healthz/deep` 同语义）；浅探活仍用
> 免鉴权 `GET /healthz`。

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
# 批量转写（生产门面绑回环、TLS 由 Caddy 终结，在服务器本机验证用 http://127.0.0.1；
# 不再提供 curl -k 示例——跳过证书校验等于放弃对中间人的全部防护）
curl -X POST http://127.0.0.1:9881/v1/audio/transcriptions \
  -H "Authorization: Bearer <API_KEY>" \
  -F "file=@sample.wav" -F "model=chii-asr"

# 流式识别
CHII_ASR_BASE_URL=http://127.0.0.1:9881 CHII_ASR_API_KEY=<API_KEY> \
  python3 tools/client_example.py stream sample.wav ja

# 深度健康检查（真实转写探测，须 API key；200=引擎可转写，503=引擎忙/不可达/变砖）
curl -H "Authorization: Bearer <API_KEY>" http://127.0.0.1:9881/healthz/deep
```

注意：Caddy 架构（2026-09-12 起）下门面绑回环、公网只放行 TCP 443，安全组
**不需要**放行 9881；引擎端口 9001/10095 不要对外开放。
