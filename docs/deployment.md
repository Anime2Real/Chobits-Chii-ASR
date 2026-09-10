# Chobits-Chii-ASR 部署实录

> 本文按家族惯例记录服务器部署全过程。首次部署时请按步骤实测并把环境信息补全到
> 「实测环境」一节（参考 Chobits-Chii-LLM/docs/deployment.md 的风格）。

## 实测环境

- 服务器：Ubuntu 24.04 + GPU（8GB 显存及以上即可，Fun-ASR-Nano 仅 0.8B）
- Docker + NVIDIA Container Toolkit
- 日期：待首次部署时填写

## 1. 构建引擎镜像

```bash
# 生产环境建议把 FUNASR_REPO_REF 锁到验证过的 commit (默认 main, 防上游漂移)
docker build -t chobits-chii-asr-engine \
  --build-arg FUNASR_REPO_REF=main \
  docker/
```

## 2. 启动引擎容器

```bash
# 模型权重首次启动经 ModelScope 下载 (~2GB), 挂载缓存卷避免重复下载
docker run -d --name chobits-chii-asr-engine \
  --gpus all --restart unless-stopped \
  -p 127.0.0.1:9001:9001 -p 127.0.0.1:10095:10095 \
  -v $HOME/.cache/modelscope:/root/.cache/modelscope \
  -e LANGUAGE=日语 \
  chobits-chii-asr-engine

docker logs -f chobits-chii-asr-engine   # 等两个服务都打印就绪
```

- 引擎只绑到 `127.0.0.1`，对外暴露统一由门面负责（鉴权/TLS 都在门面层）。
- 端口约定：容器内 `9001` = HTTP 批量转写，`10095` = WebSocket 流式。
- 首次启动留意日志确认模型加载完成（出现 server listening 字样）；
  若 `funasr-server` 参数与当前版本不符，以 `docker exec ... funasr-server --help` 为准
  并同步修正 `docker/entrypoint.sh`。

## 3. 启动门面（宿主机）

```bash
export CHII_ASR_API_KEY=<随机密钥>   # 必填, 未设置拒绝启动
bash tools/start_asr_api.sh 9881     # 首次运行自动建 .venv 装依赖
```

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
#   CHII_ASR_SSL_CERTFILE=/etc/chobits-chii-asr.crt   (可选, 见下方 TLS)
#   CHII_ASR_SSL_KEYFILE=/etc/chobits-chii-asr.key    (可选, 与上一条同时设置)
sudo systemctl daemon-reload && sudo systemctl enable --now chobits-chii-asr
journalctl -u chobits-chii-asr -f
```

## 5. TLS（可选，公网强烈建议）

```bash
sudo openssl req -x509 -newkey rsa:2048 -nodes \
  -keyout /etc/chobits-chii-asr.key -out /etc/chobits-chii-asr.crt -days 3650 \
  -subj "/CN=chii-asr" -addext "subjectAltName=IP:<服务器IP>,IP:127.0.0.1"
sudo chmod 600 /etc/chobits-chii-asr.key
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
