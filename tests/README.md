# 测试

门面的纯逻辑（票据验签 / 鉴权 / XFF / 并发准入）与端点行为的 pytest 套件。
引擎依赖全部 mock（内存假 httpx 客户端 / 假引擎 WS / 假后端适配层），
不需要启动 :9001 / :10095 推理引擎。

## 运行

```bash
.venv/bin/pip install pytest   # 一次性
.venv/bin/python -m pytest tests/ -v
```

## 覆盖范围

- `test_unit.py`：`_ticket_verify`/`_ticket_mark_used`（旧 3 段 / 新 4 段格式、
  签名错误、过期、验签不核销、mark_used 后 jti 一次性、idb64 身份提取）、
  `_getenv_int/_getenv_float` 容错、
  `_client_ip` XFF 末跳逻辑、`_authorized` 只认 Bearer、`_sanitize_filename`。
- `test_backend_funasr.py`：start 帧校验（language 白名单 ja/zh/en/auto、ko
  与命令注入拒绝、sample_rate "abc" → error + 1002、None → 缺省 16000、
  非 16000 拒绝）与引擎命令翻译（START / LANGUAGE:）；WS 背压（引擎 connect
  带 max_size=4MB、客户端单帧 >1MB → error + close 1009 且帧不进引擎、
  引擎消息超限 PayloadTooBig → 客户端收到 error 帧）；
  error 帧 code 字段（X-6 枚举契约钉死、各报错站点 code 值、qwen3 骨架
  internal_error）。
- `test_endpoints.py`：TestClient 端点级——/healthz 免鉴权、/v1/models 无 key
  401、批量转写引擎错误通用化（5xx → 502、4xx 通用文案、细节不外泄、model
  字段改写引擎注册名）、流式 WS 并发准入（同一身份第 5 个连接 4429、无身份
  回退按 IP、断开释放槽位、4429 超限拒绝不烧票且票据稍后可用、accept 后
  正常核销同票二次使用 4401）。

## 状态隔离

票据核销集合（`_used_tickets`）、限流桶（`_hits`）、WS 并发桶（`_ws_active`）
是模块级全局状态，`conftest.py` 的 autouse fixture 在每个测试前后清空，
测试可任意顺序重复运行。
