# 仓库同步规则

- 本项目唯一远程仓库为 `https://github.com/bxqan2-hub/paypal.git`，默认分支为 `main`。
- 每次修改后立即提交并推送到 `origin/main`；依次执行 `git add -A`、创建说明本次修改的提交，并执行 `git push origin main`。
- 不创建额外的本地项目备份副本；回滚和对比统一使用 Git 提交历史与 GitHub 远程仓库。
- 不得把 `.env`、`.venv`、Python 缓存或运行日志提交到仓库；只上传源码、文档和脱敏配置示例。
- 不得修改或覆盖 `https://github.com/bxqan2-hub/-pp-.git`；本项目的提交只能推送到 `paypal.git`。
- 推送失败时保留提交并重试；远程推送成功前不得把修改标记为完成。
- 除非用户明确要求，否则不得强制推送或改写远程历史。
