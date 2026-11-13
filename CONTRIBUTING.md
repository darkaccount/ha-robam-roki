# 贡献指南

感谢你愿意帮忙。这个集成完全靠真机证据推进：**每一条控制、每一个属性名都要有设备上的证据**，所以最需要的贡献是「你的设备 + 你的观察」，其次才是代码。

三条路径，按门槛从低到高：

| 你想做的事 | 走哪条 |
| :--- | :--- |
| 报告问题、补充证据、让新设备被支持 | 用户路线（学习 → 验证 → 导出 → issue/PR） |
| 改代码、修 bug、写文档 | 开发者路线（`pytest tests/` 全绿 + PR） |
| 审核并合并别人的 profile | 维护者章节（`tools/promote_profile.py`） |

---

## 一、用户路线：把设备的控制交给仓库

不需要写代码，全部在 Home Assistant 界面里完成。下面几条是操作流程；菜单里的 1–4 是界面编号，两者不同。

- ① **接入设备**：正常添加集成。能读到属性就是有证据的第一步；完全读不到的，先在 issue 里附诊断信息。
- ② **学习**：**配置 → 1. 学习新设备控制**，选设备、设时长（默认 300 秒），动作选「开始」；然后用官方 App 操作要接入的功能。最长 600 秒，到时间会**自动结束**，不必专门回来点结束；想提前结束就回到这里选「结束」。学习期间集成**只被动记录** App 发往设备的帧，不发送任何命令。
- ③ **生成草稿**：回到菜单选 **2. 生成草稿**。草稿会直接写入 `<配置目录>/robam_roki/profiles/`（同名自动加 `-2`/`-3`，不覆盖旧文件）。确认页会显示草稿文件名、controls 条数，以及下一步怎么让按钮出现；完整路径与每个动作推测出来的名称在生成的 `draft-*.yaml` 里（调用 `robam_roki.draft_profile` 服务时会出现在持久通知中）。
- ④ **验证**：确认页可以一键打开「允许未验证的控制」并重载集成。学到的动作会变成 button 实体，逐个点一下，和官方 App 对照；确认无误后把 profile 里对应条目的 `verified` 改成 `true`。
- ⑤ **导出**：回到菜单选 **3. 导出贡献包**。它只产出一份 Markdown：`<配置目录>/robam_roki/exports/<dc>-<dt>-<年月日-时分>.md`，里面是序号固定的五节 —— 设备与匹配、验证清单、profile YAML、fixture、如何贡献。
- ⑥ **反馈**：把这份 .md 整份贴到 [新设备支持 issue](https://github.com/darkaccount/ha-robam-roki/issues/new?template=device_support.yml)，或直接提 PR。维护者会用 `tools/promote_profile.py` 把它落进仓库。

### 清理现场

一次学习会在配置目录留下三类文件，菜单 **4. 清理草稿** 可以分别勾选删除（默认只勾草稿）：

| 位置 | 内容 |
| :--- | :--- |
| `robam_roki/profiles/draft-*.yaml` | 草稿 profile（你手写的 profile 不会被删） |
| `robam_roki/fixtures/*.json` | 回归 fixture |
| `robam_roki/exports/*.md` | 导出包 |

删除草稿后对应的 button 实体会变为「不可用」，可在 **设置 → 实体** 里手动删除，或在清理前先关掉「允许未验证的控制」。

### 隐私红线

- 自动生成的 fixture 属性与采集帧体已脱敏（帧体在采集阶段剥离账号信息，fixture 的属性和密钥名会替换成 `**REDACTED**`）；导出的 `.md` 还会原样带上你手写的 profile 全文、设备名和配置目录路径。
- 贴 issue 前自己再看一眼：确认里面没有设备 GUID、手机号、token 或 App GUID；只贴已脱敏的导出 `.md` 或诊断 JSON，`secrets.yaml`、`.storage/`、账号密码和 token 一律不要贴。
- 不要为了取证去“猜”命令：档位、模式这类命令发错可能让设备真的动起来。只被动观测。

---

## 二、开发者路线

### 环境与验证

```bash
# 装依赖（一次）
python -m pip install pytest pytest-asyncio cryptography aiohttp yarl pyyaml
# tools/requirements.txt 只是离线 CLI 的依赖，不含测试依赖；跑测试用上面这条

# 跑测试：CI 用的是同一套
python -m pytest tests/ -q --no-header

# 语法检查（可选，但有助发现导入期错误）
python -m compileall -q custom_components tools tests
```

> Windows 上直接用 `python`（或 `py -3`）；若自建虚拟环境，先 `python -m venv .venv`，再把命令里的 `python` 换成虚拟环境里的解释器。

- 测试基线只升不降；新行为要带测试，修 bug 要先写出能复现的测试。
- `ruff check custom_components tools tests` 不是门禁，但别新增告警。
- 文件 IO、`read_text`、`scandir` 一律走 `hass.async_add_executor_job`，不要在事件循环里做。
- 翻译三件套必须成对：`strings.json`（中文）与 `translations/zh-Hans.json` 内容完全一致，`translations/en.json` 键结构一致（有测试断言）。

### CI 会跑什么

每个 PR 都会跑：

1. **hassfest** —— manifest / 翻译 / 平台结构校验；
2. **HACS 校验** —— 仓库结构与 `hacs.json`；
3. **check_fixtures** —— 本次改动涉及内置 profile 时，必须有对应 fixture（`tests/fixtures/devices/`）；
4. **pytest** —— Python 3.12 / 3.13 两个版本。

发版另由 `.github/workflows/release.yml` 处理：GitHub 上发布 release 时，把 `custom_components/robam_roki` 打成 `robam_roki.zip` 上传为 release 资产。

本地把第 3、4 条跑一遍基本就不会挂：第 3 条是 `python tools/check_fixtures.py --base HEAD~1 --head HEAD`（需要 git 历史），第 4 条是 `python -m pytest tests/ -q --no-header`。hassfest 与 HACS 校验只在 CI 里跑。

### 硬性规则

- **绝不主动探测设备**：不发送档位/开关命令试探；证据只能来自被动观测或官方 App。
- 新驱动默认只读（`is_controllable` 已移除，能力由 `commands()` 推导）；开放控制必须有 `verified: true` 或用户显式打开实验开关。
- setup 与 config flow 只做只读属性同步，不误启动油烟机。
- 不新增第三方依赖；`api.py` / `profiles.py` / `export.py` 不得 import `homeassistant`（离线可单测）。
- 全程脱敏：手机号 / 密码 / token / GUID 一律打码，`secrets.yaml`、`*.apk` 永不提交。

### 提交

- 从 `main` 拉一个短分支，改动集中、一个 PR 做一件事，然后向 `main` 提 PR。
- 提交信息用 `feat(...)` / `fix(...)` / `docs(...)` 前缀。
- 提交前 `git status` 确认没有意外产物；不要 `git add -A`。

---

## 三、维护者：把导出包落地

### 导出包的结构

一份导出 `.md` 由集成生成，profile 与 fixture 分别夹在两个标记块里（**只认这两个标记**，不做通用 Markdown 解析）：

```
<!-- roki:profile-yaml -->   … <!-- /roki:profile-yaml -->
<!-- roki:fixture-json -->   … <!-- /roki:fixture-json -->
```

### 0. 落地

```bash
# 先看会写到哪、diff 是什么
python tools/promote_profile.py <导出.md> --dry-run

# 真正写入
python tools/promote_profile.py <导出.md> [--name 自定义名字]
```

工具会：

1. 解析两个标记块，校验 profile schema（`roki-profile/1`）、`match.dc`，并按每条 control 的 `kind` 校验对应字段：`mqtt` 查 `command` / `body_hex`，`property` 查 `on` / `off`，`invoke` 查 `function`；
2. 校验 fixture schema（`roki-fixture/1`）且 dc 与 profile 一致；
3. 写入 `custom_components/robam_roki/profiles/<name>.yaml` 与 `tests/fixtures/devices/<dc>[-<dt>].json`（品类级 match 没有 dt 时只写 `<dc>.json`），并把 fixture 的 `source` 改成 **`user`**。若目标 fixture 已是 `source: user`（真机证据），工具会**拒绝覆盖**；确认要替换时加 `--force`。
4. 打印后续清单（README 行、门禁命令）。

没有导出包时（例如只有截图 / 诊断 JSON），在 HA 里调用服务 `robam_roki.draft_profile`，或用 `python tools/roki_cli.py draft <bundle.json>` 生成，再走同一套校验；**不要凭描述手写命令号**。

### 1. 证据审查（唯一的人工判断点）

| 检查项 | 通过标准 |
| :--- | :--- |
| `verified` 声明 | 为 true 的每个控制项，PR 正文必须说明「在哪台设备、什么操作、观察到什么」 |
| fixture `source` | `user` = 真机抓取；`hand` = 合成；声明与正文一致 |
| 帧 / 属性合理性 | 命令号、body 长度、属性名与该品类已有证据不矛盾 |
| 隐私 | 无真实手机号、无 App GUID、无 token；fixture 已脱敏 |
| 范围 | 没有顺手改无关驱动、没有动安全红线（setup 不发命令、不主动探测） |

不通过就把缺什么写清楚退回，**不要自己补数据**。

### 2. 门禁

以下三条按「提交后 / PR 上」的视角写：`check_fixtures.py` 读的是两个已提交修订之间的 diff，`git diff --stat` 看不到未跟踪文件。落地后、提交前本地可以先跑前两条确认，最终以 CI 结果为准。

```bash
python -m pytest tests/ -q --no-header
python tools/check_fixtures.py --base HEAD~1 --head HEAD
git diff --stat          # 只允许 profile + fixture + README 一行
```

验收标准：

- 新 profile 必须带 fixture，且不能出现 `MISSING FIXTURE`；`uncovered (warning)` 是全库历史遗留提示，不会让构建失败，但不要新增；
- fixture `source == "user"`，PR 正文的验证描述与它一致；
- 导出包验证清单里 `false` 的条目，不得在 profile 里变成 `true`；
- pytest 与 check_fixtures 全绿，profile 与 fixture 之外没有多余改动（README 支持表那一行除外）。

### 3. 合并

- 提交信息沿用仓库风格：`feat(profile): ...` / `feat(device): ...`。
- 在 README「支持范围」表加一行（品类 / dc / dt / 目前支持），`promote_profile` 会把待填的行打印出来。
- 用户可见变化时更新版本号。

### 明确不做

- 维护者不代替贡献者做真机验证：没有他们的设备，不猜数据。
- 不为了“让 profile 合并”而放宽 `verified` 语义。
- 不手工复制粘贴导出包里的 YAML —— 一律走 `promote_profile.py`。

---

## 许可

提交即表示同意以本仓库的 [Apache License 2.0](./LICENSE) 发布你的贡献。
