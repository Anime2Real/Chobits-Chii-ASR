# 硬件适配记录（Qwen3-ASR 引擎）

> 按 GPU 型号记录 Qwen3-ASR 引擎（`qwenllm/qwen3-asr` 官方镜像 + `qwen-asr-serve`）在各硬件上的部署适配经验。
> 每节结构统一：环境 → 启动参数 → 踩坑与解法 → 实测性能 → 验证清单。
> 新适配一块卡时，复制「适配检查清单」一节，逐项过完后把结果按同结构追加为新规格节。

## 适配检查清单（通用）

1. **驱动 vs 镜像 CUDA 要求**：`docker run` 报 `unsatisfied condition: cuda>=X.Y` → 升驱动，或临时 `-e NVIDIA_DISABLE_REQUIRE=1` 绕过（绕过前确认用户态兼容，见该规格节）。
2. **attention 后端**：vLLM 自动选择的后端在老卡上可能启动卡死（症状：容器活着、GPU 0%、日志停在 load model、无 error）。换 `-e VLLM_ATTENTION_BACKEND=TRITON_ATTN` 试验。
3. **显存预算**：`GPU_MEM_UTIL`（vLLM 按总显存比例要预算）与 `MAX_MODEL_LEN`（KV cache 随 max seq len 线性增长）要一起算。报错 `KV cache is needed ... larger than available` 时二选一调。
4. **CUDA graph 捕获**：加载期 OOM（`torch.OutOfMemoryError` 出现在 `default_unquantized_gemm` 等位置）→ `--enforce-eager`。
5. **权重下载**：网络可达性（国内需 `HF_ENDPOINT` 镜像）；**下载中断的 `.incomplete` blob 会让 vLLM 在加载阶段无声卡死**——缓存完整后加 `HF_HUB_OFFLINE=1` 可跳过远程检查直接启动。
6. **dtype**：无 bf16 硬件单元的卡，vLLM 会自动 `Casting torch.bfloat16 to torch.float16`，确认推理结果正常即可，无需干预。

---

## Tesla T4 16GB（sm75）—— 2026-10-09 实测

**环境**：腾讯云 CVM，Ubuntu 20.04，驱动 525.105.17（CUDA 12.0），Docker + NVIDIA Container Toolkit；与生产 Fun-ASR-Nano（双进程）/ TTS 容器同机。

**镜像**：`qwenllm/qwen3-asr:latest`（28.1GB；vLLM 0.14.0 / torch 2.9.1 / qwen-asr 0.0.4）

### 启动参数（已固化进 `docker/entrypoint.sh` 默认值）

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

### 踩坑与解法（按遇到顺序）

| # | 症状 | 根因 | 解法 |
|---|---|---|---|
| 1 | `docker run` 直接失败：`unsatisfied condition: cuda>=12.8` | 镜像声明 CUDA 12.8，驱动 525 最高 CUDA 12.0 | `-e NVIDIA_DISABLE_REQUIRE=1`。实测用户态兼容可用（同机 Fun-ASR-Nano 的 cu12.8 基础镜像亦如此） |
| 2 | 启动卡死：容器活着、GPU 0%、日志停在 `Starting to load model`、无报错 | vLLM 默认 FLASHINFER 后端在 sm75 上挂起（首次误判为下载问题，重试复现后定位） | `-e VLLM_ATTENTION_BACKEND=TRITON_ATTN` |
| 3 | `ValueError: ... max seq len (65536), (7.0 GiB KV cache is needed...)` | 默认 max-model-len 65536 的 KV 预算 ~7GB，T4 装不下 | `--max-model-len 32768`（KV ~3.5GB，覆盖 40+ 分钟音频，远超官方 20 分钟上限） |
| 4 | `ValueError: ... (3.5 GiB KV cache is needed ... available (2.94 GiB))`（0.5 util 时） | 0.6B 权重 + 开销后 KV 余量不足 | `GPU_MEM_UTIL` 提到 0.6 |
| 5 | `torch.OutOfMemoryError`（0.65 util，出现在 CUDA graph 捕获阶段） | CUDA graph 捕获的额外显存尖峰与 TTS 容器（~4GB）叠加 | `--enforce-eager`（不捕获 graph；eager 下吞吐损失对本场景可忽略，见性能表） |
| 6 | 反复 `huggingface.co ConnectTimeout` | 国内直连 HF 不可达 | `-e HF_ENDPOINT=https://hf-mirror.com` |
| 7 | 又一次"加载卡死"（GPU 0%、无报错），重试同样 | 上次容器被杀时权重留在 `.incomplete` 断点文件，vLLM 读截断文件挂起 | 补齐下载（或 `HF_HUB_OFFLINE=1` 用完整缓存）；**教训：加载卡死先查缓存完整性** |

### 实测性能（批量转写，样本 9.36s 日语 PCM16 16k，与生产 Fun-ASR-Nano 同机对比）

| 模型 | 顺序 RTF | 并发 8 单请求耗时 | 显存预算 | 识别正确性（单样本） |
|---|---|---|---|---|
| Qwen3-ASR-0.6B | **0.02**（0.17s） | ~0.30s | util 0.5 即可 | 角色名误识（`ちいびでき…`） |
| Qwen3-ASR-1.7B | 0.09（0.87s） | ~0.95s | util 0.6 下限 | 角色名误识（`チイデキ…`） |
| Fun-ASR-Nano-2512（参照，批量走 CPU） | 0.44（4.09s） | 4 并发串行 19s | 批量不占显存 | **正确**（`ちー、秀樹のこと大好き`） |

注：单样本只说明专名（角色名）场景差异，不构成 CER 结论；全量对比用 `tools/eval_chobits.py`。

### 验证清单（T4 已通过）

- [x] 容器启动 → `GET /v1/models` 返回 `Qwen/Qwen3-ASR-*`
- [x] `POST /v1/audio/transcriptions` 日语样本 200 且文本合理
- [x] 顺序 5 次 + 并发 4/8 压测
- [x] 与生产 Fun-ASR-Nano/TTS 共存（测试期间受控停启，无干扰遗留）
- [ ] 流式（WS shim 落地前不可用，见 README Roadmap）

---

## NVIDIA V100 —— 待适配（结构预留）

> 以下仅为预研要点，**未经实测**；实际部署后按 T4 节结构补全。

预期差异（相对 T4）：

- **sm70**：无 bf16 单元（同 T4，预期自动 fallback fp16）；flashinfer 官方支持 sm75+，**大概率同样须 `TRITON_ATTN`**。
- **显存**：V100 常见 16/32GB。16GB 版参考 T4 参数起点（util 0.6 / max-model-len 32768 / eager）；32GB 版可试 util 0.8 + 默认 CUDA graph（去掉 `--enforce-eager` 前要单独验证捕获期不 OOM）。
- **驱动**：按宿主机实际驱动对照镜像 `cuda>=12.8` 要求，老驱动同样需 `NVIDIA_DISABLE_REQUIRE=1`。
- **算力**：FP16 Tensor Core 吞吐高于 T4，RTF 预期优于 T4 实测值，但以实测为准。

待办：拿到 V100 环境后过一遍「适配检查清单」，把结果与性能数据填到本节。

---

*维护约定：新硬件适配记录追加为新规格节；适配参数默认值改动须同步 `docker/entrypoint.sh` 与本文件。*
