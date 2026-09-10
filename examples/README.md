# examples/

调用示例见 `tools/client_example.py`（批量转写 + 流式识别两种模式）。

示例音频文件体积较大不入库。可自行用任意 wav 测试；流式模式要求 16-bit 单声道 wav：

```bash
# 格式转换 (任意来源 → 16kHz 单声道 16bit wav)
ffmpeg -i input.mp3 -ar 16000 -ac 1 -f wav sample.wav

export CHII_ASR_API_KEY=<密钥>
python3 tools/client_example.py batch sample.wav
python3 tools/client_example.py stream sample.wav ja
```
