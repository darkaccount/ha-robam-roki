# 贡献指南

感谢你愿意帮忙。这个集成完全靠真机证据推进：**每一条控制、每一个属性名都要有设备上的证据**，所以最需要的贡献是「你的设备 + 你的观察」，其次才是代码。

三条路径，按门槛从低到高：

| 你想做的事 | 走哪条 |
| :--- | :--- |
| 报告问题、补充证据、让新设备被支持 | 用户路线（学习 → 验证 → 导出 → issue/PR） |
| 改代码、修 bug、写文档 | 开发者路线（`bash init.sh` 全绿 + PR） |
| 审核并合并别人的 profile | 维护者章节（`tools/promote_profile.py`） |

---

## 一、用户路线：把设备的控制交给仓库

不需要写代码，全部在 Home Assistant 界面里完成。

1. **接入设备**：正常添加集成。能读到属性就是有证据的第一步；完全读不到的，先在 issue 里附诊断信息。
2. **学习**：进入集成「配置 → 学习新设备控制」，选设备与时长，动作选「开始」；用官方 App 操作要接入的功能，完成后回来选「结束」。学习期间集成**只被动记录** App 发往设备的帧，不发送任何命令。
3. **生成草稿**：调用服务 `robam_roki.draft_profile`。通知里有草稿路径、每个动作推测的名称，以及「哪些属性本来就有实体」的说明。
4. **验证**：把草稿放进 `<配置目录>/robam_roki/profiles/`，在选项里打开「允许未验证的控制」，重载。学到的动作变成 button 实体，逐个点一下，和官方 App 对照；确认无误后把对应条目的 `verified` 改成 `true`。
5. **导出**：调用服务 `robam_roki.export_profile`（选设备或填 GUID）。它只产出一份 Markdown：`<配置目录>/robam_roki/exports/<dc>-<dt>-<年月日-时分>.md`，里面有序号固定的五节 —— 设备与匹配、验证清单、profile YAML、fixture、如何贡献。
6. **反馈**：把这份 .md 整份贴到 [新设备支持 issue](https://github.com/darkaccount/ha-robam-roki/issues/new?template=device_support.yml)，或直接提 PR。维护者会用 `tools/promote_profile.py` 把它落进仓库。

### 隐私红线

- 导出文件里**不应出现**设备 GUID、手机号、token、App GUID；集成在生成时已经脱敏（fixture 的属性和密钥名都会被替换成 `**REDACTED**`）。
- 贴 issue 前自己再看一眼：不要贴 `secrets.yaml`、`.storage/`、完整诊断 JSON 之外的东西。
- 不要为了取证去“猜”命令：档位、模式这类命令发错可能让设备真的动起来。只被动观测。

---

## 二、开发者路线

### 环境与验证

```bash
# Windows 用 Git Bash；这是唯一的验收入口
bash init.sh

# 等价命令
.venv/Scripts/python.exe -m pytest tests/ -q --no-header
.venv/Scripts/python.exe -m compileall -q custom_components tools tests
```

- 测试基线只升不降；新行为要带测试，修 bug 要先写出能复现的测试。
- `ruff check custom_components tools tests` 不是门禁，但别新增告警。
- 文件 IO、`read_text`、`scandir` 一律走 `hass.async_add_executor_job`，不要在事件循环里做。
- 翻译三件套必须成对：`strings.json`（中文）与 `translations/zh-Hans.json` 内容完全一致，`translations/en.json` 键结构一致（有测试断言）。

### 硬性规则

- **绝不主动探测设备**：不发送档位/开关命令试探；证据只能来自被动观测或官方 App。
- 新驱动默认只读（`is_controllable` 已移除，能力由 `commands()` 推导）；开放控制必须有 `verified: true` 或用户显式打开实验开关。
- setup 与 config flow 只做只读属性同步，不误启动油烟机。
- 不新增第三方依赖；`api.py` / `profiles.py` / `export.py` 不得 import `homeassistant`（离线可单测）。
- 全程脱敏：手机号 / 密码 / token / GUID 一律打码，`secrets.yaml`、`*.apk` 永不提交。

### 提交

- 在 `local` 分支上提交，提交信息用 `feat(...)` / `fix(...)` / `docs(...)` 前缀，一个提交做一件事。
- 提交前 `git status` 确认没有意外产物；不要 `git add -A`。
- 推送时 pre-push 会自动跑审查门禁并生成公开快照（`public/main`）。

---

## 三、维护者：把导出包落地

### 导出包的结构

一份导出 `.md` 由集成生成，profile 与 fixture 分别夹在两个标记块里（**只认这两个标记**，不做通用 Markdown 解析）：

```
<!-- roki:profile-yaml -->   … <!-- /roki:profile-yaml -->
<!-- roki:fixture-json -->   … <!-- /roki:fixture-json -->
```

### 落地

```bash
# 先看会写到哪、diff 是什么
python tools/promote_profile.py <导出.md> --dry-run

# 真正写入
python tools/promote_profile.py <导出.md> [--name 自定义名字]
```

工具会：

1. 解析两个标记块，校验 profile schema（`roki-profile/1`）、`match.dc`、每条 control 的 `kind` / `command` / `body_hex`（以及 property 的 on/off、invoke 的 function）；
2. 校验 fixture schema（`roki-fixture/1`）且 dc 与 profile 一致；
3. 写入 `custom_components/robam_roki/profiles/<name>.yaml` 与 `tests/fixtures/devices/<dc>-<dt>.json`，并把 fixture 的 `source` 改成 **`user`**。若目标 fixture 已是 `source: user`（真机证据），工具会**拒绝覆盖**；确认要替换时加 `--force`。；
4. 打印后续清单（档位/README 行、门禁命令）。

### 命名与 source 规则

- **单机型**（`match.dt` 只有一个值）：`<dc>-<dt>.yaml` / `<dc>-<dt>.json`；profile 文件名小写（如 `ryyj-r8229.yaml`），fixture 保持设备写法（`RYYJ-R8229.json`）。
- **品类通用**（没有 `dt`，或 `dt` 是列表）：`<dc>.yaml` / `<dc>.json`。
- `--name` 覆盖 profile 文件名（fixture 名始终跟随自己的 match）。
- fixture 的 `source`：`user` = 用户真机导出（本工具写入的值）；`auto` = 草稿自动生成；`hand` = 手工合成。PR 里声明的证据等级必须与它一致。

### 验收门禁

```bash
bash init.sh
python tools/check_fixtures.py --base HEAD~1 --head HEAD
git diff --stat          # 只允许 profile + fixture + README 一行
```

外加人工检查（唯一的判断点）：`verified: true` 的控制必须在 PR 正文说明「哪台设备、什么操作、观察到什么」；命令号 / body / 属性名不能与该品类已有证据矛盾；没有真实手机号 / token / App GUID。

维护者的完整 SOP（含证据审查表与发布步骤）在仓库的 `docs/workflows/merge-device-support.md`；上面这三条命令就是它的验收门禁，公开仓库里只需要掌握这些。

### 明确不做

- 维护者不代替贡献者做真机验证；证据不足就退回要求补充，不要自己猜数据。
- 不为了“让 profile 合并”而放宽 `verified` 语义。

---

## 许可

提交即表示同意以本仓库的 [Apache License 2.0](./LICENSE) 发布你的贡献。
