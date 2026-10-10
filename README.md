<div align="center">
	<h1>Chobits-Chii-ASR</h1>
	<p><b>叽～</b> 小叽的语音识别服务</p>
	<p>OpenAI 兼容的批量转写与 WebSocket 流式识别门面：<b>Fun-ASR-Nano / Qwen3-ASR</b> 多引擎后端，按分支分发部署档案。</p>
	<p>
		<a href="https://madewithlove.org.in"><img alt="Made with Love" src="https://img.shields.io/badge/Made%20with-Love-ff69b4.svg"></a>
		<a href="https://github.com/Anime2Real/Chobits-Chii-ASR"><img alt="GitHub" src="https://img.shields.io/badge/GitHub-Chobits--Chii--ASR-181717?logo=github"></a>
		<a href="https://huggingface.co/datasets/chenxin199305/Chobits-Chii-Voice"><img alt="Dataset: Chobits-Chii-Voice" src="https://img.shields.io/badge/%F0%9F%A4%97%20Dataset-Chobits--Chii--Voice-yellow"></a>
		<a href="https://creativecommons.org/licenses/by-nc-sa/4.0/"><img alt="License: CC BY-NC-SA 4.0" src="https://img.shields.io/badge/License-CC%20BY--NC--SA%204.0-lightgrey.svg"></a>
		<img alt="Language: Japanese" src="https://img.shields.io/badge/Language-Japanese-green.svg">
	</p>
</div>

> 💖 如果这个项目对你有帮助，欢迎在 [GitHub](https://github.com/Anime2Real/Chobits-Chii-ASR) 点个 Star —— 你的支持能让更多人发现小叽！

> ⚠️ 注意：原始动画音频的版权归其权利方所有。本项目仅供学习与研究使用，请勿用于商业用途。

《人形电脑天使心》(Chobits) 中 **小叽 (Chii / ちぃ)** 角色的 ASR（语音识别）服务项目，评测数据来自 [Chobits-Chii-Voice](https://github.com/Anime2Real/Chobits-Chii-Voice) 数据集。**`main` 分支仅作入口与导航**：含引擎无关的门面代码（`tools/`）、跨服务协议契约（`docs/contracts/`）与评测工具，**不含任何具体模型的部署内容**——部署引擎、构建镜像、调参适配请切换到对应部署分支，各分支 README 有完整快速开始。

## 🌿 分支总览

| 分支 | 引擎 | 批量转写 | WS 流式 | 状态 | 适合场景 |
| --- | --- | --- | --- | --- | --- |
| [`fun-asr-nano-0.8b`](https://github.com/Anime2Real/Chobits-Chii-ASR/tree/fun-asr-nano-0.8b) | Fun-ASR-Nano-2512（0.8B，中英日） | ✅（AutoModel，可跑 CPU） | ✅（生产验证，T4 质量拐点 ~4 路） | **生产在跑**（2026-09-12 上线） | 需要流式、角色名等专名识别、中英日 |
| [`qwen3-asr-0.6b`](https://github.com/Anime2Real/Chobits-Chii-ASR/tree/qwen3-asr-0.6b) | Qwen3-ASR-0.6B（52 语） | ✅（vLLM，RTF 0.02） | ❌（shim 未落地） | T4 实测通过（2026-10-09） | 纯批量、极致吞吐、多语覆盖 |
| [`qwen3-asr-1.7b`](https://github.com/Anime2Real/Chobits-Chii-ASR/tree/qwen3-asr-1.7b) | Qwen3-ASR-1.7B（52 语，开源 SOTA 档） | ✅（vLLM，RTF 0.09） | ❌（同上） | T4 实测通过（2026-10-09） | 纯批量、准确率优先 |

三个部署分支的门面代码（`tools/`）与测试（`tests/`）完全一致，差异只在 `docker/`（引擎镜像）与 `deploy/`（env 模板）：推理引擎跑在 Docker 里，宿主机 Python 门面负责鉴权（`Authorization: Bearer`）、每 IP 限流与 OpenAI 兼容协议垫片，与家族其他服务（[LLM](https://github.com/Anime2Real/Chobits-Chii-LLM) / [TTS](https://github.com/Anime2Real/Chobits-Chii-TTS)）同一约定。切换方案 = 整分支切换 + 环境变量对齐，客户端零改动。

对客户端的协议契约（WS 会话语义、X-6 错误码、ASR 票据格式）的机器可读单一事实源见 [`docs/contracts/`](docs/contracts/)；各方案的硬件适配经验（T4 已实测、V100 预留）统一收录在 [docs/hardware-adaptation.md](docs/hardware-adaptation.md)（三分支同款，main 为原件）。

## 📄 许可协议

本项目的派生内容遵循数据集的 [CC BY-NC-SA 4.0](https://creativecommons.org/licenses/by-nc-sa/4.0/deed.zh-hans)（署名-非商业性使用-相同方式共享）协议。

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
