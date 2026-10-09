# 部署与硬件适配记录

> **本文是三个分支同款的"踩坑 / 适配 / 调参"经验唯一真相源**（`fun-asr-nano-0.8b` /
> `qwen3-asr-0.6b` / `qwen3-asr-1.7b` 内容一致，改动须同步三分支）。
> 按 GPU 型号分节，节内按引擎分小节。部署流程（镜像构建 / systemd / TLS / 验证命令）
> 见 `docs/deployment.md`（Fun-ASR-Nano 分支维护）或各分支 README「快速开始」（Qwen3-ASR）。
> 新适配一块卡：复制「适配检查清单」逐条过，然后按本文结构追加新规格节。

## 适配检查清单（通用）

1. **驱动 vs 镜像 CUDA 要求**：`docker run` 报 `unsatisfied condition: cuda>=X.Y` → 升驱动，或临时 `-e NVIDIA_DISABLE_REQUIRE=1` 绕过（绕过前确认用户态兼容，见该规格节）。
2. **attention 后端**：vLLM 自动选择的后端在老卡上可能启动卡死（症状：容器活着、GPU 0%、日志停在 load model、无 error）。换 `-e VLLM_ATTENTION_BACKEND=TRITON_ATTN` 试验。
3. **显存预算**：`GPU_MEM_UTIL`（vLLM 按总显存比例要预算）与 `MAX_MODEL_LEN`（KV cache 随 max seq len 线性增长）要一起算。报错 `KV cache is needed ... larger than available` 时二选一调。
4. **CUDA graph 捕获**：加载期 OOM（`torch.OutOfMemoryError` 出现在 `default_unquantized_gemm` 等位置）→ `--enforce-eager`。
5. **权重下载**：网络可达性（国内需 `HF_ENDPOINT` 镜像）；**下载中断的 `.incomplete` blob 会让 vLLM 在加载阶段无声卡死**——缓存完整后加 `HF_HUB_OFFLINE=1` 可跳过远程检查直接启动。
6. **dtype**：无 bf16 硬件单元的卡，vLLM 会自动 `Casting torch.bfloat16 to torch.float16`；
   Fun-ASR-Nano 则必须显式 `DTYPE=fp32`（见 T4 节 Fun-ASR-Nano 小节）。确认推理结果正常即可。
7. **vLLM 按启动那一刻的空闲显存配比预算**：同卡多进程（含其他容器）必须串行就绪，否则互相看不见而超发（两引擎方案都踩过，见各小节）。

---

## Tesla T4 16GB（sm75）—— 已实测

**环境**：腾讯云 CVM，Ubuntu 20.04 LTS，驱动 525.105.17（CUDA 12.0），Docker + NVIDIA Container Toolkit；同机常驻 TTS 容器（~3.7-4GB 显存）。

### Fun-ASR-Nano-2512（2026-09-10 首验，2026-09-12 生产上线）

镜像：`chobits-chii-asr-engine`（自建，`docker/Dockerfile`，基于 `pytorch/pytorch:2.9.0-cuda12.8` runtime）

**生产拓扑（T4 16GB + TTS 共卡，关键）**：

- **批量转写跑 CPU**（`-e HTTP_DEVICE=cpu`）：4.2s 音频约 2.5s，比实时快，不占显存；
- **流式识别独占 GPU**（`-e WS_GPU_MEM_UTIL=0.40`，vLLM fp32 ~6GB + 音频组件 ~4GB）；
- 静态占用 ~13.3GB，留 ~3GB 推理工作余量。
- 若换大显存卡/独占卡：去掉 `HTTP_DEVICE=cpu` 让批量回 GPU，`WS_GPU_MEM_UTIL` 用镜像默认 0.55。

**踩坑与解法（首验记录，均已固化进 `docker/` 与 `tools/`）**：

