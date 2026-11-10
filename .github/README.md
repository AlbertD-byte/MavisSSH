# .github 目录说明

本目录存放 GitHub 仓库配置：

| 路径 | 作用 |
|---|---|
| `ISSUE_TEMPLATE/bug_report.yml` | Bug 报告模板 |
| `ISSUE_TEMPLATE/feature_request.yml` | 功能请求模板 |
| `ISSUE_TEMPLATE/config_question.yml` | 配置类问题模板 |
| `PULL_REQUEST_TEMPLATE.md` | PR 模板 |

CI 配置（GitHub Actions）以模板形式发布在仓库的 [`docs/ci.yml`](../docs/ci.yml)，
复制到 `.github/workflows/ci.yml` 即可启用 —— 首发用的 API 令牌缺少 GitHub 的
Workflows 权限，无法自动推送 workflow 文件。

模板用 YAML 格式（GitHub 支持），放在 `ISSUE_TEMPLATE/` 下时会在新建 Issue
时出现在选择列表里；PR 模板是 Markdown，GitHub 会在新建 PR 时自动带出。
