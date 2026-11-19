# 分支许可同步

本仓库的 `main` 只维护索引、README 与许可文件；实现内容在以下分支。`main` 已切换为「严格 NC + 双授权」，但以下分支在远端仍是旧许可，**必须同步后才能宣称全仓库完成切换**。

## 待同步分支

| 分支 | 当前远端 LICENSE | 目标 | 状态 |
|---|---|---|---|
| `fun-asr-nano-0.8b` | CC BY-NC-SA 4.0 | PolyForm Noncommercial 1.0.0 + CC BY-NC-SA 4.0（数据/权重） | 待同步 |
| `qwen3-asr-0.6b` | CC BY-NC-SA 4.0 | PolyForm Noncommercial 1.0.0 + CC BY-NC-SA 4.0（数据/权重） | 待同步 |
| `qwen3-asr-1.7b` | CC BY-NC-SA 4.0 | PolyForm Noncommercial 1.0.0 + CC BY-NC-SA 4.0（数据/权重） | 待同步 |

## 同步步骤

1. 在 `main` 提交本次许可变更。
2. 对每个分支执行：
   ```bash
   git checkout <branch>
   for f in LICENSE LICENSE-DATA MODEL-TERMS.md DATASET-TERMS.md COMMERCIAL.md LICENSE-HISTORY.md NOTICE ASSETS.md; do
     git show main:$f >/dev/null 2>&1 && git checkout main -- "$f"
   done
   # README 不要整体覆盖（各分支内容不同）；只手动替换「许可协议」一节。
   git add -A
   git commit -m "chore(license): 切换为严格 NC + 双授权"
   ```
3. 推送分支。
4. 验证：
   ```bash
   git show <branch>:LICENSE | head -1   # 应为 PolyForm Noncommercial License 1.0.0
   ```

## 注意

- 分支上的代码同样受版权保护；如果分支上有外部贡献者提交，需要先补 CLA。
- 不要在内容分支保留 CC 代码许可，否则 GitHub 页面和用户会看到两套协议。
- 数据/模型权重继续使用 `LICENSE-DATA`（CC BY-NC-SA 4.0），不要用 PolyForm 覆盖。