| # | 要点 |
|---|---|
| 1 | funasr 1.4.x 服务栈基于 vLLM：流式 `funasr-realtime-server` 硬依赖（无回退）；批量 `funasr-server` 失败回退 AutoModel。镜像需显式装 `vllm fastapi uvicorn python-multipart`（后三个 funasr 未声明为依赖） |
| 2 | **dtype 只能 fp32**：T4（sm75）无 bf16 单元，vLLM 直接拒绝；funasr 又把 fp16 静默映射成 bf16，官方注释明确无 bf16 的卡用 fp32。权重显存因此翻倍 |
| 3 | **triton 需要 C 编译器**：vLLM 在 Turing 上 JIT 编译 ieee 精度 kernel，runtime 镜像无 gcc 必崩（`Failed to find C compiler`），镜像已装 gcc |
| 4 | **批量侧 vLLM 尝试必须禁用**：`funasr._server_app` 硬编码 bf16 + 0.5 显存配比，T4 上必失败且每次残留数 GB 孤儿显存。镜像内补丁加 `FUNASR_SERVER_NO_VLLM` 开关，entrypoint 默认置 1（走 AutoModel）；bf16 卡可 `-e FUNASR_SERVER_NO_VLLM=` 恢复 |
| 5 | **显存配比**：`gpu_memory_utilization` 预算**含其他进程占用**。0.25/0.35 实测 KV cache 不足（fp32 下 2048 长度需 ~0.88GiB），`WS_GPU_MEM_UTIL=0.55` 通过（与 TTS 共存时 0.40） |
| 6 | `funasr-server --model` 只接受别名，模型 ID 要走 `--model-path`；此时引擎注册名为 `custom`，门面把 `model` 改写为 `CHII_ASR_ENGINE_MODEL`（默认 custom） |
| 7 | **WS 引擎协议是纯文本命令**（非 JSON）：`START` / `STOP` / `LANGUAGE:<提示语>` / `HOTWORDS:a,b`；音频必须 16kHz PCM16；STOP 后引擎不关连接，回 `{"event":"stopped"}` 与 `is_final` 结果帧（由 `tools/backend_funasr.py` 适配）。语言提示语用「日本語」而非「日语」 |
| 8 | **构建网络**（国内）：`--build-arg PIP_INDEX_URL/PIP_TRUSTED_HOST`（PyPI 镜像）与 `--build-arg APT_MIRROR`（apt 镜像）；流式服务用 pip 包自带 `funasr-realtime-server`，不从 GitHub 拉脚本 |
| 9 | **启动顺序竞争**：vLLM 按启动那一刻空闲显存配比（`util × 总容量 ≤ 当前空闲`）。双进程并发启动互相超发把后加载方挤到 OOM——entrypoint 已改批量先就绪（HTTP 200 探测）再起流式 |
| 10 | **小卡共存拓扑**：批量 AutoModel(GPU ~2.4GB) + 流式 vLLM(fp32) 静态即占满 T4，推理工作显存为 0（双双 OOM 实测）。解法即上方"生产拓扑" |
| 11 | **TLS 私钥属主**：门面以 `User=ubuntu` 运行，`/etc/chobits-chii-asr.key` 必须 `chown ubuntu:ubuntu`（openssl sudo 生成默认 root:root，uvicorn 读不了直接起不来） |

**实测性能**：

| 指标 | 数值 | 备注 |
|---|---|---|
| 批量转写（CPU） | RTF 0.44（9.36s 样本 avg 4.09s） | 2026-10-09 复测；并发 4 时 CPU 串行排队（19s） |
| 流式识别 | 首 partial 0.59s（并发 4：0.68s） | 2026-10-09 复测，4 路正常出 final |
| 流式质量拐点 | ~4 路并发 | 2026-09-15 压测（≥8 路 vLLM 跟不上实时语速） |
| 引擎静态显存 | ~13.3GB（批量 CPU + 流式 GPU 0.40） | 生产配置 |

### Qwen3-ASR-0.6B / 1.7B（2026-10-09 实测）

镜像：`chobits-chii-asr-engine-qwen3-06` / `-17`（自建，`docker/Dockerfile`，基于 `qwenllm/qwen3-asr:latest`；vLLM 0.14.0 / torch 2.9.1 / qwen-asr 0.0.4）

**启动参数（已固化进 `docker/entrypoint.sh` 默认值）**：

```bash
docker run -d --name chobits-chii-asr-engine --gpus all --restart unless-stopped \
  -p 127.0.0.1:9001:8000 \
  -e NVIDIA_DISABLE_REQUIRE=1 \
  -e VLLM_ATTENTION_BACKEND=TRITON_ATTN \
  -e HF_ENDPOINT=https://hf-mirror.com \
  -v $HOME/.cache/huggingface:/root/.cache/huggingface \
  chobits-chii-asr-engine-qwen3
# entrypoint 内置: --gpu-memory-utilization 0.6 --max-model-len 32768 --enforce-eager
```

**踩坑与解法（按遇到顺序）**：

| # | 症状 | 根因 | 解法 |
|---|---|---|---|
| 1 | `docker run` 直接失败：`unsatisfied condition: cuda>=12.8` | 镜像声明 CUDA 12.8，驱动 525 最高 CUDA 12.0 | `-e NVIDIA_DISABLE_REQUIRE=1`。实测用户态兼容可用（同机 Fun-ASR-Nano 的 cu12.8 基础镜像亦如此） |
| 2 | 启动卡死：容器活着、GPU 0%、日志停在 `Starting to load model`、无报错 | vLLM 默认 FLASHINFER 后端在 sm75 上挂起（首次误判为下载问题，重试复现后定位） | `-e VLLM_ATTENTION_BACKEND=TRITON_ATTN` |
| 3 | `ValueError: ... max seq len (65536), (7.0 GiB KV cache is needed...)` | 默认 max-model-len 65536 的 KV 预算 ~7GB，T4 装不下 | `--max-model-len 32768`（KV ~3.5GB，覆盖 40+ 分钟音频，远超官方 20 分钟上限） |
| 4 | `ValueError: ... (3.5 GiB KV cache is needed ... available (2.94 GiB))`（0.5 util 时） | 0.6B 权重 + 开销后 KV 余量不足 | `GPU_MEM_UTIL` 提到 0.6 |
| 5 | `torch.OutOfMemoryError`（0.65 util，出现在 CUDA graph 捕获阶段） | CUDA graph 捕获的额外显存尖峰与 TTS 容器叠加 | `--enforce-eager`（不捕获 graph；eager 下吞吐损失对本场景可忽略，见性能表） |
| 6 | 反复 `huggingface.co ConnectTimeout` | 国内直连 HF 不可达 | `-e HF_ENDPOINT=https://hf-mirror.com` |
| 7 | 又一次"加载卡死"（GPU 0%、无报错），重试同样 | 上次容器被杀时权重留在 `.incomplete` 断点文件，vLLM 读截断文件挂起 | 补齐下载（或 `HF_HUB_OFFLINE=1` 用完整缓存）；**教训：加载卡死先查缓存完整性** |

**实测性能（批量转写，样本 9.36s 日语 PCM16 16k，与 Fun-ASR-Nano 同机对比）**：

| 模型 | 顺序 RTF | 并发 8 单请求耗时 | 显存预算 | 识别正确性（单样本） |
|---|---|---|---|---|
| Qwen3-ASR-0.6B | **0.02**（0.17s） | ~0.30s | util 0.5 即可 | 角色名误识（`ちいびでき…`） |
| Qwen3-ASR-1.7B | 0.09（0.87s） | ~0.95s | util 0.6 下限 | 角色名误识（`チイデキ…`） |
| Fun-ASR-Nano-2512（参照，批量走 CPU） | 0.44（4.09s） | 4 并发串行 19s | 批量不占显存 | **正确**（`ちー、秀樹のこと大好き`） |

注：单样本只说明专名（角色名）场景差异，不构成 CER 结论；全量对比用 `tools/eval_chobits.py`。

### T4 验证清单（两引擎均已通过）

- [x] 容器启动 → `GET /v1/models` 返回引擎注册名（funasr `--model-path` 模式为 `custom`；qwen3 为 `Qwen/Qwen3-ASR-*`）
- [x] `POST /v1/audio/transcriptions` 日语样本 200 且文本合理
- [x] 顺序 5 次 + 并发 4/8 压测（funasr 另测流式 1/4 路与 START/STOP 协议）
- [x] 与生产其他容器共存（测试期间受控停启，无干扰遗留）
- [ ] Qwen3-ASR 流式（WS shim 落地前不可用，见 README Roadmap）

---

## NVIDIA V100 —— 待适配（结构预留）

> 以下仅为预研要点，**未经实测**；实际部署后按 T4 节结构（分引擎小节）补全。

预期差异（相对 T4）：

- **sm70**：无 bf16 单元（同 T4）。Fun-ASR-Nano 同样须 `DTYPE=fp32`、triton 需 gcc；
  flashinfer 官方支持 sm75+，Qwen3-ASR **大概率同样须 `TRITON_ATTN`**。
- **显存**：V100 常见 16/32GB。16GB 版参考 T4 参数起点（qwen3: util 0.6 / max-model-len 32768 / eager；
  funasr: 批量 CPU + 流式 util 0.40）；32GB 版可试放开（qwen3 去 `--enforce-eager` 前单独验证捕获期不 OOM；
  funasr 批量回 GPU）。
- **驱动**：按宿主机实际驱动对照镜像 CUDA 要求（qwen3 镜像声明 `cuda>=12.8`），老驱动同样需 `NVIDIA_DISABLE_REQUIRE=1`。
- **算力**：FP16 Tensor Core 吞吐高于 T4，RTF 预期优于 T4 实测值，但以实测为准。

待办：拿到 V100 环境后过一遍「适配检查清单」，把结果与性能数据填到本节。

---

*维护约定：新硬件适配追加为新规格节；适配参数默认值改动须同步对应分支的 `docker/entrypoint.sh` 与本文档（三个分支同款，改动后同步推送）。*
